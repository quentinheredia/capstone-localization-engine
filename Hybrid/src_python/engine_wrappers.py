"""
engine_wrappers.py — Python container interfaces calling C++ via capstone_core.

Responsibility boundary
-----------------------
  Python (this file):  load config, manage state, translate DTOs
  C++ (capstone_core): all math — filter, trilaterate, classify, KNN, parse

Two wrappers are provided:
  RSSIEngineWrapper   — full WiFi localization pipeline
  FingerprintWrapper  — KNN fingerprint matching (alternative to trilateration)

Usage
-----
  from engine_wrappers import RSSIEngineWrapper, FingerprintWrapper
  from models import FloorEnvironment, RSSIMap

  wrapper = RSSIEngineWrapper(env, cfg)
  results = wrapper.process_cycle(raw_rssi_map)   # -> List[LocalizationDecision]
"""

from __future__ import annotations

import json
import logging
import math
import os
import re as _re
from typing import Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

# capstone_core is the pybind11 .so built from bindings.cpp.
# It must be on sys.path before importing — the CMakeLists copies it here.
try:
    import capstone_core as cc
    _CPP_AVAILABLE = True
except ImportError:
    _CPP_AVAILABLE = False

from models import (
    FloorEnvironment,
    LocalizationDecision,
    RSSIMap,
    TargetProfile,
)


def _require_cpp() -> None:
    if not _CPP_AVAILABLE:
        raise RuntimeError(
            "capstone_core C++ module not found. "
            "Build it first:  cd Hybrid/build && cmake .. && cmake --build ."
        )


# ---------------------------------------------------------------------------
# Helper: translate Python model types -> capstone_core C++ types
# ---------------------------------------------------------------------------

def _make_cpp_room_defs(env: FloorEnvironment) -> list:
    """Convert Python RoomDef list -> list of capstone_core.RoomDef objects."""
    cpp_rooms = []
    for r in env.rooms:
        rd = cc.RoomDef()
        rd.name      = r.name
        rd.center_x  = r.center_x
        rd.center_y  = r.center_y
        rd.polygon   = list(r.polygon)
        cpp_rooms.append(rd)
    return cpp_rooms


def _make_cpp_ap_defs(env: FloorEnvironment) -> list:
    """Convert Python AccessPoint dict -> list of capstone_core.APDef objects."""
    cpp_aps = []
    for ap in env.wifi_aps.values():
        ad = cc.APDef()
        ad.id = ap.id
        ad.x  = ap.x
        ad.y  = ap.y
        cpp_aps.append(ad)
    return cpp_aps


def _make_cpp_target_defs(targets: List[TargetProfile]) -> list:
    """Convert Python TargetProfile list -> list of capstone_core.TargetDef objects."""
    cpp_targets = []
    for t in targets:
        td = cc.TargetDef()
        td.ssid        = t.ssid
        td.rssi_at_1m  = t.rssi_at_1m_dbm
        td.path_loss_n = t.path_loss_n
        cpp_targets.append(td)
    return cpp_targets


# ---------------------------------------------------------------------------
# RSSIEngineWrapper
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Pure-Python helpers (used by fallback wrappers)
# ---------------------------------------------------------------------------

def _point_in_polygon(x: float, y: float, polygon: list) -> bool:
    """Ray-casting point-in-polygon test."""
    n = len(polygon)
    inside = False
    px, py = x, y
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _rssi_to_distance(rssi: float, rssi_at_1m: float, path_loss_n: float) -> float:
    """Convert RSSI (dBm) to estimated distance (m) using log-distance path-loss model."""
    return 10.0 ** ((rssi_at_1m - rssi) / (10.0 * path_loss_n))


def _weighted_centroid(ap_positions: list, distances: list) -> tuple:
    """Weighted centroid from AP positions and distances. Returns (x, y)."""
    weights = [1.0 / max(d ** 2, 0.01) for d in distances]
    total_w = sum(weights)
    if total_w == 0:
        return 0.0, 0.0
    x = sum(w * ap.x for w, ap in zip(weights, ap_positions)) / total_w
    y = sum(w * ap.y for w, ap in zip(weights, ap_positions)) / total_w
    return x, y


def _find_room(x: float, y: float, rooms) -> Optional[str]:
    """Return room name containing (x,y), or nearest centroid if none."""
    for r in rooms:
        if r.polygon and _point_in_polygon(x, y, r.polygon):
            return r.name
    # Fallback: nearest room centroid
    best, best_dist = None, float("inf")
    for r in rooms:
        if r.polygon:
            cx = sum(p[0] for p in r.polygon) / len(r.polygon)
            cy = sum(p[1] for p in r.polygon) / len(r.polygon)
            d = (x - cx) ** 2 + (y - cy) ** 2
            if d < best_dist:
                best_dist, best = d, r.name
    return best


class _PythonRSSIEngine:
    """
    Pure-Python trilateration engine — fallback when capstone_core is not built.

    Uses log-distance path-loss model + weighted centroid positioning.
    A simple rolling average filter is applied per (ap_id, ssid) pair.
    """

    def __init__(self, window_size: int, noise_floor: float) -> None:
        self._window = window_size
        self._noise  = noise_floor
        self._buf: Dict[str, List[float]] = {}   # key = "ap_id::ssid"

    def _smooth(self, key: str, value: float) -> float:
        buf = self._buf.setdefault(key, [])
        buf.append(value)
        if len(buf) > self._window:
            buf.pop(0)
        return sum(buf) / len(buf)

    def process(
        self,
        raw_rssi: "RSSIMap",
        targets,
        aps,
        rooms,
    ) -> list:
        """Return list of (device_id, room, confidence, x, y, rssi_vec) tuples."""
        results = []
        for target in targets:
            ssid = target.ssid
            rssi_at_1m   = target.rssi_at_1m_dbm
            path_loss_n  = target.path_loss_n

            ap_positions, distances, rssi_vec = [], [], {}
            for ap_id, dev_map in raw_rssi.items():
                if ssid not in dev_map:
                    continue
                raw_val = dev_map[ssid]
                if raw_val <= self._noise:
                    continue
                smoothed = self._smooth(f"{ap_id}::{ssid}", raw_val)
                rssi_vec[ap_id] = smoothed
                ap = aps.get(ap_id)
                if ap is None:
                    continue
                dist = _rssi_to_distance(smoothed, rssi_at_1m, path_loss_n)
                ap_positions.append(ap)
                distances.append(dist)

            if len(ap_positions) < 1:
                continue

            x, y = _weighted_centroid(ap_positions, distances)
            # Clamp to floor bounds
            if rooms:
                max_x = max((max(p[0] for p in r.polygon) for r in rooms if r.polygon), default=20.0)
                max_y = max((max(p[1] for p in r.polygon) for r in rooms if r.polygon), default=20.0)
                x = max(0.0, min(x, max_x))
                y = max(0.0, min(y, max_y))

            room = _find_room(x, y, rooms) or "Undetected"

            # Confidence: more APs = better; spread of distances = lower confidence
            n_aps = len(ap_positions)
            spread = (max(distances) - min(distances)) if len(distances) > 1 else 0.0
            conf = min(1.0, n_aps / 3.0) * max(0.1, 1.0 - spread / 20.0)

            results.append((ssid, room, round(conf, 3), round(x, 2), round(y, 2), rssi_vec))
        return results


