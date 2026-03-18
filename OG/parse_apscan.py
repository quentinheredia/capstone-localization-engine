import re
from typing import List, Dict


MAC_RE = re.compile(r"^(?P<bssid>([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})")


def parse_apscan_table(text: str) -> List[Dict[str, str]]:
    """
    Robust parser for EAP350 APSCAN output.
    Handles:
        - blank SSIDs
        - SSIDs with spaces
        - variable whitespace
        - WPA/WPA2 enc formats
        - types like 11g/n, 11ac, 11ax, etc.
    """
    rows = []
    
    for line in text.splitlines():
        line = line.rstrip()

        # Skip headers & nonsense
        if (
            not line
            or line.startswith("2.4G Scanning")
            or line.startswith("Please wait")
            or line.startswith("ath0")
            or line.startswith("BSSID")
        ):
            continue

        # Must begin with MAC
        m = MAC_RE.match(line)
        if not m:
            continue

        bssid = m.group("bssid")

        # Remove the BSSID from the string
        rest = line[len(bssid):].strip()

        # Split remaining columns into tokens
        parts = rest.split()

        # Must have at least: SSID..., LEN, MODE, CH, SIGNAL, ENC, TYPE
        if len(parts) < 6:
            continue

        # Last 6 tokens are fixed format
        type_  = parts[-1]
        enc    = parts[-2]
        signal = parts[-3]
        ch     = parts[-4]
        mode   = parts[-5]
        length = parts[-6]

        # Everything before LEN is SSID
        ssid_tokens = parts[:-6]
        ssid = " ".join(ssid_tokens).strip()

        rows.append({
            "bssid": bssid,
            "ssid": ssid,
            "len": length,
            "mode": mode,
            "ch": ch,
            "signal": signal,
            "enc": enc,
            "type": type_
        })

    return rows
