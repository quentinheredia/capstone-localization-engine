import time
import json
import os
import yaml
from concurrent.futures import ThreadPoolExecutor
from models import Environment
from poll import poll_one_ap

MAP_FILE = "radiomap.json"

def load_config():
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)

def run_survey(room_label, target_ssid, samples=10):
    cfg = load_config()
    loc_id = cfg['telemetry_config']['edge_location_id']
    floor_id = cfg['telemetry_config']['edge_floor_id']
    
    env = Environment.from_config(cfg)
    active_aps = list(env.aps.values())
    prompts = cfg.get('prompts', {"main": "eap350>", "sub": "eap350/wless2/network>"})
    
    print(f"\n📡 Starting survey for '{room_label}' tracking '{target_ssid}'")
    print(f"Stand still. Collecting {samples} samples...")
    
    collected_vectors = []
    executor = ThreadPoolExecutor(max_workers=len(active_aps))
    
    for i in range(samples):
        poll_tasks = [
            (ap.host, ap.username, ap.password, prompts["main"], prompts["sub"], [target_ssid])
            for ap in active_aps
        ]
        
        futures = [executor.submit(poll_one_ap, *task) for task in poll_tasks]
        
        current_vector = {}
        for idx, fut in enumerate(futures):
            try:
                res = fut.result()
                if res:
                    ap_id = active_aps[idx].id
                    if target_ssid in res:
                        current_vector[ap_id] = res[target_ssid]
            except Exception: pass
            
        if current_vector:
            collected_vectors.append(current_vector)
            print(f"  [{i+1}/{samples}] Captured vector: {current_vector}")
        
        time.sleep(2) # Give APs a moment to refresh their internal tables
        
    executor.shutdown(wait=False)
    
    # Save to JSON
    if os.path.exists(MAP_FILE):
        with open(MAP_FILE, "r") as f:
            radio_map = json.load(f)
    else:
        print(f"Creating new radio map file: {MAP_FILE}")
        radio_map = {}
        
    if room_label not in radio_map:
        radio_map[room_label] = []
        
    radio_map[room_label].extend(collected_vectors)
    
    with open(MAP_FILE, "w") as f:
        json.dump(radio_map, f, indent=2)
        
    print(f"✅ Saved {len(collected_vectors)} vectors for {room_label} to {MAP_FILE}")

if __name__ == "__main__":
    target = "NOTHINGPHONE"
    zone = input("Enter the zone you are standing in (e.g., 324B, Hallway, Outside): ")
    run_survey(zone, target, samples=15)