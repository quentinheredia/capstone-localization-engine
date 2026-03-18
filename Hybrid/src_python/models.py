"""
models.py — Data Transfer Objects (DTOs) only.
All geometry math lives in C++ (geometry.cpp / capstone_core).

Physical hierarchy enforced here:
    Campus  →  Building  →  Floor  →  Room

Public API
----------
  select_location(cfg, campus, building, floor)  →  FloorEnvironment
      The single entry point for resolving a location.
      campus / building / floor default to the edge_* keys in config.yaml,
      so callers can override at runtime without editing the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------
Polygon    = List[Tuple[float, float]]
RSSIVector = Dict[str, float]          # {ap_id: rssi_dbm}
RSSIMap    = Dict[str, RSSIVector]     # {ap_id: {ssid: rssi_dbm}}


# ---------------------------------------------------------------------------
# Raw scan / decision DTOs
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    """One row from the EAP350 APSCAN table (produced by telnet_parser.cpp)."""
    bssid:    str
    ssid:     str
    signal:   int                    # dBm
    channel:  Optional[str] = None
    security: Optional[str] = None


@dataclass
class LocalizationDecision:
    """Final verdict for one device per aggregation cycle."""
    decision_id:  str                # uuid4
    device_id:    str                # SSID
    campus_id:    str
    building_id:  str
    floor_id:     str
    room_id:      str
    timestamp:    str                # ISO-8601 UTC
    confidence:   float
    rssi_vector:  RSSIVector
    x:            float
    y:            float
    scan_number:  int


@dataclass
class ToFMeasurement:
    """Payload pushed by an ESP32-C3 anchor over MQTT."""
    mac:        str
    distance_m: float
    timestamp:  str                  # ISO-8601 UTC


@dataclass
class RawSnapshot:
    """Single-cycle raw poll result forwarded to the API for UI preview."""
    timestamp:    str
    scan_number:  int
    aps_present:  int
    aps_expected: int
    complete:     bool
    results:      RSSIMap


# ---------------------------------------------------------------------------
# Config-backed hardware definitions
# ---------------------------------------------------------------------------

@dataclass
class AccessPoint:
    """WiFi AP polled by TelnetPipe."""
    id:       str
    host:     str
    x:        float
    y:        float
    username: str = "admin"
    password: str = "admin"


@dataclass
class ToFAnchor:
    """ESP32-C3 anchor subscribed to by MQTTPipe."""
    id:  str
    mac: str
    x:   float
    y:   float


@dataclass
class RoomDef:
    """Floor-plan room. Geometry checked in C++ (geometry.cpp)."""
    room_id:  str
    name:     str
    center_x: float
    center_y: float
    polygon:  Polygon


@dataclass
class TargetProfile:
    ssid:           str
    rssi_at_1m_dbm: float
    path_loss_n:    float


# ---------------------------------------------------------------------------
# FloorEnvironment — the resolved context for one active floor
# ---------------------------------------------------------------------------

@dataclass
class FloorEnvironment:
    """Everything the engines need for one physical floor."""
    campus_id:    str
    building_id:  str
    floor_id:     str
    width_m:      float
    height_m:     float
    wifi_aps:     Dict[str, AccessPoint]   # keyed by AP id
    tof_anchors:  Dict[str, ToFAnchor]     # keyed by anchor id
    rooms:        List[RoomDef]
    targets:      List[TargetProfile]

    @property
    def full_path(self) -> str:
        """Human-readable identifier: 'Campus / Building / Floor'."""
        return f"{self.campus_id} / {self.building_id} / {self.floor_id}"

    @property
    def slug(self) -> str:
        """Filesystem-safe key: 'Campus_Building_Floor'."""
        return f"{self.campus_id}_{self.building_id}_{self.floor_id}"


# ---------------------------------------------------------------------------
# select_location() — the single public entry point
# ---------------------------------------------------------------------------

def select_location(
    cfg:         dict,
    campus_id:   Optional[str] = None,
    building_id: Optional[str] = None,
    floor_id:    Optional[str] = None,
) -> FloorEnvironment:
    """
    Resolve and return the FloorEnvironment for the given physical location.

    Parameters
    ----------
    cfg         : Parsed config.yaml dict.
    campus_id   : Campus key (e.g. "Carleton_University").
                  Falls back to cfg.telemetry_config.edge_campus_id.
    building_id : Building key (e.g. "Mackenzie_Building").
                  Falls back to cfg.telemetry_config.edge_building_id.
    floor_id    : Floor key (e.g. "Floor_3").
                  Falls back to cfg.telemetry_config.edge_floor_id.

    Returns
    -------
    FloorEnvironment populated with APs, rooms, anchors, and targets.

    Raises
    ------
    RuntimeError  if the campus / building / floor path is not in config.
    """
    tc = cfg.get("telemetry_config", {})

    # Resolve IDs — explicit args win over config defaults
    campus   = campus_id   or tc.get("edge_campus_id",   "unknown")
    building = building_id or tc.get("edge_building_id", "unknown")
    floor    = floor_id    or tc.get("edge_floor_id",    "unknown")

    def_creds = tc.get("default_ap_credentials", {})
    def_user  = def_creds.get("username", "admin")
    def_pass  = def_creds.get("password", "admin")

    # Targets are global (same device list regardless of floor)
    targets: List[TargetProfile] = []
    for t in cfg.get("targets", []):
        ov = t.get("radio_override", {})
        targets.append(TargetProfile(
            ssid           = t["ssid"],
            rssi_at_1m_dbm = ov.get("rssi_at_1m_dbm", -22.0),
            path_loss_n    = ov.get("path_loss_n",     4.0),
        ))

    # Walk the hierarchy: campus → building → floor
    try:
        floor_data = (
            cfg["locations"][campus]["buildings"][building]["floors"][floor]
        )
    except KeyError:
        raise RuntimeError(
            f"Location not found in config: "
            f"{campus} / {building} / {floor}\n"
            f"Check the 'locations' block in config.yaml."
        )

    width_m  = float(floor_data.get("width_m",  10.0))
    height_m = float(floor_data.get("height_m", 10.0))

    # WiFi APs
    wifi_aps: Dict[str, AccessPoint] = {}
    for ap_id, ap_data in floor_data.get("wifi_aps", {}).items():
        wifi_aps[ap_id] = AccessPoint(
            id       = ap_id,
            host     = ap_data.get("host", ""),
            x        = float(ap_data.get("x", 0.0)),
            y        = float(ap_data.get("y", 0.0)),
            username = ap_data.get("username", def_user),
            password = ap_data.get("password", def_pass),
        )

    # ToF anchors
    tof_anchors: Dict[str, ToFAnchor] = {}
    for anch_id, anch_data in floor_data.get("tof_anchors", {}).items():
        if not isinstance(anch_data, dict):
            continue
        tof_anchors[anch_id] = ToFAnchor(
            id  = anch_id,
            mac = anch_data.get("mac", ""),
            x   = float(anch_data.get("x", 0.0)),
            y   = float(anch_data.get("y", 0.0)),
        )

    # Rooms — room_id is the full physical path so it's globally unique
    rooms: List[RoomDef] = []
    for room_name, room_data in floor_data.get("rooms", {}).items():
        raw_poly = room_data.get("polygon", [])
        polygon: Polygon = [(float(v[0]), float(v[1])) for v in raw_poly]
        rooms.append(RoomDef(
            room_id  = f"{campus}_{building}_{floor}_{room_name}",
            name     = str(room_name),
            center_x = float(room_data["center_x"]),
            center_y = float(room_data["center_y"]),
            polygon  = polygon,
        ))

    return FloorEnvironment(
        campus_id   = campus,
        building_id = building,
        floor_id    = floor,
        width_m     = width_m,
        height_m    = height_m,
        wifi_aps    = wifi_aps,
        tof_anchors = tof_anchors,
        rooms       = rooms,
        targets     = targets,
    )


# ---------------------------------------------------------------------------
# Convenience alias kept for any existing call sites
# ---------------------------------------------------------------------------
def load_floor_environment(cfg: dict) -> FloorEnvironment:
    """Thin wrapper — resolves active location from config defaults."""
    return select_location(cfg)
