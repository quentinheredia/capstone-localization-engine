"""
Routes for config.yaml generation and engine management.

POST /api/v1/config/generate  — build config.yaml from DB state
POST /api/v1/engine/start     — generate config + start engine (Docker or desktop)
POST /api/v1/engine/stop      — stop the running engine
GET  /api/v1/engine/status    — check if engine is running

Desktop mode vs Docker mode
---------------------------
In Docker mode the engine runs as a container on port 8001 (host).
In desktop mode (desktop_app.py) the engine is a direct subprocess on port 8000.

Both modes are handled by:
  1. Writing the generated config.yaml to Hybrid/
  2. Attempting Docker start/stop (no-op / error if Docker not available)
  3. Always calling /config/reload + /poll/start|stop directly on the local
     engine subprocess (localhost:ENGINE_PORT).  This is the critical path
     for desktop mode and a useful fallback for Docker mode.
"""

import logging
import os
import requests as _http
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
import yaml

from models import get_db, EngineLog
from config_gen.generator import generate_config
from engine_mgr.docker_ctl import start_engine, stop_engine, engine_status

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["config"])

# Port the engine subprocess listens on in desktop mode
_ENGINE_PORT = int(os.environ.get("ENGINE_PORT", 8000))
_ENGINE_LOCAL = f"http://localhost:{_ENGINE_PORT}"


