# Capstone Localization — Telemetry Engine

Real-time indoor localization tracking a target device across a defined floor plan using WiFi RSSI (TP-Link EAP350 APs) and, optionally, Time-of-Flight ranging (ESP32-C3 anchors).

---

## Repository Layout

```
CAPSTONE_LOCALIZATION/
├── OG/               # Original pure-Python monolith (fully working, production-stable)
└── Hybrid/           # New architecture: Python I/O + C++ math (pybind11)
    ├── CMakeLists.txt
    ├── Dockerfile
    ├── config.yaml
    ├── requirements.txt
    ├── core_cpp/             # C++ engines (compiled → capstone_core.so)
    │   ├── math_core/        # trilateration, geometry, knn, signal filters
    │   ├── parsers/          # EAP350 APSCAN table parser
    │   └── engines/          # rssi_engine, fingerprint_engine, tof_engine
    ├── src_python/           # Python orchestrator + FastAPI server
    │   ├── app.py            # Entry point — FastAPI + asyncio poll loop
    │   ├── models.py         # DTOs only (no math)
    │   ├── engine_wrappers.py# Python ↔ C++ bridge
    │   ├── data_pipes.py     # TelnetPipe (WiFi) + MQTTPipe (ESP32 ToF)
    │   └── cloud_io.py       # S3, CSV log, radiomap.json
    └── tests/
        └── test_capstone_core.py
```

> **Golden Rule:** If it _waits_ (I/O, network, cloud) → Python. If it _calculates_ (trilateration, filtering, geometry, KNN) → C++.

---

## Running the Hybrid Architecture

### Option A — Docker (recommended, no local toolchain needed)

```bash
cd Hybrid

# Build image (compiles C++ inside the container)
docker build -t capstone-hybrid .

# Run — API served on http://localhost:8000
# .env lives at the repo root, one level up from Hybrid/
docker run --rm -p 8000:8000 --env-file ../.env capstone-hybrid
```

To persist the CSV log between runs, mount a volume:

```bash
docker run --rm -p 8000:8000 --env-file ../.env \
  -v $(pwd)/data:/app/src_python/data \
  capstone-hybrid
```

---

### Option B — Local (Linux / WSL / macOS)

**Prerequisites:** Python 3.11+, CMake ≥ 3.16, g++ with C++17 support.

**1. Install Python dependencies**

```bash
cd Hybrid
pip install -r requirements.txt
```

**2. Build the C++ module**

```bash
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release \
         -Dpybind11_DIR=$(python3 -m pybind11 --cmakedir)
cmake --build . --parallel
cd ..
```

This compiles all C++ engines and copies `capstone_core.<platform>.so` into `src_python/` automatically.

**3. Verify the build**

```bash
python tests/test_capstone_core.py
# Expected: 19 passed, 0 failed
```

**4. Start the server**

```bash
cd src_python
python app.py
# or
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

API is now live at **http://localhost:8000**

---

### Option C — Local (Windows, native MSVC)

Open a **VS 2022 Developer Command Prompt**, then:

```bat
cd Hybrid
pip install -r requirements.txt

mkdir build && cd build
cmake .. -G "Visual Studio 17 2022" -A x64 ^
         -Dpybind11_DIR=<output of: python -m pybind11 --cmakedir>
cmake --build . --config Release
cd ..

