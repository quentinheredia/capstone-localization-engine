"""
Database models for the IPS Management Platform.

Physical hierarchy:  Campus → Building → Floor → Room
Hardware:            Anchor (EnGenius AP | ESP32), Tag
Access:              AccessLevel (per-building security tiers)
Logging:             BoundaryCrossing, Alert, EngineLog
"""

from models.base import Base, engine, SessionLocal, get_db
from models.campus import Campus
from models.building import Building
from models.floor import Floor
from models.room import Room
from models.access_level import AccessLevel
from models.anchor import Anchor
from models.tag import Tag
from models.log import BoundaryCrossing, Alert
from models.engine_log import EngineLog

__all__ = [
    "Base", "engine", "SessionLocal", "get_db",
    "Campus", "Building", "Floor", "Room", "AccessLevel",
    "Anchor", "Tag",
    "BoundaryCrossing", "Alert",
    "EngineLog",
]
