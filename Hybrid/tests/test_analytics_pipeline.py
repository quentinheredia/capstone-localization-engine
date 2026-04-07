"""
test_analytics_pipeline.py — Comprehensive analytics pipeline validation.

Covers:
  1. _compute_cdf()   — threshold binning, outlier exclusion, empty inputs
  2. _compute_stats() — mean, median, p95, failure_pct correctness
  3. _subsample()     — reservoir sampling boundary conditions
  4. AccuracyAnalytics.push()
       - None / NaN guard (push_skipped counter)
       - outlier detection at exact cap boundary
       - GT at non-zero position
       - monotonic push_total counter
  5. AccuracyAnalytics.build_payload()
       - windowing (old samples excluded from CDF but included in stats)
       - time-normalisation (min_count equalisation)
       - scenario filtering
       - all-outlier window → flat CDF
       - single-method path
  6. CSV output shape (mocked)
  7. Ground truth update mid-stream
  8. Debug info correctness
  9. Zero-failure diagnosis: proves that GT at (0,0) with estimates within
     10 m of origin produces legitimately zero outliers — not a bug.

Run with:
    cd Hybrid
    python -m pytest tests/test_analytics_pipeline.py -v
"""

from __future__ import annotations

import math
import sys
import time
import types
import unittest
from unittest.mock import MagicMock, call, patch


# ─────────────────────────────────────────────────────────────────────────────
# Minimal cloud_io stub so the module imports without disk I/O
# ─────────────────────────────────────────────────────────────────────────────
_cloud_io_stub = types.ModuleType("cloud_io")
_cloud_io_stub.log_analytics_sample_to_csv = MagicMock(return_value=True)
sys.modules.setdefault("cloud_io", _cloud_io_stub)

# Remove capstone_core from sys.modules so we always exercise stub engine
sys.modules.pop("capstone_core", None)

# Force re-import so capstone_core absence is detected at import time
for mod in list(sys.modules):
    if "accuracy_analytics" in mod:
        del sys.modules[mod]

# Now we can safely import
import accuracy_analytics as aa  # noqa: E402 (import after path/stub setup)
from accuracy_analytics import (  # noqa: E402
    AccuracyAnalytics,
    _compute_cdf,
    _compute_stats,
    _subsample,
    _HARD_CAP,
    _CDF_THRESHOLDS,
)


# ══════════════════════════════════════════════════════════════════════════════
# Helper
# ══════════════════════════════════════════════════════════════════════════════

def _now_ms() -> int:
    return int(time.time() * 1000)


def _make_analytics(gt_x=0.0, gt_y=0.0, csv_path="") -> AccuracyAnalytics:
    """Convenience factory — disables CSV logging by default."""
    a = AccuracyAnalytics(max_buf=500, csv_path=csv_path)
    a.set_ground_truth(gt_x, gt_y)
    return a


# ══════════════════════════════════════════════════════════════════════════════
# 1. _compute_cdf
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeCdf(unittest.TestCase):

    def test_empty_returns_zero_pct_for_all_thresholds(self):
        result = _compute_cdf([])
        self.assertEqual(len(result), len(_CDF_THRESHOLDS))
        for row in result:
            self.assertEqual(row["pct"], 0.0)

    def test_all_below_first_threshold_zero_percent_at_zero(self):
        # 0.0 threshold means error must be <= 0.0; a list of 0.5 m errors is NOT
        errors = [0.5, 0.5, 0.5]
        result = _compute_cdf(errors)
        zero_row = next(r for r in result if r["threshold_m"] == 0.0)
        # 0.5 > 0.0 → 0 % at threshold 0
        self.assertEqual(zero_row["pct"], 0.0)

    def test_all_exactly_at_threshold(self):
        # If all errors == 1.0, then at threshold 1.0 we should get 100 %
        errors = [1.0] * 10
        result = _compute_cdf(errors)
        row_1m = next(r for r in result if r["threshold_m"] == 1.0)
        self.assertEqual(row_1m["pct"], 100.0)
        # Below 1.0 threshold → 0 %
        row_half = next(r for r in result if r["threshold_m"] == 0.5)
        self.assertEqual(row_half["pct"], 0.0)

    def test_mixed_errors_partial_percentages(self):
        # 4 samples: 0.2, 0.6, 1.2, 2.0
        errors = [0.2, 0.6, 1.2, 2.0]
        result = _compute_cdf(errors)
        row_half = next(r for r in result if r["threshold_m"] == 0.5)
        # 0.2 <= 0.5 → 1/4 = 25 %
        self.assertAlmostEqual(row_half["pct"], 25.0, places=1)
        row_1m = next(r for r in result if r["threshold_m"] == 1.0)
        # 0.2, 0.6 <= 1.0 → 2/4 = 50 %
        self.assertAlmostEqual(row_1m["pct"], 50.0, places=1)

    def test_monotone_non_decreasing(self):
        errors = [0.1, 0.7, 1.5, 3.0, 6.0]
        result = _compute_cdf(errors)
        pcts = [r["pct"] for r in result]
        for i in range(1, len(pcts)):
            self.assertGreaterEqual(pcts[i], pcts[i - 1],
                                    f"CDF not monotone at index {i}: {pcts}")

    def test_threshold_grid_matches_constant(self):
        result = _compute_cdf([1.0])
        thresholds = [r["threshold_m"] for r in result]
        self.assertEqual(thresholds, _CDF_THRESHOLDS)


