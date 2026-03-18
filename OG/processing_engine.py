import math
import json
import os
from collections import Counter
from trilateration import rssi_to_distance_m, refined_trilaterate
from preprocessing import RSSIFilter 
from models import Environment 

# Global state to maintain rolling averages across scans
_rssi_filter = None

# Cache the radio map in memory so we don't read the disk every cycle
_radio_map = None

def knn_fingerprint_match(live_vector: dict, k: int = 3) -> tuple:
    global _radio_map
    if _radio_map is None:
        if not os.path.exists("radiomap.json"):
            return "Undetected", 0.0
        with open("radiomap.json", "r") as f:
            _radio_map = json.load(f)

    distances = []
    
    # Compare live vector against the database
    for room, vectors in _radio_map.items():
        for map_vector in vectors:
            sq_diff = 0.0
            valid_aps = 0   
            
            for ap, live_rssi in live_vector.items():
                if ap in map_vector:
                    sq_diff += (live_rssi - map_vector[ap])**2
                    valid_aps += 1
            
            # Only count vectors where we have matching AP data
            if valid_aps > 0:
                dist = math.sqrt(sq_diff)
                distances.append((dist, room))
    
    if not distances:
        return "Outside Defined Area", 0.0
        
    # Sort by closest match (lowest distance)
    distances.sort(key=lambda x: x[0])
    
    # Get the top K rooms and take a vote
    top_k_rooms = [room for dist, room in distances[:k]]
    most_common_room = Counter(top_k_rooms).most_common(1)[0][0]
    
    # Calculate a rough confidence score (closer = higher confidence)
    avg_dist = sum(dist for dist, room in distances[:k]) / len(top_k_rooms)
    confidence = max(0.0, 1.0 - (avg_dist / 30.0)) # 30 is an arbitrary tuning baseline
    
    return most_common_room, confidence

def process_scan_data(scan_results: dict, env: Environment, cfg: dict) -> dict:
    global _rssi_filter
    output = {}
    
    loc_id = cfg['telemetry_config']['edge_location_id']
    floor_id = cfg['telemetry_config']['edge_floor_id']
    try:
        floor_cfg = cfg['locations'][loc_id]['floors'][floor_id]
        room_w = floor_cfg.get('width_m', 10.0) 
        room_h = floor_cfg.get('height_m', 10.0)
    except KeyError:
        room_w, room_h = 10.0, 10.0

    # Instantiate the filter once using the config variables
    if _rssi_filter is None:
        _rssi_filter = RSSIFilter(
            window_size=cfg['system']['rolling_average_window'],
            noise_floor_dbm=cfg['system']['signal_filter']['noise_floor_dbm']
        )
    
    # Process the RSSI using the stateful instance
    smoothed_rssi = _rssi_filter.process_rssi(scan_results)

    # Grab the full target dictionaries, not just the SSIDs
    targets_config = cfg.get("targets", [])

    for target in targets_config:
        ssid = target["ssid"]
        
        # Check which mode we are running
        loc_method = cfg['system'].get('localization_method', 'trilateration')
        
        # Build the live vector for this specific device
        live_vector = {}
        for ap in env.aps.values():
            if ap.id in smoothed_rssi and ssid in smoothed_rssi[ap.id]:
                live_vector[ap.id] = smoothed_rssi[ap.id][ssid]

        est_x, est_y = 0.0, 0.0
        best_room = "Undetected"
        best_conf = 0.0

        if loc_method == "fingerprinting":
            # --- FINGERPRINTING MODE ---
            if live_vector:
                best_room, best_conf = knn_fingerprint_match(live_vector, k=3)
            else:
                best_room = "Outside Defined Area"
        else:
            # --- TRILATERATION MODE ---
            radio = target.get("radio_override", {"rssi_at_1m_dbm": -40.0, "path_loss_n": 3.0})
            anchors = []
            dists = []

            for ap in env.aps.values():
                if ap.id in live_vector:
                    rssi = live_vector[ap.id]
                    dist = rssi_to_distance_m(rssi, radio["rssi_at_1m_dbm"], radio["path_loss_n"])
                    anchors.append((ap.x, ap.y))
                    dists.append(dist)

            found_pos = False
            min_aps = cfg['system']['signal_filter']['min_aps_for_localization']
            
            if len(anchors) >= min_aps:
                est_x, est_y = refined_trilaterate(anchors, dists, room_w, room_h)
                found_pos = True
                
            is_clamped = False
            if found_pos:
                margin = cfg['system'].get('boundary_clamp_margin_m', 0.01)
                
                if est_x <= margin: est_x, is_clamped = margin, True
                if est_x >= (room_w - margin): est_x, is_clamped = room_w - margin, True
                if est_y <= margin: est_y, is_clamped = margin, True
                if est_y >= (room_h - margin): est_y, is_clamped = room_h - margin, True

            if found_pos:
                best_room = "Outside Defined Area"
                if not is_clamped:
                    for room in env.rooms:
                        if room.point_in_room((est_x, est_y)):
                            best_room = room.name
                            dx = est_x - room.center[0]
                            dy = est_y - room.center[1]
                            dist_to_center = (dx*dx + dy*dy)**0.5
                            max_dist = cfg['system']['max_distance_for_high_confidence_m']
                            best_conf = max(0.0, 1.0 - (dist_to_center / max_dist))
                            break

        output[ssid] = {
            "room": best_room, 
            "confidence": best_conf,
            "coords": (est_x, est_y) # Fingerprinting will output (0.0, 0.0)
        }

    return output