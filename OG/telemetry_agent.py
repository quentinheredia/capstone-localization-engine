import time
import requests
import yaml
import csv
import os
import uuid
import json
import atexit
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from collections import defaultdict

import boto3
from dotenv import load_dotenv

load_dotenv()

s3_client = boto3.client(
    's3',
    region_name=os.getenv('AWS_REGION'),
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY')
)

# Import modules
from poll import poll_one_ap, close_all_sessions
from processing_engine import process_scan_data
import preprocessing
from models import Environment

# --- CONFIGURATION FOR LOCAL DEBUGGING ---
ENABLE_API_PUSH = False 
ENABLE_CSV_LOGGING = True
CSV_FILENAME = "telemetry_log.csv"
PID_FILE = "agent.pid"
ENABLE_S3_PUSH = True


def _write_pid_file():
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        print(f"[WARN] Failed to write PID file: {e}")


def _remove_pid_file():
    try:
        if os.path.isfile(PID_FILE):
            os.remove(PID_FILE)
    except Exception as e:
        print(f"[WARN] Failed to remove PID file: {e}")

def load_config():
    try:
        with open("config.yaml", "r") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print("FATAL ERROR: config.yaml not found.")
        return None



def _api_base(cfg):
    base = cfg.get("telemetry_config", {}).get("api_base_url")
    if base:
        return base.rstrip("/")
    ingest = cfg.get("telemetry_config", {}).get("api_ingest_url", "")
    if ingest.endswith("/ingest"):
        return ingest[:-7].rstrip("/")
    return ingest.rstrip("/")


def _post_json(url, payload, timeout_s=2):
    try:
        requests.post(url, json=payload, timeout=timeout_s)
    except Exception as e:
        print(f"[WARN] API push failed: {e}")


def push_config(cfg):
    base = _api_base(cfg)
    if base:
        _post_json(f"{base}/config", cfg)


def push_status(cfg, status_payload):
    base = _api_base(cfg)
    if base:
        _post_json(f"{base}/status", status_payload)


def push_decision(cfg, payload):
    ingest_url = cfg.get("telemetry_config", {}).get("api_ingest_url")
    if not ingest_url:
        base = _api_base(cfg)
        ingest_url = f"{base}/ingest" if base else ""
    if ingest_url:
        _post_json(ingest_url, payload)


def push_raw(cfg, payload):
    base = _api_base(cfg)
    if base:
        _post_json(f"{base}/raw", payload)

def log_to_csv(payload):
    if not ENABLE_CSV_LOGGING: return
    
    # print("[DEBUG] Attempting to log to CSV...")
    try:
        file_exists = os.path.isfile(CSV_FILENAME)
        with open(CSV_FILENAME, mode='a', newline='') as file:
            writer = csv.writer(file)
            
            headers = [
                "_id", "device_id", "location_id", "floor_id", "room_id", 
                "timestamp", "confidence", "rssi_vector", "x", "y", "scan_number"
            ]
            
            if not file_exists:
                writer.writerow(headers)
            
            writer.writerow([
                payload["_id"],
                payload["device_id"],
                payload["location_id"],
                payload["floor_id"],
                payload["room_id"],
                payload["timestamp"],
                f"{payload['confidence']:.2f}",
                json.dumps(payload["rssi_vector"]), 
                f"{payload['x']:.2f}", 
                f"{payload['y']:.2f}",
                payload["scan_number"]
            ])
        # print(f"[DEBUG] Data successfully appended to {CSV_FILENAME}.")
    except Exception as e:
        print(f"[ERR] Failed to write to CSV: {e}")
        
def push_to_s3(payload):
    if not ENABLE_S3_PUSH: return
    
    try:
        bucket = os.getenv('AWS_BUCKET_NAME')
        # Create a predictable filename so the Vite frontend knows exactly what to fetch
        # Example: Carleton_University_Floor_3_latest.json
        filename = f"{payload['location_id']}_{payload['floor_id']}_latest.json"
        
        # We use put_object to overwrite the file every single time
        s3_client.put_object(
            Bucket=bucket,
            Key=filename,
            Body=json.dumps(payload),
            ContentType='application/json',
            # Force browsers/proxies not to cache this for more than 2 seconds
            CacheControl='max-age=2' 
        )
        # print(f"[DEBUG] Successfully pushed {filename} to S3.")
    except Exception as e:
        print(f"[ERR] S3 Upload failed: {e}")