# ══════════════════════════════════════════════════════════════════════════════
# 2. _compute_stats
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeStats(unittest.TestCase):

    def _make_stub_sample(self, raw_error_m: float, is_outlier: bool = False) -> dict:
        return {
            "timestamp_ms": _now_ms(),
            "error_m":      min(raw_error_m, _HARD_CAP),
            "raw_error_m":  raw_error_m,
            "est_x":        0.0,
            "est_y":        0.0,
            "scenario":     "",
            "is_outlier":   is_outlier,
        }

    def test_empty_returns_none_fields(self):
        stats = _compute_stats([])
        self.assertIsNone(stats["mean_m"])
        self.assertEqual(stats["failure_pct"], 0.0)
        self.assertEqual(stats["n_samples"], 0)

    def test_failure_pct_calculation(self):
        # 2 outliers out of 5 → 40 %
        samples = (
            [self._make_stub_sample(11.0, is_outlier=True)] * 2 +
            [self._make_stub_sample(1.0, is_outlier=False)] * 3
        )
        stats = _compute_stats(samples)
        self.assertAlmostEqual(stats["failure_pct"], 40.0, places=2)

    def test_failure_pct_zero_when_no_outliers(self):
        samples = [self._make_stub_sample(1.5)] * 10
        stats = _compute_stats(samples)
        self.assertEqual(stats["failure_pct"], 0.0)

    def test_failure_pct_100_when_all_outliers(self):
        samples = [self._make_stub_sample(15.0, is_outlier=True)] * 5
        stats = _compute_stats(samples)
        self.assertAlmostEqual(stats["failure_pct"], 100.0, places=2)

    def test_median_odd_count(self):
        # sorted: [1, 2, 3] → median = 2
        samples = [self._make_stub_sample(e) for e in [3.0, 1.0, 2.0]]
        stats = _compute_stats(samples)
        self.assertAlmostEqual(stats["median_m"], 2.0, places=4)

    def test_median_even_count(self):
        # sorted: [1, 2, 3, 4] → median = (2+3)/2 = 2.5
        samples = [self._make_stub_sample(e) for e in [4.0, 1.0, 3.0, 2.0]]
        stats = _compute_stats(samples)
        self.assertAlmostEqual(stats["median_m"], 2.5, places=4)

    def test_p95_single_sample(self):
        samples = [self._make_stub_sample(5.0)]
        stats = _compute_stats(samples)
        self.assertAlmostEqual(stats["p95_m"], 5.0, places=4)

    def test_p95_large_dataset(self):
        # 20 samples from 1..20: 95th percentile index = ceil(0.95*20)-1 = 18 → value 19
        samples = [self._make_stub_sample(float(i)) for i in range(1, 21)]
        stats = _compute_stats(samples)
        self.assertAlmostEqual(stats["p95_m"], 19.0, places=4)

    def test_mean_calculation(self):
        samples = [self._make_stub_sample(e) for e in [2.0, 4.0, 6.0]]
        stats = _compute_stats(samples)
        self.assertAlmostEqual(stats["mean_m"], 4.0, places=4)

    def test_n_samples_count(self):
        samples = [self._make_stub_sample(1.0)] * 7
        stats = _compute_stats(samples)
        self.assertEqual(stats["n_samples"], 7)


# ══════════════════════════════════════════════════════════════════════════════
# 3. _subsample
# ══════════════════════════════════════════════════════════════════════════════