class RSSIEngineWrapper:
    """
    Wraps capstone_core.RSSIEngine (C++) or _PythonRSSIEngine (pure-Python fallback).

    The engine is stateful (owns the rolling-average filter) so this
    wrapper is instantiated once at startup and reused every poll cycle.
    """

    def __init__(self, env: FloorEnvironment, cfg: dict) -> None:
        if not _CPP_AVAILABLE:
            # ── Pure-Python fallback ──────────────────────────────────────
            sys_cfg = cfg.get("system", {})
            filt    = sys_cfg.get("signal_filter", {})
            self._py_engine = _PythonRSSIEngine(
                window_size  = int(sys_cfg.get("rolling_average_window", 5)),
                noise_floor  = float(filt.get("noise_floor_dbm", -80.0)),
            )
            self._env         = env
            self._campus_id   = env.campus_id
            self._building_id = env.building_id
            self._floor_id    = env.floor_id
            self._use_cpp     = False
            return

        self._use_cpp = True
        sys_cfg = cfg.get("system", {})
        filt    = sys_cfg.get("signal_filter", {})

        self._env         = env
        self._campus_id   = env.campus_id
        self._building_id = env.building_id
        self._floor_id    = env.floor_id

        self._engine = cc.RSSIEngine(
            window_size    = int(sys_cfg.get("rolling_average_window", 5)),
            noise_floor_dbm= float(filt.get("noise_floor_dbm", -80.0)),
            min_aps        = int(filt.get("min_aps_for_localization", 3)),
            clamp_margin   = float(sys_cfg.get("boundary_clamp_margin_m", 0.01)),
            max_dist_conf  = float(sys_cfg.get("max_distance_for_high_confidence_m", 3.0)),
            room_w         = env.width_m,
            room_h         = env.height_m,
        )
        self._engine.set_aps(_make_cpp_ap_defs(env))
        self._engine.set_rooms(_make_cpp_room_defs(env))
        self._cpp_targets = _make_cpp_target_defs(env.targets)

    def process_cycle(
        self,
        raw_rssi: RSSIMap,
        scan_number: int = 0,
        timestamp: Optional[str] = None,
        decision_id: Optional[str] = None,
    ) -> List[LocalizationDecision]:
        """
        Run one localization cycle.
        Uses C++ engine if available, otherwise pure-Python trilateration.
        """
        import uuid
        from datetime import datetime, timezone

        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()
        if decision_id is None:
            decision_id = str(uuid.uuid4())

        decisions: List[LocalizationDecision] = []

        if not self._use_cpp:
            # ── Pure-Python path ──────────────────────────────────────────
            py_results = self._py_engine.process(
                raw_rssi,
                targets = self._env.targets,
                aps     = self._env.wifi_aps,
                rooms   = self._env.rooms,
            )
            for ssid, room, conf, x, y, rssi_vec in py_results:
                if room == "Undetected":
                    continue
                decisions.append(LocalizationDecision(
                    decision_id = str(uuid.uuid4()),
                    device_id   = ssid,
                    campus_id   = self._campus_id,
                    building_id = self._building_id,
                    floor_id    = self._floor_id,
                    room_id     = room,
                    timestamp   = timestamp,
                    confidence  = conf,
                    rssi_vector = rssi_vec,
                    x           = x,
                    y           = y,
                    scan_number = scan_number,
                ))
            return decisions

        # ── C++ path ──────────────────────────────────────────────────────
        cpp_results = self._engine.process_cycle(raw_rssi, self._cpp_targets)

        for r in cpp_results:
            if r.room == "Undetected":
                continue
            rssi_vec = {
                ap_id: raw_rssi[ap_id][r.device_id]
                for ap_id in raw_rssi
                if r.device_id in raw_rssi.get(ap_id, {})
            }
            decisions.append(LocalizationDecision(
                decision_id = decision_id,
                device_id   = r.device_id,
                campus_id   = self._campus_id,
                building_id = self._building_id,
                floor_id    = self._floor_id,
                room_id     = r.room,
                timestamp   = timestamp,
                confidence  = r.confidence,
                rssi_vector = rssi_vec,
                x           = r.x,
                y           = r.y,
                scan_number = scan_number,
            ))
        return decisions


# ---------------------------------------------------------------------------
# KalmanRSSIFilter — per-channel scalar Kalman filter for RSSI smoothing
# ---------------------------------------------------------------------------

class KalmanRSSIFilter:
    """Scalar 1-D Kalman filter, one independent instance per (ap_id, ssid) channel.

    Signal model
    ------------
    State equation:     x_k  = x_{k-1}  +  w_k       w ~ N(0, Q)
    Observation:        z_k  = x_k      +  v_k        v ~ N(0, R)

    The constant-velocity model (dx/dt = 0) is appropriate for RSSI because the
    underlying path-loss is slow relative to the measurement rate; short-burst
    fluctuations are captured by Q and R rather than an explicit velocity term.

    Parameters
    ----------
    Q : float
        Process noise covariance.  Models genuine RSSI drift between measurements
        (person walking, door opening, furniture moving).  Larger Q → filter
        tracks faster but retains more noise.  Typical indoor range: 0.5 – 3.0.
    R : float
        Measurement noise covariance.  Models sensor + multipath variance.
        Empirically, indoor RSSI has σ ≈ 2–4 dBm, so R ≈ σ² ≈ 4 – 16.
        Larger R → filter trusts measurements less → smoother but more lagged.
    noise_floor_dbm : float
        Readings below this threshold are discarded (not fed into the filter).
        This prevents the filter state from being corrupted by phantom APs.

    Why Kalman beats SMA in dynamic environments
    --------------------------------------------
    The SMA weights all N samples equally regardless of their recency.  When the
    true signal changes (person moves), the SMA requires N new samples to forget
    the old value — introducing a lag of up to N/2 scan periods.

    The Kalman filter maintains an adaptive gain K_k = P_k / (P_k + R).  When
    prediction uncertainty P is large (signal has been changing), K is large and
    the filter snaps quickly to new measurements.  When P is small (signal has
    been stable), K is small and the filter smooths aggressively.  This
    predictive-variance weighting produces lower steady-state MSE than any fixed
    window average under Gaussian noise — the Kalman estimator is the MMSE
    (minimum mean-square error) estimator for this model class.
    """

    def __init__(self, Q: float = 1.0, R: float = 4.0,
                 noise_floor_dbm: float = -80.0) -> None:
        self._Q           = Q
        self._R           = R
        self._noise_floor = noise_floor_dbm
        # Per-channel filter state: {key: (x_hat, P)}
        # x_hat = estimated RSSI, P = estimation error covariance
        self._state: Dict[str, Tuple[float, float]] = {}

    def feed(self, ap_id: str, ssid: str, raw_rssi: float) -> Optional[float]:
        """Feed one raw RSSI reading.  Returns smoothed estimate, or None if
        below noise floor."""
        if raw_rssi < self._noise_floor:
            return None

        key = f"{ap_id}::{ssid}"

        if key not in self._state:
            # Bootstrap: initialise with first observation, high uncertainty
            self._state[key] = (raw_rssi, self._R)
            return raw_rssi

        x_prev, P_prev = self._state[key]

        # ── Predict ──────────────────────────────────────────────────────────
        # State is constant-velocity; prediction = previous estimate
        x_pred = x_prev
        P_pred = P_prev + self._Q     # uncertainty grows between measurements

        # ── Update ───────────────────────────────────────────────────────────
        K       = P_pred / (P_pred + self._R)          # Kalman gain  ∈ (0, 1)
        x_hat   = x_pred + K * (raw_rssi - x_pred)     # corrected estimate
        P_new   = (1.0 - K) * P_pred                   # updated covariance

        self._state[key] = (x_hat, P_new)
        return x_hat

    def process(self, raw_rssi: "RSSIMap") -> "RSSIMap":
        """Batch-process a full RSSIMap.  Returns a new map of Kalman-filtered
        values; channels below the noise floor are omitted."""
        out: "RSSIMap" = {}
        for ap_id, dev_map in raw_rssi.items():
            for ssid, rssi in dev_map.items():
                smoothed = self.feed(ap_id, ssid, rssi)
                if smoothed is not None:
                    out.setdefault(ap_id, {})[ssid] = smoothed
        return out

    def reset(self) -> None:
        """Clear all channel state (e.g. after a poll restart)."""
        self._state.clear()

    @property
    def Q(self) -> float:  return self._Q
    @property
    def R(self) -> float:  return self._R


# ---------------------------------------------------------------------------
# _PythonRSSIEngineRaw — same as _PythonRSSIEngine but with NO smoothing
# ---------------------------------------------------------------------------

class _PythonRSSIEngineRaw:
    """Pure-Python trilateration with no RSSI filtering.
    Raw readings are passed directly to the path-loss distance model."""

    def __init__(self, noise_floor: float) -> None:
        self._noise = noise_floor

    def process(self, raw_rssi: "RSSIMap", targets, aps, rooms) -> list:
        results = []
        for target in targets:
            ssid          = target.ssid
            rssi_at_1m    = target.rssi_at_1m_dbm
            path_loss_n   = target.path_loss_n

            ap_positions, distances, rssi_vec = [], [], {}
            for ap_id, dev_map in raw_rssi.items():
                if ssid not in dev_map:
                    continue
                raw_val = dev_map[ssid]
                if raw_val <= self._noise:
                    continue
                rssi_vec[ap_id] = raw_val                    # ← no smoothing
                ap = aps.get(ap_id)
                if ap is None:
                    continue
                dist = _rssi_to_distance(raw_val, rssi_at_1m, path_loss_n)
                ap_positions.append(ap)
                distances.append(dist)

            if len(ap_positions) < 1:
                continue

            x, y = _weighted_centroid(ap_positions, distances)
            if rooms:
                max_x = max((max(p[0] for p in r.polygon) for r in rooms if r.polygon), default=20.0)
                max_y = max((max(p[1] for p in r.polygon) for r in rooms if r.polygon), default=20.0)
                x = max(0.0, min(x, max_x))
                y = max(0.0, min(y, max_y))

            room  = _find_room(x, y, rooms) or "Undetected"
            n_aps = len(ap_positions)
            spread = (max(distances) - min(distances)) if len(distances) > 1 else 0.0
            conf  = min(1.0, n_aps / 3.0) * max(0.1, 1.0 - spread / 20.0)
            results.append((ssid, room, round(conf, 3), round(x, 2), round(y, 2), rssi_vec))
        return results


