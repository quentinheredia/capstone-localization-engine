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
import os
from typing import Dict, List, Optional

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

class RSSIEngineWrapper:
    """
    Wraps capstone_core.RSSIEngine.

    The C++ engine is stateful (owns the rolling-average filter) so this
    wrapper is instantiated once at startup and reused every poll cycle.
    """

    def __init__(self, env: FloorEnvironment, cfg: dict) -> None:
        _require_cpp()
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
        Run one localization cycle through the C++ engine.

        Parameters
        ----------
        raw_rssi     : {ap_id: {ssid: rssi_dbm}}  — direct output of TelnetPipe
        scan_number  : monotonic counter from the orchestrator
        timestamp    : ISO-8601 UTC string (filled in by caller)
        decision_id  : uuid4 string (filled in by caller)

        Returns
        -------
        List of LocalizationDecision, one per tracked target that was detected.
        """
        import uuid
        from datetime import datetime, timezone

        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()
        if decision_id is None:
            decision_id = str(uuid.uuid4())

        cpp_results = self._engine.process_cycle(raw_rssi, self._cpp_targets)

        decisions: List[LocalizationDecision] = []
        for r in cpp_results:
            if r.room == "Undetected":
                continue
            # rssi_vector: pull from raw_rssi for this device
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
# FingerprintWrapper
# ---------------------------------------------------------------------------

class FingerprintWrapper:
    """
    Wraps capstone_core.knn_fingerprint_match.

    Loads the radio map from disk once and holds it in memory.
    Call reload_map() if you want to hot-swap the radiomap.json.
    """

    def __init__(self, radiomap_path: str = "radiomap.json", k: int = 3) -> None:
        _require_cpp()
        self._path = radiomap_path
        self._k    = k
        self._radio_map: list = []
        self.reload_map()

    def reload_map(self) -> None:
        """Load (or re-load) radiomap.json into C++ RadioMapEntry objects."""
        if not os.path.exists(self._path):
            self._radio_map = []
            return

        with open(self._path, "r") as f:
            raw: Dict[str, List[Dict[str, float]]] = json.load(f)

        self._radio_map = []
        for room_label, vectors in raw.items():
            entry = cc.RadioMapEntry()
            entry.room    = room_label
            entry.vectors = vectors
            self._radio_map.append(entry)

    def match(self, live_vector: Dict[str, float]) -> tuple[str, float]:
        """
        Run KNN against the loaded radio map.

        Returns
        -------
        (room_label, confidence)  — ('Outside Defined Area', 0.0) if no match.
        """
        if not self._radio_map:
            return "Outside Defined Area", 0.0

        result = cc.knn_fingerprint_match(live_vector, self._radio_map, self._k)
        return result.room, result.confidence


# ---------------------------------------------------------------------------
# TelnetParserWrapper
# ---------------------------------------------------------------------------

class TelnetParserWrapper:
    """
    Thin shim so data_pipes.py doesn't import capstone_core directly.
    Converts raw EAP350 APSCAN text -> list of ScanResult DTOs.
    """

    @staticmethod
    def parse(raw_text: str) -> list:
        """
        Parse EAP350 APSCAN table text using C++ telnet_parser.

        Returns list of dicts with keys: bssid, ssid, signal, channel, security.
        """
        _require_cpp()
        return cc.parse_apscan_table_dicts(raw_text)
