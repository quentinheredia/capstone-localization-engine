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
from engine_wrappers import (
    RSSIEngineWrapper, RawRSSIEngineWrapper, KalmanRSSIEngineWrapper,
    FingerprintWrapper, SGPWrapper, ToFWrapper,
)
from data_pipes import TelnetPipe, MQTTPipe
import cloud_io
from beanstalk_bridge import forward_decisions

# ---------------------------------------------------------------------------
# Logging — custom CONFIG level (15, between DEBUG=10 and INFO=20)
# ---------------------------------------------------------------------------
_CONFIG_LEVEL = 15
logging.addLevelName(_CONFIG_LEVEL, "CONFIG")


def _log_config(self, message, *args, **kws):
    if self.isEnabledFor(_CONFIG_LEVEL):
        self._log(_CONFIG_LEVEL, message, args, **kws)


logging.Logger.config = _log_config

logging.basicConfig(
    level   = logging.DEBUG,
    format  = "%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt = "%H:%M:%S",
)
# Silence routine HTTP / framework noise — only our own modules produce CONFIG+
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("fastapi").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("watchfiles").setLevel(logging.WARNING)
log = logging.getLogger("app")

# ---------------------------------------------------------------------------
# Startup / shutdown lifecycle (must be defined before FastAPI() is called)
# ---------------------------------------------------------------------------
_poll_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poll_task
    # ── startup ──────────────────────────────────────────────────────────
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
# In-memory state
# ---------------------------------------------------------------------------
MAX_DECISIONS = 200
MAX_LOGS      = 500

