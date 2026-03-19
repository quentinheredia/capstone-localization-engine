"""
Floor — child of Building, parent of Room and Anchor.

Each floor has:
  - A floor plan image path
  - Real-world dimensions (width_m × height_m)
  - An 8×8 fingerprinting grid (radiomap vectors per cell)
"""

from datetime import datetime, timezone
from sqlalchemy import String, Integer, Float, ForeignKey, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from models.base import Base


class Floor(Base):
    __tablename__ = "floors"

    id:              Mapped[int]   = mapped_column(primary_key=True)
    building_id:     Mapped[int]   = mapped_column(ForeignKey("buildings.id", ondelete="CASCADE"), nullable=False)
    name:            Mapped[str]   = mapped_column(String(100), nullable=False)  # e.g. "Floor_1"
    floor_number:    Mapped[int]   = mapped_column(Integer, default=1)
    floor_plan_path: Mapped[str]   = mapped_column(String(500), default="")  # uploaded image
    width_m:         Mapped[float] = mapped_column(Float, default=10.0)
    height_m:        Mapped[float] = mapped_column(Float, default=10.0)
    grid_rows:       Mapped[int]   = mapped_column(Integer, default=8)
    grid_cols:       Mapped[int]   = mapped_column(Integer, default=8)
    created_at:      Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    building = relationship("Building", back_populates="floors")
    rooms    = relationship("Room",   back_populates="floor", cascade="all, delete-orphan")
    anchors  = relationship("Anchor", back_populates="floor", cascade="all, delete-orphan")
