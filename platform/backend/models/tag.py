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

# Translates tag priority to EngineLog severity level on scope exit.
# Priority 1 → None (scope must be ALL, no violation log generated).
PRIORITY_LOG_LEVELS: dict = {
    1: None,        # LOW   — roams freely; scope locked to ALL
    2: "warn",      # —     — Warn on exit
    3: "alert",     # —     — Alert on exit
    4: "config",    # HIGH  — Config on exit (room-level scope permitted)
    5: "security",  # CRIT  — Security on exit (room-level scope permitted)
}


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

    # ── Identity & classification ─────────────────────────────────────────────
    name:             Mapped[str]  = mapped_column(String(255), default="")   # friendly label, e.g. "Dr. Smith's Laptop"
    device_type:      Mapped[str]  = mapped_column(String(100), default="")   # "laptop" | "phone" | "badge" | "tablet" | …

    # ── Security & priority ───────────────────────────────────────────────────
    security_level:   Mapped[int]  = mapped_column(Integer, default=1)        # 1–5+ clearance integer
    priority:         Mapped[int]  = mapped_column(Integer, default=1)        # 1 (low) → 5 (critical)
    #   1 = scope MUST be ALL (free-roaming, no alerts)
    #   2 = Warn on scope exit
    #   3 = Alert on scope exit
    #   4/5 = Config/Security on scope exit; may have room-level scope

    # ── Allowed location scope ────────────────────────────────────────────────
    # floor_scope: JSON list of floor names the tag is allowed on.
    #   ["ALL"] → no floor restriction.  ["Ground", "Second"] → two floors only.
    floor_scope:      Mapped[list] = mapped_column(JSON, default=lambda: ["ALL"])

    # room_scope: JSON list of room names the tag is allowed in.
    #   [] → all rooms on the allowed floors.  ["Lab01"] → single room.
    room_scope:       Mapped[list] = mapped_column(JSON, default=list)

    # Current location estimates (populated by engine, cached here + Redis)
    trilateration_location: Mapped[str]  = mapped_column(String(500), default="")
    fingerprint_location:   Mapped[str]  = mapped_column(String(500), default="")
    ble_location:           Mapped[str]  = mapped_column(String(500), default="")
    tof_location:           Mapped[str]  = mapped_column(String(500), default="")
