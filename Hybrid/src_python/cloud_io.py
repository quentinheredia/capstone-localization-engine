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

_CSV_HEADERS = [
    "_id", "device_id", "campus_id", "building_id", "floor_id", "room_id",
    "timestamp", "confidence", "rssi_vector", "x", "y", "scan_number",
]


def log_to_csv(payload: Dict[str, Any], csv_path: str = "telemetry_log.csv") -> bool:
    """
    Append one localization decision row to the CSV log.

    Creates the file with headers if it does not already exist.
    Thread-safe for single-writer use (the orchestrator calls this
    from a single asyncio task).
    """
    try:
        file_exists = os.path.isfile(csv_path)
        with open(csv_path, mode="a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if not file_exists:
                writer.writerow(_CSV_HEADERS)
            writer.writerow([
                payload.get("_id",         ""),
                payload.get("device_id",   ""),
                payload.get("campus_id",   ""),
                payload.get("building_id", ""),
                payload.get("floor_id",    ""),
                payload.get("room_id",     ""),
                payload.get("timestamp",   ""),
                f"{payload.get('confidence', 0.0):.4f}",
                json.dumps(payload.get("rssi_vector", {})),
                f"{payload.get('x', 0.0):.4f}",
                f"{payload.get('y', 0.0):.4f}",
                payload.get("scan_number", 0),
            ])
        return True
    except Exception as exc:
        log.error("cloud_io: CSV write failed: %s", exc)
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
