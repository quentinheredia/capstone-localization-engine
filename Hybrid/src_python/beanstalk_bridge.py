"""
beanstalk_bridge.py — Forward localization decisions to the Beanstalk API
so the React frontend (via CloudFront) can display them.

Usage in app.py:
    from beanstalk_bridge import forward_decisions

    # Call after _run_verdict completes, passing the new decisions
    forward_decisions(new_decisions)
"""

import logging
import requests
from typing import List, Dict, Any

log = logging.getLogger("beanstalk_bridge")

# Direct to Beanstalk (not CloudFront) for server-to-server POST
BEANSTALK_URL = "http://capstone-api-env.eba-ppmpxs6n.us-east-2.elasticbeanstalk.com"


def forward_decisions(decisions: List[Dict[str, Any]]) -> None:
    """
    Convert localization decisions into fingerprint format and POST to Beanstalk.

    Each decision dict has:
        device_id, campus_id, building_id, floor_id, room_id,
        timestamp, confidence, rssi_vector, x, y, scan_number, ...

    The Beanstalk API expects:
        {"fingerprints": [{"room": "B7", "floor_id": "floor_1", "vector": {"WAP1": -52, ...}}]}
    """
    if not decisions:
        return

    fingerprints = []
    for d in decisions:
        room     = d.get("room_id", "")
        floor_id = d.get("floor_id", "floor_1")
        vector   = d.get("rssi_vector", {})

        if not room or not vector:
            continue

        # Convert float RSSI values to int (the API model expects int)
        int_vector = {k: int(round(v)) for k, v in vector.items()}

        fingerprints.append({
            "room":     room,
            "floor_id": floor_id,
            "vector":   int_vector,
        })

    if not fingerprints:
        log.debug("No valid fingerprints to forward")
        return

    try:
        resp = requests.post(
            f"{BEANSTALK_URL}/api/fingerprints",
            json={"fingerprints": fingerprints},
            timeout=5,
        )
        resp.raise_for_status()
        batch_id = resp.json().get("batch_id", "unknown")
        log.info(
            "Forwarded %d fingerprints to Beanstalk (batch_id=%s)",
            len(fingerprints), batch_id,
        )
    except Exception as exc:
        # Non-fatal — don't break the main pipeline if Beanstalk is down
        log.warning("Failed to forward to Beanstalk: %s", exc)