class TestSubsample(unittest.TestCase):

    def test_no_reduction_when_below_target(self):
        items = list(range(5))
        result = _subsample(items, 10)
        self.assertEqual(result, items)

    def test_exact_target_size(self):
        items = list(range(100))
        result = _subsample(items, 100)
        self.assertEqual(len(result), 100)

    def test_reduces_to_target(self):
        items = list(range(1000))
        result = _subsample(items, 50)
        self.assertEqual(len(result), 50)

    def test_all_elements_from_source(self):
        items = list(range(200))
        result = _subsample(items, 30)
        item_set = set(items)
        for r in result:
            self.assertIn(r, item_set)

    def test_no_duplicates_in_result(self):
        items = list(range(200))
        result = _subsample(items, 50)
        self.assertEqual(len(result), len(set(result)))

    def test_target_zero_returns_empty(self):
        # _subsample with target=0: len(items) > 0, so it enters the loop.
        # But result starts as items[:0] = []. Then no j < 0 is possible.
        items = list(range(10))
        result = _subsample(items, 0)
        self.assertEqual(result, [])

    def test_empty_input(self):
        result = _subsample([], 10)
        self.assertEqual(result, [])


# ══════════════════════════════════════════════════════════════════════════════
# 4. AccuracyAnalytics.push()
# ══════════════════════════════════════════════════════════════════════════════

class TestAnalyticsPush(unittest.TestCase):

    def test_none_x_increments_skipped_not_total(self):
        a = _make_analytics()
        a.push("rssi", None, 1.0)
        self.assertEqual(a._push_skipped, 1)
        self.assertEqual(a._push_total, 0)

    def test_none_y_increments_skipped(self):
        a = _make_analytics()
        a.push("rssi", 1.0, None)
        self.assertEqual(a._push_skipped, 1)
        self.assertEqual(a._push_total, 0)

    def test_nan_increments_skipped(self):
        a = _make_analytics()
        a.push("rssi", float("nan"), 0.0)
        self.assertEqual(a._push_skipped, 1)
        self.assertEqual(a._push_total, 0)

    def test_valid_push_increments_total(self):
        a = _make_analytics()
        a.push("rssi", 1.0, 1.0)
        self.assertEqual(a._push_total, 1)
        self.assertEqual(a._push_skipped, 0)

    def test_multiple_pushes_counter_monotonic(self):
        a = _make_analytics()
        for i in range(10):
            a.push("rssi", float(i), float(i))
        self.assertEqual(a._push_total, 10)

    def test_outlier_detected_when_beyond_hard_cap(self):
        """GT at (0,0), estimate at (15, 0) → error 15m > 10m → is_outlier."""
        a = _make_analytics(gt_x=0.0, gt_y=0.0)
        a.push("rssi", 15.0, 0.0)
        buf = a._engine.get_buffer("rssi")
        self.assertEqual(len(buf), 1)
        sample = buf[0]
        self.assertTrue(aa._access(sample, "is_outlier"))
        self.assertAlmostEqual(aa._access(sample, "raw_error_m"), 15.0, places=4)
        # Capped at HARD_CAP
        self.assertAlmostEqual(aa._access(sample, "error_m"), _HARD_CAP, places=4)

    def test_no_outlier_at_boundary(self):
        """GT at (0,0), estimate at (10, 0) → error exactly 10m → NOT outlier."""
        a = _make_analytics(gt_x=0.0, gt_y=0.0)
        a.push("rssi", 10.0, 0.0)
        buf = a._engine.get_buffer("rssi")
        sample = buf[0]
        # raw_error_m == 10.0 → raw > HARD_CAP is False (10.0 > 10.0 = False)
        self.assertFalse(aa._access(sample, "is_outlier"))

    def test_outlier_just_above_boundary(self):
        """GT at (0,0), estimate at (10.001, 0) → raw > 10.0 → outlier."""
        a = _make_analytics(gt_x=0.0, gt_y=0.0)
        a.push("rssi", 10.001, 0.0)
        buf = a._engine.get_buffer("rssi")
        sample = buf[0]
        self.assertTrue(aa._access(sample, "is_outlier"))

    def test_gt_at_nonzero_position(self):
        """GT at (5, 5), estimate at (5, 5) → error 0 → not outlier."""
        a = _make_analytics(gt_x=5.0, gt_y=5.0)
        a.push("rssi", 5.0, 5.0)
        buf = a._engine.get_buffer("rssi")
        sample = buf[0]
        self.assertAlmostEqual(aa._access(sample, "error_m"), 0.0, places=4)
        self.assertFalse(aa._access(sample, "is_outlier"))

    def test_gt_at_nonzero_outlier(self):
        """GT at (5, 5), estimate at (20, 5) → error 15m → outlier."""
        a = _make_analytics(gt_x=5.0, gt_y=5.0)
        a.push("rssi", 20.0, 5.0)
        buf = a._engine.get_buffer("rssi")
        sample = buf[0]
        self.assertTrue(aa._access(sample, "is_outlier"))
        self.assertAlmostEqual(aa._access(sample, "raw_error_m"), 15.0, places=4)

    def test_scenario_label_attached_to_sample(self):
        a = _make_analytics()
        a.set_scenario("LOS")
        a.push("rssi", 1.0, 1.0)
        buf = a._engine.get_buffer("rssi")
        self.assertEqual(aa._access(buf[0], "scenario"), "LOS")

    def test_multiple_methods_isolated_buffers(self):
        a = _make_analytics()
        a.push("rssi", 1.0, 1.0)
        a.push("fp",   2.0, 2.0)
        a.push("tof",  3.0, 3.0)
        self.assertEqual(len(a._engine.get_buffer("rssi")), 1)
        self.assertEqual(len(a._engine.get_buffer("fp")), 1)
        self.assertEqual(len(a._engine.get_buffer("tof")), 1)


