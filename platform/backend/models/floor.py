"""
Floor — child of Building, parent of Room and Anchor.

Each floor has:
  - Real-world dimensions (width_m × height_m)
  - A floorplan_data JSON blob storing the editor state (points, lines, etc.)
  - An optional PDF background path for tracing
  - A default fingerprint grid cell size (metres)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, Integer, Float, ForeignKey, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from models.base import Base


class Floor(Base):
    __tablename__ = "floors"

    id:              Mapped[int]   = mapped_column(primary_key=True)
    building_id:     Mapped[int]   = mapped_column(ForeignKey("buildings.id", ondelete="CASCADE"), nullable=False)
    name:            Mapped[str]   = mapped_column(String(100), nullable=False)
    floor_number:    Mapped[int]   = mapped_column(Integer, default=1)
    floor_plan_path: Mapped[str]   = mapped_column(String(500), default="")
    width_m:         Mapped[float] = mapped_column(Float, default=10.0)
    height_m:        Mapped[float] = mapped_column(Float, default=10.0)

    # ── New fields for the floor plan editor ─────────────────────────────
    floorplan_data:      Mapped[Optional[dict]]  = mapped_column(JSON, nullable=True, default=dict)  # full editor state
    pdf_path:            Mapped[Optional[str]]   = mapped_column(String(500), nullable=True, default="")  # uploaded PDF background
    default_grid_size_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=1.0)  # fingerprint grid default

    # Legacy grid fields (kept for backward compat, superseded by default_grid_size_m)
    grid_rows:       Mapped[int]   = mapped_column(Integer, default=8)
    grid_cols:       Mapped[int]   = mapped_column(Integer, default=8)

    created_at:      Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    building = relationship("Building", back_populates="floors")
    rooms    = relationship("Room",   back_populates="floor", cascade="all, delete-orphan")
    anchors  = relationship("Anchor", back_populates="floor", cascade="all, delete-orphan")
