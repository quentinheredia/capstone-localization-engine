"""
EngineLog — Unified event log for the localization platform.

Five severity levels (ascending):
  info     — Tag movement, room changes, routine telemetry
  warn     — Unusual RSSI readings, first missed scan
  alert    — Device unreachable 1–2 consecutive cycles
  config   — Device unreachable 3+ cycles; tag out of scope (priority 4)
  security — Tag left high-security zone; device offline 5+ cycles (priority 5)

Sources:
  engine       — Emitted by the Hybrid localization engine (bridged here)
  connectivity — ICMP ping failures detected by the platform poller
  boundary     — Scope violations detected by the platform scope checker
  system       — Platform-level lifecycle events
"""

from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Boolean, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column
from models.base import Base

VALID_LEVELS = {"info", "warn", "alert", "config", "security"}
VALID_SOURCES = {"engine", "connectivity", "boundary", "system"}


class EngineLog(Base):
    __tablename__ = "engine_logs"

    id:               Mapped[int]      = mapped_column(primary_key=True)

    # Severity level — drives badge colour in the UI
    level:            Mapped[str]      = mapped_column(String(20), nullable=False, index=True)

    # Which tag this entry is about (None for system/anchor logs)
    tag_id:           Mapped[str]      = mapped_column(String(100), nullable=True, index=True)

    # Where the log was generated
    source:           Mapped[str]      = mapped_column(String(30), default="system")

    # Human-readable message (mirrors the CLI line the engine would print)
    message:          Mapped[str]      = mapped_column(Text, default="")

    # Freeform JSON context: room, confidence, method, ip, cycles_missed, etc.
    meta:             Mapped[dict]     = mapped_column(JSON, default=dict)

    timestamp:        Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
        default=lambda: datetime.now(timezone.utc),
    )

    acknowledged:     Mapped[bool]     = mapped_column(Boolean, default=False)
    acknowledged_by:  Mapped[str]      = mapped_column(String(255), default="")
    acknowledged_at:  Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
