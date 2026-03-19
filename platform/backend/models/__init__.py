"""
Database models for the IPS Management Platform.

Physical hierarchy:  Campus → Building → Floor → Room
Hardware:            Anchor (EnGenius AP | ESP32), Tag
Logging:             BoundaryCrossing, Alert
"""

from models.base import Base, engine, SessionLocal, get_db
from models.campus import Campus
from models.building import Building
from models.floor import Floor
from models.room import Room
from models.anchor import Anchor
from models.tag import Tag
from models.log import BoundaryCrossing, Alert

__all__ = [
    "Base", "engine", "SessionLocal", "get_db",
    "Campus", "Building", "Floor", "Room",
    "Anchor", "Tag",
    "BoundaryCrossing", "Alert",
]