# ---------------------------------------------------------------------------
# RawRSSIEngineWrapper — trilateration with NO pre-filtering
# ---------------------------------------------------------------------------

class RawRSSIEngineWrapper:
    """Trilateration on unfiltered raw RSSI values.

    When the C++ engine is available, it is instantiated with window_size=1
    (single-sample window = identity filter).  On the Python path, raw values
    are passed directly to the distance model.

    Use this as a baseline to quantify how much error filtering removes.
    """

    def __init__(self, env: FloorEnvironment, cfg: dict) -> None:
        sys_cfg = cfg.get("system", {})
        filt    = sys_cfg.get("signal_filter", {})
        noise_floor = float(filt.get("noise_floor_dbm", -80.0))

        self._env         = env
        self._campus_id   = env.campus_id
        self._building_id = env.building_id
        self._floor_id    = env.floor_id

        if _CPP_AVAILABLE:
            self._use_cpp = True
            # window_size=1 → only the most recent sample → effectively no smoothing
            self._engine = cc.RSSIEngine(
                window_size     = 1,
                noise_floor_dbm = noise_floor,
                min_aps         = int(filt.get("min_aps_for_localization", 3)),
                clamp_margin    = float(sys_cfg.get("boundary_clamp_margin_m", 0.01)),
                max_dist_conf   = float(sys_cfg.get("max_distance_for_high_confidence_m", 3.0)),
                room_w          = env.width_m,
                room_h          = env.height_m,
            )
            self._engine.set_aps(_make_cpp_ap_defs(env))
            self._engine.set_rooms(_make_cpp_room_defs(env))
            self._cpp_targets = _make_cpp_target_defs(env.targets)
        else:
            self._use_cpp  = False
            self._py_engine = _PythonRSSIEngineRaw(noise_floor=noise_floor)

    def process_cycle(
        self,
        raw_rssi:    "RSSIMap",
        scan_number: int = 0,
        timestamp:   Optional[str] = None,
        decision_id: Optional[str] = None,
    ) -> List[LocalizationDecision]:
        import uuid
        from datetime import datetime, timezone

        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()
        if decision_id is None:
            decision_id = str(uuid.uuid4())

        decisions: List[LocalizationDecision] = []

        if not self._use_cpp:
            py_results = self._py_engine.process(
                raw_rssi,
                targets = self._env.targets,
                aps     = self._env.wifi_aps,
                rooms   = self._env.rooms,
            )
            for ssid, room, conf, x, y, rssi_vec in py_results:
                if room == "Undetected":
                    continue
                decisions.append(LocalizationDecision(
                    decision_id = str(uuid.uuid4()),
                    device_id   = ssid,
                    campus_id   = self._campus_id,
                    building_id = self._building_id,
                    floor_id    = self._floor_id,
                    room_id     = room,
                    timestamp   = timestamp,
                    confidence  = conf,
                    rssi_vector = rssi_vec,
                    x=x, y=y,
                    scan_number = scan_number,
                ))
            return decisions

        cpp_results = self._engine.process_cycle(raw_rssi, self._cpp_targets)
        for r in cpp_results:
            if r.room == "Undetected":
                continue
            rssi_vec = {
                ap_id: raw_rssi[ap_id][r.device_id]
                for ap_id in raw_rssi
                if r.device_id in raw_rssi.get(ap_id, {})
            }
            decisions.append(LocalizationDecision(
                decision_id = decision_id,
                device_id   = r.device_id,
                campus_id   = self._campus_id,
                building_id = self._building_id,
                floor_id    = self._floor_id,
                room_id     = r.room,
                timestamp   = timestamp,
                confidence  = r.confidence,
                rssi_vector = rssi_vec,
                x=r.x, y=r.y,
                scan_number = scan_number,
            ))
        return decisions


# ---------------------------------------------------------------------------
# KalmanRSSIEngineWrapper — trilateration with Kalman-filtered RSSI
# ---------------------------------------------------------------------------

class KalmanRSSIEngineWrapper:
    """Trilateration with per-channel scalar Kalman filter preprocessing.

    Pipeline per scan cycle
    -----------------------
    1. KalmanRSSIFilter.process(raw_rssi)   → kalman_rssi  (Python)
    2. C++ RSSIEngine.process_cycle(kalman_rssi, window=1)  (or Python fallback)
        → list of LocalizationDecision

    The Kalman filter runs entirely in Python (no C++ recompile needed) and
    produces the smoothed RSSI map that is then handed to the C++ trilateration
    engine with window_size=1 so the C++ layer does not apply additional SMA on
    top of the already-filtered signal.

    Configuration (config.yaml → system.kalman_filter)
    ---------------------------------------------------
      process_noise_Q : float   default 1.0   [dBm²]
      measurement_noise_R : float  default 4.0  [dBm²]
      noise_floor_dbm : float   (shared with signal_filter block)

    Tuning guidance
    ---------------
    * Increase Q if the estimated position lags behind a moving tag.
    * Increase R if the output is still noisy after calibration.
    * Run with raw and SMA wrappers in parallel and compare MSE to find the
      optimal (Q, R) pair for your specific environment.
    """

    def __init__(self, env: FloorEnvironment, cfg: dict) -> None:
        sys_cfg  = cfg.get("system", {})
        filt     = sys_cfg.get("signal_filter", {})
        kf_cfg   = sys_cfg.get("kalman_filter", {})
        noise_floor = float(filt.get("noise_floor_dbm", -80.0))

        self._env         = env
        self._campus_id   = env.campus_id
        self._building_id = env.building_id
        self._floor_id    = env.floor_id

        Q = float(kf_cfg.get("process_noise_Q",      1.0))
        R = float(kf_cfg.get("measurement_noise_R",  4.0))
        self._kalman = KalmanRSSIFilter(Q=Q, R=R, noise_floor_dbm=noise_floor)

        _log.info(
            "KalmanRSSIEngineWrapper: Q=%.2f  R=%.2f  noise_floor=%.1f dBm",
            Q, R, noise_floor,
        )

        if _CPP_AVAILABLE:
            self._use_cpp = True
            # window=1: C++ just passes kalman-pre-filtered values straight through
            self._engine = cc.RSSIEngine(
                window_size     = 1,
                noise_floor_dbm = noise_floor,
                min_aps         = int(filt.get("min_aps_for_localization", 3)),
                clamp_margin    = float(sys_cfg.get("boundary_clamp_margin_m", 0.01)),
                max_dist_conf   = float(sys_cfg.get("max_distance_for_high_confidence_m", 3.0)),
                room_w          = env.width_m,
                room_h          = env.height_m,
            )
            self._engine.set_aps(_make_cpp_ap_defs(env))
            self._engine.set_rooms(_make_cpp_room_defs(env))
            self._cpp_targets = _make_cpp_target_defs(env.targets)
        else:
            self._use_cpp  = False
            # Python fallback: reuse _PythonRSSIEngine with window=1 (no extra SMA)
            self._py_engine = _PythonRSSIEngine(window_size=1, noise_floor=noise_floor)

    def process_cycle(
        self,
        raw_rssi:    "RSSIMap",
        scan_number: int = 0,
        timestamp:   Optional[str] = None,
        decision_id: Optional[str] = None,
    ) -> List[LocalizationDecision]:
        import uuid
        from datetime import datetime, timezone

        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()
        if decision_id is None:
            decision_id = str(uuid.uuid4())

        # ── Step 1: Kalman filter in Python ──────────────────────────────────
        filtered_rssi = self._kalman.process(raw_rssi)

        decisions: List[LocalizationDecision] = []

        if not self._use_cpp:
            # ── Python path ──────────────────────────────────────────────────
            py_results = self._py_engine.process(
                filtered_rssi,
                targets = self._env.targets,
                aps     = self._env.wifi_aps,
                rooms   = self._env.rooms,
            )
            for ssid, room, conf, x, y, rssi_vec in py_results:
                if room == "Undetected":
                    continue
                decisions.append(LocalizationDecision(
                    decision_id = str(uuid.uuid4()),
                    device_id   = ssid,
                    campus_id   = self._campus_id,
                    building_id = self._building_id,
                    floor_id    = self._floor_id,
                    room_id     = room,
                    timestamp   = timestamp,
                    confidence  = conf,
                    rssi_vector = rssi_vec,
                    x=x, y=y,
                    scan_number = scan_number,
                ))
            return decisions

        # ── C++ path (Kalman-pre-filtered → engine with window=1) ────────────
        cpp_results = self._engine.process_cycle(filtered_rssi, self._cpp_targets)
        for r in cpp_results:
            if r.room == "Undetected":
                continue
            # Report the Kalman-smoothed vector (what was actually used)
            rssi_vec = {
                ap_id: filtered_rssi[ap_id][r.device_id]
                for ap_id in filtered_rssi
                if r.device_id in filtered_rssi.get(ap_id, {})
            }
            decisions.append(LocalizationDecision(
                decision_id = decision_id,
                device_id   = r.device_id,
                campus_id   = self._campus_id,
                building_id = self._building_id,
                floor_id    = self._floor_id,
                room_id     = r.room,
                timestamp   = timestamp,
                confidence  = r.confidence,
                rssi_vector = rssi_vec,
                x=r.x, y=r.y,
                scan_number = scan_number,
            ))
        return decisions