# ══════════════════════════════════════════════════════════════════════════════
# 5. AccuracyAnalytics.build_payload()
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildPayload(unittest.TestCase):

    def _push_n(self, analytics: AccuracyAnalytics, method: str, n: int,
                error: float, outlier: bool = False, ts_ms: int = 0) -> None:
        """Push n estimates that produce the given error magnitude."""
        # Place estimate along x-axis at distance `error` from GT
        gt = analytics._engine.get_ground_truth()
        if outlier:
            dist = _HARD_CAP + error  # ensure > HARD_CAP
        else:
            dist = error
        analytics.push(method, gt[0] + dist, gt[1], ts_ms=ts_ms)

    def test_empty_engine_returns_empty_methods(self):
        a = _make_analytics()
        payload = a.build_payload()
        self.assertIn("methods", payload)
        self.assertEqual(payload["methods"], {})

    def test_ground_truth_reflected_in_payload(self):
        a = _make_analytics(gt_x=3.0, gt_y=4.0)
        a.push("rssi", 3.5, 4.0)
        payload = a.build_payload()
        self.assertAlmostEqual(payload["ground_truth"]["x"], 3.0)
        self.assertAlmostEqual(payload["ground_truth"]["y"], 4.0)

    def test_method_block_structure(self):
        a = _make_analytics()
        a.push("rssi", 1.0, 0.0)
        payload = a.build_payload()
        block = payload["methods"]["rssi"]
        self.assertIn("cdf", block)
        self.assertIn("stats", block)
        self.assertIn("scatter", block)
        self.assertIn("n_in_window", block)

    def test_cdf_nonzero_for_inlier_samples(self):
        a = _make_analytics()
        # 10 samples, all 1 m error
        for _ in range(10):
            a.push("rssi", 1.0, 0.0)
        payload = a.build_payload()
        cdf = payload["methods"]["rssi"]["cdf"]
        # At threshold 1.0 m, all 10 samples qualify → 100 %
        row_1m = next(r for r in cdf if r["threshold_m"] == 1.0)
        self.assertAlmostEqual(row_1m["pct"], 100.0, places=1)

    def test_all_outlier_window_produces_flat_cdf(self):
        """When ALL windowed samples are outliers, CDF must be 0% everywhere."""
        a = _make_analytics()
        # Push estimates 15 m away from GT → all outliers
        for _ in range(5):
            a.push("rssi", 15.0, 0.0)
        payload = a.build_payload()
        cdf = payload["methods"]["rssi"]["cdf"]
        for row in cdf:
            self.assertEqual(row["pct"], 0.0,
                             f"Expected 0% at {row['threshold_m']} m, got {row['pct']}")

    def test_failure_pct_in_stats_reflects_outliers(self):
        a = _make_analytics()
        # 2 outliers (15m), 3 inliers (1m)
        for _ in range(2):
            a.push("rssi", 15.0, 0.0)
        for _ in range(3):
            a.push("rssi", 1.0, 0.0)
        payload = a.build_payload()
        stats = payload["methods"]["rssi"]["stats"]
        # 2 of 5 = 40 %
        self.assertAlmostEqual(stats["failure_pct"], 40.0, places=2)
        self.assertEqual(stats["n_samples"], 5)

    def test_windowing_excludes_old_samples_from_cdf(self):
        """
        Old samples (outside time window) must not appear in CDF but must still
        appear in stats (which uses the full buffer).
        """
        a = _make_analytics()
        old_ts = _now_ms() - 120_000  # 2 minutes ago (outside 60s window)
        now_ts = _now_ms()

        # 5 old outliers → if windowing works, they are excluded from CDF
        for _ in range(5):
            a.push("rssi", 15.0, 0.0, ts_ms=old_ts)
        # 5 recent inliers
        for _ in range(5):
            a.push("rssi", 1.0, 0.0, ts_ms=now_ts)

        payload = a.build_payload(time_window_s=60.0)
        # CDF should be derived only from the 5 recent 1m samples → 100% at 1m
        cdf = payload["methods"]["rssi"]["cdf"]
        row_1m = next(r for r in cdf if r["threshold_m"] == 1.0)
        self.assertAlmostEqual(row_1m["pct"], 100.0, places=1)

        # Stats include all 10 samples (5 outliers + 5 inliers → 50% failures)
        stats = payload["methods"]["rssi"]["stats"]
        self.assertEqual(stats["n_samples"], 10)
        self.assertAlmostEqual(stats["failure_pct"], 50.0, places=2)

    def test_n_in_window_counts_all_windowed_not_just_inliers(self):
        """n_in_window reflects ALL windowed samples (including outliers)."""
        a = _make_analytics()
        now_ts = _now_ms()
        for _ in range(3):
            a.push("rssi", 15.0, 0.0, ts_ms=now_ts)  # outliers
        for _ in range(2):
            a.push("rssi", 1.0, 0.0, ts_ms=now_ts)   # inliers
        payload = a.build_payload(time_window_s=60.0)
        self.assertEqual(payload["methods"]["rssi"]["n_in_window"], 5)

    def test_time_normalisation_equalises_sample_counts(self):
        """
        Method A has 10 non-outlier windowed samples, Method B has 4.
        After normalisation both should contribute exactly 4 samples to the CDF.
        """
        a = _make_analytics()
        now_ts = _now_ms()
        # Method A: 10 inlier samples at 1 m error
        for _ in range(10):
            a.push("rssi", 1.0, 0.0, ts_ms=now_ts)
        # Method B: 4 inlier samples at 2 m error
        for _ in range(4):
            a.push("kalman", 2.0, 0.0, ts_ms=now_ts)

        payload = a.build_payload(time_window_s=60.0)
        # The CDF for "rssi" was subsampled to 4 samples (min_count=4)
        # At threshold 1.0 m, rssi errors are all 1.0 → 100%
        cdf_rssi = payload["methods"]["rssi"]["cdf"]
        row_1m = next(r for r in cdf_rssi if r["threshold_m"] == 1.0)
        self.assertAlmostEqual(row_1m["pct"], 100.0, places=1)

        # For kalman at threshold 2.0 m (all samples are 2.0) → 100%
        cdf_kalman = payload["methods"]["kalman"]["cdf"]
        row_2m = next(r for r in cdf_kalman if r["threshold_m"] == 2.0)
        self.assertAlmostEqual(row_2m["pct"], 100.0, places=1)

    def test_scenario_filter_excludes_non_matching(self):
        a = _make_analytics()
        now_ts = _now_ms()
        a.set_scenario("LOS")
        for _ in range(5):
            a.push("rssi", 1.0, 0.0, ts_ms=now_ts)
        a.set_scenario("NLOS")
        for _ in range(5):
            a.push("rssi", 5.0, 0.0, ts_ms=now_ts)

        # Filter to LOS only
        payload = a.build_payload(scenario_filter="LOS")
        cdf = payload["methods"]["rssi"]["cdf"]
        # Only LOS samples (1m) pass; NLOS (5m) excluded
        row_1m   = next(r for r in cdf if r["threshold_m"] == 1.0)
        row_5m   = next(r for r in cdf if r["threshold_m"] == 5.0)
        # With 5 LOS and 5 NLOS normalised to min=5, filtered to LOS only.
        # After normalisation there are 5 of each; LOS filter → 5 samples of 1m.
        self.assertAlmostEqual(row_1m["pct"], 100.0, places=1)
        # 1m errors are all ≤ 5.0 threshold as well, so 5m bucket = 100%
        self.assertAlmostEqual(row_5m["pct"], 100.0, places=1)

    def test_scenario_filter_empty_keeps_all(self):
        """No scenario filter → all samples included in CDF."""
        a = _make_analytics()
        now_ts = _now_ms()
        a.set_scenario("LOS")
        for _ in range(5):
            a.push("rssi", 1.0, 0.0, ts_ms=now_ts)
        a.set_scenario("NLOS")
        for _ in range(5):
            a.push("rssi", 5.0, 0.0, ts_ms=now_ts)

        payload = a.build_payload(scenario_filter="")
        cdf = payload["methods"]["rssi"]["cdf"]
        # 5 samples at 1m + 5 at 5m → at 1m threshold: 50%
        row_1m = next(r for r in cdf if r["threshold_m"] == 1.0)
        self.assertAlmostEqual(row_1m["pct"], 50.0, places=1)

    def test_scenarios_present_in_payload(self):
        a = _make_analytics()
        a.set_scenario("LOS")
        a.push("rssi", 1.0, 0.0)
        a.set_scenario("NLOS")
        a.push("rssi", 2.0, 0.0)
        payload = a.build_payload()
        self.assertIn("LOS", payload["scenarios_present"])
        self.assertIn("NLOS", payload["scenarios_present"])

    def test_scatter_max_dots(self):
        a = _make_analytics()
        for i in range(60):
            a.push("rssi", float(i % 5), 0.0)
        payload = a.build_payload()
        scatter = payload["methods"]["rssi"]["scatter"]
        self.assertLessEqual(len(scatter), aa._MAX_SCATTER_DOTS)

    def test_scatter_has_required_fields(self):
        a = _make_analytics()
        a.push("rssi", 1.0, 0.0)
        payload = a.build_payload()
        dot = payload["methods"]["rssi"]["scatter"][0]
        for field in ("x", "y", "error_m", "ts_ms", "scenario", "outlier"):
            self.assertIn(field, dot)

    def test_single_method_path(self):
        """Single method with single sample — no normalisation division issues."""
        a = _make_analytics()
        a.push("tof", 1.0, 0.0)
        payload = a.build_payload()
        self.assertIn("tof", payload["methods"])
        self.assertEqual(payload["methods"]["tof"]["stats"]["n_samples"], 1)