python tests\test_capstone_core.py
cd src_python && python app.py
```

---

## Environment Variables (`.env`)

Create `Hybrid/.env` — never commit this file:

```env
AWS_ACCESS_KEY_ID=YOUR_KEY
AWS_SECRET_ACCESS_KEY=YOUR_SECRET
AWS_BUCKET_NAME=capstone-telemetry-bucket
AWS_REGION=us-east-2
```

S3 is optional. If these are not set, the engine still runs and logs to `telemetry_log.csv` locally.

---

## Configuration (`config.yaml`)

All runtime behaviour is controlled by `Hybrid/config.yaml`. Key fields:

| Field                                | Default         | Description                                   |
| ------------------------------------ | --------------- | --------------------------------------------- |
| `system.localization_method`         | `trilateration` | `"trilateration"` or `"fingerprinting"`       |
| `system.rolling_average_window`      | `5`             | Smoothing window fed into C++ RSSIFilter      |
| `telemetry_config.poll_interval_s`   | `3`             | How often each AP is polled                   |
| `telemetry_config.update_interval_s` | `60`            | Aggregation + verdict window                  |
| `locations.*.floors.*.wifi_aps`      | —               | AP host IPs and (x, y) coordinates            |
| `locations.*.floors.*.tof_anchors`   | `{}`            | ESP32-C3 MAC addresses and (x, y) coordinates |
| `locations.*.floors.*.rooms`         | —               | Room polygons (metres from floor-plan origin) |

**Hot-swap localization method** without restarting:

```bash
curl -X POST http://localhost:8000/config/reload
```

---

## API Endpoints

| Method | Path                  | Description                                     |
| ------ | --------------------- | ----------------------------------------------- |
| GET    | `/health`             | Liveness check, poll loop status                |
| GET    | `/status`             | Last verdict timestamp, decision count          |
| GET    | `/map`                | Floor geometry + live device positions (for UI) |
| GET    | `/decisions?limit=50` | Recent localization decisions                   |
| GET    | `/devices`            | Target device reachability                      |
| GET    | `/raw`                | Last raw RSSI snapshot from APs                 |
| GET    | `/logs?severity=WARN` | System logs (filterable)                        |
| GET    | `/config`             | Current loaded config                           |
| POST   | `/config/reload`      | Re-read config.yaml from disk                   |
| POST   | `/config/upload`      | Upload a new config.yaml via multipart          |
| POST   | `/survey/{room}`      | Add a fingerprint sample to radiomap.json       |

---

## RF Fingerprinting (optional)

Fingerprinting is more reliable than trilateration in cluttered environments. To use it:

**1. Collect calibration samples**

Stand in a room and POST its RSSI snapshot to the survey endpoint. Collect at least 10 samples per room, including hallways and "outside" areas:

```bash
curl -X POST http://localhost:8000/survey/472 \
     -H "Content-Type: application/json" \
     -d '{"AP31": -45.0, "AP32": -55.0, "AP33": -68.0}'
```

This appends to `radiomap.json` automatically.

**2. Switch method**

In `config.yaml`, set:

```yaml
system:
  localization_method: "fingerprinting"
```

Then `POST /config/reload`.

---

## Adding ESP32-C3 ToF Anchors

When the hardware is on-site, populate `tof_anchors` in `config.yaml`:

```yaml
tof_anchors:
  ESP_1:
    mac: "AA:BB:CC:DD:EE:01"
    x: 2.0
    y: 5.0
```

Each ESP32-C3 must publish JSON to the MQTT topic `capstone/<mac>/tof`:

```json
{ "mac": "AA:BB:CC:DD:EE:01", "distance_m": 1.85, "ts": "2024-01-01T12:00:00Z" }
```

Configure the broker in `config.yaml` under `mqtt:` and restart the server.

---

## VS Code IntelliSense

IntelliSense **requires a C++ compiler to be reachable** so it can locate system headers like `<string>`. Pick one of the three options below, then select the matching configuration.

### Option 1 — MSYS2 / MinGW-w64 (easiest Windows install)

1. Download and run the installer from **https://www.msys2.org**
2. Open the **MSYS2 MinGW 64-bit** terminal and run:
   ```bash
   pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-cmake
   ```
3. Add `C:\msys64\mingw64\bin` to your Windows **System PATH**
   (`Settings → System → Advanced system settings → Environment Variables → Path → New`)
4. Restart VS Code
5. `Ctrl+Shift+P` → **C/C++: Select a Configuration** → **MSYS2 / MinGW-w64 (recommended on Windows)**

### Option 2 — Dev Containers (zero local install, runs inside Docker)

Requires **Docker Desktop** and the VS Code **Dev Containers** extension (`ms-vscode-remote.remote-containers`).

1. Install Docker Desktop from **https://www.docker.com/products/docker-desktop**
2. Install the **Dev Containers** extension in VS Code
3. Open the Command Palette: `Ctrl+Shift+P` → **Dev Containers: Reopen in Container**

VS Code reopens inside the Docker build image where `g++` and all system headers are already present. IntelliSense, build, and run all work without any local toolchain. The `builder` stage of `Hybrid/Dockerfile` is used.

### Option 3 — WSL2 (if already installed)

1. Open VS Code, install the **WSL** extension (`ms-vscode-remote.remote-wsl`)
2. `Ctrl+Shift+P` → **WSL: Reopen Folder in WSL**
3. Inside WSL, install build tools if not present:
   ```bash
   sudo apt update && sudo apt install -y build-essential cmake python3-pip
   pip3 install pybind11
   ```
4. Select the **WSL2 (GCC inside Windows Subsystem for Linux)** configuration

Include paths for project headers are pre-configured in `.vscode/c_cpp_properties.json` for all three options.

---

## Running the OG (Pure Python) System

The original monolith in `OG/` remains fully functional and is the stable fallback.

```bash
cd OG
pip install -r requirements.txt

# Terminal 1 — API server
python api_server.py

# Terminal 2 — Telemetry agent
python telemetry_agent.py
```

The web UI is served at **http://localhost:8000** via the `index.html` in the same folder.
