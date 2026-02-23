import telnetlib3
import time
import threading
from parse_apscan import parse_apscan_table

_sessions = {}
_lock = threading.Lock()

def get_session(host, username, password, prompt_main, timeout=5):
    with _lock:
        if host in _sessions:
            return _sessions[host]

        try:
            print(f"[DEBUG] Connecting to {host}...")
            tn = telnetlib3.Telnet(host, 23, timeout)
            tn.read_until(b"login:", timeout)
            tn.write(username.encode("ascii") + b"\n")
            tn.read_until(b"Password:", timeout)
            tn.write(password.encode("ascii") + b"\n")
            tn.read_until(prompt_main.encode("ascii"), timeout)
            
            _sessions[host] = tn
            return tn
        except Exception as e:
            print(f"[ERR] Connect failed {host}: {e}")
            return None

def poll_one_ap(host, user, pwd, prompt_main, prompt_sub, target_ssids):
    for attempt in range(2):
        tn = get_session(host, user, pwd, prompt_main)
        if not tn:
            return None 

        try:
            tn.write(b"wless2\n")
            tn.read_until(prompt_main.encode("ascii"), 3)
            tn.write(b"network\n")
            tn.read_until(prompt_sub.encode("ascii"), 3)
            tn.write(b"apscan\n")
            
            # INCREASED TIMEOUT to 8s
            raw_data = tn.read_until(prompt_sub.encode("ascii"), 8).decode(errors="ignore")
            
            if len(raw_data) < 50:
                 # It might be empty, but let's print it to see WHAT it is
                 print(f"[WARN] {host} Low Data ({len(raw_data)}B). Raw dump: {repr(raw_data)}")
                 raise ValueError("Insufficient data.")

            rows = parse_apscan_table(raw_data)
            
            if not rows:
                # DUMP RAW DATA TO DEBUG WAP13
                print(f"[WARN] {host} Parsed 0 rows. Raw dump start: {repr(raw_data[:100])}...")
                return {}

            results = {}
            for r in rows:
                if r["ssid"] in target_ssids:
                    results[r["ssid"]] = int(r["signal"])
            
            return results

        except Exception as e:
            print(f"[WARN] Polling {host} attempt {attempt+1} failed: {e}")
            with _lock:
                if host in _sessions:
                    try: _sessions[host].close()
                    except: pass
                    del _sessions[host]
            
            time.sleep(0.5)
            
            if attempt == 1:
                return {}
    return {}

def close_all_sessions():
    with _lock:
        for host, tn in _sessions.items():
            try:
                tn.close()
            except: pass
        _sessions.clear()