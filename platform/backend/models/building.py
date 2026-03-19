"""Building — child of Campus, parent of Floor."""

from datetime import datetime, timezone
from sqlalchemy import String, Integer, ForeignKey, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from models.base import Base


class Building(Base):
    __tablename__ = "buildings"

    id:          Mapped[int]  = mapped_column(primary_key=True)
    campus_id:   Mapped[int]  = mapped_column(ForeignKey("campuses.id", ondelete="CASCADE"), nullable=False)
    name:        Mapped[str]  = mapped_column(String(255), nullable=False)
    description: Mapped[str]  = mapped_column(Text, default="")
    created_at:  Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    campus = relationship("Campus", back_populates="buildings")
    floors = relationship("Floor", back_populates="building", cascade="all, delete-orphan")
