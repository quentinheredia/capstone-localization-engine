# IPS Platform — Indoor Positioning System

Real-time indoor localization that tracks devices across campus floor plans using WiFi RSSI trilateration, RF fingerprinting (KNN), BLE, and Time-of-Flight. Includes a full desktop management app for configuring and monitoring the system.

---

## For Teammates — Getting Started

### What You Need

| Requirement | Version | Download |
|---|---|---|
| Python | 3.11 or newer | https://python.org/downloads |
| PostgreSQL | 15 or newer | https://postgresql.org/download or Docker |
| Git | Any | https://git-scm.com |
| Docker (optional) | Desktop | https://docker.com/products/docker-desktop |

> **Windows users:** Python must be on your PATH. When installing Python, check the box that says "Add python.exe to PATH".

---

### Step 1 — Clone the Repo

```bash
git clone <your-repo-url>
cd CAPSTONE_LOCALIZATION
```

---

### Step 2 — Set Up PostgreSQL

The platform backend needs a Postgres database. Pick whichever option is easiest:

**Option A: Docker (no Postgres install needed)**

```bash
docker run --name ips-postgres \
  -e POSTGRES_USER=ips \
  -e POSTGRES_PASSWORD=ips \
  -e POSTGRES_DB=ips_platform \
  -p 5432:5432 \
  -d postgres:15
```

Run this once. After that, start it again any time with:
```bash
docker start ips-postgres
```

**Option B: System PostgreSQL (if you already have it installed)**

```bash
# Windows
net start postgresql-x64-15

# macOS (Homebrew)
brew services start postgresql@15

# Linux
sudo systemctl start postgresql
```

Then create the database:
```bash
psql -U postgres -c "CREATE USER ips WITH PASSWORD 'ips';"
psql -U postgres -c "CREATE DATABASE ips_platform OWNER ips;"
```

---

### Step 3 — Install Python Dependencies

Open a terminal in the repo root and run all three:

```bash
# Desktop app
pip install pywebview requests

# Platform backend (admin API + database)
pip install -r platform/backend/requirements.txt

# Hybrid engine (localization)
pip install -r Hybrid/requirements.txt
```

> If you see `pip: command not found`, try `pip3` instead. On Windows you may need to run as Administrator.

---

### Step 4 — Create the `.env` File

Copy the example and fill in your AWS credentials (S3 logging is optional — the system works without it):

```bash
cp .env.example .env
```

If there is no `.env.example`, create `.env` in the repo root:

```env
AWS_ACCESS_KEY_ID=your_key_here
AWS_SECRET_ACCESS_KEY=your_secret_here
AWS_DEFAULT_REGION=us-east-1
S3_BUCKET_NAME=capstone-telemetry-bucket
MQTT_BROKER=localhost
MQTT_PORT=1883
```

> S3 is only needed for cloud logging. Leave the AWS fields blank if you don't have credentials — the system still runs and logs locally.

---

### Step 5 — Run the Desktop App

**Windows:** Double-click `IPS Platform.bat` in the repo root.

**Any OS (or if you prefer the terminal):**
```bash
python desktop_app.py
```

This opens a native desktop window and automatically starts both backend servers. The app is ready when the loading screen disappears and the sidebar appears.

That's it. Everything runs from one command.

---

## What the Desktop App Does

When you run `desktop_app.py` it:

1. Starts the **Platform Backend** on port 8080 (manages your campus/floor/room configuration in Postgres)
2. Starts the **Hybrid Engine** on port 8000 (the actual localization engine — polls APs, runs C++ math, tracks devices)
3. Opens the **Management UI** in a native desktop window

When you close the window, both servers shut down cleanly.

---

## Running the Backends Manually (for development)

If you prefer to run things separately, open three terminals:

**Terminal 1 — PostgreSQL** (skip if already running)
```bash
docker start ips-postgres
```

**Terminal 2 — Platform Backend** (port 8080)
```bash
cd platform/backend
python main.py
```

**Terminal 3 — Hybrid Engine** (port 8000)
```bash
cd Hybrid
python src_python/app.py
```

Then open `platform/frontend/index.html` directly in a browser.

---

## Repo Structure

```
CAPSTONE_LOCALIZATION/
│
├── desktop_app.py                  # ← Run this to start everything
│
├── platform/
│   ├── frontend/
│   │   └── index.html              # Management UI (React, single file)
│   └── backend/
│       ├── main.py                 # FastAPI admin API (port 8080)
│       ├── models/                 # SQLAlchemy: campus, building, floor, room, anchor, tag
│       ├── api/                    # REST routes
│       └── config_gen/             # Generates Hybrid config.yaml from database
│
├── Hybrid/                         # Localization engine (port 8000)
│   ├── src_python/
│   │   └── app.py                  # FastAPI + asyncio poll loop
│   ├── core_cpp/                   # C++ math engines (trilateration, KNN, ToF)
│   ├── CMakeLists.txt              # pybind11 build
│   └── config.yaml                 # AP positions, floor layout, targets
│
├── OG/                             # Original Python monolith (stable fallback)
│   ├── api_server.py
│   └── telemetry_agent.py
│
└── .env                            # AWS + MQTT credentials (never commit this)
```

---

## The Management UI

The desktop app loads the UI automatically. If running manually, open `platform/frontend/index.html` in a browser.