def main_loop():
    cfg = load_config()
    if not cfg: return

    _write_pid_file()
    atexit.register(_remove_pid_file)

    if ENABLE_API_PUSH:
        push_config(cfg)

    env = Environment.from_config(cfg)
    target_ssids = [t["ssid"] for t in cfg["targets"]]
    active_aps = list(env.aps.values())
    
    loc_id = cfg['telemetry_config']['edge_location_id']
    floor_id = cfg['telemetry_config']['edge_floor_id']
    poll_interval = cfg['telemetry_config']['poll_interval_s']
    update_interval = cfg.get('telemetry_config', {}).get('update_interval_s', 60)
    
    ap_ids = [ap.id for ap in active_aps]
    #preprocessing.initialize_history(ap_ids, target_ssids, cfg['system']['rolling_average_window'])
    
    prompts = cfg.get('prompts', {"main": "eap350>", "sub": "eap350/wless2/network>"})
    
    print(f"   Telemetry Agent Started.")
    print(f"   Localization Method: {cfg['system'].get('localization_method')}")
    print(f"   Targets: {target_ssids}")
    print(f"   Context: {loc_id} / {floor_id}")
    print(f"   Polling Every: {poll_interval}s | Aggregating Every: {update_interval}s")
    
    executor = ThreadPoolExecutor(max_workers=len(active_aps))

    # BUFFER STATE
    scan_buffer = [] 
    last_update_time = time.time()
    total_cycles_in_window = 0

    try:
        while True:
            cycle_start_time = time.time()
            total_cycles_in_window += 1
            
            # --- 1. POLL (Fast Loop - "Raw Data") ---
            poll_tasks = []
            for ap in active_aps:
                poll_tasks.append((ap.host, ap.username, ap.password, prompts["main"], prompts["sub"], target_ssids))

            futures = [executor.submit(poll_one_ap, *task) for task in poll_tasks]
            
            current_scan_results = {}
            for i, fut in enumerate(futures):
                try:
                    result = fut.result()
                    if result:
                        host_polled = poll_tasks[i][0]
                        ap_obj = next(a for a in active_aps if a.host == host_polled)
                        current_scan_results[ap_obj.id] = result
                except Exception: pass
            
            # Store this scan in the buffer
            if current_scan_results:
                scan_buffer.append(current_scan_results)
                
                # --- VISUALIZE RAW SNAPSHOT (User Request) ---
                # Check completeness for this single snapshot
                present_ap_ids = set(current_scan_results.keys())
                all_aps_ids = set(a.id for a in active_aps)
                is_complete = all_aps_ids.issubset(present_ap_ids)

                if ENABLE_API_PUSH:
                    raw_payload = {
                        "timestamp": datetime.utcnow().isoformat(),
                        "scan_number": total_cycles_in_window,
                        "aps_present": len(present_ap_ids),
                        "aps_expected": len(all_aps_ids),
                        "complete": is_complete,
                        "results": current_scan_results,
                    }
                    push_raw(cfg, raw_payload)
                
                status_icon = "COMPLETE" if is_complete else "NOT COMPLETE"
                print(f"\\n[Raw #{total_cycles_in_window}] {status_icon} Captured {len(current_scan_results)}/{len(active_aps)} APs")
                
                # Run a quick "Preview" calculation to show user where it looks like right now
                # We do NOT log this to CSV, it's just for console visibility
                preview_class = process_scan_data(current_scan_results, env, cfg)
                for dev_id, res in preview_class.items():
                    if res["room"] != "Undetected":
                        print(f"   ↳ {dev_id}: {res['room']} ({res['confidence']:.2f})")
                        # Print the raw RSSI for this snapshot so user sees the "grab"
                        raw_vector = {ap: current_scan_results[ap].get(dev_id, 'N/A') for ap in current_scan_results}
                        print(f"     Vector: {raw_vector}")

            # --- 2. AGGREGATE & DECIDE (Slow Loop - "The Verdict") ---
            if time.time() - last_update_time >= update_interval:
                print(f"\\n{'='*20} VERDICT (Window: {update_interval}s) {'='*20}")
                
                for dev_id in target_ssids:
                    # Filter for perfect datasets (Datasets with ALL APs)
                    complete_scans = []
                    required_ap_ids = set(ap.id for ap in active_aps)
                    
                    for scan in scan_buffer:
                        # Check which APs in this scan saw the device
                        aps_seeing_device = {ap_id for ap_id, data in scan.items() if dev_id in data}
                        if required_ap_ids.issubset(aps_seeing_device):
                            complete_scans.append(scan)
                    
                    # USER REQUEST: "If less than half the datasets have sufficient data then we throw a warning"
                    total_scans = len(scan_buffer)
                    valid_scans = len(complete_scans)
                    
                    if total_scans == 0:
                        print(f"!!!  {dev_id}: No data collected.")
                        continue

                    data_health_ratio = valid_scans / total_scans
                    print(f" {dev_id} Data Health: {int(data_health_ratio*100)}% ({valid_scans}/{total_scans} valid scans)")

                    if ENABLE_API_PUSH:
                        status_payload = {
                            "device_id": dev_id,
                            "total_scans": total_scans,
                            "valid_scans": valid_scans,
                            "data_health_ratio": data_health_ratio,
                        }
                        push_status(cfg, status_payload)

                    if data_health_ratio < 0.5:
                        print(f"!!!  WARNING: Unstable Data! Less than 50% of scans were complete.")
                        # We continue anyway to give a best-effort guess, or break if you want to be strict.
                    
                    if not complete_scans:
                        print(f"!X! {dev_id}: Cannot determine location (0 complete scans).")
                        continue

                    # Average the COMPLETE scans only
                    averaged_scan_results = defaultdict(dict)
                    for ap_id in required_ap_ids:
                        rssi_values = [scan[ap_id][dev_id] for scan in complete_scans]
                        avg_rssi = sum(rssi_values) / len(rssi_values)
                        averaged_scan_results[ap_id][dev_id] = avg_rssi
                    
                    averaged_scan_results = dict(averaged_scan_results)
                    
                    # Final Processing
                    classification = process_scan_data(averaged_scan_results, env, cfg)
                    room_data = classification[dev_id]
                    
                    timestamp = datetime.utcnow().isoformat()
                    rssi_vector = {ap_id: averaged_scan_results[ap_id][dev_id] for ap_id in averaged_scan_results}
                    
                    if room_data["room"] != "Undetected":
                        payload = {
                            "_id": str(uuid.uuid4()),
                            "device_id": dev_id, 
                            "location_id": loc_id,
                            "floor_id": floor_id,
                            "room_id": room_data["room"],
                            "timestamp": timestamp,
                            "confidence": room_data["confidence"],
                            "rssi_vector": rssi_vector,
                            "x": room_data["coords"][0], 
                            "y": room_data["coords"][1],
                            "scan_number": total_cycles_in_window
                        }   
                        if ENABLE_API_PUSH:
                            push_decision(cfg, payload)
                        log_to_csv(payload)
                        push_to_s3(payload)
                        print(f"   FINAL DECISION: {room_data['room']} [x={payload['x']}, y={payload['y']}]")
                        print(f"   (Logged to CSV)")

                # Reset
                scan_buffer = []
                last_update_time = time.time()
                total_cycles_in_window = 0
                print("="*60 + "\\n")

            # Sleep remainder
            elapsed = time.time() - cycle_start_time
            time.sleep(max(0, poll_interval - elapsed))

    except KeyboardInterrupt:
        print("\\n Stopping...")
        close_all_sessions()
        executor.shutdown(wait=False)
        _remove_pid_file()

if __name__ == "__main__":
    main_loop()
