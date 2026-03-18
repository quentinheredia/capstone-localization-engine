import os
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass

# --- Type Aliases ---
Coordinates = Tuple[float, float]
Polygon = List[Coordinates]

# --- Strictly Typed Data Classes (pybind11 ready) ---

@dataclass
class ScanResult:
    bssid: str
    ssid: str
    signal: int
    channel: Optional[str] = None
    security: Optional[str] = None

@dataclass
class TargetProfile:
    ssid: str
    rssi_at_1m_dbm: float
    path_loss_n: float

class AccessPoint:
    def __init__(self, id: str, x: float, y: float, host: str, username: str, password: str):
        self.id = id
        self.x = x
        self.y = y
        self.host = host
        self.username = username
        self.password = password

class Room:
    def __init__(self, room_id: str, name: str, center_x: float, center_y: float, polygon: Polygon):
        self.room_id = room_id
        self.name = name
        self.center = (center_x, center_y)
        self.polygon = polygon

    def point_in_room(self, p: Coordinates) -> bool:
        x, y = p
        n = len(self.polygon)
        inside = False
        p1x, p1y = self.polygon[0]
        for i in range(1, n + 1):
            p2x, p2y = self.polygon[i % n]
            if min(p1y, p2y) < y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xints = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                    if p1x == p2x or x <= xints:
                        inside = not inside
            p1x, p1y = p2x, p2y
        return inside

class Environment:
    def __init__(self, rooms: List[Room], aps: Dict[str, AccessPoint], targets: Dict[str, TargetProfile]):
        self.rooms = rooms
        self.aps = aps
        self.targets = targets

    @classmethod
    def from_config(cls, config: dict):
        all_aps = {}
        all_rooms = []
        all_targets = {}
        
        # 1. Read Edge Node Identity directly from the config dictionary
        loc_config = config.get('telemetry_config', {})
        loc_id = loc_config.get('edge_location_id', 'Qs_House')
        floor_id = loc_config.get('edge_floor_id', 'Ground_Floor')
        
        # 2. Assign Default AP Credentials
        default_creds = config.get('telemetry_config', {}).get('default_ap_credentials', {})
        def_user = default_creds.get('username', 'admin')
        def_pass = default_creds.get('password', 'admin')
        
        # 3. Load Targets and their unique Radio Profiles
        for t in config.get('targets', []):
            ssid = t.get('ssid')
            overrides = t.get('radio_override', {})
            all_targets[ssid] = TargetProfile(
                ssid=ssid,
                rssi_at_1m_dbm=overrides.get('rssi_at_1m_dbm', -22.0),
                path_loss_n=overrides.get('path_loss_n', 4.0)
            )

        # Access specific floor data
        try:
            floor_data = config['locations'][loc_id]['floors'][floor_id]
        except KeyError:
            print(f"CRITICAL ERROR: Location '{loc_id}' or Floor '{floor_id}' not found in config.")
            return cls([], {}, {})
        
        # Load APs, injecting defaults if missing
        for ap_id, ap_data in floor_data.get('aps', {}).items():
            unique_ap_id = f"{loc_id}_{floor_id}_{ap_id}"
            all_aps[unique_ap_id] = AccessPoint(
                ap_id, 
                ap_data.get('x', 0.0), 
                ap_data.get('y', 0.0), 
                ap_data.get('host', ''), 
                ap_data.get('username', def_user), 
                ap_data.get('password', def_pass)
            )

        # Load Rooms
        for room_name, room_data in floor_data.get('rooms', {}).items():
            unique_room_name = f"{loc_id}_{floor_id}_{room_name}"
            all_rooms.append(
                Room(
                    unique_room_name,
                    room_name,
                    room_data['center_x'],
                    room_data['center_y'],
                    room_data['polygon']
                )
            )

        return cls(all_rooms, all_aps, all_targets)