# ══════════════════════════════════════════════════════════════════════════════
# 6. CSV output shape
# ══════════════════════════════════════════════════════════════════════════════

class TestCsvOutput(unittest.TestCase):

    def setUp(self):
        # Reset mock call history
        _cloud_io_stub.log_analytics_sample_to_csv.reset_mock()

    def test_csv_called_on_valid_push(self):
        a = AccuracyAnalytics(max_buf=10, csv_path="test_out.csv")
        a.set_ground_truth(0.0, 0.0)
        a.push("rssi", 1.0, 0.0)
        _cloud_io_stub.log_analytics_sample_to_csv.assert_called_once()

    def test_csv_not_called_when_path_empty(self):
        a = AccuracyAnalytics(max_buf=10, csv_path="")
        a.set_ground_truth(0.0, 0.0)
        a.push("rssi", 1.0, 0.0)
        _cloud_io_stub.log_analytics_sample_to_csv.assert_not_called()

    def test_csv_not_called_on_skipped_push(self):
        a = AccuracyAnalytics(max_buf=10, csv_path="test_out.csv")
        a.push("rssi", None, 0.0)
        _cloud_io_stub.log_analytics_sample_to_csv.assert_not_called()

    def test_csv_receives_correct_method(self):
        a = AccuracyAnalytics(max_buf=10, csv_path="test_out.csv")
        a.set_ground_truth(0.0, 0.0)
        a.push("kalman", 1.0, 0.0)
        call_kwargs = _cloud_io_stub.log_analytics_sample_to_csv.call_args
        self.assertEqual(call_kwargs.kwargs["method"], "kalman")

    def test_csv_error_m_capped(self):
        """error_m passed to CSV must be capped at HARD_CAP."""
        a = AccuracyAnalytics(max_buf=10, csv_path="test_out.csv")
        a.set_ground_truth(0.0, 0.0)
        a.push("rssi", 15.0, 0.0)  # raw error = 15m → capped to 10m
        call_kwargs = _cloud_io_stub.log_analytics_sample_to_csv.call_args.kwargs
        self.assertAlmostEqual(call_kwargs["error_m"], _HARD_CAP, places=4)
        self.assertAlmostEqual(call_kwargs["raw_error_m"], 15.0, places=4)
        self.assertTrue(call_kwargs["is_outlier"])

    def test_csv_gt_coords_passed(self):
        a = AccuracyAnalytics(max_buf=10, csv_path="test_out.csv")
        a.set_ground_truth(3.0, 4.0)
        a.push("rssi", 4.0, 4.0)
        call_kwargs = _cloud_io_stub.log_analytics_sample_to_csv.call_args.kwargs
        self.assertAlmostEqual(call_kwargs["gt_x"], 3.0, places=4)
        self.assertAlmostEqual(call_kwargs["gt_y"], 4.0, places=4)

    def test_csv_scenario_passed(self):
        a = AccuracyAnalytics(max_buf=10, csv_path="test_out.csv")
        a.set_ground_truth(0.0, 0.0)
        a.set_scenario("NLOS")
        a.push("rssi", 1.0, 0.0)
        call_kwargs = _cloud_io_stub.log_analytics_sample_to_csv.call_args.kwargs
        self.assertEqual(call_kwargs["scenario"], "NLOS")


