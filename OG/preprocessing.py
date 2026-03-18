from typing import Dict, List
from collections import deque
import statistics

class RSSIFilter:
    """
    Encapsulates the rolling average history to prevent global state collisions.
    Translates directly to a stateful C++ class via pybind11.
    """
    def __init__(self, window_size: int, noise_floor_dbm: float):
        # Structure: self.history[AP_ID][Device_ID] = deque([rssi1, rssi2, ...])
        self.history: Dict[str, Dict[str, deque]] = {}
        self.window_size = window_size
        self.noise_floor_dbm = noise_floor_dbm

    def initialize_history(self, ap_ids: List[str], device_ssids: List[str]):
        """
        Initializes the rolling history queues for APs and Targets.
        """
        for ap_id in ap_ids:
            self.history[ap_id] = {}
            for ssid in device_ssids:
                # maxlen automatically handles popping old values when new ones are added
                self.history[ap_id][ssid] = deque(maxlen=self.window_size)
        
        print(f"[INFO] Initialized RSSIFilter history for {len(ap_ids)} APs tracking {len(device_ssids)} devices.")

    def process_rssi(self, raw_rssi: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, float]]:
        """
        Applies noise filtering and rolling averages over the instantiated window state.
        """
        processed_rssi: Dict[str, Dict[str, float]] = {}

        for ap_name, device_rssi_map in raw_rssi.items():
            processed_rssi[ap_name] = {}

            for ssid, raw_rssi_val in device_rssi_map.items():
                
                # 1. Noise Floor Filter
                if raw_rssi_val < self.noise_floor_dbm:
                    continue

                # 2. Dynamic History Initialization 
                # Catch new APs or devices added during runtime
                if ap_name not in self.history: 
                    self.history[ap_name] = {}
                
                if ssid not in self.history[ap_name]:
                    self.history[ap_name][ssid] = deque(maxlen=self.window_size)

                # 3. Update Rolling History
                history_queue = self.history[ap_name][ssid]
                history_queue.append(raw_rssi_val)

                # 4. Apply Smoothing
                if len(history_queue) > 0:
                    processed_rssi[ap_name][ssid] = statistics.mean(history_queue)

        return processed_rssi