def _engine_call(
    method: str,
    path: str,
    timeout: float = 5.0,
    json: dict | None = None,
) -> dict | None:
    """
    Make an HTTP call to the local engine subprocess.
    Returns the parsed JSON on success, None on any error.

    Pass `json=` to send a JSON body (used for POST /config to push
    the generated payload directly without touching the disk file).

    On failure, returns a dict with ``{"ok": False, "error": ...}``
    containing the actual error details (connection refused, timeout,
    HTTP error code, etc.) so callers can surface meaningful messages.
    """
    try:
        url = f"{_ENGINE_LOCAL}{path}"
        r = _http.request(method, url, timeout=timeout, json=json)
        if r.status_code >= 400:
            body = r.text[:500] if r.text else "(empty body)"
            log.warning("Engine call %s %s returned HTTP %s: %s", method, path, r.status_code, body)
            return {"ok": False, "error": f"HTTP {r.status_code}: {body}"}
        return r.json() if r.content else {}
    except _http.ConnectionError:
        # Engine subprocess is not running — expected in standby mode.
        # engine_monitor (main.py) owns up/down transition logging; suppress
        # the per-call noise here so the console stays readable.
        log.debug("Engine call %s %s — connection refused (engine not running)", method, path)
        return {"ok": False, "error": f"Connection refused — engine subprocess not reachable at {_ENGINE_LOCAL}{path}"}
    except _http.Timeout:
        log.debug("Engine call %s %s — timed out after %.1fs (engine not running?)", method, path, timeout)
        return {"ok": False, "error": f"Timeout after {timeout}s — engine not responding"}
    except Exception as exc:
        log.warning("Engine call %s %s failed unexpectedly: %s (%s)", method, path, type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _log_engine_event(db: Session, level: str, message: str, meta: dict | None = None) -> None:
    """Write an engine lifecycle event to the EngineLog table."""
    try:
        db.add(EngineLog(
            level=level,
            source="system",
            message=message,
            meta=meta or {},
            timestamp=datetime.now(timezone.utc),
        ))
        db.commit()
    except Exception as exc:
        log.warning("Failed to log engine event: %s", exc)


class _GenerateRequest:
    """Query params for config generation."""
    def __init__(
        self,
        campus_id: int,
        building: Optional[str] = None,
        floor: Optional[str] = None,
    ):
        self.campus_id = campus_id
        self.building = building
        self.floor = floor


@router.post("/config/generate")
def generate(
    campus_id: int,
    building: Optional[str] = None,
    floor: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Generate config.yaml from the current database state.

    Returns the YAML as JSON (for preview) and writes it to disk
    at Hybrid/config.yaml so the engine can load it.
    """
    try:
        cfg = generate_config(db, campus_id, building, floor)
    except ValueError as e:
        raise HTTPException(404, str(e))

    # Write to Hybrid directory
    import os
    hybrid_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "Hybrid")
    config_path = os.path.join(hybrid_dir, "config.yaml")
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
    except Exception as exc:
        raise HTTPException(500, f"Failed to write config.yaml: {exc}")

    return {"ok": True, "config": cfg, "path": config_path}


@router.post("/engine/start")
def start(
    campus_id:       int,
    building:        Optional[str]   = None,
    floor:           Optional[str]   = None,
    poll_interval_s: Optional[float] = None,
    testing_mode:    Optional[bool]  = None,
    methods:         Optional[str]   = None,   # comma-sep e.g. "trilateration,fingerprinting"
    db: Session = Depends(get_db),
):
    """
    Generate config from the DB state, inject runtime overrides, then
    start the Hybrid engine Docker container.

    Extra query params (all optional):
      poll_interval_s  — override default scan interval (default 1.5 s)
      testing_mode     — accept scans with only 1 AP visible
      methods          — comma-separated method list (informational; engine
                         runs both trilateration and fingerprinting when a
                         radiomap is present regardless of this flag)
    """
    try:
        cfg = generate_config(db, campus_id, building, floor)
    except ValueError as e:
        raise HTTPException(404, str(e))

    # Inject runtime overrides into the generated config
    if poll_interval_s is not None:
        cfg.setdefault("telemetry_config", {})["poll_interval_s"] = poll_interval_s
    if testing_mode is not None:
        cfg.setdefault("system", {})["testing_mode"] = testing_mode
    if methods is not None:
        cfg.setdefault("system", {})["methods"] = [m.strip() for m in methods.split(",") if m.strip()]

    result = start_engine(cfg)

    # Push the fully-generated config directly to the engine subprocess.
    # We use POST /config (not /config/reload) so the engine receives the
    # freshly-built payload with real APs, rooms, and targets — NOT
    # whatever placeholder config.yaml happens to be on disk.
    # This is the critical path for desktop mode; it also acts as a
    # reliable fallback when Docker is unavailable.
    config_result = _engine_call("POST", "/config", json=cfg)

    # Only attempt poll/start if config was accepted
    if config_result and config_result.get("ok") is not False:
        poll_result = _engine_call("POST", "/poll/start")
    else:
        poll_result = {"ok": False, "error": "Skipped — config push failed"}

    # Merge local-engine results into the response
    result["local_config"] = config_result
    result["local_poll"]   = poll_result

    # Consider the start successful if Docker OR the local config+poll succeeded
    config_ok = bool(config_result and config_result.get("ok") is not False and "error" not in (config_result or {}))
    poll_ok   = bool(poll_result and poll_result.get("ok") is not False and "error" not in (poll_result or {}))
    local_ok  = config_ok and poll_ok
    overall_ok = result.get("ok") or local_ok
    result["ok"] = overall_ok

    # Build a human-readable failure detail string
    _fail_details = []
    if not config_ok:
        _fail_details.append(f"Config push: {(config_result or {}).get('error', 'no response')}")
    if not poll_ok:
        _fail_details.append(f"Poll start: {(poll_result or {}).get('error', 'no response')}")
    docker_err = result.get("error")
    if docker_err:
        _fail_details.append(f"Docker: {docker_err}")

    if overall_ok:
        _log_engine_event(db, "info",
            f"Engine started — container '{result.get('container', '?')}'; "
            f"local config+poll: {'started' if local_ok else 'skipped/failed'}",
            {"campus_id": campus_id, "methods": methods, "poll_interval_s": poll_interval_s,
             "testing_mode": testing_mode, **result})
    else:
        detail_str = "; ".join(_fail_details) if _fail_details else "unknown error"
        _log_engine_event(db, "security",
            f"Engine failed to start — {detail_str}",
            {"campus_id": campus_id, "fail_details": _fail_details, **result})

    return result


@router.post("/engine/load-config")
def load_config(
    campus_id:       int,
    building:        Optional[str] = None,
    floor:           Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Generate config from DB state and push it to the engine WITHOUT starting
    the poll loop.  Used by the Survey page so the engine knows its APs and
    targets before a survey is run, without activating localization.
    """
    try:
        cfg = generate_config(db, campus_id, building, floor)
    except ValueError as e:
        raise HTTPException(404, str(e))

    config_result = _engine_call("POST", "/config", json=cfg)
    ok = bool(config_result and config_result.get("ok") is not False and "error" not in (config_result or {}))

    if ok:
        _log_engine_event(db, "info",
            "Engine config loaded (poll not started)",
            {"campus_id": campus_id, "building": building, "floor": floor})
    else:
        err_detail = (config_result or {}).get("error", "no response from engine")
        _log_engine_event(db, "warn",
            f"Engine config load failed — {err_detail}",
            {"campus_id": campus_id, "error": err_detail})

    return {"ok": ok, "local_config": config_result}


@router.post("/engine/stop")
def stop(db: Session = Depends(get_db)):
    """Stop the running engine container and the local poll subprocess."""
    result = stop_engine()

    # Always stop the local engine poll as well (desktop mode + Docker fallback)
    poll_result = _engine_call("POST", "/poll/stop")
    result["local_poll"] = poll_result
    local_stopped = bool(poll_result and poll_result.get("ok"))

    if result.get("ok") or local_stopped:
        _log_engine_event(db, "info",
            f"Engine stopped — container '{result.get('container', '?')}'; "
            f"local poll: {'stopped' if local_stopped else 'skipped/failed'}",
            result)
    else:
        _log_engine_event(db, "warn", f"Engine stop issue: {result}", result)
    return result


@router.get("/engine/status")
def status():
    """
    Check engine status.

    Returns Docker container status AND local engine health.
    The local engine health is the authoritative source in desktop mode
    (where Docker is not used).
    """
    docker = engine_status()

    # Also probe the local engine subprocess directly
    health = _engine_call("GET", "/health", timeout=2.0)
    if health and health.get("ok") is not False and "error" not in (health or {}):
        docker["local_engine"] = "online"
        docker["poll_running"] = health.get("poll_running", False)
    else:
        docker["local_engine"] = "offline"
        docker["poll_running"] = False

    # "running" should reflect actual engine availability, not just Docker
    if not docker.get("running"):
        docker["running"] = docker["local_engine"] == "online"

    return docker
