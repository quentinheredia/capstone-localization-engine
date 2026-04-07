"""
One-shot script to update the Lobby Foyer polygon in the Platform database.
Run this on your machine while the Platform API is running:

    python fix_lobby_polygon.py

It calls the Platform API at http://localhost:8080/api/v1 — no direct DB access needed.
"""

import json
import sys
import urllib.request
import urllib.error

BASE = "http://localhost:8080/api/v1"

NEW_POLYGON = [
    [8.217,  39.784],
    [13.817, 40.784],
    [14.617, 27.784],
    [23.817, 27.784],
    [23.817, 40.384],
    [24.217, 40.384],
    [24.217, 43.049],
    [8.117,  43.049],
    [8.117,  39.784],
    [14.617, 39.784],
]
NEW_CENTER_X = 16.167
NEW_CENTER_Y = 35.417


def api_get(path):
    with urllib.request.urlopen(f"{BASE}{path}") as r:
        return json.loads(r.read())


def api_patch(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def main():
    # 1. Find the campus
    campuses = api_get("/campuses")
    campus = next((c for c in campuses if "carleton" in c["name"].lower()), campuses[0])
    print(f"Campus: {campus['name']} (id={campus['id']})")

    # 2. Find Canal Building
    buildings = api_get(f"/campuses/{campus['id']}/buildings")
    building = next((b for b in buildings if "canal" in b["name"].lower()), buildings[0])
    print(f"Building: {building['name']} (id={building['id']})")

    # 3. Find Floor 1
    floors = api_get(f"/buildings/{building['id']}/floors")
    floor = next((f for f in floors if f["name"] in ("1", "Floor 1", "Ground")), floors[0])
    print(f"Floor: {floor['name']} (id={floor['id']})")

    # 4. Find Lobby Foyer room
    rooms = api_get(f"/floors/{floor['id']}/rooms")
    room = next((r for r in rooms if "lobby" in r["name"].lower()), None)
    if room is None:
        print("ERROR: Could not find a room matching 'lobby'. Available rooms:")
        for r in rooms:
            print(f"  - {r['name']} (id={r['id']})")
        sys.exit(1)
    print(f"Room: {room['name']} (id={room['id']})")
    print(f"  Current polygon: {room['polygon']}")

    # 5. PATCH the polygon
    result = api_patch(f"/rooms/{room['id']}", {
        "polygon":  NEW_POLYGON,
        "center_x": NEW_CENTER_X,
        "center_y": NEW_CENTER_Y,
    })
    print(f"\nUpdated successfully!")
    print(f"  New polygon: {result['polygon']}")
    print(f"  New center: ({result['center_x']}, {result['center_y']})")


if __name__ == "__main__":
    main()
