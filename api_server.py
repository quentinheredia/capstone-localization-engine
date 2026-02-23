import csv
import os
import sys
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
import yaml
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Telemetry Agent API", version="0.1.0")

WEB_ROOT = Path(__file__).parent
RAW_DATA_FILE = WEB_ROOT / "telemetry_log.csv"
PID_FILE = WEB_ROOT / "agent.pid"
CONFIG_ENV_VAR = "TELEMETRY_CONFIG_PATH"
CONFIG_PATHS = [
    Path(os.environ.get(CONFIG_ENV_VAR, "")) if os.environ.get(CONFIG_ENV_VAR) else None,
    WEB_ROOT / "config.yaml",
    WEB_ROOT / "config" / "config.yaml",
]

# Allow local UI fetches; tighten origins later if needed
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory state (single-process)
_state: Dict[str, Any] = {
    "config": None,
    "config_error": None,
    "latest": None,
    "decisions": [],
    "raw": None,
    "logs": [],
    "device_status": {},
    "positions": {},
    "status": {
        "last_update": None,
        "total_decisions": 0,
        "last_device_id": None,
    },
    "accepting": True,
    "agent": {
        "running": False,
        "pid": None,
        "proc": None,
    },
}

MAX_DECISIONS = 200
MAX_LOGS = 500


def _reset_state() -> None:
    _state["config"] = None
    _state["config_error"] = None
    _state["latest"] = None
    _state["decisions"] = []
    _state["raw"] = None
    _state["logs"] = []
    _state["device_status"] = {}
    _state["positions"] = {}
    _state["status"] = {
        "last_update": None,
        "total_decisions": 0,
        "last_device_id": None,
    }
    _state["accepting"] = True
    _state["agent"] = {"running": False, "pid": None, "proc": None}


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _config_path() -> Optional[Path]:
    for p in CONFIG_PATHS:
        if p and p.exists():
            return p
    return None


def _load_config_from_disk() -> Optional[Dict[str, Any]]:
    path = _config_path()
    if not path:
        _state["config_error"] = "config.yaml not found"
        return None

    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        _state["config_error"] = f"Invalid YAML: {e}"
        return None

    if not isinstance(cfg, dict):
        _state["config_error"] = "YAML root must be a mapping"
        return None

    _state["config_error"] = None
    _state["config"] = cfg
    return cfg


def _targets_from_config(cfg: Optional[Dict[str, Any]]) -> List[str]:
    if not cfg:
        return []
    targets = cfg.get("targets", [])
    out = []
    for t in targets:
        if isinstance(t, dict) and "ssid" in t:
            out.append(t["ssid"])
        elif isinstance(t, str):
            out.append(t)
    return out


