"""
Config YAML Generator.

Reads the full spatial hierarchy + device definitions from PostgreSQL and
outputs a config.yaml that the Hybrid localization engine can consume.

The generated YAML matches the schema expected by:
  Hybrid/src_python/models.py  →  select_location()
  Hybrid/src_python/app.py     →  _apply_cfg()

Entry point:
  generate_config(db, campus_id, target_floor_building, target_floor)
      → returns a dict that can be yaml.dump()'d directly

Or call the API route:
  POST /api/v1/config/generate
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from sqlalchemy.orm import Session

from models import Campus, Building, Floor, Room, Anchor, Tag


def generate_config(
    db: Session,
    campus_id: int,
    active_building_name: Optional[str] = None,
    active_floor_name: Optional[str] = None,
    poll_interval_s: int = 3,
    update_interval_s: int = 60,
) -> Dict[str, Any]:
    """
    Build a complete config.yaml dict from the database.

    Parameters
    ----------
    db                    : SQLAlchemy session.
    campus_id             : Campus PK to export.
    active_building_name  : The building to set as edge_building_id.
                            If None, uses the first building.
    active_floor_name     : The floor to set as edge_floor_id.
                            If None, uses the first floor of the active building.
    poll_interval_s       : Telnet poll cadence.
    update_interval_s     : Aggregation/verdict window.

    Returns
    -------
    Dict ready for yaml.dump().
    """
    campus = db.get(Campus, campus_id)
    if not campus:
        raise ValueError(f"Campus {campus_id} not found")

    # ── Build the locations hierarchy ─────────────────────────────────────
    buildings_block: Dict[str, Any] = {}
    first_building_name: Optional[str] = None
    first_floor_name: Optional[str] = None

    for building in campus.buildings:
        if first_building_name is None:
            first_building_name = building.name

        floors_block: Dict[str, Any] = {}
        for floor in building.floors:
            if building.name == (active_building_name or first_building_name):
                if first_floor_name is None:
                    first_floor_name = floor.name

            # WiFi APs (EnGenius only — ESP32 stubs excluded from engine config)
            wifi_aps: Dict[str, Any] = {}
            for anchor in floor.anchors:
                if anchor.anchor_type == "engenius_ap" and anchor.enabled:
                    wifi_aps[anchor.anchor_id] = {
                        "host":     anchor.ip_address,
                        "x":        anchor.x_m,
                        "y":        anchor.y_m,
                        "username": anchor.username,
                        "password": anchor.password,
                    }

            # ToF anchors (ESP32 with tof capability)
            tof_anchors: Dict[str, Any] = {}
            for anchor in floor.anchors:
                if anchor.anchor_type == "esp32" and "tof" in (anchor.capabilities or []):
                    tof_anchors[anchor.anchor_id] = {
                        "mac": anchor.mac_address,
                        "x":   anchor.x_m,
                        "y":   anchor.y_m,
                    }

            # Rooms
            rooms_block: Dict[str, Any] = {}
            for room in floor.rooms:
                rooms_block[room.name] = {
                    "center_x":          room.center_x,
                    "center_y":          room.center_y,
                    "polygon":           room.polygon or [],
                    "priority_label":    room.priority_label,
                    "localization_type": room.localization_type,
                    "alert_on_exit":     room.alert_on_exit,
                }

            floors_block[floor.name] = {
                "width_m":      floor.width_m,
                "height_m":     floor.height_m,
                "wifi_aps":     wifi_aps,
                "tof_anchors":  tof_anchors if tof_anchors else {},
                "rooms":        rooms_block,
            }

        buildings_block[building.name] = {"floors": floors_block}

    # Resolve active location
    act_building = active_building_name or first_building_name or "unknown"
    act_floor    = active_floor_name    or first_floor_name    or "unknown"

    # ── Targets from registered tags ──────────────────────────────────────
    tags = db.query(Tag).filter_by(enabled=True).all()
    targets = []
    for tag in tags:
        entry: Dict[str, Any] = {"ssid": tag.ssid}
        entry["radio_override"] = {
            "rssi_at_1m_dbm": tag.rssi_at_1m_dbm,
            "path_loss_n":    tag.path_loss_n,
        }
        targets.append(entry)

    # ── Assemble the full config ──────────────────────────────────────────
    config = {
        "system": {
            "localization_method":              "fingerprinting",
            "rolling_average_window":           5,
            "signal_filter": {
                "noise_floor_dbm":              -80,
                "min_aps_for_localization":      3,
            },
            "max_distance_for_high_confidence_m": 3.0,
            "boundary_clamp_margin_m":          0.01,
            "consensus_threshold":              0.80,
            "frame_window_minutes":             1,
        },
        "telemetry_config": {
            "edge_campus_id":   campus.name,
            "edge_building_id": act_building,
            "edge_floor_id":    act_floor,
            "poll_interval_s":  poll_interval_s,
            "update_interval_s": update_interval_s,
            "prompts": {
                "main": "eap350>",
                "sub":  "eap350/wless2/network>",
            },
            "default_ap_credentials": {
                "username": "admin",
                "password": "admin",
            },
        },
        "targets": targets,
        "locations": {
            campus.name: {
                "buildings": buildings_block,
            },
        },
        "mqtt": {
            "broker_host":  "localhost",
            "broker_port":  1883,
            "topic_prefix": "capstone",
            "keepalive_s":  60,
        },
        "cloud": {
            "s3_key_template":  "{campus}_{building}_{floor}_latest.json",
            "s3_cache_control": "max-age=2",
            "radiomap_path":    "radiomap_{campus}_{building}_{floor}.json",
            "csv_log_path":     "telemetry_log.csv",
        },
    }

    return config
