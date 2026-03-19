"""Campus — top-level entity in the spatial hierarchy."""

from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from models.base import Base


class Campus(Base):
    __tablename__ = "campuses"

    id:          Mapped[int]  = mapped_column(primary_key=True)
    name:        Mapped[str]  = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str]  = mapped_column(Text, default="")
    created_at:  Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at:  Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Children
    buildings = relationship("Building", back_populates="campus", cascade="all, delete-orphan")