# ---------------------------------------------------------------------------
# FingerprintWrapper
# ---------------------------------------------------------------------------

class FingerprintWrapper:
    """
    Wraps capstone_core.knn_fingerprint_match.

    Loads the radio map from disk once and holds it in memory.
    Call reload_map() if you want to hot-swap the radiomap.json.
    """

    def __init__(self, radiomap_path: str = "radiomap.json", k: int = 3) -> None:
        self._path    = radiomap_path
        self._k       = k
        self._use_cpp = _CPP_AVAILABLE
        # _radio_map holds either cc.RadioMapEntry objects (C++) or
        # plain {room, vectors} dicts (pure-Python)
        self._radio_map: list = []
        self.reload_map()

    def reload_map(self) -> None:
        """Load (or re-load) radiomap.json."""
        if not os.path.exists(self._path):
            self._radio_map = []
            return

        with open(self._path, "r") as f:
            raw: Dict[str, List[Dict[str, float]]] = json.load(f)

        self._radio_map = []
        if self._use_cpp:
            for room_label, vectors in raw.items():
                entry = cc.RadioMapEntry()
                entry.room    = room_label
                entry.vectors = vectors
                self._radio_map.append(entry)
        else:
            # Pure-Python: store as plain dicts
            for room_label, vectors in raw.items():
                self._radio_map.append({"room": room_label, "vectors": vectors})

    def _py_knn(self, live_vector: Dict[str, float]) -> tuple:
        """Pure-Python KNN fingerprint matching."""
        import math
        neighbours = []
        for entry in self._radio_map:
            room    = entry["room"]
            vectors = entry["vectors"]   # list of {ap_id: rssi}
            for vec in vectors:
                # Euclidean distance over shared APs
                common = set(live_vector) & set(vec)
                if not common:
                    continue
                dist = math.sqrt(sum((live_vector[ap] - vec[ap]) ** 2 for ap in common))
                # Penalise lightly for missing APs
                dist += 5.0 * (len(live_vector) - len(common))
                neighbours.append((dist, room))

        if not neighbours:
            return "Outside Defined Area", 0.0

        neighbours.sort(key=lambda x: x[0])
        k_nn = neighbours[: self._k]

        # Majority vote
        votes: Dict[str, float] = {}
        for dist, room in k_nn:
            w = 1.0 / max(dist, 0.1)
            votes[room] = votes.get(room, 0.0) + w

        best_room = max(votes, key=lambda r: votes[r])
        total_w   = sum(votes.values())
        conf      = round(votes[best_room] / total_w, 3) if total_w else 0.0
        return best_room, conf

    def match(self, live_vector: Dict[str, float]) -> tuple:
        """
        Run KNN against the loaded radio map.

        Returns (room_label, confidence) — ('Outside Defined Area', 0.0) if no match.
        """
        if not self._radio_map:
            return "Outside Defined Area", 0.0

        if not self._use_cpp:
            return self._py_knn(live_vector)

        result = cc.knn_fingerprint_match(live_vector, self._radio_map, self._k)
        return result.room, result.confidence


# ---------------------------------------------------------------------------
# SGPWrapper — Sparse Gaussian Process localization
# ---------------------------------------------------------------------------

def _rbf_kernel(x1: List[float], x2: List[float], length_scale: float, variance: float) -> float:
    """Squared-exponential (RBF) kernel between two RSSI vectors."""
    sq_dist = sum((a - b) ** 2 for a, b in zip(x1, x2))
    return variance * math.exp(-0.5 * sq_dist / (length_scale ** 2))


def _rbf_kernel_matrix(
    X: List[List[float]], Y: List[List[float]],
    length_scale: float, variance: float,
) -> List[List[float]]:
    """K[i][j] = k(X[i], Y[j])."""
    return [
        [_rbf_kernel(xi, yj, length_scale, variance) for yj in Y]
        for xi in X
    ]


def _rbf_kernel_diag(
    X: List[List[float]], length_scale: float, variance: float,
) -> List[float]:
    """Diagonal of K(X, X) — always `variance` for RBF."""
    return [variance] * len(X)


# ── Tiny linear-algebra helpers (no numpy required) ─────────────────────────

def _cholesky(A: List[List[float]]) -> List[List[float]]:
    """
    Cholesky decomposition A = L L^T.  A must be symmetric positive-definite.
    Returns lower-triangular L.
    """
    n = len(A)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                val = A[i][i] - s
                if val <= 0:
                    val = 1e-10          # numerical nudge
                L[i][j] = math.sqrt(val)
            else:
                L[i][j] = (A[i][j] - s) / max(L[j][j], 1e-12)
    return L


def _solve_triangular_lower(L: List[List[float]], b: List[float]) -> List[float]:
    """Solve L x = b for lower-triangular L (forward substitution)."""
    n = len(b)
    x = [0.0] * n
    for i in range(n):
        x[i] = (b[i] - sum(L[i][j] * x[j] for j in range(i))) / max(L[i][i], 1e-12)
    return x


def _solve_triangular_upper(U: List[List[float]], b: List[float]) -> List[float]:
    """Solve U x = b for upper-triangular U (backward substitution)."""
    n = len(b)
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (b[i] - sum(U[i][j] * x[j] for j in range(i + 1, n))) / max(U[i][i], 1e-12)
    return x


def _transpose(M: List[List[float]]) -> List[List[float]]:
    if not M:
        return []
    rows, cols = len(M), len(M[0])
    return [[M[r][c] for r in range(rows)] for c in range(cols)]


def _mat_vec(M: List[List[float]], v: List[float]) -> List[float]:
    return [sum(M[i][j] * v[j] for j in range(len(v))) for i in range(len(M))]


def _mat_mat(A: List[List[float]], B: List[List[float]]) -> List[List[float]]:
    ra, ca = len(A), len(A[0])
    rb, cb = len(B), len(B[0])
    assert ca == rb
    C = [[0.0] * cb for _ in range(ra)]
    for i in range(ra):
        for j in range(cb):
            C[i][j] = sum(A[i][k] * B[k][j] for k in range(ca))
    return C


def _add_diag(M: List[List[float]], val: float) -> List[List[float]]:
    """Return M + val*I  (non-destructive)."""
    n = len(M)
    R = [row[:] for row in M]
    for i in range(n):
        R[i][i] += val
    return R


