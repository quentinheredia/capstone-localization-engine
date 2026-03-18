#!/usr/bin/env python3
"""
Verification test for the capstone_core pybind11 module.

Run after building:
    cd Hybrid/build && cmake .. && cmake --build . && cd ..
    python tests/test_capstone_core.py

Each test prints PASS/FAIL so you can spot-check without pytest.
"""

import sys, os, math

# Ensure src_python/ (where the .so lands) is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src_python'))

try:
    import capstone_core as cc
    print("[OK] capstone_core imported successfully")
except ImportError as e:
    print(f"[FAIL] Could not import capstone_core: {e}")
    sys.exit(1)

passed = 0
failed = 0

def check(name, condition):
    global passed, failed
    if condition:
        print(f"  PASS  {name}")
        passed += 1
    else:
        print(f"  FAIL  {name}")
        failed += 1


# ── 1. rssi_to_distance_m ──────────────────────────────────────────────────
print("\n=== rssi_to_distance_m ===")
d = cc.rssi_to_distance_m(-22.0, -22.0, 4.0)
check("RSSI == P0 -> ~1 metre", abs(d - 1.0) < 0.01)

d_far = cc.rssi_to_distance_m(-80.0, -22.0, 4.0)
check("Very weak signal -> longer distance", d_far > 5.0)

d_clamp = cc.rssi_to_distance_m(-200.0, -22.0, 4.0)
check("Extreme RSSI clamped to 50m max", d_clamp == 50.0)


# ── 2. refined_trilaterate ─────────────────────────────────────────────────
print("\n=== refined_trilaterate ===")
anchors = [(0.0, 0.0), (6.0, 0.0), (3.0, 7.0)]
dists   = [3.5, 3.5, 4.0]
x, y = cc.refined_trilaterate(anchors, dists, 6.0, 7.0)
check("Position within room bounds", 0.0 <= x <= 6.0 and 0.0 <= y <= 7.0)
check("Roughly centred (x ~3)", abs(x - 3.0) < 1.5)

# Fallback with <2 APs
x2, y2 = cc.refined_trilaterate([(0.0, 0.0)], [1.0], 10.0, 10.0)
check("Single AP -> room centre fallback", x2 == 5.0 and y2 == 5.0)


# ── 3. point_in_polygon ────────────────────────────────────────────────────
print("\n=== point_in_polygon ===")
square = [(0.0, 0.0), (6.0, 0.0), (6.0, 7.0), (0.0, 7.0)]
check("Inside square", cc.point_in_polygon(3.0, 3.5, square))
check("Outside square", not cc.point_in_polygon(-1.0, 3.5, square))
check("Corner edge case", not cc.point_in_polygon(0.0, 0.0, square))


# ── 4. boundary_clamp ──────────────────────────────────────────────────────
print("\n=== boundary_clamp ===")
cr = cc.boundary_clamp(-0.5, 3.0, 6.0, 7.0, 0.01)
check("Negative X clamped to margin", cr.clamped and abs(cr.x - 0.01) < 1e-6)

cr2 = cc.boundary_clamp(3.0, 3.0, 6.0, 7.0, 0.01)
check("Interior point NOT clamped", not cr2.clamped)


# ── 5. classify_position ───────────────────────────────────────────────────
print("\n=== classify_position ===")
room = cc.RoomDef()
room.name = "472"
room.center_x = 3.0
room.center_y = 3.5
room.polygon = square

cls = cc.classify_position(3.0, 3.5, [room], 6.0, 7.0, 0.01, 3.0)
check("Centre of room -> room found", cls.room_name == "472")
check("Centre -> high confidence", cls.confidence > 0.9)

cls2 = cc.classify_position(0.0, 0.0, [room], 6.0, 7.0, 0.01, 3.0)
check("At wall -> clamped -> Outside", cls2.room_name == "Outside Defined Area")


# ── 6. RSSIFilter ──────────────────────────────────────────────────────────
print("\n=== RSSIFilter ===")
filt = cc.RSSIFilter(window_size=3, noise_floor_dbm=-80.0)
v1 = filt.feed("AP31", "NOTHINGPHONE", -40.0)
check("First reading = itself", abs(v1 - (-40.0)) < 0.01)

