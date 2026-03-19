"""
Pydantic schemas for request/response validation.

Naming convention:
  *Create  — POST body
  *Update  — PATCH body (all fields optional)
  *Out     — response model
"""

from __future__ import annotations
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


# ── Campus ────────────────────────────────────────────────────────────────────

class CampusCreate(BaseModel):
    name: str
    description: str = ""

class CampusUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None

class CampusOut(BaseModel):
    id: int
    name: str
    description: str
    created_at: datetime
    class Config:
        from_attributes = True


# ── Building ──────────────────────────────────────────────────────────────────

class BuildingCreate(BaseModel):
    name: str
    description: str = ""

class BuildingUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None

class BuildingOut(BaseModel):
    id: int
    campus_id: int
    name: str
    description: str
    created_at: datetime
    class Config:
        from_attributes = True


# ── Floor ─────────────────────────────────────────────────────────────────────

class FloorCreate(BaseModel):
    name: str
    floor_number: int = 1
    width_m: float = 10.0
    height_m: float = 10.0
    grid_rows: int = 8
    grid_cols: int = 8

class FloorUpdate(BaseModel):
    name: Optional[str] = None
    floor_number: Optional[int] = None
    floor_plan_path: Optional[str] = None
    width_m: Optional[float] = None
    height_m: Optional[float] = None
    grid_rows: Optional[int] = None
    grid_cols: Optional[int] = None

class FloorOut(BaseModel):
    id: int
    building_id: int
    name: str
    floor_number: int
    floor_plan_path: str
    width_m: float
    height_m: float
    grid_rows: int
    grid_cols: int
    created_at: datetime
    class Config:
        from_attributes = True


# ── Room ──────────────────────────────────────────────────────────────────────

class RoomCreate(BaseModel):
    name: str
    priority_label: str = "Standard"
    localization_type: str = "rssi_section"  # "rssi_section" | "high_accuracy"
    center_x: float = 0.0
    center_y: float = 0.0
    polygon: list = Field(default_factory=list)  # [[x1,y1],[x2,y2],...]
    alert_on_exit: bool = False

class RoomUpdate(BaseModel):
    name: Optional[str] = None
    priority_label: Optional[str] = None
    localization_type: Optional[str] = None
    center_x: Optional[float] = None
    center_y: Optional[float] = None
    polygon: Optional[list] = None
    alert_on_exit: Optional[bool] = None

class RoomOut(BaseModel):
    id: int
    floor_id: int
    name: str
    priority_label: str
    localization_type: str
    center_x: float
    center_y: float
    polygon: list
    alert_on_exit: bool
    created_at: datetime
    class Config:
        from_attributes = True


# ── Anchor ────────────────────────────────────────────────────────────────────

class AnchorCreate(BaseModel):
    anchor_id: str
    anchor_type: str = "engenius_ap"       # "engenius_ap" | "esp32"
    ip_address: str = ""
    mac_address: str = ""
    brand: str = ""
    model: str = ""
    firmware_version: str = ""
    capabilities: list = Field(default_factory=lambda: ["rssi", "fingerprinting"])
    x_m: float = 0.0
    y_m: float = 0.0
    room_name: str = ""
    username: str = "admin"
    password: str = "admin"

class AnchorUpdate(BaseModel):
    anchor_type: Optional[str] = None
    ip_address: Optional[str] = None
    mac_address: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    firmware_version: Optional[str] = None
    capabilities: Optional[list] = None
    x_m: Optional[float] = None
    y_m: Optional[float] = None
    room_name: Optional[str] = None
    enabled: Optional[bool] = None
    username: Optional[str] = None
    password: Optional[str] = None

class AnchorOut(BaseModel):
    id: int
    floor_id: int
    anchor_id: str
    anchor_type: str
    ip_address: str
    mac_address: str
    brand: str
    model: str
    firmware_version: str
    capabilities: list
    x_m: float
    y_m: float
    room_name: str
    enabled: bool
    status: str
    last_polled: Optional[datetime]
    username: str
    created_at: datetime
    class Config:
        from_attributes = True


# ── Tag ───────────────────────────────────────────────────────────────────────

class TagCreate(BaseModel):
    tag_id: str
    ssid: str = ""
    mac_address: str = ""
    brand: str = ""
    model: str = ""
    firmware_version: str = ""
    capabilities: list = Field(default_factory=lambda: ["rssi", "fingerprinting"])
    rssi_at_1m_dbm: float = -22.0
    path_loss_n: float = 4.0
    assigned_room_id: Optional[int] = None

class TagUpdate(BaseModel):
    ssid: Optional[str] = None
    mac_address: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    firmware_version: Optional[str] = None
    capabilities: Optional[list] = None
    rssi_at_1m_dbm: Optional[float] = None
    path_loss_n: Optional[float] = None
    assigned_room_id: Optional[int] = None
    enabled: Optional[bool] = None

class TagOut(BaseModel):
    id: int
    tag_id: str
    ssid: str
    mac_address: str
    brand: str
    model: str
    firmware_version: str
    capabilities: list
    rssi_at_1m_dbm: float
    path_loss_n: float
    assigned_room_id: Optional[int]
    enabled: bool
    status: str
    last_polled: Optional[datetime]
    trilateration_location: str
    fingerprint_location: str
    ble_location: str
    tof_location: str
    created_at: datetime
    class Config:
        from_attributes = True


# ── Logs ──────────────────────────────────────────────────────────────────────

class BoundaryCrossingOut(BaseModel):
    id: int
    tag_id: str
    timestamp: datetime
    previous_location: str
    new_location: str
    localization_method: str
    confidence: float
    class Config:
        from_attributes = True

class AlertOut(BaseModel):
    id: int
    crossing_id: int
    tag_id: str
    timestamp: datetime
    severity: str
    assigned_room: str
    exited_to: str
    acknowledged: bool
    acknowledged_by: str
    acknowledged_at: Optional[datetime]
    class Config:
        from_attributes = True

class AlertAck(BaseModel):
    acknowledged_by: str = "admin"