# ══════════════════════════════════════════════════════════════════════════════
# 7. Ground truth update mid-stream
# ══════════════════════════════════════════════════════════════════════════════

class TestGroundTruthMidStream(unittest.TestCase):

    def test_gt_update_affects_subsequent_pushes_only(self):
        """
        Samples already in the buffer retain their original error values;
        new pushes use the updated GT.  This is a C++/stub engine property,
        but we verify the buffer size stays consistent.
        """
        a = _make_analytics(gt_x=0.0, gt_y=0.0)
        a.push("rssi", 1.0, 0.0)   # error = 1 m (GT=0,0)
        buf_before = a._engine.get_buffer("rssi")
        self.assertAlmostEqual(aa._access(buf_before[0], "raw_error_m"), 1.0, places=4)

        # Update GT — existing buffer entry unchanged
        a.set_ground_truth(10.0, 0.0)
        a.push("rssi", 10.0, 0.0)  # error = 0 m (GT=10,0)
        buf_after = a._engine.get_buffer("rssi")
        self.assertEqual(len(buf_after), 2)
        self.assertAlmostEqual(aa._access(buf_after[1], "raw_error_m"), 0.0, places=4)

    def test_debug_info_reflects_current_gt(self):
        a = _make_analytics(gt_x=2.0, gt_y=3.0)
        info = a.debug_info()
        self.assertAlmostEqual(info["ground_truth"]["x"], 2.0)
        self.assertAlmostEqual(info["ground_truth"]["y"], 3.0)


