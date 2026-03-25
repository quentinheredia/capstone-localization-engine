"""
AccessLevel — custom security/access classifications for rooms.

Users create named access levels per building (e.g., "Public", "Restricted",
"Server Room Only").  Rooms reference these, and tags/devices inherit the
access level of their assigned room.
"""

from datetime import datetime, timezone
from sqlalchemy import String, Integer, ForeignKey, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from models.base import Base


class AccessLevel(Base):
    __tablename__ = "access_levels"

    id:          Mapped[int] = mapped_column(primary_key=True)
    building_id: Mapped[int] = mapped_column(
        ForeignKey("buildings.id", ondelete="CASCADE"), nullable=False
    )
    name:        Mapped[str] = mapped_column(String(100), nullable=False)
    color:       Mapped[str] = mapped_column(String(30), default="#6b7280")
    description: Mapped[str] = mapped_column(String(500), default="")
    sort_order:  Mapped[int] = mapped_column(Integer, default=0)
    created_at:  Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    building = relationship("Building", back_populates="access_levels")
    rooms    = relationship("Room", back_populates="access_level")