**Setup Wizard** — First-time configuration. Walk through:
- Create a campus (e.g. "Carleton University")
- Add a building (e.g. "Canal Building")
- Add floors with dimensions in metres
- Draw rooms by entering polygon coordinates
- Place anchors (APs and ESP32s) with their IP/MAC and physical position

**Map View** — Live floor plan rendered from your polygon coordinates. Shows room outlines, anchor positions, and real-time device location (updates every 3 seconds when the engine is running).

**Devices** — Tables for all anchors (APs, ESP32s) and tracked tags. Shows status, last poll time, and current localization estimates.

**Logs** — Boundary crossings, alerts, and live engine logs.

---

## Configuration (`Hybrid/config.yaml`)

Controls the localization engine. The platform backend can generate this file for you from the database via the Setup Wizard. Key fields:

| Field | Default | Description |
|---|---|---|
| `system.localization_method` | `trilateration` | `"trilateration"` or `"fingerprinting"` |
| `telemetry_config.poll_interval_s` | `3` | How often each AP is polled (seconds) |
| `telemetry_config.update_interval_s` | `60` | How often a verdict is emitted (seconds) |
| `locations.*.floors.*.wifi_aps` | — | AP host IPs and (x, y) coordinates |
| `locations.*.floors.*.rooms` | — | Room polygons in metres from floor origin |

Hot-swap the localization method without restarting:
```bash
curl -X POST http://localhost:8000/config/reload
```

---

## Engine API (port 8000)

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness check, poll loop status |
| GET | `/status` | Last verdict timestamp, decision count |
| GET | `/map` | Floor geometry + live device positions |
| GET | `/decisions?limit=50` | Recent localization decisions |
| GET | `/devices` | Target device reachability |
| GET | `/raw` | Last raw RSSI snapshot from APs |
| GET | `/logs?severity=WARN` | System logs |
| POST | `/config/reload` | Re-read config.yaml from disk |
| POST | `/survey/{room}` | Add a fingerprint sample for a room |

---

## Building the C++ Engine (Hybrid only, optional)

The C++ engines are pre-compiled in the Docker image. If you're running locally and want to compile them:

**Linux / macOS / WSL**
```bash
cd Hybrid
pip install pybind11
mkdir -p build && cd build
cmake .. -Dpybind11_DIR=$(python3 -m pybind11 --cmakedir)
cmake --build . --parallel
cd ..
python tests/test_capstone_core.py  # should show 19 passed
```

**Windows (MSYS2 recommended)**
```bash
# Install MSYS2 from https://msys2.org, then in the MinGW 64-bit terminal:
pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake
# Add C:\msys64\mingw64\bin to your Windows PATH, then:
cd Hybrid
mkdir build && cd build
cmake .. -Dpybind11_DIR=$(python -m pybind11 --cmakedir)
cmake --build . --parallel
```

**Windows (VS 2022 Developer Command Prompt)**
```bat
cd Hybrid
mkdir build && cd build
cmake .. -G "Visual Studio 17 2022" -A x64 -Dpybind11_DIR=<output of: python -m pybind11 --cmakedir>
cmake --build . --config Release
```

---

## RF Fingerprinting (optional, more accurate)

Fingerprinting is more reliable than trilateration in cluttered environments.

**1. Collect calibration samples** — Stand in a room with the target device and POST its RSSI snapshot. Aim for at least 10 samples per room:

```bash
curl -X POST http://localhost:8000/survey/Room101 \
  -H "Content-Type: application/json" \
  -d '{"AP1": -45.0, "AP2": -55.0, "AP3": -68.0}'
```

**2. Switch the method** in `Hybrid/config.yaml`:
```yaml
system:
  localization_method: "fingerprinting"
```

Then reload: `curl -X POST http://localhost:8000/config/reload`

---

## Hardware

| Device | Role | Connection |
|---|---|---|
| TP-Link EAP350 APs | RSSI source | Telnet on 192.168.1.0/24 |
| ESP32-C3 boards | ToF + BLE ranging | MQTT |
| NothingPhone | Tracked target | Detected via SSID/RSSI |

AP IPs are configured in `Hybrid/config.yaml` under `wifi_aps`. The engine polls them on the same network.

---

## Troubleshooting

**`psycopg2.OperationalError: connection refused`**
PostgreSQL isn't running. Start it with `docker start ips-postgres` or `net start postgresql-x64-15`.

**`ModuleNotFoundError: No module named 'webview'`**
Run `pip install pywebview requests`.

**`ModuleNotFoundError: No module named 'capstone_core'`**
The C++ module hasn't been compiled. Either build it (see above) or use Docker.

**`Port 8080/8000 already in use`**
A previous server session is still running. Kill it:
```bash
# Windows
netstat -ano | findstr :8080
taskkill /PID <pid> /F

# macOS/Linux
lsof -ti:8080 | xargs kill
```

**Frontend shows "Engine offline"**
The Hybrid engine (port 8000) isn't reachable. Check that `python src_python/app.py` is running and that `config.yaml` loaded without errors.

---

## OG System (original Python monolith)

The original system in `OG/` is fully functional and production-stable. Use it as a fallback if the Hybrid architecture has issues.

```bash
cd OG
pip install -r requirements.txt

# Terminal 1
python api_server.py

# Terminal 2
python telemetry_agent.py
```

Web UI is available at `http://localhost:8000` via `OG/index.html`.
