"""
Routes for config.yaml generation and engine management.

POST /api/v1/config/generate  — build config.yaml from DB state
POST /api/v1/engine/start     — generate config + start Docker container
POST /api/v1/engine/stop      — stop the running engine container
GET  /api/v1/engine/status    — check if engine is running
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
import yaml

from models import get_db
from config_gen.generator import generate_config
from engine_mgr.docker_ctl import start_engine, stop_engine, engine_status

router = APIRouter(prefix="/api/v1", tags=["config"])


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
    campus_id: int,
    building: Optional[str] = None,
    floor: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Generate config, then start the Hybrid engine Docker container."""
    try:
        cfg = generate_config(db, campus_id, building, floor)
    except ValueError as e:
        raise HTTPException(404, str(e))

    result = start_engine(cfg)
    return result


@router.post("/engine/stop")
def stop():
    """Stop the running engine container."""
    return stop_engine()


@router.get("/engine/status")
def status():
    """Check engine container status."""
    return engine_status()
