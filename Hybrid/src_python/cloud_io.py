"""
cloud_io.py — All persistence and cloud I/O.  Python owns waiting; no math here.

Responsibilities
----------------
  push_to_s3()      — Overwrite the "latest" JSON in S3 (one file per floor).
  log_to_csv()      — Append a decision row to the local telemetry_log.csv.
  load_radiomap()   — Read radiomap.json from disk into a Python dict.
  save_radiomap()   — Write / merge new fingerprint vectors into radiomap.json.

All functions are designed to be called from async code via
  await asyncio.get_event_loop().run_in_executor(None, push_to_s3, decision)
or called directly from sync context (e.g. startup).

AWS credentials are loaded from .env via python-dotenv.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy S3 client — only initialised when actually needed
# ---------------------------------------------------------------------------
_s3_client = None


def _get_s3():
    global _s3_client
    if _s3_client is None:
        try:
            import boto3
            _s3_client = boto3.client(
                "s3",
                region_name          = os.getenv("AWS_REGION"),
                aws_access_key_id    = os.getenv("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key= os.getenv("AWS_SECRET_ACCESS_KEY"),
            )
        except ImportError:
            log.error("cloud_io: boto3 not installed — S3 push disabled")
            _s3_client = None
    return _s3_client


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------

def push_to_s3(
    payload: Dict[str, Any],
    bucket:        Optional[str] = None,
    key_template:  str = "{campus}_{building}_{floor}_latest.json",
    cache_control: str = "max-age=2",
) -> bool:
    """
    Overwrite the single "latest" JSON file in S3 for this floor.

    The React/Vite frontend polls this static URL on a short interval.
    Using put_object() ensures the old file is always replaced — no versioning
    accumulation, no separate delete step.

    Returns True on success, False on any error (logged, not raised).
    """
    s3 = _get_s3()
    if s3 is None:
        return False

    bucket = bucket or os.getenv("AWS_BUCKET_NAME", "")
    if not bucket:
        log.warning("cloud_io: AWS_BUCKET_NAME not set — S3 push skipped")
        return False

    key = key_template.format(
        campus   = payload.get("campus_id",   "unknown"),
        building = payload.get("building_id", "unknown"),
        floor    = payload.get("floor_id",    "unknown"),
    )

    try:
        s3.put_object(
            Bucket      = bucket,
            Key         = key,
            Body        = json.dumps(payload),
            ContentType = "application/json",
            CacheControl= cache_control,
        )
        log.debug("cloud_io: pushed %s to s3://%s/%s", payload.get("device_id"), bucket, key)
        return True
    except Exception as exc:
        log.error("cloud_io: S3 upload failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# CSV logging
# ---------------------------------------------------------------------------

# Full header set (trilateration / BLE / ToF — all have x,y coordinates)
_CSV_HEADERS_FULL: List[str] = [
    "_id", "device_id", "campus_id", "building_id", "floor_id", "room_id",
    "timestamp", "confidence", "rssi_vector", "x", "y", "scan_number",
]

# Fingerprinting produces room-level labels only — no x,y coordinates
_CSV_HEADERS_FINGER: List[str] = [
    "_id", "device_id", "campus_id", "building_id", "floor_id", "room_id",
    "timestamp", "confidence", "rssi_vector", "scan_number",
]

# Keep the old name as an alias so any external code keeps working
_CSV_HEADERS = _CSV_HEADERS_FULL

# Map method short-key → (config key for path, headers list)
_METHOD_CSV_CONFIG: Dict[str, tuple] = {
    "rssi": ("csv_trilat_log_path",  _CSV_HEADERS_FULL),    # trilateration
    "fp":   ("csv_finger_log_path",  _CSV_HEADERS_FINGER),  # fingerprinting
    "gp":   ("csv_gp_log_path",      _CSV_HEADERS_FULL),    # Sparse GP
    "ble":  ("csv_ble_log_path",     _CSV_HEADERS_FULL),    # BLE (original headers)
    "tof":  ("csv_tof_log_path",     _CSV_HEADERS_FULL),    # Time-of-Flight
}

_DEFAULT_CSV_PATHS: Dict[str, str] = {
    "rssi": "trilat_log.csv",
    "fp":   "finger_log.csv",
    "gp":   "gp_log.csv",
    "ble":  "ble_log.csv",
    "tof":  "tof_log.csv",
}


def _build_row(payload: Dict[str, Any], headers: List[str]) -> list:
    """Serialise a decision payload to a CSV row matching the given header list."""
    row = []
    for h in headers:
        if h == "confidence":
            row.append(f"{payload.get('confidence', 0.0):.4f}")
        elif h == "rssi_vector":
            row.append(json.dumps(payload.get("rssi_vector", {})))
        elif h == "x":
            row.append(f"{payload.get('x', 0.0):.4f}")
        elif h == "y":
            row.append(f"{payload.get('y', 0.0):.4f}")
        else:
            row.append(payload.get(h, ""))
    return row


def log_to_csv_method(
    payload:    Dict[str, Any],
    method_key: str,
    cloud_cfg:  Dict[str, Any],
) -> bool:
    """
    Append one localization decision to the correct per-method CSV log.

    Parameters
    ----------
    payload     : decision dict from _store_decision()
    method_key  : "rssi_decisions" | "fp_decisions" | "ble_decisions" | "tof_decisions"
    cloud_cfg   : the ``cloud`` section of config.yaml

    Files are created with the correct headers on first write.
    """
    # Strip "_decisions" suffix → "rssi" | "fp" | "ble" | "tof"
    method = method_key.replace("_decisions", "")
    cfg_key, headers = _METHOD_CSV_CONFIG.get(method, ("csv_trilat_log_path", _CSV_HEADERS_FULL))
    path = cloud_cfg.get(cfg_key, _DEFAULT_CSV_PATHS.get(method, "telemetry_log.csv"))
    return _write_csv_row(payload, path, headers)


def log_to_csv(payload: Dict[str, Any], csv_path: str = "telemetry_log.csv") -> bool:
    """
    Legacy single-file CSV writer — kept for backward compatibility.
    New code should use log_to_csv_method() instead.
    """
    return _write_csv_row(payload, csv_path, _CSV_HEADERS_FULL)


def _write_csv_row(
    payload: Dict[str, Any],
    csv_path: str,
    headers: List[str],
) -> bool:
    """Core CSV append — creates file with headers if it does not exist."""
    try:
        file_exists = os.path.isfile(csv_path)
        with open(csv_path, mode="a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if not file_exists:
                writer.writerow(headers)
            writer.writerow(_build_row(payload, headers))
        return True
    except Exception as exc:
        log.error("cloud_io: CSV write failed (%s): %s", csv_path, exc)
        return False


def read_csv_decisions(
    csv_path: str = "telemetry_log.csv",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """
    Read the most recent `limit` rows from the CSV log.
    Returns a list of dicts keyed by the CSV header names.
    Used by app.py as a fallback when the in-memory decisions list is empty.
    """
    if not os.path.isfile(csv_path):
        return []
    try:
        with open(csv_path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        return rows[-limit:]
    except Exception as exc:
        log.error("cloud_io: CSV read failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Radio map (fingerprinting)
# ---------------------------------------------------------------------------

def resolve_radiomap_path(template: str, campus: str, building: str, floor: str) -> str:
    """
    Resolve a radiomap path template using the active location.

    Template example (from config.yaml):
        "radiomap_{campus}_{building}_{floor}.json"
    Resolves to:
        "radiomap_Carleton_University_Mackenzie_Building_Floor_3.json"

    Each floor gets its own calibration file so RSSI vectors are always
    physically anchored to the correct location.
    """
    return template.format(campus=campus, building=building, floor=floor)


def load_radiomap(path: str) -> Dict[str, List[Dict[str, float]]]:
    """
    Load a radiomap file from disk.

    Pass a resolved path from resolve_radiomap_path() so the vectors are
    guaranteed to belong to the correct physical location.

    Returns {room_label: [{ap_id: rssi_dbm, ...}, ...]}
    Returns an empty dict if the file does not exist.
    """
    if not os.path.isfile(path):
        log.info("cloud_io: radiomap not found at %s — fingerprinting disabled", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            log.warning("cloud_io: radiomap.json root is not a dict — ignoring")
            return {}
        log.info("cloud_io: loaded radiomap from %s (%d rooms)", path, len(data))
        return data
    except Exception as exc:
        log.error("cloud_io: failed to load radiomap: %s", exc)
        return {}


def save_radiomap(
    room_label: str,
    new_vector: Dict[str, float],
    path: str,
) -> None:
    """
    Append one RSSI fingerprint vector to a room's entry in the radiomap file.

    Used by POST /survey/{room}.  Pass a resolved path from
    resolve_radiomap_path() to ensure calibration data stays with the
    correct physical location.
    """
    existing = load_radiomap(path)
    existing.setdefault(room_label, [])
    existing[room_label].append(new_vector)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(existing, fh, indent=2)
        log.info(
            "cloud_io: saved fingerprint for '%s' → %s  (total samples: %d)",
            room_label, path, len(existing[room_label]),
        )
    except Exception as exc:
        log.error("cloud_io: failed to save radiomap: %s", exc)
