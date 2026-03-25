"""
Anchor — hardware placed on a floor for localization.

Two types:
  "engenius_ap" — EnGenius AP, IP in 192.168.1.0/24, fully integrated.
  "esp32"       — ESP32-C3 module, engine not yet integrated (stubbed out).

Capabilities (stored as JSON list):
  ["rssi", "fingerprinting", "ble", "tof"]

Device lifecycle:
  device_status — inventory/deployment state:
    "in_stock"               — registered but not yet deployed
    "in_use"                 — deployed and actively used
    "deployment_in_progress" — being set up / moved
    "decommissioned"         — retired from service

  flags — administrative tags (JSON list, any combination):
    "needs_provision"   — device requires initial provisioning
    "ready_to_deploy"   — provisioned and ready for placement
    "admin_down"        — administratively disabled by operator
    "shutdown"          — device has been shut down
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, Float, ForeignKey, DateTime, Boolean, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from models.base import Base


VALID_DEVICE_STATUSES = {"in_stock", "in_use", "deployment_in_progress", "decommissioned"}
VALID_FLAGS = {"needs_provision", "ready_to_deploy", "admin_down", "shutdown"}


class Anchor(Base):
    __tablename__ = "anchors"

    id:               Mapped[int]            = mapped_column(primary_key=True)
    # floor_id is nullable — anchors can exist in inventory without a floor assignment
    floor_id:         Mapped[Optional[int]]  = mapped_column(
        ForeignKey("floors.id", ondelete="SET NULL"), nullable=True, default=None
    )
    anchor_id:        Mapped[str]            = mapped_column(String(100), unique=True, nullable=False)
    anchor_type:      Mapped[str]            = mapped_column(String(50), default="engenius_ap")
    ip_address:       Mapped[str]            = mapped_column(String(45), default="")
    mac_address:      Mapped[str]            = mapped_column(String(17), default="")
    brand:            Mapped[str]            = mapped_column(String(100), default="")
    model:            Mapped[str]            = mapped_column(String(100), default="")
    firmware_version: Mapped[str]            = mapped_column(String(50), default="")
    capabilities:     Mapped[list]           = mapped_column(JSON, default=list)
    x_m:              Mapped[float]          = mapped_column(Float, default=0.0)
    y_m:              Mapped[float]          = mapped_column(Float, default=0.0)
    room_name:        Mapped[str]            = mapped_column(String(255), default="")
    enabled:          Mapped[bool]           = mapped_column(Boolean, default=True)

    # Connectivity status — updated by the polling engine
    status:           Mapped[str]            = mapped_column(String(20), default="offline")

    # Inventory / lifecycle status
    device_status:    Mapped[str]            = mapped_column(String(50), default="in_stock")

    # Administrative flags (list of strings from VALID_FLAGS)
    flags:            Mapped[list]           = mapped_column(JSON, default=list)

    last_polled:      Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:       Mapped[datetime]       = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Telnet credentials (per-anchor override; falls back to config defaults)
    username: Mapped[str] = mapped_column(String(100), default="admin")
    password: Mapped[str] = mapped_column(String(100), default="admin")

    # Relationships
    floor = relationship("Floor", back_populates="anchors")