_state: Dict[str, Any] = {
    "cfg":           None,
    "cfg_error":     None,
    "env":           None,
    "rssi_decisions":   [],
    "raw_decisions":    [],
    "kalman_decisions": [],
    "fp_decisions":     [],
    "gp_decisions":     [],
    "ble_decisions":    [],
    "tof_decisions":    [],
    "rssi_positions":    {},
    "raw_positions":     {},
    "kalman_positions":  {},
    "fp_positions":      {},
    "gp_positions":      {},
    "tof_positions":     {},
    "device_status": {},
    "raw":           None,
    "logs":          [],
    "status": {
        "last_update":     None,
        "total_decisions": 0,
        "last_device_id":  None,
    },
    "scan_counter":  0,
    "poll_running":  False,
    "seen_ssids":    {},
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
    try:
        env = select_location(cfg, campus_id, building_id, floor_id)
        _state["env"] = env
        log.config(
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
    _method   = method_key.replace("_decisions", "")
    _vec_str  = "  ".join(
        f"{k}:{v:+.0f}" for k, v in sorted((d.rssi_vector or {}).items())
    )
    _log_msg  = (
        f"[{_method}] {d.device_id} → {d.room_id}  "
        f"conf={d.confidence:.2f}  [{_vec_str}]"
    )
    _append_log(_severity(d.confidence), d.device_id, _log_msg)
    log.info("DECISION [%s]  %s → %s  conf=%.2f  vec=[%s]",
             _method, d.device_id, d.room_id, d.confidence, _vec_str)

    cfg = _state.get("cfg") or {}
    cloud_cfg   = cfg.get("cloud", {})
    s3_template = cloud_cfg.get("s3_key_template", "{campus}_{building}_{floor}_latest.json")
    cloud_io.log_to_csv_method(payload, method_key, cloud_cfg)
    cloud_io.push_to_s3(payload, key_template=s3_template)


# ---------------------------------------------------------------------------
# Background polling task
# ---------------------------------------------------------------------------

async def _poll_loop() -> None:
    cfg = _state.get("cfg")
    env: Optional[FloorEnvironment] = _state.get("env")
    if cfg is None or env is None:
        log.error("Poll loop: no valid config — aborting")
        return

    _state["poll_running"] = True
    tc            = cfg.get("telemetry_config", {})
    poll_interval = float(tc.get("poll_interval_s", 1.5))
    update_int    = float(tc.get("update_interval_s", 60.0))
    prompts       = tc.get("prompts", {"main": "eap350>", "sub": "eap350/wless2/network>"})
    testing_mode  = bool(cfg.get("system", {}).get("testing_mode", False))
    target_ssids  = [t.ssid for t in env.targets]

    try:
        rssi_wrapper = RSSIEngineWrapper(env, cfg)
    except Exception as _e:
        _msg = f"[Poll startup] FAILED to build RSSIEngineWrapper: {type(_e).__name__}: {_e}"
        log.error(_msg); _append_log("ERROR", "", _msg)
        _state["poll_running"] = False; return

    try:
        raw_wrapper = RawRSSIEngineWrapper(env, cfg)
        _msg = "Raw trilateration enabled (no RSSI filtering — baseline)"
        log.config(_msg); _append_log("CONFIG", "", _msg)
    except Exception as _e:
        raw_wrapper = None
        _msg = f"[Poll startup] RawRSSIEngineWrapper failed (non-fatal): {_e}"
        log.warning(_msg); _append_log("WARN", "", _msg)

    try:
        kalman_wrapper = KalmanRSSIEngineWrapper(env, cfg)
        kf_cfg = cfg.get("system", {}).get("kalman_filter", {})
        _msg = (
            f"Kalman trilateration enabled  "
            f"Q={kf_cfg.get('process_noise_Q', 1.0)}  "
            f"R={kf_cfg.get('measurement_noise_R', 4.0)}"
        )
        log.config(_msg); _append_log("CONFIG", "", _msg)
    except Exception as _e:
        kalman_wrapper = None
        _msg = f"[Poll startup] KalmanRSSIEngineWrapper failed (non-fatal): {_e}"
        log.warning(_msg); _append_log("WARN", "", _msg)

    fp_wrapper: Optional[FingerprintWrapper] = None
    rm_template   = cfg.get("cloud", {}).get("radiomap_path", "radiomap_{campus}_{building}_{floor}.json")
    radiomap_path = cloud_io.resolve_radiomap_path(rm_template, env.campus_id, env.building_id, env.floor_id)
    try:
        if radiomap_path and Path(radiomap_path).exists():
            fp_wrapper = FingerprintWrapper(radiomap_path)
            _msg = f"Fingerprinting enabled (radiomap: {radiomap_path})"
            log.config(_msg); _append_log("CONFIG", "", _msg)
        else:
            _msg = f"Fingerprinting disabled (no radiomap at {radiomap_path})"
            log.config(_msg); _append_log("CONFIG", "", _msg)
    except Exception as _e:
        _msg = f"[Poll startup] FAILED to load FingerprintWrapper: {type(_e).__name__}: {_e}"
        log.error(_msg); _append_log("ERROR", "", _msg)

    gp_wrapper: Optional[SGPWrapper] = None
    try:
        if radiomap_path and Path(radiomap_path).exists():
            gp_cfg = cfg.get("system", {}).get("sparse_gp", {})
            gp_wrapper = SGPWrapper(
                radiomap_path   = radiomap_path,
                env             = env,
                n_inducing      = int(gp_cfg.get("n_inducing", 30)),
                length_scale    = float(gp_cfg.get("length_scale", 12.0)),
                signal_variance = float(gp_cfg.get("signal_variance", 50.0)),
                noise_variance  = float(gp_cfg.get("noise_variance", 5.0)),
            )
            if gp_wrapper.trained:
                _msg = (
                    f"Sparse GP enabled  (N={gp_wrapper.n_training}, "
                    f"M={gp_wrapper.n_inducing} inducing pts, radiomap: {radiomap_path})"
                )
                log.config(_msg); _append_log("CONFIG", "", _msg)
            else:
                _msg = "Sparse GP loaded but not enough data to train — will be skipped"
                log.config(_msg); _append_log("CONFIG", "", _msg)
                gp_wrapper = None
        else:
            _msg = f"Sparse GP disabled (no radiomap at {radiomap_path})"
            log.config(_msg); _append_log("CONFIG", "", _msg)
    except Exception as _e:
        _msg = f"[Poll startup] FAILED to load SGPWrapper: {type(_e).__name__}: {_e}"
        log.error(_msg); _append_log("ERROR", "", _msg)

    tof_wrapper: Optional[ToFWrapper] = None
    if env.tof_anchors:
        tof_wrapper = ToFWrapper(anchor_map=env.tof_anchors)
        _msg = f"ToF enabled  ({len(env.tof_anchors)} anchor(s): {', '.join(env.tof_anchors.keys())})"
        log.config(_msg); _append_log("CONFIG", "", _msg)
    else:
        _msg = "ToF disabled (no tof_anchors configured)"
        log.config(_msg); _append_log("CONFIG", "", _msg)

    try:
        telnet_pipe = TelnetPipe(
            aps             = list(env.wifi_aps.values()),
            target_ssids    = target_ssids,
            prompts         = prompts,
            poll_interval_s = poll_interval,
        )
    except Exception as _e:
        _msg = f"[Poll startup] FAILED to build TelnetPipe: {type(_e).__name__}: {_e}"
        log.error(_msg); _append_log("ERROR", "", _msg)
        _state["poll_running"] = False; return

    mqtt_pipe = MQTTPipe(
        anchors      = list(env.tof_anchors.values()),
        broker_host  = cfg.get("mqtt", {}).get("broker_host", "localhost"),
        broker_port  = int(cfg.get("mqtt", {}).get("broker_port", 1883)),
        topic_prefix = cfg.get("mqtt", {}).get("topic_prefix", "capstone"),
        keepalive_s  = int(cfg.get("mqtt", {}).get("keepalive_s", 60)),
    )

    try:
        _msg = f"Connecting to {len(env.wifi_aps)} AP(s): {', '.join(env.wifi_aps.keys())}"
        log.config(_msg); _append_log("CONFIG", "", _msg)
        await telnet_pipe.connect()
        _msg = "Telnet pipe connected"
        log.config(_msg); _append_log("CONFIG", "", _msg)
    except Exception as _e:
        _msg = f"[Poll startup] FAILED to connect TelnetPipe: {type(_e).__name__}: {_e}"
        log.error(_msg); _append_log("ERROR", "", _msg)
        _state["poll_running"] = False; return

    try:
        await mqtt_pipe.connect()
    except Exception as _e:
        _msg = f"[Poll startup] MQTT connect failed (non-fatal): {type(_e).__name__}: {_e}"
        log.warning(_msg); _append_log("WARN", "", _msg)

    scan_buffer: list    = []
    last_verdict         = asyncio.get_event_loop().time()
    required_ap_ids      = set(env.wifi_aps.keys())
    min_aps_for_valid    = 1 if testing_mode else len(env.wifi_aps)

    _start_msg = (
        f"Poll loop started  poll={poll_interval:.1f}s  verdict_window={update_int:.0f}s"
        f"  testing={testing_mode}  targets={target_ssids}"
    )
    log.config(_start_msg)
    _append_log("CONFIG", "", _start_msg)

    async def _mqtt_consumer():
        async for meas in mqtt_pipe.stream():
            if tof_wrapper is None:
                continue
            tof_wrapper.ingest(meas)
            _ts_tof = _now()
            for _tag_id in tof_wrapper.tracked_tags():
                _result = tof_wrapper.locate(_tag_id)
                if _result is None:
                    continue
                _tx, _ty, _tconf = _result
                _state["tof_positions"][_tag_id] = {
                    "x":         _tx,
                    "y":         _ty,
                    "confidence": _tconf,
                    "timestamp": _ts_tof,
                }
                _tof_msg = (
                    f"[ToF] {_tag_id}  pos=({_tx:.1f}, {_ty:.1f}) cm"
                    f"  conf={_tconf:.2f}"
                )
                log.config(_tof_msg)
                _append_log("CONFIG", _tag_id, _tof_msg)

    mqtt_task = asyncio.ensure_future(_mqtt_consumer())

    try:
        async for rssi_map in telnet_pipe.stream():
            _state["scan_counter"] += 1
            n = _state["scan_counter"]
            _ts = _now()

            for _ap_id, _devs in rssi_map.items():
                for _ssid, _rssi in _devs.items():
                    _entry = _state["seen_ssids"].setdefault(
                        _ssid, {"last_seen": _ts, "signals": {}}
                    )
                    _entry["last_seen"]          = _ts
                    _entry["signals"][_ap_id]    = _rssi

            for ap_id, dev_map in rssi_map.items():
                for ssid in dev_map:
                    _state["device_status"][ssid] = {
                        "reachable": True, "last_scan": _ts
                    }

            _targets_seen = [s for s in target_ssids if any(s in d for d in rssi_map.values())]
            _targets_missing = [s for s in target_ssids if s not in _targets_seen]
            _scan_msg = (
                f"[Scan #{n}]  APs={len(rssi_map)}/{len(env.wifi_aps)}"
                + (f"  ✓ {', '.join(_targets_seen)}" if _targets_seen else "  — no targets seen")
                + (f"  ✗ missing: {', '.join(_targets_missing)}" if _targets_missing else "")
            )
            log.config(_scan_msg)
            _append_log("CONFIG", "", _scan_msg)

            for _ssid in target_ssids:
                _vec = {
                    _ap: _dat[_ssid]
                    for _ap, _dat in rssi_map.items()
                    if _ssid in _dat
                }
                if _vec:
                    _vec_msg = (
                        f"[Scan #{n}] {_ssid}  "
                        + "  ".join(f"{k}:{v:+.0f}" for k, v in sorted(_vec.items()))
                    )
                    log.config(_vec_msg)
                    _append_log("CONFIG", _ssid, _vec_msg)

            present  = len(rssi_map)
            expected = len(env.wifi_aps)
            _state["raw"] = {
                "timestamp":    _ts,
                "scan_number":  n,
                "aps_present":  present,
                "aps_expected": expected,
                "complete":     present >= min_aps_for_valid,
                "testing_mode": testing_mode,
                "results":      rssi_map,
            }

            for d in rssi_wrapper.process_cycle(rssi_map, scan_number=n):
                _prev_msg = (
                    f"[Scan #{n}][SMA] {d.device_id} → {d.room_id}"
                    f"  conf={d.confidence:.2f}  pos=({d.x:.1f}, {d.y:.1f})"
                )
                log.config(_prev_msg)
                _append_log("CONFIG", d.device_id, _prev_msg)
                _state["rssi_positions"][d.device_id] = {
                    "x": d.x, "y": d.y, "room_id": d.room_id, "timestamp": _ts,
                }

            if raw_wrapper:
                for d in raw_wrapper.process_cycle(rssi_map, scan_number=n):
                    _raw_msg = (
                        f"[Scan #{n}][RAW] {d.device_id} → {d.room_id}"
                        f"  conf={d.confidence:.2f}  pos=({d.x:.1f}, {d.y:.1f})"
                    )
                    log.config(_raw_msg)
                    _append_log("CONFIG", d.device_id, _raw_msg)
                    _state["raw_positions"][d.device_id] = {
                        "x": d.x, "y": d.y, "room_id": d.room_id, "timestamp": _ts,
                    }

            if kalman_wrapper:
                for d in kalman_wrapper.process_cycle(rssi_map, scan_number=n):
                    _kal_msg = (
                        f"[Scan #{n}][KAL] {d.device_id} → {d.room_id}"
                        f"  conf={d.confidence:.2f}  pos=({d.x:.1f}, {d.y:.1f})"
                    )
                    log.config(_kal_msg)
                    _append_log("CONFIG", d.device_id, _kal_msg)
                    _state["kalman_positions"][d.device_id] = {
                        "x": d.x, "y": d.y, "room_id": d.room_id, "timestamp": _ts,
                    }

            if gp_wrapper and gp_wrapper.trained:
                for _ssid in target_ssids:
                    _gp_vec = {
                        _ap: _dat[_ssid]
                        for _ap, _dat in rssi_map.items()
                        if _ssid in _dat
                    }
                    if _gp_vec:
                        _gp_result = gp_wrapper.predict_with_room(_gp_vec)
                        if _gp_result:
                            _gp_x, _gp_y, _gp_room, _gp_conf = _gp_result
                            _gp_msg = (
                                f"[Scan #{n}][GP] {_ssid} → {_gp_room}"
                                f"  conf={_gp_conf:.2f}  pos=({_gp_x:.1f}, {_gp_y:.1f})"
                            )
                            log.config(_gp_msg)
                            _append_log("CONFIG", _ssid, _gp_msg)
                            _state["gp_positions"][_ssid] = {
                                "x":         _gp_x,
                                "y":         _gp_y,
                                "room_id":   _gp_room,
                                "timestamp": _ts,
                            }

            scan_buffer.append(rssi_map)

            if asyncio.get_event_loop().time() - last_verdict >= update_int:
                await _run_verdict(
                    scan_buffer, env,
                    rssi_wrapper, raw_wrapper, kalman_wrapper,
                    fp_wrapper, gp_wrapper, tof_wrapper,
                    target_ssids, required_ap_ids, min_aps_for_valid, n,
                )
                scan_buffer  = []
                last_verdict = asyncio.get_event_loop().time()

    except asyncio.CancelledError:
        _msg = "Poll loop cancelled"
        log.info(_msg); _append_log("INFO", "", _msg)
    except Exception as _e:
        import traceback as _tb
        _msg = f"[Poll loop CRASHED] {type(_e).__name__}: {_e}\n{_tb.format_exc()}"
        log.error(_msg); _append_log("ERROR", "", _msg)
    finally:
        mqtt_task.cancel()
        await telnet_pipe.close()
        await mqtt_pipe.close()
        _state["poll_running"] = False


async def _run_verdict(
    scan_buffer:       list,
    env:               FloorEnvironment,
    rssi_wrapper:      RSSIEngineWrapper,
    raw_wrapper:       Optional[RawRSSIEngineWrapper],
    kalman_wrapper:    Optional[KalmanRSSIEngineWrapper],
    fp_wrapper:        Optional[FingerprintWrapper],
    gp_wrapper:        Optional[SGPWrapper],
    tof_wrapper:       Optional[ToFWrapper],
    target_ssids:      List[str],
    required_ap_ids:   set,
    min_aps_for_valid: int,
    scan_counter:      int,
) -> None:
    timestamp = _now()
    log.config("=" * 55 + "  VERDICT")

    for ssid in target_ssids:
        complete_scans = [
            s for s in scan_buffer
            if sum(1 for ap, devs in s.items() if ssid in devs) >= min_aps_for_valid
        ]

        total  = len(scan_buffer)
        valid  = len(complete_scans)
        health = valid / total if total else 0.0
        _health_msg = f"[Verdict] {ssid}  health={int(health*100)}%  ({valid}/{total} valid scans)"
        log.config(_health_msg)
        _append_log("CONFIG", ssid, _health_msg)

        if health < 0.5:
            _warn = f"[Verdict] {ssid}  WARNING: less than 50% of scans complete"
            log.warning(_warn)
            _append_log("WARN", ssid, _warn)
        if not complete_scans:
            _warn = f"[Verdict] {ssid}  no valid scans — skipping verdict"
            log.warning(_warn)
            _append_log("WARN", ssid, _warn)
            continue

        averaged: Dict[str, Dict[str, float]] = defaultdict(dict)
        for ap_id in required_ap_ids:
            vals = [s[ap_id][ssid] for s in complete_scans if ssid in s.get(ap_id, {})]
            if vals:
                averaged[ap_id][ssid] = sum(vals) / len(vals)
        averaged = dict(averaged)

        # ── SMA trilateration verdict ─────────────────────────────────────
        dec_id = str(uuid.uuid4())
        for d in rssi_wrapper.process_cycle(
            averaged,
            scan_number = scan_counter,
            timestamp   = timestamp,
            decision_id = dec_id,
        ):
            _store_decision(d, "rssi_decisions")

        # ── Raw trilateration verdict ─────────────────────────────────────
        if raw_wrapper:
            for d in raw_wrapper.process_cycle(
                averaged,
                scan_number = scan_counter,
                timestamp   = timestamp,
            ):
                _store_decision(d, "raw_decisions")

        # ── Kalman trilateration verdict ──────────────────────────────────
        if kalman_wrapper:
            for d in kalman_wrapper.process_cycle(
                averaged,
                scan_number = scan_counter,
                timestamp   = timestamp,
            ):
                _store_decision(d, "kalman_decisions")

        # ── Fingerprinting verdict ────────────────────────────────────────
        if fp_wrapper:
            live_vec = {
                ap_id: averaged[ap_id][ssid]
                for ap_id in averaged if ssid in averaged.get(ap_id, {})
            }
            room, conf = fp_wrapper.match(live_vec)
            if room not in ("Outside Defined Area", "Undetected"):
                fp_x, fp_y = 0.0, 0.0
                matched_room = next(
                    (r for r in env.rooms if r.name == room), None
                )
                if matched_room and matched_room.polygon:
                    pts = matched_room.polygon
                    fp_x = sum(p[0] for p in pts) / len(pts)
                    fp_y = sum(p[1] for p in pts) / len(pts)

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
                    x           = fp_x,
                    y           = fp_y,
                    scan_number = scan_counter,
                )
                _store_decision(d, "fp_decisions")

        # ── Sparse GP verdict ─────────────────────────────────────────────
        if gp_wrapper and gp_wrapper.trained:
            live_vec = {
                ap_id: averaged[ap_id][ssid]
                for ap_id in averaged if ssid in averaged.get(ap_id, {})
            }
            gp_result = gp_wrapper.predict_with_room(live_vec)
            if gp_result is not None:
                gp_x, gp_y, gp_room, gp_conf = gp_result
                if gp_room not in ("Undetected",):
                    d = LocalizationDecision(
                        decision_id = str(uuid.uuid4()),
                        device_id   = ssid,
                        campus_id   = env.campus_id,
                        building_id = env.building_id,
                        floor_id    = env.floor_id,
                        room_id     = gp_room,
                        timestamp   = timestamp,
                        confidence  = gp_conf,
                        rssi_vector = live_vec,
                        x           = gp_x,
                        y           = gp_y,
                        scan_number = scan_counter,
                    )
                    _store_decision(d, "gp_decisions")

    # ── ToF verdict ───────────────────────────────────────────────────────
    if tof_wrapper:
        for tag_id in tof_wrapper.tracked_tags():
            tof_result = tof_wrapper.locate(tag_id)
            if tof_result is None:
                continue
            tof_x, tof_y, tof_conf = tof_result

            tof_room = "Undetected"
            for r in env.rooms:
                poly = r.polygon
                if poly and _point_in_polygon(tof_x, tof_y, poly):
                    tof_room = r.name
                    break
            if tof_room == "Undetected" and env.rooms:
                best = min(
                    env.rooms,
                    key=lambda r: (r.center_x - tof_x)**2 + (r.center_y - tof_y)**2,
                )
                tof_room = best.name

            d = LocalizationDecision(
                decision_id = str(uuid.uuid4()),
                device_id   = tag_id,
                campus_id   = env.campus_id,
                building_id = env.building_id,
                floor_id    = env.floor_id,
                room_id     = tof_room,
                timestamp   = timestamp,
                confidence  = tof_conf,
                rssi_vector = {},
                x           = tof_x,
                y           = tof_y,
                scan_number = scan_counter,
            )
            _store_decision(d, "tof_decisions")

    # ── Forward all new decisions to Beanstalk for the React frontend ─────
    new_decisions = (
        _state["rssi_decisions"][-1:]
        + _state["raw_decisions"][-1:]
        + _state["kalman_decisions"][-1:]
        + _state["fp_decisions"][-1:]
    )
    if new_decisions:
        forward_decisions(new_decisions)


def _point_in_polygon(x: float, y: float, poly: list) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.get("/health")
def get_health():
    return {"ok": True, "time": _now(), "poll_running": _state["poll_running"]}


@app.post("/poll/start")
async def start_poll():
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


@app.get("/decisions")
def get_decisions(limit: int = 50):
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


@app.get("/decisions/raw")
def get_decisions_raw(limit: int = Query(50, ge=1, le=500)):
    items = _state["raw_decisions"][-limit:]
    return {"method": "raw", "count": len(items), "items": items}


@app.get("/decisions/kalman")
def get_decisions_kalman(limit: int = Query(50, ge=1, le=500)):
    items = _state["kalman_decisions"][-limit:]
    return {"method": "kalman", "count": len(items), "items": items}


@app.get("/decisions/fingerprinting")
def get_decisions_fingerprinting(limit: int = Query(50, ge=1, le=500)):
    items = _state["fp_decisions"][-limit:]
    return {"method": "fingerprinting", "count": len(items), "items": items}


@app.get("/decisions/gp")
def get_decisions_gp(limit: int = Query(50, ge=1, le=500)):
    items = _state["gp_decisions"][-limit:]
    return {"method": "gp", "count": len(items), "items": items}


@app.get("/decisions/ble")
def get_decisions_ble(limit: int = Query(50, ge=1, le=500)):
    items = _state["ble_decisions"][-limit:]
    return {
        "method":  "ble",
        "count":   len(items),
        "items":   items,
        "note":    "BLE pipeline not yet active; data populated externally when BLE anchors are present.",
    }


@app.get("/decisions/tof")
def get_decisions_tof(limit: int = Query(50, ge=1, le=500)):
    items = _state["tof_decisions"][-limit:]
    return {"method": "tof", "count": len(items), "items": items}


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


@app.get("/raw")
def get_raw():
    if not _state["raw"]:
        raise HTTPException(404, "No raw scans yet")
    return _state["raw"]


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


@app.get("/map")
def get_map(method: str = Query("trilateration", description="trilateration | fingerprinting | gp | ble | tof")):
    env: Optional[FloorEnvironment] = _state.get("env")
    rooms   = []
    floor_w = floor_h = None

    if env:
        floor_w = env.width_m
        floor_h = env.height_m
        rooms   = [{"name": r.name, "polygon": r.polygon} for r in env.rooms]

    pos_map = {
        "trilateration":  _state["rssi_positions"],
        "raw":            _state["raw_positions"],
        "kalman":         _state["kalman_positions"],
        "fingerprinting": _state["fp_positions"],
        "gp":             _state["gp_positions"],
        "tof":            _state["tof_positions"],
        "ble":            {},
    }.get(method, _state["rssi_positions"])

    devices = [
        {
            "device_id": dev, **pos,
            "reachable": _state["device_status"].get(dev, {}).get("reachable", False),
            "method":    method,
        }
        for dev, pos in pos_map.items()
    ]

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


# ── Survey ────────────────────────────────────────────────────────────────────

_survey_state: Dict[str, Any] = {
    "running":           False,
    "room_label":        None,
    "target_ssid":       None,
    "total_samples":     0,
    "collected_samples": 0,
    "status":            "idle",
    "error":             None,
}
_survey_task: Optional[asyncio.Task] = None


@app.post("/survey/start")
async def survey_start(
    room_label:  str,
    target_ssid: Optional[str] = None,
    samples:     int = Query(10, ge=1, le=60),
):
    global _survey_task
    if _state["poll_running"]:
        raise HTTPException(409, "Cannot survey while localization is running — POST /poll/stop first")
    if _survey_state["running"]:
        raise HTTPException(409, "A survey is already in progress")

    cfg = _state.get("cfg")
    if cfg is None:
        cfg = _load_cfg_from_disk()
        if cfg:
            _apply_cfg(cfg)
    env: Optional[FloorEnvironment] = _state.get("env")
    if cfg is None or env is None:
        raise HTTPException(503, "No config / location loaded — POST /config/reload first")

    if not target_ssid:
        targets = [t.ssid for t in env.targets]
        if not targets:
            raise HTTPException(400, "No target SSIDs in config — specify target_ssid= query parameter")
        target_ssid = targets[0]

    _survey_state.update({
        "running":           True,
        "room_label":        room_label,
        "target_ssid":       target_ssid,
        "total_samples":     samples,
        "collected_samples": 0,
        "status":            "running",
        "error":             None,
    })
    _survey_task = asyncio.create_task(
        _run_survey(room_label, target_ssid, samples),
        name="survey_loop",
    )
    log.info("Survey started — room=%r  ssid=%r  n=%d", room_label, target_ssid, samples)
    return {
        "ok":            True,
        "room_label":    room_label,
        "target_ssid":   target_ssid,
        "total_samples": samples,
    }


@app.get("/survey/status")
def survey_status():
    total = _survey_state["total_samples"] or 1
    return {
        **_survey_state,
        "progress_pct": int(_survey_state["collected_samples"] / total * 100),
    }


@app.post("/survey/cancel")
async def survey_cancel():
    global _survey_task
    if _survey_task and not _survey_task.done():
        _survey_task.cancel()
        try:
            await _survey_task
        except asyncio.CancelledError:
            pass
    _survey_state["running"] = False
    _survey_state["status"]  = "cancelled"
    return {"ok": True}


@app.get("/survey/radiomap")
def get_survey_radiomap():
    cfg = _state.get("cfg") or {}
    env: Optional[FloorEnvironment] = _state.get("env")
    if env is None:
        raise HTTPException(503, "No location selected — POST /config/reload first")

    rm_template   = cfg.get("cloud", {}).get("radiomap_path", "radiomap_{campus}_{building}_{floor}.json")
    radiomap_path = cloud_io.resolve_radiomap_path(rm_template, env.campus_id, env.building_id, env.floor_id)

    if not radiomap_path or not Path(radiomap_path).exists():
        return {"rooms": {}, "path": radiomap_path, "exists": False, "total_rooms": 0}

    try:
        data = json.loads(Path(radiomap_path).read_text(encoding="utf-8"))
        rooms_summary = {
            room: {
                "sample_count": len(vectors),
                "ap_count":     len({k for vec in vectors for k in vec}),
            }
            for room, vectors in data.items()
            if isinstance(vectors, list)
        }
        return {
            "rooms":       rooms_summary,
            "path":        radiomap_path,
            "exists":      True,
            "total_rooms": len(rooms_summary),
        }
    except Exception as exc:
        raise HTTPException(500, f"Failed to read radiomap: {exc}")


@app.get("/survey/radiomap/{room_label}")
def get_survey_room_vectors(room_label: str):
    cfg = _state.get("cfg") or {}
    env: Optional[FloorEnvironment] = _state.get("env")
    if env is None:
        raise HTTPException(503, "No location selected — POST /config/reload first")

    rm_template   = cfg.get("cloud", {}).get("radiomap_path", "radiomap_{campus}_{building}_{floor}.json")
    radiomap_path = cloud_io.resolve_radiomap_path(rm_template, env.campus_id, env.building_id, env.floor_id)

    if not radiomap_path or not Path(radiomap_path).exists():
        raise HTTPException(404, "Radiomap file not found")

    try:
        data = json.loads(Path(radiomap_path).read_text(encoding="utf-8"))
        if room_label not in data:
            raise HTTPException(404, f"Room '{room_label}' not found in radiomap")
        vectors = data[room_label]
        ap_keys = {k for v in vectors for k in v}
        averages = {
            ap: round(sum(v[ap] for v in vectors if ap in v) / sum(1 for v in vectors if ap in v), 1)
            for ap in ap_keys
        }
        return {"room": room_label, "count": len(vectors), "vectors": vectors, "averages": averages}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to read radiomap: {exc}")


@app.get("/seen_ssids")
def get_seen_ssids(max_age_s: int = 120):
    from datetime import timezone as _tz
    cutoff_ts = datetime.now(_tz.utc).timestamp() - max_age_s
    fresh = {}
    for ssid, entry in _state["seen_ssids"].items():
        try:
            seen_ts = datetime.fromisoformat(entry["last_seen"]).timestamp()
        except Exception:
            continue
        if seen_ts >= cutoff_ts:
            fresh[ssid] = entry
    return {"ssids": fresh, "count": len(fresh), "max_age_s": max_age_s}


@app.delete("/survey/radiomap/{room_label}")
def delete_survey_room(room_label: str):
    cfg = _state.get("cfg") or {}
    env: Optional[FloorEnvironment] = _state.get("env")
    if env is None:
        raise HTTPException(503, "No location selected — POST /config/reload first")

    rm_template   = cfg.get("cloud", {}).get("radiomap_path", "radiomap_{campus}_{building}_{floor}.json")
    radiomap_path = cloud_io.resolve_radiomap_path(rm_template, env.campus_id, env.building_id, env.floor_id)

    if not radiomap_path or not Path(radiomap_path).exists():
        raise HTTPException(404, "Radiomap file not found")

    try:
        data = json.loads(Path(radiomap_path).read_text(encoding="utf-8"))
        if room_label not in data:
            raise HTTPException(404, f"Room '{room_label}' not found in radiomap")
        del data[room_label]
        Path(radiomap_path).write_text(json.dumps(data, indent=2), encoding="utf-8")
        return {"ok": True, "deleted_room": room_label, "remaining_rooms": len(data)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to update radiomap: {exc}")


@app.get("/survey/targets")
def survey_targets():
    env: Optional[FloorEnvironment] = _state.get("env")
    if env is None:
        return {"targets": []}
    return {"targets": [t.ssid for t in env.targets]}


@app.post("/survey/{room_label}")
def post_survey(room_label: str, payload: Dict[str, Any]):
    cfg  = _state.get("cfg") or {}
    env: Optional[FloorEnvironment] = _state.get("env")
    if env is None:
        raise HTTPException(503, "No location selected — POST /config/reload first")

    rm_template   = cfg.get("cloud", {}).get("radiomap_path", "radiomap_{campus}_{building}_{floor}.json")
    radiomap_path = cloud_io.resolve_radiomap_path(rm_template, env.campus_id, env.building_id, env.floor_id)
    cloud_io.save_radiomap(room_label, payload, radiomap_path)
    return {"ok": True, "room": room_label, "file": radiomap_path, "samples_added": 1}


async def _run_survey(room_label: str, target_ssid: str, total_samples: int) -> None:
    pipe: Optional[TelnetPipe] = None
    try:
        cfg  = _state.get("cfg") or {}
        env: Optional[FloorEnvironment] = _state.get("env")

        if env is None:
            raise RuntimeError("No floor environment loaded — POST /config/reload first")

        tc      = cfg.get("telemetry_config", {})
        prompts = tc.get("prompts", {"main": "eap350>", "sub": "eap350/wless2/network>"})

        rm_template   = cfg.get("cloud", {}).get("radiomap_path", "radiomap_{campus}_{building}_{floor}.json")
        radiomap_path = cloud_io.resolve_radiomap_path(
            rm_template, env.campus_id, env.building_id, env.floor_id
        )

        wifi_aps = list(env.wifi_aps.values())
        log.info(
            "[Survey] Starting — room=%r  ssid=%r  samples=%d  radiomap=%s",
            room_label, target_ssid, total_samples, radiomap_path,
        )

        if not wifi_aps:
            raise RuntimeError("No WiFi APs in the loaded environment")

        pipe = TelnetPipe(
            aps             = wifi_aps,
            target_ssids    = [target_ssid],
            prompts         = prompts,
            poll_interval_s = 2.0,
        )

        await pipe.connect()
        log.info("[Survey] TelnetPipe ready — collecting %d samples", total_samples)

        async for rssi_map in pipe.stream():
            sample_n = _survey_state["collected_samples"] + 1
            vector = {
                ap_id: ap_data[target_ssid]
                for ap_id, ap_data in rssi_map.items()
                if target_ssid in ap_data
            }

            if not vector:
                log.debug("[Survey] No data for %r this cycle — skipping", target_ssid)
            else:
                log.info("[Survey #%d/%d] room=%r  vector=%s", sample_n, total_samples, room_label, vector)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None, cloud_io.save_radiomap, room_label, vector, radiomap_path,
                )
                _survey_state["collected_samples"] += 1

            if _survey_state["collected_samples"] >= total_samples:
                break

        _survey_state["status"] = "done"
        log.info("[Survey] Complete — %d/%d vectors saved for room %r",
                 _survey_state["collected_samples"], total_samples, room_label)

    except asyncio.CancelledError:
        _survey_state["status"] = "cancelled"
        log.info("[Survey] Cancelled — saved %d/%d samples",
                 _survey_state["collected_samples"], total_samples)
        raise
    except Exception as exc:
        _survey_state["status"] = "error"
        _survey_state["error"]  = str(exc)
        log.error("[Survey] Error — %s: %s", type(exc).__name__, exc, exc_info=True)
    finally:
        if pipe is not None:
            try:
                await pipe.close()
            except Exception:
                pass
        _survey_state["running"] = False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
