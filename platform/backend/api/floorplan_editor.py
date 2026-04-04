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
  POST   /floors/{id}/import-dxf                Parse DXF file → store wall segments
  POST   /floors/{id}/import-rooms-json         Import FLOOR_PLAN_V2 dict → Room records

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

# DXF unit conversion: AutoCAD typically uses inches for architectural drawings
# Scale factor: DXF units per metre  (39.3701 in/m for inch-based DXF files)
_DXF_DEFAULT_SCALE = 39.3701


# ── DXF helpers ───────────────────────────────────────────────────────────

def _parse_dxf_lines(content: str):
    """
    Extract LINE entities from a text DXF file.

    DXF files are sequential group-code / value pairs (one per line).
    We scan the ENTITIES section and collect every LINE entity's start/end
    coordinates (group codes 10/20 and 11/21).

    Returns a list of dicts: [{"x1": float, "y1": float, "x2": float, "y2": float}, ...]
    in raw DXF units (unconverted).
    """
    raw = content.splitlines()
    segments: list = []
    current: dict = {}
    in_entities = False
    i = 0

    while i < len(raw) - 1:
        code = raw[i].strip()
        val  = raw[i + 1].strip()
        i += 2

        if code == "2":
            if val.upper() == "ENTITIES":
                in_entities = True
                continue
            if val.upper() == "ENDSEC":
                in_entities = False
                continue

        if not in_entities:
            continue

        if code == "0":
            # Start of a new entity — flush previous LINE if any
            if current.get("type") == "LINE":
                _flush_dxf_line(current, segments)
            current = {"type": val}
        else:
            current[code] = val

    # Flush the last entity
    if current.get("type") == "LINE":
        _flush_dxf_line(current, segments)

    return segments


def _flush_dxf_line(d: dict, out: list) -> None:
    """Append one parsed LINE segment to *out*, ignoring malformed entries."""
    try:
        out.append({
            "x1": float(d.get("10", 0)),
            "y1": float(d.get("20", 0)),
            "x2": float(d.get("11", 0)),
            "y2": float(d.get("21", 0)),
        })
    except (ValueError, TypeError):
        pass


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


# ── DXF Import ────────────────────────────────────────────────────────────

@router.post("/floors/{floor_id}/import-dxf")
async def import_dxf(
    floor_id: int,
    file: UploadFile = File(...),
    scale: float = _DXF_DEFAULT_SCALE,
    db: Session = Depends(get_db),
):
    """
    Upload a DXF file and extract its wall (LINE) segments.

    The segments are converted from DXF units → metres using *scale*
    (default 39.3701 = inches per metre for AutoCAD architectural files).

    Coordinates are shifted so the floor origin is at (0, 0) and the
    floor dimensions (width_m, height_m) are updated from the DXF extents.

    The parsed walls are stored in floorplan_data["dxf_walls"] alongside
    the coordinate-shift metadata in floorplan_data["dxf_meta"] so that a
    subsequent import-rooms-json call can apply the same offset.
    """
    floor = _get_floor(floor_id, db)
    if not file.filename.lower().endswith(".dxf"):
        raise HTTPException(400, "Only .dxf files are accepted")

    content = (await file.read()).decode("utf-8", errors="replace")
    raw_segs = _parse_dxf_lines(content)

    if not raw_segs:
        raise HTTPException(422, "No LINE entities found in DXF file")

    # Compute bounding box in raw DXF units
    all_x = [s["x1"] for s in raw_segs] + [s["x2"] for s in raw_segs]
    all_y = [s["y1"] for s in raw_segs] + [s["y2"] for s in raw_segs]
    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)

    # Convert to metres, shift so origin = (0, 0)
    x_off_m = min_x / scale   # will be negative for drawings with negative coords
    y_off_m = min_y / scale
    width_m  = round((max_x - min_x) / scale, 3)
    height_m = round((max_y - min_y) / scale, 3)

    walls = [
        {
            "x1": round(s["x1"] / scale - x_off_m, 3),
            "y1": round(s["y1"] / scale - y_off_m, 3),
            "x2": round(s["x2"] / scale - x_off_m, 3),
            "y2": round(s["y2"] / scale - y_off_m, 3),
        }
        for s in raw_segs
    ]

    # Merge with existing floorplan_data (preserve editor points/lines/rooms)
    data = dict(floor.floorplan_data or {})
    data["dxf_walls"] = walls
    data["dxf_meta"] = {
        "x_offset_m": round(x_off_m, 6),
        "y_offset_m": round(y_off_m, 6),
        "source_scale": scale,
        "source_file": file.filename,
        "wall_count": len(walls),
    }

    floor.floorplan_data = data
    floor.width_m  = width_m
    floor.height_m = height_m
    db.commit()

    log.info(
        "DXF import: floor=%d file=%s walls=%d  %.1f×%.1f m",
        floor_id, file.filename, len(walls), width_m, height_m,
    )
    return {
        "ok": True,
        "walls": len(walls),
        "width_m": width_m,
        "height_m": height_m,
        "x_offset_m": round(x_off_m, 6),
        "y_offset_m": round(y_off_m, 6),
    }


