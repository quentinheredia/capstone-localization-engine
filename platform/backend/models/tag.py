"""
Tag — a tracked device (e.g. NOTHINGPHONE, Apple, etc.).

Tags are global (not scoped to a floor) because they move between floors.
Their current location is derived from the localization engine at runtime.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    String, Integer, Float, ForeignKey, DateTime, Boolean, JSON, Text,
)
from sqlalchemy.orm import Mapped, mapped_column
from models.base import Base


class Tag(Base):
    __tablename__ = "tags"

    id:               Mapped[int]  = mapped_column(primary_key=True)
    tag_id:           Mapped[str]  = mapped_column(String(100), unique=True, nullable=False)  # e.g. "tag_001"
    ssid:             Mapped[str]  = mapped_column(String(255), default="")  # WiFi SSID for RSSI matching
    mac_address:      Mapped[str]  = mapped_column(String(17), default="")
    brand:            Mapped[str]  = mapped_column(String(100), default="")
    model:            Mapped[str]  = mapped_column(String(100), default="")
    firmware_version: Mapped[str]  = mapped_column(String(50),  default="")
    capabilities:     Mapped[dict] = mapped_column(JSON, default=list)  # ["rssi","fingerprinting","ble","tof"]

    # Radio override (per-tag path-loss model)
    rssi_at_1m_dbm:   Mapped[float] = mapped_column(Float, default=-22.0)
    path_loss_n:       Mapped[float] = mapped_column(Float, default=4.0)

    # Assigned room (for high-priority equipment alerting)
    assigned_room_id:  Mapped[int]  = mapped_column(ForeignKey("rooms.id", ondelete="SET NULL"), nullable=True)

    enabled:           Mapped[bool] = mapped_column(Boolean, default=True)
    status:            Mapped[str]  = mapped_column(String(20), default="offline")  # "online" | "offline"
    last_polled:       Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:        Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Current location estimates (populated by engine, cached here + Redis)
    trilateration_location: Mapped[str]  = mapped_column(String(500), default="")
    fingerprint_location:   Mapped[str]  = mapped_column(String(500), default="")
    ble_location:           Mapped[str]  = mapped_column(String(500), default="")
    tof_location:           Mapped[str]  = mapped_column(String(500), default="")