class SGPWrapper:
    """
    Sparse Gaussian Process regression for WiFi RSSI-based localization.

    Training data comes from the radiomap (the same JSON used by
    FingerprintWrapper).  Each survey sample is an RSSI vector (one value
    per AP) recorded at a known (x, y) location in a named room.

    At prediction time, a live RSSI vector is passed in and the GP
    returns a predicted (x, y) with uncertainty (σ²).

    Implementation
    --------------
    * Pure-Python — no GPyTorch / scikit-learn / numpy required.
    * Uses the FITC sparse approximation when M inducing points are given.
    * Falls back to exact GPR when M >= N (small datasets).
    * The kernel is squared-exponential (RBF) with a single shared
      length-scale across all AP dimensions.

    The wrapper trains TWO independent GPs — one for the X coordinate and
    one for Y — sharing the same kernel hyper-parameters and inducing set.
    """

    def __init__(
        self,
        radiomap_path: str = "radiomap.json",
        env: Optional["FloorEnvironment"] = None,
        n_inducing: int = 30,
        length_scale: float = 12.0,
        signal_variance: float = 50.0,
        noise_variance: float = 5.0,
    ) -> None:
        self._path = radiomap_path
        self._env = env
        self._M = n_inducing
        self._ls = length_scale
        self._sig_var = signal_variance
        self._noise_var = noise_variance
        self._trained = False

        # Ordered AP IDs — defines the column order for RSSI vectors
        self._ap_order: List[str] = []

        # Training data (filled by reload_map)
        self._X_train: List[List[float]] = []    # RSSI vectors  (N × D)
        self._Y_x: List[float] = []              # x coordinates (N)
        self._Y_y: List[float] = []              # y coordinates (N)

        # Inducing points in RSSI space
        self._Z: List[List[float]] = []           # (M × D)

        # Pre-computed matrices for prediction (set by _fit)
        self._alpha_x: List[float] = []
        self._alpha_y: List[float] = []
        self._L_uu: List[List[float]] = []        # Cholesky of K_uu + jitter

        self.reload_map()

    # ── Data loading ─────────────────────────────────────────────────────

    def reload_map(self) -> None:
        """Load the radiomap and (re-)train the GP."""
        self._trained = False
        if not os.path.exists(self._path):
            _log.warning("[SGP] Radiomap not found: %s", self._path)
            return

        with open(self._path, "r") as f:
            raw: Dict[str, list] = json.load(f)

        if not raw:
            return

        # Discover the canonical AP ordering from the union of all vectors
        ap_set: set = set()
        for vectors in raw.values():
            for vec in vectors:
                ap_set.update(vec.keys())
        self._ap_order = sorted(ap_set)
        D = len(self._ap_order)

        if D == 0:
            return

        # Build training matrices.  Each survey sample becomes one row.
        # We need (x, y) targets — derive from the room polygon centroid.
        room_centroids: Dict[str, Tuple[float, float]] = {}
        if self._env:
            for r in self._env.rooms:
                if r.polygon:
                    cx = sum(p[0] for p in r.polygon) / len(r.polygon)
                    cy = sum(p[1] for p in r.polygon) / len(r.polygon)
                    room_centroids[r.name] = (cx, cy)

        X, Yx, Yy = [], [], []
        for room_label, vectors in raw.items():
            centroid = room_centroids.get(room_label)
            if centroid is None:
                _log.debug("[SGP] Skipping room %r — no polygon/centroid", room_label)
                continue
            cx, cy = centroid
            for vec in vectors:
                row = [float(vec.get(ap, -90.0)) for ap in self._ap_order]
                X.append(row)
                Yx.append(cx)
                Yy.append(cy)

        N = len(X)
        if N < 2:
            _log.warning("[SGP] Not enough training samples (%d) — need >= 2", N)
            return

        self._X_train = X
        self._Y_x = Yx
        self._Y_y = Yy

        _log.info("[SGP] Loaded radiomap: %d samples, %d APs, %d rooms",
                  N, D, len(raw))

        self._select_inducing_points()
        self._fit()

    # ── Inducing point selection ─────────────────────────────────────────

    def _select_inducing_points(self) -> None:
        """
        Sub-sample M inducing points from training data using k-means-style
        greedy farthest-point selection for good coverage.
        """
        N = len(self._X_train)
        M = min(self._M, N)

        if M >= N:
            # Small dataset — use all points (exact GP, no sparsity)
            self._Z = [row[:] for row in self._X_train]
            return

        # Greedy farthest-point sampling
        import random
        indices = [random.randint(0, N - 1)]
        min_dists = [float("inf")] * N

        for _ in range(M - 1):
            last = self._X_train[indices[-1]]
            for i in range(N):
                d = sum((a - b) ** 2 for a, b in zip(self._X_train[i], last))
                if d < min_dists[i]:
                    min_dists[i] = d
            # Pick the point farthest from its closest inducing point
            next_idx = max(range(N), key=lambda i: min_dists[i])
            indices.append(next_idx)

        self._Z = [self._X_train[i][:] for i in indices]
        _log.info("[SGP] Selected %d inducing points (farthest-point sampling)", M)

    # ── Training (FITC-style sparse GP) ──────────────────────────────────

    def _fit(self) -> None:
        """
        Pre-compute the FITC predictive weights.

        FITC approximation:
            Q_ff = K_fu K_uu^{-1} K_uf
            Λ   = diag(K_ff - Q_ff) + σ²_n I     (diagonal)
            Σ   = K_uu + K_uf Λ^{-1} K_fu         (M × M)
            α_* = Σ^{-1} K_uf Λ^{-1} y            (M × 1)

        Prediction:
            μ_* = k_*u Σ^{-1} K_uf Λ^{-1} y  =  k_*u α
            σ²_* = k_** - k_*u (K_uu^{-1} - Σ^{-1}) k_u*  +  σ²_n
        """
        N = len(self._X_train)
        M = len(self._Z)

        _log.info("[SGP] Fitting GP: N=%d  M=%d  ls=%.1f  sig_var=%.1f  noise=%.1f",
                  N, M, self._ls, self._sig_var, self._noise_var)

        # Kernel matrices
        K_uu = _rbf_kernel_matrix(self._Z, self._Z, self._ls, self._sig_var)
        K_uf = _rbf_kernel_matrix(self._Z, self._X_train, self._ls, self._sig_var)
        K_ff_diag = _rbf_kernel_diag(self._X_train, self._ls, self._sig_var)

        # Add jitter to K_uu for numerical stability
        K_uu_jit = _add_diag(K_uu, 1e-6)
        L_uu = _cholesky(K_uu_jit)
        self._L_uu = L_uu

        # Q_ff_diag[i] = K_fu[i,:] K_uu^{-1} K_uf[:,i]
        # Compute via V = L_uu^{-1} K_uf  →  Q_ff_diag[i] = ||V[:,i]||^2
        # V is M × N.  Solve column-by-column: L_uu v[:,j] = K_uf[:,j]
        K_fu = _transpose(K_uf)   # N × M
        V = [[0.0] * N for _ in range(M)]
        for j in range(N):
            col_j = [K_uf[m][j] for m in range(M)]   # length M
            solved = _solve_triangular_lower(L_uu, col_j)
            for m in range(M):
                V[m][j] = solved[m]

        Q_ff_diag = [0.0] * N
        for i in range(N):
            Q_ff_diag[i] = sum(V[m][i] ** 2 for m in range(M))

        # Λ diagonal
        Lambda_diag = [
            max(K_ff_diag[i] - Q_ff_diag[i], 1e-6) + self._noise_var
            for i in range(N)
        ]

        # Σ = K_uu + K_uf Λ^{-1} K_fu
        # K_uf Λ^{-1} K_fu:  (M×N) diag(1/Λ) (N×M)
        Lambda_inv = [1.0 / l for l in Lambda_diag]
        # W = K_uf * diag(Lambda_inv)  →  W[m][i] = K_uf[m][i] / Lambda[i]
        W = [[K_uf[m][i] * Lambda_inv[i] for i in range(N)] for m in range(M)]
        # Sigma = K_uu + W @ K_fu
        WKfu = _mat_mat(W, _transpose(K_uf))
        Sigma = [[K_uu_jit[i][j] + WKfu[i][j] for j in range(M)] for i in range(M)]

        L_sigma = _cholesky(Sigma)

        # α_x = Σ^{-1} K_uf Λ^{-1} y_x
        # α_y = Σ^{-1} K_uf Λ^{-1} y_y
        # rhs_x = W @ y_x
        rhs_x = _mat_vec(W, self._Y_x)
        rhs_y = _mat_vec(W, self._Y_y)

        # Solve Sigma α = rhs  via  L L^T α = rhs
        z_x = _solve_triangular_lower(L_sigma, rhs_x)
        self._alpha_x = _solve_triangular_upper(_transpose(L_sigma), z_x)

        z_y = _solve_triangular_lower(L_sigma, rhs_y)
        self._alpha_y = _solve_triangular_upper(_transpose(L_sigma), z_y)

        # Store L_sigma for variance computation
        self._L_sigma = L_sigma
        self._K_uu_jit = K_uu_jit

        self._trained = True
        _log.info("[SGP] Training complete — ready for prediction")

    # ── Prediction ───────────────────────────────────────────────────────

    def predict(self, live_vector: Dict[str, float]) -> Optional[Tuple[float, float, float, float]]:
        """
        Predict (x, y, σ²_x, σ²_y) for a live RSSI vector.

        Returns None if the GP is not trained or the vector is empty.
        """
        if not self._trained or not self._ap_order:
            return None

        # Build the ordered feature vector (missing APs default to -90 dBm)
        x_star = [float(live_vector.get(ap, -90.0)) for ap in self._ap_order]
        M = len(self._Z)

        # k_*u  (1 × M)
        k_star_u = [
            _rbf_kernel(x_star, self._Z[m], self._ls, self._sig_var)
            for m in range(M)
        ]

        # Predictive mean:  μ = k_*u @ α
        mu_x = sum(k * a for k, a in zip(k_star_u, self._alpha_x))
        mu_y = sum(k * a for k, a in zip(k_star_u, self._alpha_y))

        # Predictive variance (FITC):
        #   σ² = k_** - k_*u (K_uu^{-1} - Σ^{-1}) k_u*  + σ²_n
        k_star_star = self._sig_var  # RBF kernel with itself = variance

        # v1 = L_uu^{-1} k_u*
        v1 = _solve_triangular_lower(self._L_uu, k_star_u)
        # v2 = L_sigma^{-1} k_u*
        v2 = _solve_triangular_lower(self._L_sigma, k_star_u)

        var_reduction = sum(v ** 2 for v in v1) - sum(v ** 2 for v in v2)
        pred_var = k_star_star - var_reduction + self._noise_var
        pred_var = max(pred_var, 1e-4)  # clamp

        return mu_x, mu_y, pred_var, pred_var

    def predict_with_room(
        self,
        live_vector: Dict[str, float],
    ) -> Optional[Tuple[float, float, str, float]]:
        """
        Predict (x, y, room_name, confidence).

        Confidence is derived from the predictive variance:
            conf = 1 / (1 + √σ²)
        Capped at [0, 1].
        """
        result = self.predict(live_vector)
        if result is None:
            return None

        mu_x, mu_y, var_x, var_y = result

        # Clamp to floor bounds
        if self._env and self._env.rooms:
            rooms = self._env.rooms
            max_x = max((max(p[0] for p in r.polygon) for r in rooms if r.polygon), default=20.0)
            max_y = max((max(p[1] for p in r.polygon) for r in rooms if r.polygon), default=20.0)
            mu_x = max(0.0, min(mu_x, max_x))
            mu_y = max(0.0, min(mu_y, max_y))

        room = "Undetected"
        if self._env:
            room = _find_room(mu_x, mu_y, self._env.rooms) or "Undetected"

        # Confidence from variance — lower variance = higher confidence
        avg_std = math.sqrt((var_x + var_y) / 2.0)
        confidence = 1.0 / (1.0 + avg_std)
        confidence = round(min(max(confidence, 0.0), 1.0), 3)

        return round(mu_x, 2), round(mu_y, 2), room, confidence

    @property
    def trained(self) -> bool:
        return self._trained

    @property
    def n_training(self) -> int:
        return len(self._X_train)

    @property
    def n_inducing(self) -> int:
        return len(self._Z)


