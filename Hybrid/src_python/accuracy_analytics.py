"""
accuracy_analytics.py — Analytics Dashboard: Python aggregation bridge.

Responsibility boundary
-----------------------
  C++ AccuracyEngine  : raw ErrorSample storage, immediate Euclidean error calc
  This module         : pull C++ buffers, normalise across methods,
                        compute CDF + statistics, emit lightweight JSON payload
  app.py              : call push_estimate() on every decision; serve payload
                        via GET /analytics/accuracy

Design decisions
----------------

Time-window normalisation (The Time-Desynced Data fix)
  Different algorithms emit decisions at different rates (ToF ≈ 10 Hz, RSSI ≈ 2 Hz).
  Comparing their CDFs using all buffered samples would bias toward the slower
  method (fewer samples → smoother-looking CDF).  This layer applies a rolling
  time window (default 60 s) and additionally resamples each method's error
  series to the same count (the *minimum* across all active methods) so every
  line in the CDF chart represents the same number of observations.

Outlier handling (The Outlier Squeeze fix)
  raw_error_m values > HARD_CAP_M are stored at HARD_CAP_M by the C++ engine.
  is_outlier=True samples are counted separately as "failure" events and excluded
  from the main CDF curve, but the ">10 m failure" bucket is still reported so
  the UI can show a "% failures" badge without distorting the chart.

Scenario tagging (The Ground Truth Context fix)
  Every ErrorSample carries a `scenario` string (e.g. "LOS", "NLOS").  The
  payload's `scenarios_present` set lets the frontend populate the filter
  dropdown.  When `scenario_filter` is non-empty, only matching samples feed
  the CDF; the full summary stats always reflect all scenarios.

CDF construction
  Bins: 0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0 m
  Each bin value = fraction of (non-outlier) samples with error_m <= threshold.
  Expressed as 0–100 percentage.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import cloud_io

_log = logging.getLogger(__name__)

# ── Import C++ engine (falls back gracefully when not compiled) ───────────────
try:
    import capstone_core as cc
    _CPP_AVAILABLE = True
    _HARD_CAP: float = cc.ACCURACY_HARD_CAP_M
except ImportError:
    _CPP_AVAILABLE = False
    _HARD_CAP: float = 10.0
    _log.warning("accuracy_analytics: capstone_core not available — running in stub mode")

# ── CDF threshold grid (metres) ───────────────────────────────────────────────
_CDF_THRESHOLDS: List[float] = [
    0.0, 0.25, 0.5, 0.75,
    1.0, 1.25, 1.5, 2.0, 2.5,
    3.0, 4.0, 5.0, 7.0, 10.0,
]

# Time window used for normalisation (seconds).  Only samples inside this window
# are considered for CDF computation so the comparison stays temporally aligned.
_TIME_WINDOW_S: float = 60.0

# Maximum recent scatter dots sent to the frontend per method (keeps payload small).
_MAX_SCATTER_DOTS: int = 40


# ═════════════════════════════════════════════════════════════════════════════
class _StubEngine:
    """
    Pure-Python fallback used when capstone_core is not compiled.
    Provides the same public interface as cc.AccuracyEngine.
    """
    def __init__(self, max_buf: int = 500):
        self._max_buf = max_buf
        self._gt = (0.0, 0.0)
        self._buffers: Dict[str, list] = {}
        self._lock = threading.Lock()

    def set_ground_truth(self, x: float, y: float) -> None:
        with self._lock:
            self._gt = (x, y)

    def get_ground_truth(self) -> Tuple[float, float]:
        with self._lock:
            return self._gt

    def push_estimate(self, method: str, est_x: float, est_y: float,
                      scenario: str = "", ts_ms: int = 0) -> None:
        gx, gy = self.get_ground_truth()
        dx, dy = est_x - gx, est_y - gy
        raw_err = math.sqrt(dx * dx + dy * dy)
        is_out  = raw_err > _HARD_CAP
        capped  = _HARD_CAP if is_out else raw_err
        ts = ts_ms if ts_ms > 0 else int(time.time() * 1000)
        sample = {
            "timestamp_ms": ts, "error_m": capped, "raw_error_m": raw_err,
            "est_x": est_x, "est_y": est_y, "scenario": scenario,
            "is_outlier": is_out,
        }
        with self._lock:
            buf = self._buffers.setdefault(method, [])
            buf.append(sample)
            if len(buf) > self._max_buf:
                self._buffers[method] = buf[-self._max_buf:]

    def get_buffer(self, method: str) -> list:
        with self._lock:
            return list(self._buffers.get(method, []))

    def method_keys(self) -> List[str]:
        with self._lock:
            return sorted(k for k, v in self._buffers.items() if v)

    def clear_method(self, method: str) -> None:
        with self._lock:
            self._buffers.pop(method, None)

    def clear_all(self) -> None:
        with self._lock:
            self._buffers.clear()

    @property
    def max_buf(self) -> int:
        return self._max_buf


# ═════════════════════════════════════════════════════════════════════════════
def _make_engine(max_buf: int = 500):
    """Return cc.AccuracyEngine if C++ is available, else stub."""
    if _CPP_AVAILABLE:
        return cc.AccuracyEngine(max_buf)
    return _StubEngine(max_buf)


# ═════════════════════════════════════════════════════════════════════════════
def _access(sample, field: str):
    """Uniform access for both cc.ErrorSample (attrs) and stub dicts."""
    if isinstance(sample, dict):
        return sample[field]
    return getattr(sample, field)


# ═════════════════════════════════════════════════════════════════════════════
def _compute_cdf(errors: List[float]) -> List[Dict[str, float]]:
    """
    Given a list of (non-outlier) error values in metres, return a CDF curve:
    [{threshold_m, pct}, …] for each threshold in _CDF_THRESHOLDS.
    """
    if not errors:
        return [{"threshold_m": t, "pct": 0.0} for t in _CDF_THRESHOLDS]
    n = len(errors)
    return [
        {"threshold_m": t, "pct": round(100.0 * sum(1 for e in errors if e <= t) / n, 2)}
        for t in _CDF_THRESHOLDS
    ]


def _compute_stats(all_samples: list) -> Dict[str, Any]:
    """
    Summary statistics over all samples for one method (all scenarios).
    Returns: mean_m, median_m, p95_m, failure_pct, n_samples
    """
    if not all_samples:
        return {"mean_m": None, "median_m": None, "p95_m": None,
                "failure_pct": 0.0, "n_samples": 0}
    n       = len(all_samples)
    n_out   = sum(1 for s in all_samples if _access(s, "is_outlier"))
    raw_err = sorted(_access(s, "raw_error_m") for s in all_samples)
    mean_m  = round(sum(raw_err) / n, 4)
    mid     = n // 2
    median_m = round(
        raw_err[mid] if n % 2 == 1 else (raw_err[mid - 1] + raw_err[mid]) / 2.0,
        4)
    p95_idx = max(0, int(math.ceil(0.95 * n)) - 1)
    p95_m   = round(raw_err[p95_idx], 4)
    return {
        "mean_m":       mean_m,
        "median_m":     median_m,
        "p95_m":        p95_m,
        "failure_pct":  round(100.0 * n_out / n, 2),
        "n_samples":    n,
    }


def _subsample(items: list, target: int) -> list:
    """
    Reduce `items` to at most `target` entries using reservoir sampling so that
    the statistical distribution is preserved (not just the newest values).
    If len(items) <= target, items are returned unchanged.
    """
    if len(items) <= target:
        return items
    # Reservoir sampling (Fisher-Yates variant)
    result = list(items[:target])
    for i in range(target, len(items)):
        j = random.randint(0, i)
        if j < target:
            result[j] = items[i]
    return result


# ═════════════════════════════════════════════════════════════════════════════
class AccuracyAnalytics:
    """
    High-level interface used by app.py.

    Typical usage::

        analytics = AccuracyAnalytics(max_buf=500)

        # On every localization decision:
        analytics.push(method="rssi", est_x=d.x, est_y=d.y)

        # On GET /analytics/accuracy:
        return analytics.build_payload(scenario_filter="LOS")

    Thread safety
    -------------
    push() is safe to call from any asyncio task because the underlying
    AccuracyEngine is mutex-protected.  build_payload() acquires no
    additional lock — it reads a snapshot from the engine.
    """

    def __init__(self, max_buf: int = 500, csv_path: str = "analytics_log.csv"):
        self._engine = _make_engine(max_buf)
        self._current_scenario: str = ""   # set by POST /analytics/scenario
        self._csv_path: str = csv_path     # file written by push(); "" disables
        # Diagnostic counters — never reset, monotonically increasing
        self._push_total:    int = 0    # total calls to push() that passed guards
        self._push_skipped:  int = 0    # calls skipped (None/NaN coordinates)
        _log.info(
            "AccuracyAnalytics init  cpp=%s  max_buf=%d  csv=%r",
            _CPP_AVAILABLE, max_buf, csv_path,
        )

    # ── Configuration / control ───────────────────────────────────────────────

    def set_ground_truth(self, x: float, y: float) -> None:
        """Update reference position.  Safe to call at any time."""
        self._engine.set_ground_truth(x, y)

    def set_scenario(self, scenario: str) -> None:
        """Tag future pushes with this scenario label."""
        self._current_scenario = scenario.strip()

    def get_scenario(self) -> str:
        return self._current_scenario

    def set_csv_path(self, path: str) -> None:
        """Change the CSV log path at runtime. Pass '' to disable CSV logging."""
        self._csv_path = path
        _log.info("AccuracyAnalytics: CSV path updated to %r", path)

    def get_csv_path(self) -> str:
        return self._csv_path

    def clear(self) -> None:
        self._engine.clear_all()

    def clear_method(self, method: str) -> None:
        self._engine.clear_method(method)

    # ── Data ingestion ────────────────────────────────────────────────────────

    def push(self, method: str, est_x: Optional[float], est_y: Optional[float],
             ts_ms: int = 0) -> None:
        """
        Record a position estimate from an algorithm.

        Silently skips None / NaN coordinates so callers don't need to guard.
        """
        if est_x is None or est_y is None:
            self._push_skipped += 1
            _log.debug(
                "[analytics] push SKIPPED  method=%s  coords=(%s, %s)  reason=None",
                method, est_x, est_y,
            )
            return
        if math.isnan(est_x) or math.isnan(est_y):
            self._push_skipped += 1
            _log.debug(
                "[analytics] push SKIPPED  method=%s  coords=(%.4f, %.4f)  reason=NaN",
                method, est_x, est_y,
            )
            return

        gt = self._engine.get_ground_truth()
        dx = est_x - gt[0]
        dy = est_y - gt[1]
        raw_err = math.sqrt(dx * dx + dy * dy)

        self._push_total += 1
        _log.debug(
            "[analytics] push #%d  method=%-12s  pos=(%.3f, %.3f)  gt=(%.3f, %.3f)"
            "  err=%.3fm  scenario=%r  outlier=%s",
            self._push_total, method, est_x, est_y, gt[0], gt[1],
            raw_err, self._current_scenario, raw_err > _HARD_CAP,
        )

        actual_ts = ts_ms if ts_ms > 0 else int(time.time() * 1000)
        self._engine.push_estimate(
            method, est_x, est_y,
            self._current_scenario,
            actual_ts,
        )

        # ── CSV logging ───────────────────────────────────────────────────────
        if self._csv_path:
            cloud_io.log_analytics_sample_to_csv(
                method       = method,
                est_x        = est_x,
                est_y        = est_y,
                gt_x         = gt[0],
                gt_y         = gt[1],
                error_m      = min(raw_err, _HARD_CAP),
                raw_error_m  = raw_err,
                is_outlier   = raw_err > _HARD_CAP,
                scenario     = self._current_scenario,
                timestamp_ms = actual_ts,
                csv_path     = self._csv_path,
            )

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def debug_info(self) -> Dict[str, Any]:
        """
        Return a lightweight diagnostic snapshot — used by GET /analytics/debug.
        Shows everything needed to understand why data is or isn't appearing.
        """
        gt    = self._engine.get_ground_truth()
        keys  = self._engine.method_keys()
        now_ms = int(time.time() * 1000)

        per_method = {}
        for m in keys:
            buf = self._engine.get_buffer(m)
            if buf:
                newest = buf[-1]
                oldest = buf[0]
                n_out  = sum(1 for s in buf if _access(s, "is_outlier"))
                age_newest_s = (now_ms - _access(newest, "timestamp_ms")) / 1000.0
                per_method[m] = {
                    "n_total":       len(buf),
                    "n_outliers":    n_out,
                    "newest_age_s":  round(age_newest_s, 1),
                    "newest_err_m":  round(_access(newest, "error_m"), 4),
                    "newest_pos":    [round(_access(newest, "est_x"), 3),
                                      round(_access(newest, "est_y"), 3)],
                    "oldest_ts_ms":  _access(oldest, "timestamp_ms"),
                    "scenarios":     sorted({_access(s, "scenario") for s in buf}),
                }
            else:
                per_method[m] = {"n_total": 0}

        return {
            "cpp_available":     _CPP_AVAILABLE,
            "hard_cap_m":        _HARD_CAP,
            "push_total":        self._push_total,
            "push_skipped":      self._push_skipped,
            "current_scenario":  self._current_scenario,
            "ground_truth":      {"x": gt[0], "y": gt[1]},
            "method_keys":       keys,
            "per_method":        per_method,
            "time_window_s":     _TIME_WINDOW_S,
            "now_ms":            now_ms,
            "csv_path":          self._csv_path,
            "csv_logging":       bool(self._csv_path),
        }

    # ── Payload construction ──────────────────────────────────────────────────

    def build_payload(self, scenario_filter: str = "",
                      time_window_s: float = _TIME_WINDOW_S) -> Dict[str, Any]:
        """
        Build the full JSON-serialisable payload for the Analytics Dashboard.

        Parameters
        ----------
        scenario_filter : str
            If non-empty, only samples with this exact scenario contribute to
            the CDF.  Stats always reflect all scenarios.
        time_window_s : float
            Rolling window in seconds.  Only samples within this window are used
            for CDF computation to keep methods time-aligned.

        Returns
        -------
        dict  (JSON-serialisable)
        {
          "ground_truth":       {"x": float, "y": float},
          "current_scenario":   str,
          "scenarios_present":  [str, ...],
          "time_window_s":      float,
          "methods":            {method_name: <MethodBlock>},
        }

        MethodBlock:
        {
          "cdf":          [{threshold_m, pct}, ...],   # normalised / filtered
          "stats":        {mean_m, median_m, p95_m, failure_pct, n_samples},
          "scatter":      [{x, y, error_m, ts_ms, scenario}, ...],
          "n_in_window":  int,   # samples used for CDF
        }
        """
        now_ms  = int(time.time() * 1000)
        cutoff  = now_ms - int(time_window_s * 1000)
        gt      = self._engine.get_ground_truth()
        keys    = self._engine.method_keys()

        # ── 1. Collect per-method windowed samples ────────────────────────────
        windowed: Dict[str, list] = {}
        all_samples: Dict[str, list] = {}
        scenarios_present: set = set()

        for method in keys:
            buf = self._engine.get_buffer(method)
            all_samples[method] = buf
            w   = [s for s in buf if _access(s, "timestamp_ms") >= cutoff]
            windowed[method] = w
            for s in buf:
                sc = _access(s, "scenario")
                if sc:
                    scenarios_present.add(sc)

        # ── 2. Time-normalise: subsample all methods to the same count ────────
        # Use only non-outlier samples for the shared count so one bad method
        # can't drag others down to zero.
        win_non_out: Dict[str, list] = {
            m: [s for s in samps if not _access(s, "is_outlier")]
            for m, samps in windowed.items()
        }
        # Minimum sample count across methods that actually have data
        counts = [len(v) for v in win_non_out.values() if v]
        min_count = min(counts) if counts else 0

        normalised: Dict[str, list] = {}
        if min_count > 0:
            for method, samps in win_non_out.items():
                normalised[method] = _subsample(samps, min_count)
        else:
            normalised = {m: [] for m in keys}

        # ── 3. Apply scenario filter to normalised samples ────────────────────
        filtered: Dict[str, list] = {}
        for method, samps in normalised.items():
            if scenario_filter:
                filtered[method] = [s for s in samps
                                    if _access(s, "scenario") == scenario_filter]
            else:
                filtered[method] = samps

        # ── 4. Build per-method blocks ────────────────────────────────────────
        methods_out: Dict[str, Any] = {}
        for method in keys:
            cdf_errors = [_access(s, "error_m") for s in filtered.get(method, [])]
            stats      = _compute_stats(all_samples.get(method, []))

            # Scatter: newest _MAX_SCATTER_DOTS samples from the window
            scatter_src = windowed.get(method, [])[-_MAX_SCATTER_DOTS:]
            scatter = [
                {
                    "x":        _access(s, "est_x"),
                    "y":        _access(s, "est_y"),
                    "error_m":  round(_access(s, "raw_error_m"), 4),
                    "ts_ms":    _access(s, "timestamp_ms"),
                    "scenario": _access(s, "scenario"),
                    "outlier":  _access(s, "is_outlier"),
                }
                for s in scatter_src
            ]

            methods_out[method] = {
                "cdf":         _compute_cdf(cdf_errors),
                "stats":       stats,
                "scatter":     scatter,
                "n_in_window": len(windowed.get(method, [])),
            }

        return {
            "ground_truth":      {"x": gt[0], "y": gt[1]},
            "current_scenario":  self._current_scenario,
            "scenarios_present": sorted(scenarios_present),
            "time_window_s":     time_window_s,
            "methods":           methods_out,
            "hard_cap_m":        _HARD_CAP,
        }

    # ── Convenience: is the C++ engine compiled? ──────────────────────────────
    @staticmethod
    def cpp_available() -> bool:
        return _CPP_AVAILABLE