def _info_from_config(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not cfg:
        return {}
    info = cfg.get("information", {})
    return info if isinstance(info, dict) else {}


def _devices_from_info(info: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    devices: Dict[str, Dict[str, Any]] = {}
    raw = info.get("devices") if isinstance(info, dict) else None
    if isinstance(raw, list):
        for d in raw:
            if not isinstance(d, dict):
                continue
            dev_id = d.get("id") or d.get("ssid") or d.get("name")
            if not dev_id:
                continue
            if "x" in d and "y" in d:
                devices[dev_id] = {"x": d.get("x"), "y": d.get("y"), "source": "information.devices"}
    elif isinstance(raw, dict):
        for dev_id, d in raw.items():
            if isinstance(d, dict) and "x" in d and "y" in d:
                devices[dev_id] = {"x": d.get("x"), "y": d.get("y"), "source": "information.devices"}
    return devices


def _append_log(entry: Dict[str, Any]) -> None:
    _state["logs"].append(entry)
    if len(_state["logs"]) > MAX_LOGS:
        _state["logs"] = _state["logs"][-MAX_LOGS:]


def _decision_severity(conf: Optional[float]) -> str:
    if conf is None:
        return "INFO"
    return "WARN" if conf < 0.5 else "INFO"


def _read_log_file(limit: int = 200) -> List[Dict[str, Any]]:
    if not RAW_DATA_FILE.exists():
        return []
    try:
        with RAW_DATA_FILE.open("r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return []

    items = []
    for row in rows[-limit:]:
        conf = None
        try:
            conf = float(row.get("confidence", ""))
        except Exception:
            conf = None
        items.append({
            "timestamp": row.get("timestamp"),
            "severity": _decision_severity(conf),
            "device_id": row.get("device_id"),
            "message": f"{row.get('device_id')} -> {row.get('room_id')} ({row.get('confidence')})",
        })
    return items


def _update_device_status_from_raw(payload: Dict[str, Any]) -> None:
    cfg = _state.get("config")
    targets = _targets_from_config(cfg)
    results = payload.get("results", {})
    if not isinstance(results, dict):
        return

    seen = {t: False for t in targets} if targets else {}
    for ap_id, devs in results.items():
        if not isinstance(devs, dict):
            continue
        for dev_id in devs.keys():
            if not targets:
                seen.setdefault(dev_id, False)
            seen[dev_id] = True

    ts = payload.get("timestamp", _now_iso())
    for dev_id, reachable in seen.items():
        _state["device_status"][dev_id] = {
            "reachable": bool(reachable),
            "last_scan": ts,
        }


def _update_position_from_decision(payload: Dict[str, Any]) -> None:
    dev_id = payload.get("device_id")
    if not dev_id:
        return
    _state["positions"][dev_id] = {
        "x": payload.get("x"),
        "y": payload.get("y"),
        "room_id": payload.get("room_id"),
        "timestamp": payload.get("timestamp"),
    }


def _positions_from_log_file() -> Dict[str, Dict[str, Any]]:
    if not RAW_DATA_FILE.exists():
        return {}
    try:
        with RAW_DATA_FILE.open("r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return {}

    positions: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        dev_id = row.get("device_id")
        if not dev_id:
            continue
        try:
            x = float(row.get("x", ""))
            y = float(row.get("y", ""))
        except Exception:
            continue
        positions[dev_id] = {
            "x": x,
            "y": y,
            "room_id": row.get("room_id"),
            "timestamp": row.get("timestamp"),
            "source": "telemetry_log.csv",
        }
    return positions


def _agent_cmd() -> List[str]:
    return [sys.executable, str(WEB_ROOT / "telemetry_agent.py")]


def _read_pid_file() -> Optional[int]:
    try:
        if PID_FILE.exists():
            return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    return None


def _is_agent_running() -> bool:
    proc = _state["agent"].get("proc")
    if proc and proc.poll() is None:
        return True
    pid = _state["agent"].get("pid")
    if not pid:
        pid = _read_pid_file()
        if not pid:
            return False
    try:
        os.kill(pid, 0)
    except Exception:
        return False
    return True


@app.get("/health")
def get_health() -> Dict[str, Any]:
    return {
        "ok": True,
        "time": _now_iso(),
        "accepting": _state["accepting"],
    }


@app.get("/")
def get_root():
    index_path = WEB_ROOT / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return JSONResponse({"ok": False, "error": "index.html not found"}, status_code=404)


@app.get("/status")
def get_status() -> Dict[str, Any]:
    status = dict(_state["status"])
    status["agent_running"] = _is_agent_running()
    status["config_error"] = _state["config_error"]
    return status


@app.get("/latest")
def get_latest() -> Dict[str, Any]:
    if not _state["latest"]:
        raise HTTPException(status_code=404, detail="No data received yet")
    return _state["latest"]


@app.get("/decisions")
def get_decisions(limit: int = 50) -> Dict[str, Any]:
    if limit < 1:
        raise HTTPException(status_code=400, detail="limit must be >= 1")
    return {
        "count": len(_state["decisions"]),
        "items": _state["decisions"][-limit:],
    }


@app.get("/raw")
def get_raw() -> Dict[str, Any]:
    if not _state["raw"]:
        raise HTTPException(status_code=404, detail="No raw scans received yet")
    return _state["raw"]


@app.get("/config")
def get_config() -> Dict[str, Any]:
    if _state["config"] is None:
        raise HTTPException(status_code=404, detail="No config posted yet")
    return _state["config"]


@app.post("/config")
def post_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    _state["config"] = payload
    _state["config_error"] = None
    return {"ok": True, "received_at": _now_iso()}


@app.post("/config/upload")
async def post_config_upload(file: UploadFile = File(...)) -> Dict[str, Any]:
    if not file.filename.endswith((".yml", ".yaml")):
        raise HTTPException(status_code=400, detail="Only .yml/.yaml files supported")

    data = await file.read()
    try:
        cfg = yaml.safe_load(data.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")

    if not isinstance(cfg, dict):
        raise HTTPException(status_code=400, detail="YAML root must be a mapping")

    _state["config"] = cfg
    _state["config_error"] = None
    return {"ok": True, "received_at": _now_iso()}


@app.post("/config/reload")
def post_config_reload() -> Dict[str, Any]:
    cfg = _load_config_from_disk()
    if cfg is None:
        raise HTTPException(status_code=404, detail=_state["config_error"] or "config.yaml not found")
    return {"ok": True, "received_at": _now_iso()}


@app.get("/info")
def get_info() -> Dict[str, Any]:
    return {"items": _info_from_config(_state.get("config"))}


@app.get("/devices")
def get_devices() -> Dict[str, Any]:
    cfg = _state.get("config")
    targets = _targets_from_config(cfg)
    if not targets:
        info_targets = list(_devices_from_info(_info_from_config(cfg)).keys())
        targets = info_targets or list(_state["device_status"].keys())

    items = []
    for dev_id in targets:
        st = _state["device_status"].get(dev_id, {})
        items.append({
            "device_id": dev_id,
            "reachable": st.get("reachable", False),
            "last_scan": st.get("last_scan"),
        })
    return {"items": items}


@app.get("/logs")
def get_logs(limit: int = 200, severity: Optional[str] = None, q: Optional[str] = None) -> Dict[str, Any]:
    if limit < 1:
        raise HTTPException(status_code=400, detail="limit must be >= 1")
    items = list(_state["logs"]) if _state["logs"] else _read_log_file(limit)
    if severity:
        items = [i for i in items if i.get("severity") == severity.upper()]
    if q:
        ql = q.lower()
        items = [i for i in items if ql in (i.get("message", "").lower())]
    return {"count": len(items), "items": items[-limit:]}


@app.get("/map")
def get_map() -> Dict[str, Any]:
    cfg = _state.get("config")
    rooms = []
    floor_w = None
    floor_h = None
    if cfg:
        loc_id = cfg.get("telemetry_config", {}).get("edge_location_id")
        floor_id = cfg.get("telemetry_config", {}).get("edge_floor_id")
        try:
            floor_cfg = cfg["locations"][loc_id]["floors"][floor_id]
            floor_w = floor_cfg.get("width_m")
            floor_h = floor_cfg.get("height_m")
            for room_name, room_data in floor_cfg.get("rooms", {}).items():
                rooms.append({
                    "name": room_name,
                    "polygon": room_data.get("polygon", []),
                })
        except Exception:
            pass

    positions = dict(_state["positions"])
    if not positions:
        positions = _positions_from_log_file()

    info_positions = _devices_from_info(_info_from_config(cfg))
    for dev_id, pos in info_positions.items():
        positions.setdefault(dev_id, pos)

    devices = []
    for dev_id, pos in positions.items():
        devices.append({
            "device_id": dev_id,
            "x": pos.get("x"),
            "y": pos.get("y"),
            "room_id": pos.get("room_id"),
            "timestamp": pos.get("timestamp"),
            "reachable": _state["device_status"].get(dev_id, {}).get("reachable", False),
            "source": pos.get("source", "ingest"),
        })

    return {
        "floor": {"width_m": floor_w, "height_m": floor_h},
        "rooms": rooms,
        "devices": devices,
    }


@app.post("/ingest")
def post_ingest(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not _state["accepting"]:
        raise HTTPException(status_code=409, detail="Ingest paused")

    _state["latest"] = payload
    _state["decisions"].append(payload)
    if len(_state["decisions"]) > MAX_DECISIONS:
        _state["decisions"] = _state["decisions"][-MAX_DECISIONS:]

    _state["status"] = {
        "last_update": _now_iso(),
        "total_decisions": _state["status"]["total_decisions"] + 1,
        "last_device_id": payload.get("device_id"),
    }
    _update_position_from_decision(payload)
    _append_log({
        "timestamp": payload.get("timestamp", _now_iso()),
        "severity": _decision_severity(payload.get("confidence")),
        "device_id": payload.get("device_id"),
        "message": f"{payload.get('device_id')} -> {payload.get('room_id')} ({payload.get('confidence')})",
    })
    return {"ok": True}


@app.post("/status")
def post_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    # Optional health / scan metrics from agent
    payload["last_update"] = _now_iso()
    _state["status"].update(payload)
    return {"ok": True}


@app.post("/raw")
def post_raw(payload: Dict[str, Any]) -> Dict[str, Any]:
    # Raw scan snapshot from agent
    payload["received_at"] = _now_iso()
    _state["raw"] = payload
    _update_device_status_from_raw(payload)
    return {"ok": True}


@app.post("/start")
def start_ingest() -> Dict[str, Any]:
    _state["accepting"] = True
    return {"ok": True, "accepting": True}


@app.post("/stop")
def stop_ingest() -> Dict[str, Any]:
    _state["accepting"] = False
    return {"ok": True, "accepting": False}


@app.get("/agent/status")
def get_agent_status() -> Dict[str, Any]:
    running = _is_agent_running()
    _state["agent"]["running"] = running
    return {"running": running, "pid": _state["agent"].get("pid")}


@app.post("/agent/start")
def start_agent() -> Dict[str, Any]:
    if _is_agent_running():
        return {"ok": True, "running": True, "pid": _state["agent"].get("pid") or _read_pid_file()}

    proc = subprocess.Popen(_agent_cmd(), cwd=str(WEB_ROOT))
    _state["agent"]["pid"] = proc.pid
    _state["agent"]["proc"] = proc
    _state["agent"]["running"] = True
    return {"ok": True, "running": True, "pid": proc.pid}


@app.post("/agent/stop")
def stop_agent() -> Dict[str, Any]:
    pid = _state["agent"].get("pid")
    proc = _state["agent"].get("proc")
    if not pid and not proc:
        pid = _read_pid_file()
    if not pid and not proc:
        _state["agent"]["running"] = False
        return {"ok": True, "running": False}

    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    elif pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
            except Exception:
                pass

    _state["agent"]["running"] = _is_agent_running()
    if not _state["agent"]["running"]:
        _state["agent"]["pid"] = None
        _state["agent"]["proc"] = None
        try:
            if PID_FILE.exists():
                PID_FILE.unlink()
        except Exception:
            pass
    return {"ok": True, "running": _state["agent"]["running"], "pid": _state["agent"].get("pid")}


_load_config_from_disk()