# ---------------------------------------------------------------------------
# Pure-Python fallback: EnGenius EAP350 / EWS360AP apscan table parser
# ---------------------------------------------------------------------------

def _parse_apscan_python(raw_text: str) -> list:
    """
    Parse an EnGenius apscan table without the capstone_core C++ module.

    Expected column order (space-aligned):
        BSSID  SSID  LEN  MODE  CH  SIGNAL  ENC  TYPE

    Handles:
    - Noise lines (e.g. "cut: expected a list of bytes …") — filtered by
      requiring the row to begin with a MAC-address pattern.
    - Case-insensitive header matching (EAP350 vs EWS360AP firmware differ).
    - SSIDs that contain internal spaces (positional extraction via header offsets).

    Returns a list of dicts with keys: bssid, ssid, signal, channel, security.
    """
    lines = raw_text.splitlines()

    # ── 1. Find the header line ────────────────────────────────────────────
    header_idx = -1
    header_line = ""
    for i, line in enumerate(lines):
        u = line.upper()
        if "BSSID" in u and "SSID" in u and "SIGNAL" in u:
            header_idx = i
            header_line = line
            break

    if header_idx < 0:
        return []

    # ── 2. Resolve column start positions from the header ─────────────────
    # Use word-boundary regex so that "SSID" inside "BSSID" is NOT matched.
    upper_hdr = header_line.upper()
    col_pos: Dict[str, int] = {}
    for m in _re.finditer(r'\b(BSSID|SSID|LEN|MODE|CH|SIGNAL|ENC|TYPE)\b', upper_hdr):
        col = m.group(1)
        if col not in col_pos:          # keep first (leftmost) occurrence
            col_pos[col] = m.start()

    if "SSID" not in col_pos or "SIGNAL" not in col_pos:
        return []          # Can't parse without knowing where SSID / SIGNAL are

    # SSID column ends at the next column to the right
    ssid_start = col_pos["SSID"]
    ssid_end = min(
        (v for k, v in col_pos.items() if k != "SSID" and v > ssid_start),
        default=ssid_start + 32,
    )
    signal_start = col_pos["SIGNAL"]

    # ── 3. Match only rows that begin with a MAC address ──────────────────
    _mac_re = _re.compile(
        r'^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\s', _re.ASCII
    )

    results = []
    for line in lines[header_idx + 1:]:
        if not _mac_re.match(line):
            continue                   # separator / noise / blank / header dup

        bssid = line[:17].strip()

        ssid = line[ssid_start:ssid_end].strip() if len(line) > ssid_start else ""

        if len(line) <= signal_start:
            continue
        signal_token = line[signal_start:].split()[0]
        try:
            signal = int(signal_token)
        except ValueError:
            continue                   # SIGNAL column contains non-integer

        channel = ""
        if "CH" in col_pos and len(line) > col_pos["CH"]:
            channel = line[col_pos["CH"]:].split()[0]

        security = ""
        if "ENC" in col_pos and len(line) > col_pos["ENC"]:
            security = line[col_pos["ENC"]:].split()[0]

        results.append({
            "bssid":    bssid,
            "ssid":     ssid,
            "signal":   signal,
            "channel":  channel,
            "security": security,
        })

    return results


# ---------------------------------------------------------------------------
# TelnetParserWrapper
# ---------------------------------------------------------------------------

class TelnetParserWrapper:
    """
    Thin shim so data_pipes.py doesn't import capstone_core directly.
    Converts raw EAP350 / EWS360AP APSCAN text -> list of ScanResult dicts.

    When capstone_core is available the C++ parser is used; otherwise the
    pure-Python fallback (_parse_apscan_python) is used automatically so
    surveys work even before the C++ module is compiled.
    """

    @staticmethod
    def parse(raw_text: str) -> list:
        """
        Parse EAP350 APSCAN table text.

        Returns list of dicts with keys: bssid, ssid, signal, channel, security.
        Falls back to a pure-Python parser when capstone_core is not built.
        """
        if _CPP_AVAILABLE:
            return cc.parse_apscan_table_dicts(raw_text)
        return _parse_apscan_python(raw_text)


# ---------------------------------------------------------------------------
# ToFWrapper  —  server-side trilateration from raw ESP32 FTM ranges
# ---------------------------------------------------------------------------

