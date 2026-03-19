"""
CRUD routes for the spatial hierarchy:  Campus → Building → Floor → Room

All routes nested under /api/v1/.
Floor plan uploads handled separately via /api/v1/.../floor_plan.
"""

from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from models import Campus, Building, Floor, Room, get_db
from api.schemas import (
    CampusCreate, CampusUpdate, CampusOut,
    BuildingCreate, BuildingUpdate, BuildingOut,
    FloorCreate, FloorUpdate, FloorOut,
    RoomCreate, RoomUpdate, RoomOut,
)

router = APIRouter(prefix="/api/v1", tags=["hierarchy"])


# ── Campus ────────────────────────────────────────────────────────────────────

@router.post("/campuses", response_model=CampusOut, status_code=201)
def create_campus(body: CampusCreate, db: Session = Depends(get_db)):
    campus = Campus(name=body.name, description=body.description)
    db.add(campus)
    db.commit()
    db.refresh(campus)
    return campus


@router.get("/campuses", response_model=List[CampusOut])
def list_campuses(db: Session = Depends(get_db)):
    return db.query(Campus).order_by(Campus.name).all()


@router.get("/campuses/{campus_id}", response_model=CampusOut)
def get_campus(campus_id: int, db: Session = Depends(get_db)):
    campus = db.get(Campus, campus_id)
    if not campus:
        raise HTTPException(404, "Campus not found")
    return campus


@router.patch("/campuses/{campus_id}", response_model=CampusOut)
def update_campus(campus_id: int, body: CampusUpdate, db: Session = Depends(get_db)):
    campus = db.get(Campus, campus_id)
    if not campus:
        raise HTTPException(404, "Campus not found")
    for key, val in body.model_dump(exclude_unset=True).items():
        setattr(campus, key, val)
    db.commit()
    db.refresh(campus)
    return campus


@router.delete("/campuses/{campus_id}", status_code=204)
def delete_campus(campus_id: int, db: Session = Depends(get_db)):
    campus = db.get(Campus, campus_id)
    if not campus:
        raise HTTPException(404, "Campus not found")
    db.delete(campus)
    db.commit()


# ── Building ──────────────────────────────────────────────────────────────────

@router.post("/campuses/{campus_id}/buildings", response_model=BuildingOut, status_code=201)
def create_building(campus_id: int, body: BuildingCreate, db: Session = Depends(get_db)):
    if not db.get(Campus, campus_id):
        raise HTTPException(404, "Campus not found")
    building = Building(campus_id=campus_id, name=body.name, description=body.description)
    db.add(building)
    db.commit()
    db.refresh(building)
    return building


@router.get("/campuses/{campus_id}/buildings", response_model=List[BuildingOut])
def list_buildings(campus_id: int, db: Session = Depends(get_db)):
    return db.query(Building).filter_by(campus_id=campus_id).order_by(Building.name).all()


@router.get("/buildings/{building_id}", response_model=BuildingOut)
def get_building(building_id: int, db: Session = Depends(get_db)):
    building = db.get(Building, building_id)
    if not building:
        raise HTTPException(404, "Building not found")
    return building


@router.patch("/buildings/{building_id}", response_model=BuildingOut)
def update_building(building_id: int, body: BuildingUpdate, db: Session = Depends(get_db)):
    building = db.get(Building, building_id)
    if not building:
        raise HTTPException(404, "Building not found")
    for key, val in body.model_dump(exclude_unset=True).items():
        setattr(building, key, val)
    db.commit()
    db.refresh(building)
    return building


@router.delete("/buildings/{building_id}", status_code=204)
def delete_building(building_id: int, db: Session = Depends(get_db)):
    building = db.get(Building, building_id)
    if not building:
        raise HTTPException(404, "Building not found")
    db.delete(building)
    db.commit()


# ── Floor ─────────────────────────────────────────────────────────────────────

@router.post("/buildings/{building_id}/floors", response_model=FloorOut, status_code=201)
def create_floor(building_id: int, body: FloorCreate, db: Session = Depends(get_db)):
    if not db.get(Building, building_id):
        raise HTTPException(404, "Building not found")
    floor = Floor(
        building_id=building_id,
        name=body.name,
        floor_number=body.floor_number,
        width_m=body.width_m,
        height_m=body.height_m,
        grid_rows=body.grid_rows,
        grid_cols=body.grid_cols,
    )
    db.add(floor)
    db.commit()
    db.refresh(floor)
    return floor


@router.get("/buildings/{building_id}/floors", response_model=List[FloorOut])
def list_floors(building_id: int, db: Session = Depends(get_db)):
    return db.query(Floor).filter_by(building_id=building_id).order_by(Floor.floor_number).all()


@router.get("/floors/{floor_id}", response_model=FloorOut)
def get_floor(floor_id: int, db: Session = Depends(get_db)):
    floor = db.get(Floor, floor_id)
    if not floor:
        raise HTTPException(404, "Floor not found")
    return floor


@router.patch("/floors/{floor_id}", response_model=FloorOut)
def update_floor(floor_id: int, body: FloorUpdate, db: Session = Depends(get_db)):
    floor = db.get(Floor, floor_id)
    if not floor:
        raise HTTPException(404, "Floor not found")
    for key, val in body.model_dump(exclude_unset=True).items():
        setattr(floor, key, val)
    db.commit()
    db.refresh(floor)
    return floor


@router.delete("/floors/{floor_id}", status_code=204)
def delete_floor(floor_id: int, db: Session = Depends(get_db)):
    floor = db.get(Floor, floor_id)
    if not floor:
        raise HTTPException(404, "Floor not found")
    db.delete(floor)
    db.commit()


# ── Room ──────────────────────────────────────────────────────────────────────

@router.post("/floors/{floor_id}/rooms", response_model=RoomOut, status_code=201)
def create_room(floor_id: int, body: RoomCreate, db: Session = Depends(get_db)):
    if not db.get(Floor, floor_id):
        raise HTTPException(404, "Floor not found")
    room = Room(
        floor_id=floor_id,
        name=body.name,
        priority_label=body.priority_label,
        localization_type=body.localization_type,
        center_x=body.center_x,
        center_y=body.center_y,
        polygon=body.polygon,
        alert_on_exit=body.alert_on_exit,
    )
    db.add(room)
    db.commit()
    db.refresh(room)
    return room


@router.get("/floors/{floor_id}/rooms", response_model=List[RoomOut])
def list_rooms(floor_id: int, db: Session = Depends(get_db)):
    return db.query(Room).filter_by(floor_id=floor_id).order_by(Room.name).all()


@router.get("/rooms/{room_id}", response_model=RoomOut)
def get_room(room_id: int, db: Session = Depends(get_db)):
    room = db.get(Room, room_id)
    if not room:
        raise HTTPException(404, "Room not found")
    return room


@router.patch("/rooms/{room_id}", response_model=RoomOut)
def update_room(room_id: int, body: RoomUpdate, db: Session = Depends(get_db)):
    room = db.get(Room, room_id)
    if not room:
        raise HTTPException(404, "Room not found")
    for key, val in body.model_dump(exclude_unset=True).items():
        setattr(room, key, val)
    db.commit()
    db.refresh(room)
    return room


@router.delete("/rooms/{room_id}", status_code=204)
def delete_room(room_id: int, db: Session = Depends(get_db)):
    room = db.get(Room, room_id)
    if not room:
        raise HTTPException(404, "Room not found")
    db.delete(room)
    db.commit()
