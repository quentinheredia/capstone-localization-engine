# Build IPS Platform Desktop App (.exe)

## Overview
This creates a single `.exe` file that bundles everything: Python backends + HTML/JS frontend into one native Windows application.

## Prerequisites

1. **Python 3.8+** (Windows)
2. **PostgreSQL** running locally (or via Docker)
3. **Git** (optional, for version control)

## Quick Start

### 1. Install Dependencies

```bash
# Desktop app dependencies
pip install -r desktop_requirements.txt

# Platform backend dependencies
cd platform/backend && pip install -r requirements.txt && cd ../..

# Hybrid engine dependencies
cd Hybrid && pip install -r requirements.txt && cd ..
```

### 2. Start PostgreSQL

Either:

**Option A: System PostgreSQL**
```bash
# Windows Service
net start postgresql-x64-15
```

**Option B: Docker**
```bash
docker run --name ips-postgres -e POSTGRES_USER=ips -e POSTGRES_PASSWORD=ips -e POSTGRES_DB=ips_platform -p 5432:5432 -d postgres:15
```

### 3. Test the App (Dev Mode)

```bash
python desktop_app.py
```

This will:
- Start a native window
- Launch Platform Backend (port 8080)
- Launch Hybrid Engine (port 8000)
- Load the frontend HTML

## Building the .exe

### Using PyInstaller (Recommended)

```bash
pip install pyinstaller

pyinstaller \
  --onefile \
  --windowed \
  --name "IPS Platform" \
  --icon=electron/icon.png \
  --add-data "platform/frontend/index.html:." \
  --add-data "platform/backend:platform/backend" \
  --add-data "Hybrid:Hybrid" \
  desktop_app.py
```

Output: `dist/IPS Platform.exe`

### Bundling Python Runtime (Standalone .exe)

If you want users to run it **without Python installed**:

```bash
pip install pyinstaller
pyinstaller \
  --onefile \
  --windowed \
  --name "IPS Platform" \
  --hidden-import=PyQt5 \
  --hidden-import=PyQt5.QtWebEngineWidgets \
  --collect-all PyQt5 \
  desktop_app.py
```

## Creating an Installer (.msi or .exe Installer)

Use **NSIS** or **InnoSetup**:

### With NSIS:
```bash
pip install pyinstaller-hooks-contrib
# (Same PyInstaller command as above)
# Then use NSIS to create an installer that bundles PostgreSQL check
```

## What's Included

- ✅ Full React frontend (Setup Wizard, Map, Devices, Logs)
- ✅ Platform Backend (SQLAlchemy models, REST API)
- ✅ Hybrid Engine (C++ localization with Python bindings)
- ✅ Auto-start both backends when app launches
- ✅ Auto-shutdown when app closes
- ✅ Single executable file (~500MB with Python runtime)

## Troubleshooting

### "PostgreSQL connection refused"
- Ensure PostgreSQL is running: `net start postgresql-x64-15`
- Or use Docker instead

### "Port 8080/8000 already in use"
- Change ports in `desktop_app.py` (PLATFORM_PORT, HYBRID_PORT)
- Or kill processes: `netstat -ano | findstr :8080`

### "Python not found" in built .exe
- Use PyInstaller with `--hidden-import` to bundle Python
- Or include Python runtime in the installer

## Next Steps

1. Test locally with `python desktop_app.py`
2. Build .exe: `pyinstaller --onefile --windowed desktop_app.py`
3. Create installer with NSIS or InnoSetup
4. Distribute `dist/IPS Platform.exe` to users
5. (Optional) Code sign the .exe for Windows SmartScreen

## Size Optimization

- Current: ~500MB (with Python runtime)
- Can reduce to ~200MB by:
  - Removing test files
  - Compressing assets
  - Using UPX packer
  - Building only necessary parts

## Distribution

Users just need to:
1. Run `IPS Platform.exe`
2. Installer optionally prompts to install PostgreSQL (if not present)
3. App starts automatically with all servers running

That's it! No Python installation needed.