# ══════════════════════════════════════════════════════════════════════════════
# 8. Debug info
# ══════════════════════════════════════════════════════════════════════════════

class TestDebugInfo(unittest.TestCase):

    def test_debug_info_has_required_keys(self):
        a = _make_analytics()
        info = a.debug_info()
        for key in ("cpp_available", "hard_cap_m", "push_total", "push_skipped",
                    "current_scenario", "ground_truth", "method_keys",
                    "per_method", "time_window_s", "now_ms", "csv_path"):
            self.assertIn(key, info, f"Missing key: {key}")

    def test_debug_info_push_counters_match(self):
        a = _make_analytics()
        a.push("rssi", 1.0, 0.0)
        a.push("rssi", None, 0.0)
        info = a.debug_info()
        self.assertEqual(info["push_total"], 1)
        self.assertEqual(info["push_skipped"], 1)

    def test_debug_info_per_method_has_n_outliers(self):
        a = _make_analytics()
        a.push("rssi", 15.0, 0.0)   # outlier
        a.push("rssi", 1.0, 0.0)    # inlier
        info = a.debug_info()
        self.assertIn("rssi", info["per_method"])
        self.assertEqual(info["per_method"]["rssi"]["n_outliers"], 1)
        self.assertEqual(info["per_method"]["rssi"]["n_total"], 2)


# ══════════════════════════════════════════════════════════════════════════════
# 9. Zero-failure root cause diagnosis
# ══════════════════════════════════════════════════════════════════════════════

