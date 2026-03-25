"""
app.py — Single-process orchestrator + FastAPI server.

Architecture
------------
  One process.  FastAPI runs on the main thread via uvicorn.
  asyncio background tasks drive TelnetPipe and MQTTPipe concurrently.
  Both trilateration AND fingerprinting run simultaneously (shared RSSI pipe).
  Per-method decision rings are kept separately; /decisions returns trilateration
  by default for backward-compat.

Start
-----
  python app.py                     # reads config.yaml from the same directory
  uvicorn app:app --reload          # for development

Environment
-----------
  AWS_* credentials in .env (loaded by cloud_io.py at import time)
  TELEMETRY_CONFIG_PATH   override config path (optional)

Poll interval
-------------
  Default poll_interval_s = 1.5 s (half the original 3 s default).
  Set system.testing_mode: true in config.yaml to run with only 1 AP present.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from models import (
    FloorEnvironment,
    LocalizationDecision,
    select_location,
)
from engine_wrappers import RSSIEngineWrapper, FingerprintWrapper
from data_pipes import TelnetPipe, MQTTPipe
import cloud_io

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt = "%H:%M:%S",
)
log = logging.getLogger("app")

# ---------------------------------------------------------------------------
# Startup / shutdown lifecycle (must be defined before FastAPI() is called)
# ---------------------------------------------------------------------------
_poll_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poll_task
    # ── startup ──────────────────────────────────────────────────────────
    # Load config (if present) so /config and /status work, but do NOT
    # auto-start the poll loop.  The user starts localization explicitly
    # via the platform's Start button (POST /engine/start) or the REST
    # endpoint POST /poll/start.
    cfg = _load_cfg_from_disk()
    if cfg:
        _apply_cfg(cfg)
        log.info("Startup complete — config loaded, poll NOT auto-started (use POST /poll/start)")
    else:
        log.warning("Startup: no config found — serving API only")

    yield  # application runs here

    # ── shutdown ─────────────────────────────────────────────────────────
    if _poll_task and not _poll_task.done():
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
    log.info("Shutdown complete")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Capstone Telemetry — Hybrid", version="3.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ---------------------------------------------------------------------------
# In-memory state  (single-process; no locking needed in async context)
# ---------------------------------------------------------------------------
MAX_DECISIONS = 200
MAX_LOGS      = 500

_state: Dict[str, Any] = {
    "cfg":           None,   # raw config dict
    "cfg_error":     None,
    "env":           None,   # FloorEnvironment
    # Per-method decision rings
    "rssi_decisions": [],    # trilateration verdicts
    "fp_decisions":   [],    # fingerprinting verdicts
    "ble_decisions":  [],    # BLE (stub — populated externally)
    "tof_decisions":  [],    # ToF (stub — populated externally)
    # Latest position per device per method
    "rssi_positions": {},
    "fp_positions":   {},
    "device_status": {},     # {device_id: {reachable, last_scan}}
    "raw":           None,   # last RawSnapshot dict
    "logs":          [],     # List[{timestamp, severity, device_id, message}]
    "status": {
        "last_update":     None,
        "total_decisions": 0,
        "last_device_id":  None,
    },
    "scan_counter":  0,
    "poll_running":  False,
}

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
_CONFIG_PATH_OVERRIDE = os.environ.get("TELEMETRY_CONFIG_PATH", "")
_HERE = Path(__file__).parent


def _find_config() -> Optional[Path]:
    candidates = [
        Path(_CONFIG_PATH_OVERRIDE) if _CONFIG_PATH_OVERRIDE else None,
        _HERE / "config.yaml",
        _HERE.parent / "config.yaml",
    ]
    for p in candidates:
        if p and p.exists():
            return p
    return None


def _load_cfg_from_disk() -> Optional[dict]:
    path = _find_config()
    if not path:
        _state["cfg_error"] = "config.yaml not found"
        return None
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _state["cfg_error"] = str(exc)
        return None
    if not isinstance(cfg, dict):
        _state["cfg_error"] = "YAML root must be a mapping"
        return None
    _state["cfg"]       = cfg
    _state["cfg_error"] = None
    return cfg


def _apply_cfg(
    cfg:         dict,
    campus_id:   Optional[str] = None,
    building_id: Optional[str] = None,
    floor_id:    Optional[str] = None,
) -> None:
    """
    Resolve the active location and rebuild FloorEnvironment.

    campus_id / building_id / floor_id are hardcoded to None here so the
    values come from config.yaml's edge_* keys.  Pass explicit values when
    calling from a future location-selector endpoint or CLI flag.
    """
    try:
        env = select_location(cfg, campus_id, building_id, floor_id)
        _state["env"] = env
        log.info(
            "Location selected: %s  (%d APs, %d rooms, %d targets)",
            env.full_path,
            len(env.wifi_aps), len(env.rooms), len(env.targets),
        )
    except Exception as exc:
        _state["cfg_error"] = str(exc)
        log.error("Config parse failed: %s", exc)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_log(severity: str, device_id: str, message: str) -> None:
    _state["logs"].append({
        "timestamp": _now(),
        "severity":  severity,
        "device_id": device_id,
        "message":   message,
    })
    if len(_state["logs"]) > MAX_LOGS:
        _state["logs"] = _state["logs"][-MAX_LOGS:]


def _severity(conf: Optional[float]) -> str:
    if conf is None:
        return "INFO"
    return "WARN" if conf < 0.5 else "INFO"


def _store_decision(d: LocalizationDecision, method_key: str) -> None:
    """
    Commit a verdict to the per-method ring, positions dict, and S3/CSV.

    method_key: "rssi_decisions" | "fp_decisions" | "ble_decisions" | "tof_decisions"
    """
    payload = {
        "_id":                  d.decision_id,
        "device_id":            d.device_id,
        "campus_id":            d.campus_id,
        "building_id":          d.building_id,
        "floor_id":             d.floor_id,
        "room_id":              d.room_id,
        "timestamp":            d.timestamp,
        "confidence":           d.confidence,
        "rssi_vector":          d.rssi_vector,
        "x":                    d.x,
        "y":                    d.y,
        "scan_number":          d.scan_number,
        "localization_method":  method_key.replace("_decisions", ""),
    }

    ring = _state[method_key]
    ring.append(payload)
    if len(ring) > MAX_DECISIONS:
        _state[method_key] = ring[-MAX_DECISIONS:]

    pos_key = method_key.replace("decisions", "positions")
    if pos_key in _state:
        _state[pos_key][d.device_id] = {
            "x": d.x, "y": d.y,
            "room_id":   d.room_id,
            "timestamp": d.timestamp,
        }

    _state["status"].update({
        "last_update":     d.timestamp,
        "total_decisions": _state["status"]["total_decisions"] + 1,
        "last_device_id":  d.device_id,
    })
    _append_log(_severity(d.confidence), d.device_id,
                f"[{method_key.replace('_decisions','')}] {d.device_id} -> {d.room_id} ({d.confidence:.2f})")

    cfg = _state.get("cfg") or {}
    cloud_cfg    = cfg.get("cloud", {})
    csv_path     = cloud_cfg.get("csv_log_path", "telemetry_log.csv")
    s3_template  = cloud_cfg.get("s3_key_template", "{campus}_{building}_{floor}_latest.json")
    cloud_io.log_to_csv(payload, csv_path)
    cloud_io.push_to_s3(payload, key_template=s3_template)


# ---------------------------------------------------------------------------
# Background polling task
# ---------------------------------------------------------------------------

async def _poll_loop() -> None:
    """
    Core async loop.
    Shared RSSI pipe feeds BOTH trilateration (every cycle, as preview) and
    fingerprinting (verdict window only, when a radiomap exists).
    """
    cfg = _state.get("cfg")
    env: Optional[FloorEnvironment] = _state.get("env")
    if cfg is None or env is None:
        log.error("Poll loop: no valid config — aborting")
        return

    _state["poll_running"] = True
    tc            = cfg.get("telemetry_config", {})
    # Default halved to 1.5 s; can still be overridden in config.yaml
    poll_interval = float(tc.get("poll_interval_s", 1.5))
    update_int    = float(tc.get("update_interval_s", 60.0))
    prompts       = tc.get("prompts", {"main": "eap350>", "sub": "eap350/wless2/network>"})
    testing_mode  = bool(cfg.get("system", {}).get("testing_mode", False))
    target_ssids  = [t.ssid for t in env.targets]

    # Build engine wrappers
    rssi_wrapper = RSSIEngineWrapper(env, cfg)

    fp_wrapper: Optional[FingerprintWrapper] = None
    rm_template   = cfg.get("cloud", {}).get("radiomap_path", "radiomap_{campus}_{building}_{floor}.json")
    radiomap_path = cloud_io.resolve_radiomap_path(rm_template, env.campus_id, env.building_id, env.floor_id)
    if radiomap_path and Path(radiomap_path).exists():
        fp_wrapper = FingerprintWrapper(radiomap_path)
        log.info("Fingerprinting enabled (radiomap: %s)", radiomap_path)
    else:
        log.info("Fingerprinting disabled (no radiomap at %s)", radiomap_path)

    # Build pipes
    telnet_pipe = TelnetPipe(
        aps             = list(env.wifi_aps.values()),
        target_ssids    = target_ssids,
        prompts         = prompts,
        poll_interval_s = poll_interval,
    )
    mqtt_pipe = MQTTPipe(
        anchors      = list(env.tof_anchors.values()),
        broker_host  = cfg.get("mqtt", {}).get("broker_host", "localhost"),
        broker_port  = int(cfg.get("mqtt", {}).get("broker_port", 1883)),
        topic_prefix = cfg.get("mqtt", {}).get("topic_prefix", "capstone"),
        keepalive_s  = int(cfg.get("mqtt", {}).get("keepalive_s", 60)),
    )

    await telnet_pipe.connect()
    await mqtt_pipe.connect()

    scan_buffer: list    = []
    last_verdict         = asyncio.get_event_loop().time()
    required_ap_ids      = set(env.wifi_aps.keys())
    # In testing mode, 1 AP is enough for a valid scan; otherwise all APs must report
    min_aps_for_valid    = 1 if testing_mode else len(env.wifi_aps)

    log.info(
        "Poll loop started  poll=%.1fs  verdict_window=%.0fs  testing=%s  targets=%s",
        poll_interval, update_int, testing_mode, target_ssids,
    )

    try:
        async for rssi_map in telnet_pipe.stream():
            _state["scan_counter"] += 1
            n = _state["scan_counter"]

            # Update reachability
            for ap_id, dev_map in rssi_map.items():
                for ssid in dev_map:
                    _state["device_status"][ssid] = {
                        "reachable": True, "last_scan": _now()
                    }

            # Raw snapshot for UI
            present  = len(rssi_map)
            expected = len(env.wifi_aps)
            _state["raw"] = {
                "timestamp":    _now(),
                "scan_number":  n,
                "aps_present":  present,
                "aps_expected": expected,
                "complete":     present >= min_aps_for_valid,
                "testing_mode": testing_mode,
                "results":      rssi_map,
            }

            # ── Trilateration preview (every scan cycle) ──────────────────
            for d in rssi_wrapper.process_cycle(rssi_map, scan_number=n):
                log.info("[Trilat #%d] %s -> %s (%.2f)  (%.2f, %.2f)",
                         n, d.device_id, d.room_id, d.confidence, d.x, d.y)

            scan_buffer.append(rssi_map)

            # ── Verdict window ─────────────────────────────────────────────
            if asyncio.get_event_loop().time() - last_verdict >= update_int:
                await _run_verdict(
                    scan_buffer, env, rssi_wrapper, fp_wrapper,
                    target_ssids, required_ap_ids, min_aps_for_valid, n,
                )
                scan_buffer  = []
                last_verdict = asyncio.get_event_loop().time()

    except asyncio.CancelledError:
        log.info("Poll loop cancelled")
    finally:
        await telnet_pipe.close()
        await mqtt_pipe.close()
        _state["poll_running"] = False


async def _run_verdict(
    scan_buffer:      list,
    env:              FloorEnvironment,
    rssi_wrapper:     RSSIEngineWrapper,
    fp_wrapper:       Optional[FingerprintWrapper],
    target_ssids:     List[str],
    required_ap_ids:  set,
    min_aps_for_valid: int,
    scan_counter:     int,
) -> None:
    """
    Aggregate the scan buffer and emit final localization decisions.
    Runs BOTH trilateration AND fingerprinting (when available).
    """
    timestamp = _now()
    log.info("=" * 55 + "  VERDICT")

    for ssid in target_ssids:
        # Collect scans where enough APs saw this SSID
        complete_scans = [
            s for s in scan_buffer
            if sum(1 for ap, devs in s.items() if ssid in devs) >= min_aps_for_valid
        ]

        total  = len(scan_buffer)
        valid  = len(complete_scans)
        health = valid / total if total else 0.0
        log.info("%s  health=%d%%  (%d/%d valid)", ssid, int(health * 100), valid, total)

        if health < 0.5:
            log.warning("%s: less than 50%% of scans were complete", ssid)
        if not complete_scans:
            log.warning("%s: 0 complete scans — no verdict", ssid)
            continue

        # Average the complete scans per AP
        averaged: Dict[str, Dict[str, float]] = defaultdict(dict)
        for ap_id in required_ap_ids:
            vals = [s[ap_id][ssid] for s in complete_scans if ssid in s.get(ap_id, {})]
            if vals:
                averaged[ap_id][ssid] = sum(vals) / len(vals)
        averaged = dict(averaged)

        # ── Trilateration verdict ─────────────────────────────────────────
        dec_id = str(uuid.uuid4())
        for d in rssi_wrapper.process_cycle(
            averaged,
            scan_number = scan_counter,
            timestamp   = timestamp,
            decision_id = dec_id,
        ):
            _store_decision(d, "rssi_decisions")
            log.info("VERDICT [trilat]  %s -> %s  conf=%.2f  (%.2f, %.2f)",
                     d.device_id, d.room_id, d.confidence, d.x, d.y)

        # ── Fingerprinting verdict ────────────────────────────────────────
        if fp_wrapper:
            live_vec = {
                ap_id: averaged[ap_id][ssid]
                for ap_id in averaged if ssid in averaged.get(ap_id, {})
            }
            room, conf = fp_wrapper.match(live_vec)
            if room not in ("Outside Defined Area", "Undetected"):
                d = LocalizationDecision(
                    decision_id = str(uuid.uuid4()),
                    device_id   = ssid,
                    campus_id   = env.campus_id,
                    building_id = env.building_id,
                    floor_id    = env.floor_id,
                    room_id     = room,
                    timestamp   = timestamp,
                    confidence  = conf,
                    rssi_vector = live_vec,
                    x=0.0, y=0.0,
                    scan_number = scan_counter,
                )
                _store_decision(d, "fp_decisions")
                log.info("VERDICT [fp]  %s -> %s  conf=%.2f", ssid, room, conf)


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.get("/health")
def get_health():
    return {"ok": True, "time": _now(), "poll_running": _state["poll_running"]}


@app.post("/poll/start")
async def start_poll():
    """Start the background poll loop (if not already running)."""
    global _poll_task
    if _state["poll_running"]:
        return {"ok": True, "message": "Already running"}
    cfg = _state.get("cfg")
    if cfg is None:
        cfg = _load_cfg_from_disk()
        if cfg:
            _apply_cfg(cfg)
    if cfg is None:
        raise HTTPException(503, "No config loaded — POST /config first")
    _poll_task = asyncio.create_task(_poll_loop(), name="poll_loop")
    return {"ok": True, "message": "Poll loop started"}


@app.post("/poll/stop")
async def stop_poll():
    """Stop the background poll loop."""
    global _poll_task
    if _poll_task and not _poll_task.done():
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
        _poll_task = None
        return {"ok": True, "message": "Poll loop stopped"}
    return {"ok": True, "message": "Not running"}


@app.get("/")
def get_root():
    index = _HERE.parent / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse({"ok": False, "error": "index.html not found"}, status_code=404)


@app.get("/status")
def get_status():
    return {
        **_state["status"],
        "config_error": _state["cfg_error"],
        "poll_running": _state["poll_running"],
    }


@app.get("/config")
def get_config():
    if _state["cfg"] is None:
        raise HTTPException(404, "No config loaded")
    return _state["cfg"]


@app.post("/config")
def post_config(payload: Dict[str, Any]):
    _state["cfg"] = payload
    _apply_cfg(payload)
    return {"ok": True, "received_at": _now()}


@app.post("/config/reload")
def reload_config():
    cfg = _load_cfg_from_disk()
    if cfg is None:
        raise HTTPException(404, _state.get("cfg_error") or "config.yaml not found")
    _apply_cfg(cfg)
    return {"ok": True, "received_at": _now()}


@app.post("/config/upload")
async def upload_config(file: UploadFile = File(...)):
    if not (file.filename or "").endswith((".yml", ".yaml")):
        raise HTTPException(400, "Only .yml/.yaml supported")
    data = await file.read()
    try:
        cfg = yaml.safe_load(data.decode())
    except Exception as exc:
        raise HTTPException(400, f"Invalid YAML: {exc}")
    if not isinstance(cfg, dict):
        raise HTTPException(400, "YAML root must be a mapping")
    _state["cfg"] = cfg
    _apply_cfg(cfg)
    return {"ok": True, "received_at": _now()}


# ── Decisions — combined + per-method ────────────────────────────────────────

@app.get("/decisions")
def get_decisions(limit: int = 50):
    """
    Returns trilateration decisions by default (backward-compatible).
    Falls back to CSV if the in-memory ring is empty.
    """
    items = _state["rssi_decisions"][-limit:]
    if not items:
        cfg      = _state.get("cfg") or {}
        csv_path = cfg.get("cloud", {}).get("csv_log_path", "telemetry_log.csv")
        items = list(cloud_io.read_csv_decisions(csv_path, limit))
    return {"count": len(items), "items": items}


@app.get("/decisions/trilateration")
def get_decisions_trilateration(limit: int = Query(50, ge=1, le=500)):
    items = _state["rssi_decisions"][-limit:]
    return {"method": "trilateration", "count": len(items), "items": items}


@app.get("/decisions/fingerprinting")
def get_decisions_fingerprinting(limit: int = Query(50, ge=1, le=500)):
    items = _state["fp_decisions"][-limit:]
    return {"method": "fingerprinting", "count": len(items), "items": items}


@app.get("/decisions/ble")
def get_decisions_ble(limit: int = Query(50, ge=1, le=500)):
    """BLE localization — stub. Populated by external BLE pipeline if available."""
    items = _state["ble_decisions"][-limit:]
    return {
        "method":  "ble",
        "count":   len(items),
        "items":   items,
        "note":    "BLE pipeline not yet active; data populated externally when BLE anchors are present.",
    }


@app.get("/decisions/tof")
def get_decisions_tof(limit: int = Query(50, ge=1, le=500)):
    """ToF localization — stub. Populated by MQTT pipeline when ToF anchors are configured."""
    items = _state["tof_decisions"][-limit:]
    return {
        "method":  "tof",
        "count":   len(items),
        "items":   items,
        "note":    "ToF pipeline reads from MQTT; data appears here when ToF anchors report.",
    }


# ── Devices ───────────────────────────────────────────────────────────────────

@app.get("/devices")
def get_devices():
    cfg     = _state.get("cfg") or {}
    targets = [t["ssid"] for t in cfg.get("targets", [])]
    items   = []
    for ssid in targets or list(_state["device_status"].keys()):
        st = _state["device_status"].get(ssid, {})
        items.append({
            "device_id": ssid,
            "reachable": st.get("reachable", False),
            "last_scan": st.get("last_scan"),
        })
    return {"items": items}


# ── Raw scan ──────────────────────────────────────────────────────────────────

@app.get("/raw")
def get_raw():
    if not _state["raw"]:
        raise HTTPException(404, "No raw scans yet")
    return _state["raw"]


# ── Logs ──────────────────────────────────────────────────────────────────────

@app.get("/logs")
def get_logs(
    limit:    int           = 200,
    severity: Optional[str] = None,
    q:        Optional[str] = None,
):
    items = list(_state["logs"])
    if severity:
        items = [i for i in items if i.get("severity") == severity.upper()]
    if q:
        items = [i for i in items if q.lower() in i.get("message", "").lower()]
    return {"count": len(items), "items": items[-limit:]}


# ── Map — per-method ──────────────────────────────────────────────────────────

@app.get("/map")
def get_map(method: str = Query("trilateration", description="trilateration | fingerprinting | ble | tof")):
    """
    Returns room polygons + latest device positions for the requested method.
    Use ?method=trilateration (default) | fingerprinting | ble | tof.
    """
    env: Optional[FloorEnvironment] = _state.get("env")
    rooms   = []
    floor_w = floor_h = None

    if env:
        floor_w = env.width_m
        floor_h = env.height_m
        rooms   = [{"name": r.name, "polygon": r.polygon} for r in env.rooms]

    # Choose the right positions dict
    pos_map = {
        "trilateration": _state["rssi_positions"],
        "fingerprinting": _state["fp_positions"],
        "ble":  {},
        "tof":  {},
    }.get(method, _state["rssi_positions"])

    devices = [
        {
            "device_id": dev, **pos,
            "reachable": _state["device_status"].get(dev, {}).get("reachable", False),
            "method":    method,
        }
        for dev, pos in pos_map.items()
    ]

    # Fallback to CSV if nothing in memory (trilateration only)
    if not devices and method == "trilateration":
        cfg      = _state.get("cfg") or {}
        csv_path = cfg.get("cloud", {}).get("csv_log_path", "telemetry_log.csv")
        for row in cloud_io.read_csv_decisions(csv_path, limit=1):
            dev = row.get("device_id")
            if dev:
                devices.append({
                    "device_id": dev,
                    "x":         float(row.get("x", 0)),
                    "y":         float(row.get("y", 0)),
                    "room_id":   row.get("room_id"),
                    "timestamp": row.get("timestamp"),
                    "reachable": False,
                    "method":    "trilateration",
                    "source":    "csv",
                })

    return {
        "method":  method,
        "floor":   {"width_m": floor_w, "height_m": floor_h},
        "rooms":   rooms,
        "devices": devices,
    }


# ── Survey / fingerprint calibration ──────────────────────────────────────────

@app.post("/survey/{room_label}")
def post_survey(room_label: str, payload: Dict[str, Any]):
    """
    Receive one RSSI fingerprint vector for a named room.
    Body: {"AP1": -45.0, "AP2": -55.0, ...}

    The radiomap file is resolved to the active location so calibration
    data is always physically anchored:
        radiomap_Carleton_University_Mackenzie_Building_Floor_3.json
    """
    cfg  = _state.get("cfg") or {}
    env: Optional[FloorEnvironment] = _state.get("env")
    if env is None:
        raise HTTPException(503, "No location selected — POST /config/reload first")

    rm_template   = cfg.get("cloud", {}).get("radiomap_path", "radiomap_{campus}_{building}_{floor}.json")
    radiomap_path = cloud_io.resolve_radiomap_path(rm_template, env.campus_id, env.building_id, env.floor_id)
    cloud_io.save_radiomap(room_label, payload, radiomap_path)
    return {"ok": True, "room": room_label, "file": radiomap_path, "samples_added": 1}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
