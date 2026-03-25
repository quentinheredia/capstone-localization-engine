"""
Routes for config.yaml generation and engine management.

POST /api/v1/config/generate  — build config.yaml from DB state
POST /api/v1/engine/start     — generate config + start Docker container
POST /api/v1/engine/stop      — stop the running engine container
GET  /api/v1/engine/status    — check if engine is running
"""

import logging
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

    # Log the outcome
    if result.get("ok"):
        _log_engine_event(db, "info",
            f"Engine started — container '{result.get('container', '?')}' on port {result.get('port', '?')}",
            {"campus_id": campus_id, "methods": methods, "poll_interval_s": poll_interval_s,
             "testing_mode": testing_mode, **result})
    else:
        _log_engine_event(db, "security",
            f"Engine failed to start: {result.get('error', 'unknown error')}",
            {"campus_id": campus_id, **result})

    return result


@router.post("/engine/stop")
def stop(db: Session = Depends(get_db)):
    """Stop the running engine container."""
    result = stop_engine()
    if result.get("ok"):
        _log_engine_event(db, "info", f"Engine stopped — container '{result.get('container', '?')}'", result)
    else:
        _log_engine_event(db, "warn", f"Engine stop issue: {result}", result)
    return result


@router.get("/engine/status")
def status():
    """Check engine container status."""
    return engine_status()