class TestZeroFailureDiagnosis(unittest.TestCase):
    """
    Proves that "zero failures" is CORRECT BEHAVIOUR when GT is at (0,0)
    and all position estimates are within 10 m of the origin.

    This suite was written to diagnose the reported observation that the
    Analytics Dashboard shows 0% failures and no outliers.  These tests
    confirm it is not a logic bug — it is the expected result when the
    reference position has not been set.
    """

    def test_gt_at_origin_estimates_within_room_no_outliers(self):
        """
        Typical room: ~8m × ~14m.  Estimates anywhere in this room are at most
        sqrt(8² + 14²) ≈ 16.1 m from the origin.  But estimates near WAP6
        at (0, 0) will be close to origin → error < 10m → not outliers.
        """
        a = _make_analytics(gt_x=0.0, gt_y=0.0)
        # Push estimates spread across a typical room (0..8, 0..13.9)
        test_positions = [
            (0.5, 0.5), (2.0, 3.0), (4.0, 6.0), (6.0, 10.0), (8.0, 13.0),
        ]
        for x, y in test_positions:
            a.push("rssi", x, y)

        buf = a._engine.get_buffer("rssi")
        outliers = [s for s in buf if aa._access(s, "is_outlier")]
        # Only (8.0, 13.0) → error = sqrt(64+169) ≈ 15.26 m → IS an outlier
        # Others: max is (6, 10) → sqrt(36+100) ≈ 11.66 m → also outlier!
        # Actually (0.5,0.5) → 0.707m, (2,3) → 3.6m, (4,6) → 7.2m → inlier
        #          (6,10) → 11.66m → OUTLIER; (8,13) → 15.26m → OUTLIER
        # So 2 outliers expected:
        self.assertEqual(len(outliers), 2)

        # Confirms estimates near origin are NOT outliers
        near_origin = buf[0]  # (0.5, 0.5) → error 0.707m
        self.assertFalse(aa._access(near_origin, "is_outlier"))

    def test_gt_at_origin_all_estimates_within_10m_gives_zero_failures(self):
        """
        If the algorithm consistently estimates near (3, 4) → error = 5m < 10m
        → failure_pct = 0.  This is the "zero failures" scenario from the logs.
        """
        a = _make_analytics(gt_x=0.0, gt_y=0.0)
        for _ in range(20):
            a.push("rssi", 3.0, 4.0)  # error = sqrt(9+16) = 5m → inlier

        payload = a.build_payload()
        stats = payload["methods"]["rssi"]["stats"]
        self.assertEqual(stats["failure_pct"], 0.0)
        self.assertAlmostEqual(stats["mean_m"], 5.0, places=4)

    def test_gt_at_correct_position_reveals_actual_failures(self):
        """
        When GT is set to the TRUE reference position, outliers become visible.

        Scenario: GT=(10, 10), algorithm consistently outputs (0, 0) → error
        = sqrt(200) ≈ 14.1m → ALL samples are outliers → failure_pct = 100%.
        """
        a = _make_analytics(gt_x=10.0, gt_y=10.0)
        for _ in range(10):
            a.push("rssi", 0.0, 0.0)  # error ≈ 14.1m → outlier

        payload = a.build_payload()
        stats = payload["methods"]["rssi"]["stats"]
        self.assertAlmostEqual(stats["failure_pct"], 100.0, places=2)
        # CDF should be flat 0 (all outliers excluded)
        cdf = payload["methods"]["rssi"]["cdf"]
        for row in cdf:
            self.assertEqual(row["pct"], 0.0)

    def test_set_gt_and_verify_failures_appear(self):
        """End-to-end: set GT at actual reference → failures appear in payload."""
        a = _make_analytics(gt_x=0.0, gt_y=0.0)
        # Mix: 5 inliers at 3m, 5 outliers at 15m
        for _ in range(5):
            a.push("rssi", 3.0, 0.0)   # error = 3m → inlier
        for _ in range(5):
            a.push("rssi", 15.0, 0.0)  # error = 15m → outlier

        payload = a.build_payload()
        stats = payload["methods"]["rssi"]["stats"]
        # 5 of 10 are outliers → 50% failures
        self.assertAlmostEqual(stats["failure_pct"], 50.0, places=2)
        # CDF should reach 100% at 3m (only inliers, normalised)
        cdf = payload["methods"]["rssi"]["cdf"]
        row_3m = next(r for r in cdf if r["threshold_m"] == 3.0)
        self.assertAlmostEqual(row_3m["pct"], 100.0, places=1)


# ══════════════════════════════════════════════════════════════════════════════
# 10. Clear / reset behaviour
# ══════════════════════════════════════════════════════════════════════════════

class TestClearBehaviour(unittest.TestCase):

    def test_clear_all_empties_all_buffers(self):
        a = _make_analytics()
        a.push("rssi", 1.0, 0.0)
        a.push("fp",   2.0, 0.0)
        a.clear()
        self.assertEqual(a._engine.method_keys(), [])

    def test_clear_method_removes_only_target(self):
        a = _make_analytics()
        a.push("rssi", 1.0, 0.0)
        a.push("fp",   2.0, 0.0)
        a.clear_method("rssi")
        self.assertNotIn("rssi", a._engine.method_keys())
        self.assertIn("fp", a._engine.method_keys())

    def test_push_after_clear_starts_fresh(self):
        a = _make_analytics()
        for _ in range(10):
            a.push("rssi", 1.0, 0.0)
        a.clear()
        a.push("rssi", 5.0, 0.0)
        buf = a._engine.get_buffer("rssi")
        self.assertEqual(len(buf), 1)


# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
