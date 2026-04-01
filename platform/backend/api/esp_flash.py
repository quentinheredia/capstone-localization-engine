"""
esp_flash.py — ESP32 firmware build + flash router.

Endpoints
---------
  GET  /api/v1/esp/ports          List available serial ports
  POST /api/v1/esp/flash          Trigger build + flash for a device
  GET  /api/v1/esp/flash/stream   SSE stream of build/flash log lines
  POST /api/v1/esp/flash/cancel   Cancel a running job

Build pipeline
--------------
  1. Validate prerequisites (IDF_PATH env var, idf.py on PATH, pyserial)
  2. Generate device_config.h from the request body
  3. Write it into the firmware source tree
  4. Spawn:  idf.py build   (in the firmware project dir)
  5. Spawn:  idf.py -p <port> flash
  6. Stream stdout/stderr line-by-line to the SSE endpoint

Server connectivity note
------------------------
  The ESP32 STA interface connects to the same WiFi network as the
  localization server.  MQTT_BROKER_URI should point to the server's
  IP address on that network so the ESP can reach the broker
  (e.g. "mqtt://192.168.1.100:1883").  The /api/v1/esp/flash endpoint
  accepts mqtt_broker_uri as an explicit field so the UI can pre-fill
  the server's own address.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import textwrap
import threading
import time
from pathlib import Path
from typing import AsyncIterator, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/esp", tags=["ESP Flash"])

# ---------------------------------------------------------------------------
# Firmware root — resolved relative to this file's location
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[3]   # CAPSTONE_LOCALIZATION/
# Prefer the new firmware/ layout; fall back to legacy MQTT_TOF/espfix/
_FW_ROOT   = (
    _REPO_ROOT / "firmware"
    if (_REPO_ROOT / "firmware").is_dir()
    else _REPO_ROOT / "MQTT_TOF" / "espfix"
)

# ---------------------------------------------------------------------------
# Job state (one job at a time)
# ---------------------------------------------------------------------------
_job: Dict = {
    "running":    False,
    "log_lines":  [],       # accumulated output lines
    "success":    None,     # True / False / None (in progress)
    "error":      None,
    "started_at": None,
}
_job_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class FlashRequest(BaseModel):
    """Parameters for a single build+flash operation."""
    device_type:      str            # "anchor" or "tag"
    device_num:       int            # 0, 1, 2 ... (ANCHOR_ID / TAG_NUM)
    serial_port:      str            # e.g. "/dev/ttyUSB0" or "COM3"
    wifi_ssid:        str            # STA network SSID
    wifi_pass:        str = ""       # STA network password (empty = open)
    mqtt_broker_uri:  str            # e.g. "mqtt://192.168.1.100:1883"
    location_id:      str = "lab_1"
    floor_id:         str = "floor_1"
    # Anchor-only fields
    anchor_x_cm:      int = 0
    anchor_y_cm:      int = 0
    ap_channel:       int = 6
    # Tag-only fields
    tag_ap_channel:   int = 1


# ---------------------------------------------------------------------------
# Helper: list serial ports
# ---------------------------------------------------------------------------

@router.get("/ports")
def list_ports():
    """Return available serial ports detected by pyserial."""
    try:
        import serial.tools.list_ports as lp
        ports = [
            {"port": p.device, "description": p.description}
            for p in lp.comports()
        ]
        return {"ports": ports}
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="pyserial not installed — run: pip install pyserial",
        )


# ---------------------------------------------------------------------------
# Helper: prerequisite validation
# ---------------------------------------------------------------------------

def _check_prerequisites() -> Optional[str]:
    """Return an error string if prerequisites are missing, else None."""
    if not shutil.which("idf.py"):
        return (
            "idf.py not found on PATH. "
            "Source the ESP-IDF export script first: "
            ". $IDF_PATH/export.sh"
        )
    idf_path = os.environ.get("IDF_PATH")
    if not idf_path or not Path(idf_path).is_dir():
        return (
            "IDF_PATH environment variable is not set or points to a "
            "non-existent directory. "
            "Source the ESP-IDF export script: . $IDF_PATH/export.sh"
        )
    return None


# ---------------------------------------------------------------------------
# Helper: generate device_config.h
# ---------------------------------------------------------------------------

def _generate_config_header(req: FlashRequest) -> str:
    """Render a device_config.h string for the given request."""
    if req.device_type == "anchor":
        return textwrap.dedent(f"""\
            /* AUTO-GENERATED by esp_flash.py — DO NOT EDIT MANUALLY */
            #pragma once

            #define ANCHOR_ID           {req.device_num}
            #define ANCHOR_X_CM         {req.anchor_x_cm}
            #define ANCHOR_Y_CM         {req.anchor_y_cm}

            #define LOCATION_ID         "{req.location_id}"
            #define FLOOR_ID            "{req.floor_id}"

            #define WIFI_STA_SSID       "{req.wifi_ssid}"
            #define WIFI_STA_PASS       "{req.wifi_pass}"
            #define MQTT_BROKER_URI     "{req.mqtt_broker_uri}"

            #define AP_CHANNEL          {req.ap_channel}
        """)
    else:  # tag
        device_id = f"tag_{req.device_num}"
        ip_last   = req.device_num + 100   # e.g. tag_0 → 10.0.0.100
        return textwrap.dedent(f"""\
            /* AUTO-GENERATED by esp_flash.py — DO NOT EDIT MANUALLY */
            #pragma once

            #define TAG_NUM             {req.device_num}
            #define DEVICE_ID           "{device_id}"

            #define LOCATION_ID         "{req.location_id}"
            #define FLOOR_ID            "{req.floor_id}"

            #define WIFI_STA_SSID       "{req.wifi_ssid}"
            #define WIFI_STA_PASS       "{req.wifi_pass}"
            #define MQTT_BROKER_URI     "{req.mqtt_broker_uri}"

            #define TAG_AP_CHANNEL      {req.tag_ap_channel}
            #define TAG_AP_IP_LAST      {ip_last}
        """)


# ---------------------------------------------------------------------------
# Background flash job
# ---------------------------------------------------------------------------

def _run_flash_job(req: FlashRequest, fw_dir: Path) -> None:
    """Run idf.py build + flash in a background thread.  Writes to _job."""

    def _emit(line: str) -> None:
        with _job_lock:
            _job["log_lines"].append(line)

    def _run_cmd(cmd: list[str], cwd: Path) -> int:
        """Run *cmd* in *cwd*, streaming output to _emit(). Returns exit code."""
        _emit(f"$ {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in proc.stdout:
                _emit(line.rstrip())
            proc.wait()
            return proc.returncode
        except FileNotFoundError as exc:
            _emit(f"ERROR: {exc}")
            return 1

    try:
        config_path = fw_dir / "main" / "device_config.h"
        header      = _generate_config_header(req)
        config_path.write_text(header)
        _emit(f"[esp_flash] Wrote device_config.h → {config_path}")
        _emit(header)

        # Step 1: build
        _emit(f"\n[esp_flash] ── BUILDING ({req.device_type}_{req.device_num}) ──")
        rc = _run_cmd(["idf.py", "build"], fw_dir)
        if rc != 0:
            with _job_lock:
                _job["success"] = False
                _job["error"]   = f"idf.py build failed (exit {rc})"
                _job["running"] = False
            return

        # Step 2: flash
        _emit(f"\n[esp_flash] ── FLASHING → {req.serial_port} ──")
        rc = _run_cmd(
            ["idf.py", "-p", req.serial_port, "flash"],
            fw_dir,
        )
        if rc != 0:
            with _job_lock:
                _job["success"] = False
                _job["error"]   = f"idf.py flash failed (exit {rc})"
                _job["running"] = False
            return

        _emit(f"\n[esp_flash] ✓ Flash complete for {req.device_type}_{req.device_num}")
        with _job_lock:
            _job["success"] = True
            _job["running"] = False

    except Exception as exc:
        with _job_lock:
            _job["success"] = False
            _job["error"]   = str(exc)
            _job["running"] = False
        _emit(f"[esp_flash] EXCEPTION: {exc}")


# ---------------------------------------------------------------------------
# POST /flash  — start a build+flash job
# ---------------------------------------------------------------------------

@router.post("/flash")
def start_flash(req: FlashRequest):
    """Validate prerequisites, write device_config.h, start build+flash."""
    with _job_lock:
        if _job["running"]:
            raise HTTPException(
                status_code=409,
                detail="A flash job is already running. Cancel it first.",
            )

    err = _check_prerequisites()
    if err:
        raise HTTPException(status_code=503, detail=err)

    if req.device_type not in ("anchor", "tag"):
        raise HTTPException(
            status_code=422,
            detail="device_type must be 'anchor' or 'tag'",
        )

    fw_dir = _FW_ROOT / req.device_type
    if not fw_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Firmware directory not found: {fw_dir}",
        )

    try:
        import serial.tools.list_ports as lp
        available = [p.device for p in lp.comports()]
        if req.serial_port not in available:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Serial port '{req.serial_port}' not found. "
                    f"Available: {available}"
                ),
            )
    except ImportError:
        pass   # pyserial missing — allow anyway, idf.py will fail with a clear message

    with _job_lock:
        _job["running"]    = True
        _job["log_lines"]  = []
        _job["success"]    = None
        _job["error"]      = None
        _job["started_at"] = time.time()

    thread = threading.Thread(
        target=_run_flash_job,
        args=(req, fw_dir),
        daemon=True,
    )
    thread.start()

    return {
        "status":    "started",
        "device":    f"{req.device_type}_{req.device_num}",
        "port":      req.serial_port,
        "stream_url": "/api/v1/esp/flash/stream",
    }


# ---------------------------------------------------------------------------
# GET /flash/stream  — SSE log stream
# ---------------------------------------------------------------------------

@router.get("/flash/stream")
def stream_flash_log():
    """Server-Sent Events stream of build/flash log lines."""

    async def _event_generator() -> AsyncIterator[str]:
        sent = 0
        while True:
            with _job_lock:
                lines    = _job["log_lines"]
                running  = _job["running"]
                success  = _job["success"]
                error    = _job["error"]

            # Send any new lines
            new_lines = lines[sent:]
            for line in new_lines:
                yield f"data: {line}\n\n"
            sent += len(new_lines)

            if not running:
                if success is True:
                    yield "data: [DONE] Flash succeeded.\n\n"
                elif success is False:
                    yield f"data: [ERROR] {error or 'Flash failed.'}\n\n"
                yield "event: close\ndata: done\n\n"
                break

            await asyncio.sleep(0.25)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ---------------------------------------------------------------------------
# GET /flash/status  — one-shot job status
# ---------------------------------------------------------------------------

@router.get("/flash/status")
def flash_status():
    """Return current job state without streaming."""
    with _job_lock:
        return {
            "running":    _job["running"],
            "success":    _job["success"],
            "error":      _job["error"],
            "lines":      len(_job["log_lines"]),
            "started_at": _job["started_at"],
        }


# ---------------------------------------------------------------------------
# POST /flash/cancel  — kill the running job
# ---------------------------------------------------------------------------

@router.post("/flash/cancel")
def cancel_flash():
    """Mark the job as cancelled.  The background thread will exit naturally
    once the current subprocess finishes; we can't kill it mid-build without
    risking a corrupted binary.  Cancellation prevents the *next* step
    (e.g. prevents flash after a completed build)."""
    with _job_lock:
        if not _job["running"]:
            return {"status": "no job running"}
        _job["running"] = False
        _job["success"] = False
        _job["error"]   = "Cancelled by user"
        _job["log_lines"].append("[esp_flash] Job cancelled by user")
    return {"status": "cancelled"}
