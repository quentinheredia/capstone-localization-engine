"""
Floor Plan Editor API — save/load editor state, PDF background, copy, access levels.

Endpoints:
  GET    /floors/{id}/editor-data               Load editor state (points, lines, rooms)
  PUT    /floors/{id}/editor-data               Save editor state
  POST   /floors/{id}/editor-pdf                Upload PDF background
  GET    /floors/{id}/editor-pdf                Serve PDF file
  DELETE /floors/{id}/editor-pdf                Remove PDF
  POST   /floors/{id}/editor-sync-rooms         Sync editor rooms → database Room records
  POST   /floors/{id}/copy-from/{src_floor_id}  Copy floorplan from another floor

  POST   /buildings/{bid}/access-levels         Create access level
  GET    /buildings/{bid}/access-levels         List access levels
  PATCH  /access-levels/{id}                    Update access level
  DELETE /access-levels/{id}                    Delete access level
"""

from __future__ import annotations

import json
import os
import shutil
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from models import get_db, Floor, Room

log = logging.getLogger("floorplan-editor")

router = APIRouter(prefix="/api/v1", tags=["floorplan-editor"])

# Directory for PDF background files
PDF_DIR = Path(__file__).resolve().parent.parent / "floorplan_pdfs"
PDF_DIR.mkdir(exist_ok=True)


# ── Helper ────────────────────────────────────────────────────────────────

def _get_floor(floor_id: int, db: Session) -> Floor:
    floor = db.query(Floor).filter(Floor.id == floor_id).first()
    if not floor:
        raise HTTPException(404, f"Floor {floor_id} not found")
    return floor


# ── Editor Data ───────────────────────────────────────────────────────────

@router.get("/floors/{floor_id}/editor-data")
def get_editor_data(floor_id: int, db: Session = Depends(get_db)):
    """Return the full editor state for a floor (points, lines, room defs, settings)."""
    floor = _get_floor(floor_id, db)
    return {
        "floor_id": floor.id,
        "width_m": floor.width_m,
        "height_m": floor.height_m,
        "default_grid_size_m": floor.default_grid_size_m,
        "has_pdf": bool(floor.pdf_path and os.path.isfile(floor.pdf_path)),
        "data": floor.floorplan_data or {
            "points": [],
            "lines": [],
            "rooms": [],
        },
    }


@router.put("/floors/{floor_id}/editor-data")
def save_editor_data(floor_id: int, payload: dict, db: Session = Depends(get_db)):
    """Persist the full editor state and optionally update floor dimensions."""
    floor = _get_floor(floor_id, db)

    if "data" in payload:
        floor.floorplan_data = payload["data"]
    if "width_m" in payload:
        floor.width_m = float(payload["width_m"])
    if "height_m" in payload:
        floor.height_m = float(payload["height_m"])
    if "default_grid_size_m" in payload:
        floor.default_grid_size_m = float(payload["default_grid_size_m"])

    db.commit()
    db.refresh(floor)
    return {"ok": True, "floor_id": floor.id}


# ── Room sync (editor rooms → database Room records) ─────────────────────

@router.post("/floors/{floor_id}/editor-sync-rooms")
def sync_rooms(floor_id: int, payload: dict, db: Session = Depends(get_db)):
    """
    Receive the rooms array from the editor and synchronize database Room
    records for this floor.  Rooms not in the editor list are deleted.

    Payload: { "rooms": [ { "editor_id": "r1", "name": "...", "polygon": [...],
                             "access_level_id": null, "grid_size_override": null } ] }
    """
    floor = _get_floor(floor_id, db)
    editor_rooms = payload.get("rooms", [])

    # Delete existing rooms on this floor
    db.query(Room).filter(Room.floor_id == floor_id).delete()
    db.flush()

    created = []
    for er in editor_rooms:
        polygon = er.get("polygon", [])
        if len(polygon) < 3:
            continue

        # Compute centroid
        cx = sum(p[0] for p in polygon) / len(polygon)
        cy = sum(p[1] for p in polygon) / len(polygon)

        room = Room(
            floor_id=floor_id,
            name=er.get("name", "Unnamed"),
            polygon=polygon,
            center_x=round(cx, 3),
            center_y=round(cy, 3),
            access_level_id=er.get("access_level_id"),
            grid_size_override=er.get("grid_size_override"),
            priority_label=er.get("priority_label", "Standard"),
            localization_type=er.get("localization_type", "rssi_section"),
            alert_on_exit=er.get("alert_on_exit", False),
        )
        db.add(room)
        created.append(room)

    db.commit()
    return {"ok": True, "rooms_created": len(created)}


# ── PDF Background ────────────────────────────────────────────────────────

@router.post("/floors/{floor_id}/editor-pdf")
async def upload_pdf(floor_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload a PDF to use as the background tracing image."""
    floor = _get_floor(floor_id, db)

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")

    dest = PDF_DIR / f"floor_{floor_id}.pdf"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    floor.pdf_path = str(dest)
    db.commit()
    return {"ok": True, "path": str(dest)}


@router.get("/floors/{floor_id}/editor-pdf")
def get_pdf(floor_id: int, db: Session = Depends(get_db)):
    """Serve the PDF background file."""
    floor = _get_floor(floor_id, db)
    if not floor.pdf_path or not os.path.isfile(floor.pdf_path):
        raise HTTPException(404, "No PDF background uploaded for this floor")
    return FileResponse(floor.pdf_path, media_type="application/pdf")


@router.delete("/floors/{floor_id}/editor-pdf")
def delete_pdf(floor_id: int, db: Session = Depends(get_db)):
    """Remove the PDF background."""
    floor = _get_floor(floor_id, db)
    if floor.pdf_path and os.path.isfile(floor.pdf_path):
        os.remove(floor.pdf_path)
    floor.pdf_path = ""
    db.commit()
    return {"ok": True}


# ── Copy Floor Plan ──────────────────────────────────────────────────────

@router.post("/floors/{floor_id}/copy-from/{src_floor_id}")
def copy_floorplan(floor_id: int, src_floor_id: int, db: Session = Depends(get_db)):
    """
    Copy the floorplan data (points, lines, room defs) from another floor.
    Also copies the PDF background if one exists.  Does NOT copy database
    Room records — use editor-sync-rooms after reviewing the copy.
    """
    dest_floor = _get_floor(floor_id, db)
    src_floor = _get_floor(src_floor_id, db)

    # Copy editor data
    import copy
    dest_floor.floorplan_data = copy.deepcopy(src_floor.floorplan_data or {})
    dest_floor.default_grid_size_m = src_floor.default_grid_size_m

    # Copy PDF if exists
    if src_floor.pdf_path and os.path.isfile(src_floor.pdf_path):
        dest_pdf = PDF_DIR / f"floor_{floor_id}.pdf"
        shutil.copy2(src_floor.pdf_path, dest_pdf)
        dest_floor.pdf_path = str(dest_pdf)

    db.commit()
    return {
        "ok": True,
        "copied_from": src_floor_id,
        "has_data": bool(dest_floor.floorplan_data),
        "has_pdf": bool(dest_floor.pdf_path),
    }
