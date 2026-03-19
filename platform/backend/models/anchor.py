"""
Anchor — hardware placed on a floor for localization.

Two types:
  "engenius_ap" — TP-Link/EnGenius AP, IP in 192.168.1.0/24, fully integrated.
  "esp32"       — ESP32-C3 module, engine not yet integrated (stubbed out).

Capabilities (stored as JSON list):
  ["rssi", "fingerprinting", "ble", "tof"]
"""

from datetime import datetime, timezone
from sqlalchemy import (
    String, Integer, Float, ForeignKey, DateTime, Boolean, JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from models.base import Base


class Anchor(Base):
    __tablename__ = "anchors"

    id:               Mapped[int]   = mapped_column(primary_key=True)
    floor_id:         Mapped[int]   = mapped_column(ForeignKey("floors.id", ondelete="CASCADE"), nullable=False)
    anchor_id:        Mapped[str]   = mapped_column(String(100), unique=True, nullable=False)  # e.g. "AP1"
    anchor_type:      Mapped[str]   = mapped_column(String(50), default="engenius_ap")  # "engenius_ap" | "esp32"
    ip_address:       Mapped[str]   = mapped_column(String(45), default="")  # must be in 192.168.1.0/24 for EnGenius
    mac_address:      Mapped[str]   = mapped_column(String(17), default="")
    brand:            Mapped[str]   = mapped_column(String(100), default="")
    model:            Mapped[str]   = mapped_column(String(100), default="")
    firmware_version: Mapped[str]   = mapped_column(String(50),  default="")
    capabilities:     Mapped[dict]  = mapped_column(JSON, default=list)  # ["rssi","fingerprinting","ble","tof"]
    x_m:              Mapped[float] = mapped_column(Float, default=0.0)
    y_m:              Mapped[float] = mapped_column(Float, default=0.0)
    room_name:        Mapped[str]   = mapped_column(String(255), default="")  # which room it's physically in
    enabled:          Mapped[bool]  = mapped_column(Boolean, default=True)
    status:           Mapped[str]   = mapped_column(String(20), default="offline")  # "online" | "offline"
    last_polled:      Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:       Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Telnet credentials (per-anchor override; falls back to config defaults)
    username: Mapped[str] = mapped_column(String(100), default="admin")
    password: Mapped[str] = mapped_column(String(100), default="admin")

    # Relationships
    floor = relationship("Floor", back_populates="anchors")
