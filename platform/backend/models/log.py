"""
Logging models — BoundaryCrossing and Alert.

BoundaryCrossing
    Logged every time a tag transitions from one room to another.
    Debounce: the engine must confirm the tag is consistently in the new room
    for `debounce_s` seconds (default 5) before the crossing is committed.

Alert
    Fired when a high-priority equipment tag leaves its assigned room.
    Inherits all fields from BoundaryCrossing plus severity and ack status.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    String, Integer, Float, ForeignKey, DateTime, Boolean, Text,
)
from sqlalchemy.orm import Mapped, mapped_column
from models.base import Base


class BoundaryCrossing(Base):
    __tablename__ = "boundary_crossings"

    id:                  Mapped[int]      = mapped_column(primary_key=True)
    tag_id:              Mapped[str]      = mapped_column(String(100), nullable=False, index=True)
    timestamp:           Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    # Full hierarchy paths
    previous_location:   Mapped[str]      = mapped_column(String(500), default="")  # Campus/Building/Floor/Room
    new_location:        Mapped[str]      = mapped_column(String(500), default="")

    # Which localization method triggered this
    localization_method: Mapped[str]      = mapped_column(String(50), default="")  # trilateration|fingerprinting|ble|tof
    confidence:          Mapped[float]    = mapped_column(Float, default=0.0)

    # Debounce metadata
    debounce_s:          Mapped[int]      = mapped_column(Integer, default=5)
    first_seen_at:       Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class Alert(Base):
    __tablename__ = "alerts"

    id:                  Mapped[int]      = mapped_column(primary_key=True)
    crossing_id:         Mapped[int]      = mapped_column(
        ForeignKey("boundary_crossings.id", ondelete="CASCADE"), nullable=False
    )
    tag_id:              Mapped[str]      = mapped_column(String(100), nullable=False, index=True)
    timestamp:           Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Alert details
    severity:            Mapped[str]      = mapped_column(String(50), default="high")  # from room priority_label
    assigned_room:       Mapped[str]      = mapped_column(String(500), default="")     # room the tag was supposed to be in
    exited_to:           Mapped[str]      = mapped_column(String(500), default="")     # where it went
    equipment_details:   Mapped[str]      = mapped_column(Text, default="")

    # Acknowledgement
    acknowledged:        Mapped[bool]     = mapped_column(Boolean, default=False)
    acknowledged_by:     Mapped[str]      = mapped_column(String(255), default="")
    acknowledged_at:     Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