@router.post("/floors/{floor_id}/import-rooms-json")
def import_rooms_json(
    floor_id: int,
    payload: dict,
    db: Session = Depends(get_db),
):
    """
    Import room definitions from a FLOOR_PLAN_V2-format dict.

    Expected payload::

        {
          "rooms": {
            "Lobby":    {"label": [cx, cy], "polygon": [[x1,y1],[x2,y2],...]},
            "Lab":      {"label": [cx, cy], "bounds": [xmin, ymin, xmax, ymax]},
            "Stairwell": {"label": [cx, cy], "pos": [cx, cy]}
          },
          "x_offset_m": 9.02,  // optional — auto-read from dxf_meta if omitted
          "y_offset_m": 9.28
        }

    Rooms with a "polygon" key get arbitrary polygon geometry.
    Rooms with a "bounds" key become rectangular Room records.
    Entries with only "pos" (exits, stairs) are skipped.

    The coordinate offset shifts raw blueprint coordinates into the same
    (0,0)-based system used by the imported DXF walls.  If *x_offset_m* /
    *y_offset_m* are omitted the offset is derived from the dxf_meta stored
    by a prior import-dxf call (negated, because dxf_meta stores the min
    coord of the drawing, and we want to add that amount to room coords).

    After creating DB Room records the rooms are also stored in
    floorplan_data["dxf_rooms"] so the floor-plan editor can render them.
    """
    floor = _get_floor(floor_id, db)
    rooms_dict = payload.get("rooms", {})

    # Derive coordinate offset from prior DXF import or explicit payload
    data = dict(floor.floorplan_data or {})
    meta = data.get("dxf_meta", {})

    if "x_offset_m" in payload:
        x_off = float(payload["x_offset_m"])
    else:
        # dxf_meta stores the minimum coord (negative for drawings with -x/-y).
        # Negate it to get the shift that maps raw blueprint coords → DXF space.
        x_off = -float(meta.get("x_offset_m", 0.0))

    if "y_offset_m" in payload:
        y_off = float(payload["y_offset_m"])
    else:
        y_off = -float(meta.get("y_offset_m", 0.0))

    # Delete existing rooms on this floor (clean slate)
    db.query(Room).filter(Room.floor_id == floor_id).delete()
    db.flush()

    created_names: list = []
    dxf_rooms_for_editor: list = []

    for name, info in rooms_dict.items():
        poly_raw = info.get("polygon")
        bounds = info.get("bounds")

        if poly_raw and len(poly_raw) >= 3:
            # Arbitrary polygon — apply offset to each vertex
            polygon = [
                [round(float(pt[0]) + x_off, 3), round(float(pt[1]) + y_off, 3)]
                for pt in poly_raw
            ]
        elif bounds and len(bounds) >= 4:
            # Rectangle from bounds
            xmin, ymin, xmax, ymax = (float(v) for v in bounds)
            xmin += x_off;  xmax += x_off
            ymin += y_off;  ymax += y_off
            polygon = [
                [round(xmin, 3), round(ymin, 3)],
                [round(xmax, 3), round(ymin, 3)],
                [round(xmax, 3), round(ymax, 3)],
                [round(xmin, 3), round(ymax, 3)],
            ]
        else:
            continue  # Skip exits/stairs that have only a "pos" marker

        cx = round(sum(p[0] for p in polygon) / len(polygon), 3)
        cy = round(sum(p[1] for p in polygon) / len(polygon), 3)

        room = Room(
            floor_id=floor_id,
            name=name,
            polygon=polygon,
            center_x=cx,
            center_y=cy,
            priority_label="Standard",
            localization_type="rssi_section",
            alert_on_exit=False,
        )
        db.add(room)
        created_names.append(name)
        dxf_rooms_for_editor.append({"name": name, "polygon": polygon})

    # Persist rooms for the editor overlay (separate from editor's point-based rooms)
    data["dxf_rooms"] = dxf_rooms_for_editor
    floor.floorplan_data = data
    db.commit()

    log.info("Room import: floor=%d  %d rooms created", floor_id, len(created_names))
    return {"ok": True, "rooms_created": len(created_names), "rooms": created_names}


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