v2 = filt.feed("AP31", "NOTHINGPHONE", -50.0)
check("Two readings averaged", abs(v2 - (-45.0)) < 0.01)

v_noise = filt.feed("AP31", "NOTHINGPHONE", -90.0)
check("Below noise floor -> -999", v_noise == -999.0)

batch_out = filt.process({"AP31": {"NOTHINGPHONE": -60.0}})
check("Batch process returns dict", "AP31" in batch_out and "NOTHINGPHONE" in batch_out["AP31"])


# ── 7. parse_apscan_table ──────────────────────────────────────────────────
print("\n=== parse_apscan_table ===")
sample_text = """2.4G Scanning ......
BSSID              SSID  LEN  MODE CH SIGNAL ENC   TYPE
AA:BB:CC:DD:EE:FF  My WiFi Net  32  11g  6  -45  WPA2  AP
11:22:33:44:55:66             28  11n  1  -72  WPA   AP
"""
rows = cc.parse_apscan_table(sample_text)
check("Parsed 2 rows", len(rows) == 2)
check("First BSSID correct", rows[0].bssid == "AA:BB:CC:DD:EE:FF")
check("First SSID has spaces", rows[0].ssid == "My WiFi Net")
check("Second SSID is blank", rows[1].ssid == "")

dicts = cc.parse_apscan_table_dicts(sample_text)
check("Dict output matches", dicts[0]["signal"] == "-45")


# ── 8. KNN fingerprinting ─────────────────────────────────────────────────
print("\n=== knn_fingerprint_match ===")
entry1 = cc.RadioMapEntry()
entry1.room = "472"
entry1.vectors = [
    {"AP31": -40.0, "AP32": -55.0},
    {"AP31": -42.0, "AP32": -53.0},
]
entry2 = cc.RadioMapEntry()
entry2.room = "HALL"
entry2.vectors = [
    {"AP31": -70.0, "AP32": -30.0},
]

live = {"AP31": -41.0, "AP32": -54.0}
result = cc.knn_fingerprint_match(live, [entry1, entry2], k=3)
check("KNN matches room 472", result.room == "472")
check("KNN confidence > 0", result.confidence > 0.0)


# ── 9. RSSIEngine (full pipeline) ─────────────────────────────────────────
print("\n=== RSSIEngine ===")
engine = cc.RSSIEngine(
    window_size=3, noise_floor_dbm=-80.0,
    min_aps=2, clamp_margin=0.01,
    max_dist_conf=3.0,
    room_w=6.0, room_h=7.0
)

ap1 = cc.APDef(); ap1.id = "AP31"; ap1.x = 0.0; ap1.y = 0.0
ap2 = cc.APDef(); ap2.id = "AP32"; ap2.x = 6.0; ap2.y = 0.0
ap3 = cc.APDef(); ap3.id = "AP33"; ap3.x = 3.0; ap3.y = 7.0
engine.set_aps([ap1, ap2, ap3])

room_def = cc.RoomDef()
room_def.name = "472"
room_def.center_x = 3.0
room_def.center_y = 3.5
room_def.polygon = square
engine.set_rooms([room_def])

tgt = cc.TargetDef()
tgt.ssid = "NOTHINGPHONE"
tgt.rssi_at_1m = -22.0
tgt.path_loss_n = 4.0

raw_rssi = {
    "AP31": {"NOTHINGPHONE": -45.0},
    "AP32": {"NOTHINGPHONE": -48.0},
    "AP33": {"NOTHINGPHONE": -50.0},
}
results = engine.process_cycle(raw_rssi, [tgt])
check("Engine returns 1 result", len(results) == 1)
check("Engine device_id correct", results[0].device_id == "NOTHINGPHONE")
check("Engine produced coordinates", results[0].x > 0 or results[0].y > 0)
print(f"       -> room={results[0].room}, conf={results[0].confidence:.2f}, "
      f"pos=({results[0].x:.2f}, {results[0].y:.2f})")


# ── Summary ────────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed, {passed+failed} total")
if failed:
    sys.exit(1)
else:
    print("All tests passed!")
