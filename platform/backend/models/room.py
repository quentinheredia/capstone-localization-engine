"""
Room — child of Floor.

Each room has:
  - A priority classification (customer-configurable labels)
  - A localization type: "rssi_section" or "high_accuracy" (ToF)
  - A polygon boundary (vertices in metres from floor-plan origin)
  - An optional access level (custom security classification)
  - An optional fingerprint grid size override (metres)
  - Optional equipment assignments (tags bound to this room for alerting)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import (
    String, Integer, Float, ForeignKey, DateTime, Text, Boolean, JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from models.base import Base


class Room(Base):
    __tablename__ = "rooms"

    id:                Mapped[int]   = mapped_column(primary_key=True)
    floor_id:          Mapped[int]   = mapped_column(ForeignKey("floors.id", ondelete="CASCADE"), nullable=False)
    name:              Mapped[str]   = mapped_column(String(255), nullable=False)
    priority_label:    Mapped[str]   = mapped_column(String(100), default="Standard")
    localization_type: Mapped[str]   = mapped_column(String(50),  default="rssi_section")
    center_x:          Mapped[float] = mapped_column(Float, default=0.0)
    center_y:          Mapped[float] = mapped_column(Float, default=0.0)
    polygon:           Mapped[dict]  = mapped_column(JSON, default=list)  # [[x1,y1],[x2,y2],...]
    alert_on_exit:     Mapped[bool]  = mapped_column(Boolean, default=False)

    # ── New fields ───────────────────────────────────────────────────────
    access_level_id:    Mapped[Optional[int]]   = mapped_column(
        ForeignKey("access_levels.id", ondelete="SET NULL"), nullable=True, default=None
    )
    grid_size_override: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)  # null = use floor default

    created_at:        Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    floor        = relationship("Floor", back_populates="rooms")
    access_level = relationship("AccessLevel", back_populates="rooms")

    @property
    def full_path(self) -> str:
        """Campus/Building/Floor/Room string built from the parent chain."""
        floor = self.floor
        building = floor.building if floor else None
        campus = building.campus if building else None
        return "/".join([
            campus.name   if campus   else "?",
            building.name if building else "?",
            floor.name    if floor    else "?",
            self.name,
        ])