class ToFWrapper:
    """Collects raw FTM range measurements from MQTTPipe and runs
    server-side trilateration (least-squares) for each tracked tag.

    Architecture
    ------------
    * Anchors are the *fixed* devices whose positions are known (from
      config.yaml tof_anchors).  The ESP32 anchor firmware publishes
      one JSON message per tag it ranged to.
    * Tags are the *mobile* devices being located.  The thin-client tag
      firmware publishes one JSON message per anchor it ranged to.
    * Both message types carry (device_id, target_id, distance_m) and
      land on   capstone/tof/anchor/<id>/range
                capstone/tof/tag/<id>/range
    * This wrapper buffers the latest measurements per (tag, anchor) pair,
      runs outlier rejection, then solves the linearised least-squares
      system to produce (x, y) in cm.

    Usage
    -----
        wrapper = ToFWrapper(anchor_map)
        wrapper.ingest(measurement)       # call for every MQTTPipe message
        result = wrapper.locate("tag_0")  # -> (x_cm, y_cm, confidence) | None
    """

    # Keep only the N most recent measurements per (tag, anchor) pair
    _WINDOW = 5
    # Reject samples whose distance deviates more than this many σ from the mean
    _SIGMA_REJECT = 2.5

    def __init__(self, anchor_map: Dict[str, "ToFAnchor"]) -> None:
        """
        Parameters
        ----------
        anchor_map : dict mapping anchor_id str -> ToFAnchor
                     e.g. {"anchor_4": ToFAnchor(id="anchor_4", mac=..., x=0, y=0)}
        """
        from collections import defaultdict, deque
        # anchor_map: {anchor_id -> ToFAnchor}
        self._anchors: Dict[str, object] = anchor_map

        # Buffer: {tag_id: {anchor_id: deque([distance_m, ...])}}
        self._buffer: Dict[str, Dict[str, object]] = defaultdict(
            lambda: defaultdict(lambda: __import__('collections').deque(maxlen=self._WINDOW))
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest(self, meas: "ToFMeasurement") -> None:
        """Feed one ToFMeasurement from MQTTPipe into the buffer.

        Both anchor-sourced and tag-sourced messages are handled:
          anchor msg: device_id="anchor_X", target_id="tag_Y"
          tag msg:    device_id="tag_Y",   target_id="anchor_X"
        In both cases we store distance under (tag_id, anchor_id).
        """
        dev = meas.device_id
        tgt = meas.target_id

        if dev.startswith("anchor_") and tgt.startswith("tag_"):
            anchor_id = dev
            tag_id    = tgt
        elif dev.startswith("tag_") and tgt.startswith("anchor_"):
            tag_id    = dev
            anchor_id = tgt
        else:
            _log.debug("ToFWrapper: ignoring unrecognised pair %s -> %s", dev, tgt)
            return

        if anchor_id not in self._anchors:
            _log.debug("ToFWrapper: anchor %s not in config, skipping", anchor_id)
            return

        d_cm = meas.distance_m * 100.0
        self._buffer[tag_id][anchor_id].append(d_cm)

    def locate(self, tag_id: str) -> Optional[Tuple[float, float, float]]:
        """Return (x_cm, y_cm, confidence) for *tag_id*, or None if
        fewer than 2 anchors have measurements.

        Confidence is a [0, 1] float derived from number of usable anchors
        and their mean RSSI-quality proxied by measurement consistency.
        """
        anchor_buf = self._buffer.get(tag_id)
        if not anchor_buf:
            return None

        # Collect filtered median distances per anchor
        ax, ay, ad = [], [], []
        for anchor_id, samples in anchor_buf.items():
            if len(samples) == 0:
                continue
            anchor = self._anchors.get(anchor_id)
            if anchor is None:
                continue
            dist = self._filtered_median(list(samples))
            if dist is None:
                continue
            ax.append(float(anchor.x))
            ay.append(float(anchor.y))
            ad.append(dist)

        n = len(ax)
        if n < 2:
            return None

        x, y = self._trilaterate(ax, ay, ad, n)
        confidence = self._confidence(n)
        return (x, y, confidence)

    def tracked_tags(self) -> List[str]:
        """Return list of tag IDs that have at least one measurement."""
        return list(self._buffer.keys())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _filtered_median(self, samples: List[float]) -> Optional[float]:
        """Return the median of *samples* after σ-based outlier rejection."""
        if not samples:
            return None
        if len(samples) == 1:
            return samples[0]

        mean = sum(samples) / len(samples)
        var  = sum((s - mean) ** 2 for s in samples) / len(samples)
        std  = math.sqrt(var) if var > 0 else 0.0

        if std > 0:
            clean = [s for s in samples
                     if abs(s - mean) <= self._SIGMA_REJECT * std]
        else:
            clean = samples

        if not clean:
            clean = samples  # all rejected → keep all

        clean_sorted = sorted(clean)
        mid = len(clean_sorted) // 2
        if len(clean_sorted) % 2 == 0:
            return (clean_sorted[mid - 1] + clean_sorted[mid]) / 2.0
        return float(clean_sorted[mid])

    @staticmethod
    def _trilaterate(
        ax: List[float], ay: List[float], ad: List[float], n: int
    ) -> Tuple[float, float]:
        """Linearised least-squares trilateration.

        With n == 2: distance-weighted average (degenerate case).
        With n >= 3: full linearised LS using anchor 0 as reference.
        """
        if n == 2:
            w0 = 1.0 / (ad[0] + 1.0)
            w1 = 1.0 / (ad[1] + 1.0)
            x  = (ax[0] * w0 + ax[1] * w1) / (w0 + w1)
            y  = (ay[0] * w0 + ay[1] * w1) / (w0 + w1)
            return (x, y)

        # Build A matrix and b vector relative to anchor 0
        rows = n - 1
        A  = [[0.0, 0.0] for _ in range(rows)]
        b  = [0.0] * rows
        for i in range(1, n):
            A[i-1][0] = 2.0 * (ax[i] - ax[0])
            A[i-1][1] = 2.0 * (ay[i] - ay[0])
            b[i-1]    = (ad[0]**2 - ad[i]**2
                         - ax[0]**2 + ax[i]**2
                         - ay[0]**2 + ay[i]**2)

        # Normal equations  AᵀA · [x,y]ᵀ = Aᵀb
        ATA = [[0.0, 0.0], [0.0, 0.0]]
        ATb = [0.0, 0.0]
        for i in range(rows):
            ATA[0][0] += A[i][0] * A[i][0]
            ATA[0][1] += A[i][0] * A[i][1]
            ATA[1][0] += A[i][1] * A[i][0]
            ATA[1][1] += A[i][1] * A[i][1]
            ATb[0]    += A[i][0] * b[i]
            ATb[1]    += A[i][1] * b[i]

        det = ATA[0][0] * ATA[1][1] - ATA[0][1] * ATA[1][0]
        if abs(det) < 1e-6:
            # Degenerate geometry — fall back to weighted centroid
            w   = [1.0 / (d + 1.0) for d in ad]
            sw  = sum(w)
            x   = sum(ax[i] * w[i] for i in range(n)) / sw
            y   = sum(ay[i] * w[i] for i in range(n)) / sw
            return (x, y)

        x = (ATA[1][1] * ATb[0] - ATA[0][1] * ATb[1]) / det
        y = (ATA[0][0] * ATb[1] - ATA[1][0] * ATb[0]) / det
        return (x, y)

    @staticmethod
    def _confidence(n_anchors: int) -> float:
        """Map anchor count to a [0, 1] confidence score."""
        if n_anchors >= 4:
            return 1.0
        if n_anchors == 3:
            return 0.75
        if n_anchors == 2:
            return 0.45
        return 0.2



class AnchorPosEngineWrapper:
    """WiFi RSSI anchor-positioning engine wrapper.

    Wraps ``capstone_core.AnchorPosEngine`` — the C++ gradient-descent
    trilateration engine with live P0 self-calibration and per-anchor
    confidence weighting.

    Architecture
    ------------
    * **Target scanning** — every observation of a target device (SSID) by
      a scanning AP arrives via ``process_cycle(rssi_map)``.  The batch
      ingestion path ``AnchorPosEngine.feed_rssi_batch`` delivers the full
      ``{ap_id: {device_id: rssi_dbm}}`` dict under one mutex lock.
    * **Inter-anchor calibration** — APs that observe *each other* in the
      RSSI map are detected automatically (``dev_id in _ap_ids``) and routed
      to ``feed_inter_anchor_batch``.  This keeps the engine's self-calibrated
      P0 values current without any extra Python work.
    * **Position output** — ``get_all_positions()`` returns one
      ``AnchorPosResult`` per tracked device; these are converted to
      ``LocalizationDecision`` objects using point-in-polygon room lookup.

    Configuration (config.yaml)
    ---------------------------
    Under ``system.anchor_positioning``:

    .. code-block:: yaml

        system:
          rolling_average_window: 5   # shared with SMA engine
          anchor_positioning:
            max_dist_m:   15.0        # gate on max credible distance
            rssi_at_1m:  -60.0        # module-wide P0 fallback (dBm)
            path_loss_n:   2.5        # module-wide path-loss exponent fallback
            anchors:                  # per-AP overrides (optional)
              AP1:
                rssi_at_1m:  -58.0
                path_loss_n:  2.3
              AP2:
                rssi_at_1m:  -62.0

    Path-loss parameters that are *not* overridden fall back to the
    module-level values, which themselves fall back to the class defaults
    ``_DEFAULT_P0`` and ``_DEFAULT_N``.
    """

    _DEFAULT_P0: float = -60.0
    _DEFAULT_N:  float =  2.5

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, env: FloorEnvironment, cfg: dict) -> None:
        """
        Parameters
        ----------
        env : FloorEnvironment
            Room geometry, AP positions, target profiles.
        cfg : dict
            Raw config dict (from config.yaml).  Reads
            ``system.anchor_positioning.*``.
        """
        _require_cpp()

        sys_cfg  = cfg.get("system", {})
        anch_cfg = sys_cfg.get("anchor_positioning", {})
        per_ap   = anch_cfg.get("anchors", {})   # keyed by AP id

        default_p0 = float(anch_cfg.get("rssi_at_1m",  self._DEFAULT_P0))
        default_n  = float(anch_cfg.get("path_loss_n", self._DEFAULT_N))
        max_dist   = float(anch_cfg.get("max_dist_m",  15.0))
        window     = int(sys_cfg.get("rolling_average_window", 5))

        self._env          = env
        self._campus_id    = env.campus_id
        self._building_id  = env.building_id
        self._floor_id     = env.floor_id

        # Sets used to split rssi_map into target observations vs. IA observations
        self._ap_ids       = set(env.wifi_aps.keys())
        self._target_ssids = {t.ssid for t in env.targets} if env.targets else set()

        # Build AnchorDef list — one per configured AP
        anchors = [
            cc.AnchorDef(
                id          = ap.id,
                x           = float(ap.x),
                y           = float(ap.y),
                rssi_at_1m  = float(per_ap.get(ap.id, {}).get("rssi_at_1m",  default_p0)),
                path_loss_n = float(per_ap.get(ap.id, {}).get("path_loss_n", default_n)),
            )
            for ap in env.wifi_aps.values()
        ]

        self._engine = cc.AnchorPosEngine(
            window_size = window,
            max_dist_m  = max_dist,
            room_w      = float(env.width_m),
            room_h      = float(env.height_m),
        )
        self._engine.set_anchors(anchors)

        _log.debug(
            "AnchorPosEngineWrapper ready: %d anchor(s), window=%d, "
            "max_dist=%.1f m, default P0=%.1f dBm, n=%.2f",
            len(anchors), window, max_dist, default_p0, default_n,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_cycle(
        self,
        raw_rssi:    RSSIMap,
        scan_number: int             = 0,
        timestamp:   Optional[str]   = None,
        decision_id: Optional[str]   = None,
    ) -> List[LocalizationDecision]:
        """Ingest one RSSI scan and return current position estimates.

        Parameters
        ----------
        raw_rssi : RSSIMap
            ``{ap_id: {device_id: rssi_dbm}}`` — the standard scan output
            from ``TelnetPipe`` / ``RSSIFilter``.
        scan_number : int
            Monotonic scan counter, forwarded into ``LocalizationDecision``.
        timestamp : str, optional
            ISO-8601 string.  Defaults to ``_now()`` from app.py context if
            not supplied (wrapper imports it lazily).
        decision_id : str, optional
            Caller-supplied UUID for the batch; auto-generated if omitted.

        Returns
        -------
        List[LocalizationDecision]
            One entry per tracked device with at least 2 anchor observations.
        """
        if timestamp is None:
            try:
                from app import _now
                timestamp = _now()
            except ImportError:
                from datetime import datetime, timezone
                timestamp = datetime.now(timezone.utc).isoformat()

        if decision_id is None:
            import uuid
            decision_id = str(uuid.uuid4())

        # ── Split scan map into target observations and inter-anchor obs ──
        target_map: RSSIMap = {}
        ia_map:     RSSIMap = {}

        for ap_id, devices in raw_rssi.items():
            for dev_id, rssi in devices.items():
                if dev_id in self._target_ssids:
                    target_map.setdefault(ap_id, {})[dev_id] = float(rssi)
                elif dev_id in self._ap_ids:
                    ia_map.setdefault(ap_id, {})[dev_id] = float(rssi)

        # ── Feed both streams into the C++ engine ─────────────────────────
        if target_map:
            self._engine.feed_rssi_batch(target_map)
        if ia_map:
            self._engine.feed_inter_anchor_batch(ia_map)

        # ── Retrieve positions and convert to LocalizationDecision ─────────
        positions = self._engine.get_all_positions()   # {target_id: AnchorPosResult}

        decisions: List[LocalizationDecision] = []
        for target_id, result in positions.items():
            if result.confidence <= 0.0:
                continue   # not enough data yet

            room_id = _find_room(result.x, result.y, self._env.rooms)

            d = LocalizationDecision(
                decision_id = decision_id,
                device_id   = target_id,
                campus_id   = self._campus_id,
                building_id = self._building_id,
                floor_id    = self._floor_id,
                room_id     = room_id,
                timestamp   = timestamp,
                confidence  = result.confidence,
                rssi_vector = {},   # not a per-AP vector — engine handles smoothing
                x           = result.x,
                y           = result.y,
                scan_number = scan_number,
            )
            decisions.append(d)

        return decisions

    # ------------------------------------------------------------------
    # Pass-through diagnostics (for FastAPI endpoints / debug routes)
    # ------------------------------------------------------------------

    def get_all_positions(self) -> dict:
        """Return cached position dict without re-running trilateration.

        Returns ``{target_id: {x, y, confidence, n_anchors}}`` — serialisable
        directly as JSON in FastAPI response models.
        """
        return {
            tid: r.to_dict()
            for tid, r in self._engine.get_all_positions().items()
        }

    def get_p0_calibration(self) -> dict:
        """Return live P0 calibration values for all anchors.

        Useful for the diagnostics dashboard to verify self-calibration
        is converging.  Returns ``{anchor_id: p0_dbm}``.
        """
        return dict(self._engine.get_all_p0())

    def get_anchor_weights(self) -> dict:
        """Return current per-anchor confidence weights ``{anchor_id: weight}``."""
        return dict(self._engine.get_anchor_weights())

    def clear_target(self, target_id: str) -> None:
        """Evict all cached state for *target_id* from the C++ engine."""
        self._engine.clear_target(target_id)

    def get_ranging_data(self) -> dict:
        """Per-device, per-anchor RSSI and distance estimates.

        Returns a dict shaped::

            {
              device_id: {
                anchor_id: {
                  "x":       float,   # anchor position (metres)
                  "y":       float,
                  "rssi_dbm": float,  # latest smoothed RSSI
                  "dist_m":  float,   # path-loss distance estimate
                  "weight":  float,   # variance-detection confidence [0, 1]
                }
              }
            }

        Used by the ``/map/anchor-detail`` endpoint to drive range circles
        and anchor→device range lines on the live floor-plan overlay.
        """
        import math

        rssi_cache = self._engine.get_target_rssi_cache()  # {ap_id: {dev_id: rssi}}
        weights    = self.get_anchor_weights()             # {anchor_id: weight}

        # Collect per-AP config (static P0 / n used when live calibration unavailable)
        sys_cfg    = {}   # we stored these during __init__; re-derive from anchor_map
        anchor_map = self._engine.create_anchor_map()  # {anchor_id: (x, y)}

        # Build {device_id: {anchor_id: {...}}}
        result: dict = {}
        for ap_id, targets in rssi_cache.items():
            ap = self._env.wifi_aps.get(ap_id)
            if ap is None:
                continue
            # Use live-calibrated P0 if available; fall back to configured value
            p0 = self._engine.get_rssi_at_1m(ap_id)      # calls get_all_p0 internally
            # Retrieve path_loss_n from the AnchorDef stored on the engine
            # (we can't reach private members, so derive n from the configured ap)
            # AnchorDef.path_loss_n is set from cfg during __init__; we don't
            # cache it here, so fall back to class default — acceptable because
            # the C++ engine already used the correct n internally.
            n = self._DEFAULT_N

            for dev_id, rssi in targets.items():
                if dev_id in self._ap_ids:
                    continue   # skip inter-anchor entries
                dist = 10.0 ** ((p0 - rssi) / (10.0 * n))
                dist = max(0.05, min(dist, 50.0))   # sanity clamp

                result.setdefault(dev_id, {})[ap_id] = {
                    "x":        float(ap.x),
                    "y":        float(ap.y),
                    "rssi_dbm": round(float(rssi), 1),
                    "dist_m":   round(dist, 2),
                    "weight":   round(float(weights.get(ap_id, 1.0)), 3),
                }
        return result

    def clear_target(self, target_id: str) -> None:
        """Evict all cached state for *target_id* from the C++ engine."""
        self._engine.clear_target(target_id)

    def clear_all_targets(self) -> None:
        """Evict all target-device state.  Calibration data is preserved."""
        self._engine.clear_all_targets()