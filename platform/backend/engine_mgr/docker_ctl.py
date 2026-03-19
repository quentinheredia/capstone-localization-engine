"""
Docker container lifecycle management for the localization engine.

Design decision: ONE container per campus.
Rationale: Each campus has its own AP infrastructure and config.yaml.
Multiple buildings/floors within a campus share the same engine instance
because select_location() in the engine already handles floor switching
and the TelnetPipe polls all APs concurrently.

The container runs the Hybrid/Dockerfile image (capstone-hybrid)
with the generated config.yaml mounted in.
"""

from __future__ import annotations

import os
import logging
import subprocess
from typing import Any, Dict, Optional

import yaml

log = logging.getLogger(__name__)

# Container name for the localization engine
ENGINE_CONTAINER = "capstone-engine"

# Paths
_REPO_ROOT  = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_HYBRID_DIR = os.path.join(_REPO_ROOT, "Hybrid")
_ENV_FILE   = os.path.join(_REPO_ROOT, ".env")


def start_engine(cfg: dict) -> Dict[str, Any]:
    """
    Write config.yaml into Hybrid/ and start the Docker container.

    The container uses:
      - The capstone-hybrid image (pre-built via `docker build`)
      - config.yaml mounted from the host
      - .env passed via --env-file
      - Port 8000 exposed for the engine's FastAPI server
    """
    config_path = os.path.join(_HYBRID_DIR, "config.yaml")

    # Write the generated config
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
        log.info("engine_mgr: wrote config.yaml to %s", config_path)
    except Exception as exc:
        log.error("engine_mgr: failed to write config: %s", exc)
        return {"ok": False, "error": str(exc)}

    # Stop any existing engine container
    _run(["docker", "rm", "-f", ENGINE_CONTAINER])

    # Start the new container
    cmd = [
        "docker", "run", "-d",
        "--name", ENGINE_CONTAINER,
        "-p", "8001:8000",
        "-v", f"{config_path}:/app/config.yaml:ro",
    ]
    if os.path.isfile(_ENV_FILE):
        cmd += ["--env-file", _ENV_FILE]

    # Mount radiomap files (all of them)
    for fname in os.listdir(_HYBRID_DIR):
        if fname.startswith("radiomap_") and fname.endswith(".json"):
            host_path = os.path.join(_HYBRID_DIR, fname)
            cmd += ["-v", f"{host_path}:/app/{fname}:ro"]

    cmd.append("capstone-hybrid")

    result = _run(cmd)
    if result.returncode == 0:
        log.info("engine_mgr: container %s started", ENGINE_CONTAINER)
        return {"ok": True, "container": ENGINE_CONTAINER, "port": 8001}
    else:
        log.error("engine_mgr: failed to start: %s", result.stderr)
        return {"ok": False, "error": result.stderr}


def stop_engine() -> Dict[str, Any]:
    """Stop and remove the engine container."""
    result = _run(["docker", "rm", "-f", ENGINE_CONTAINER])
    return {"ok": result.returncode == 0, "container": ENGINE_CONTAINER}


def engine_status() -> Dict[str, Any]:
    """Check if the engine container is running."""
    result = _run(["docker", "inspect", "-f", "{{.State.Status}}", ENGINE_CONTAINER])
    if result.returncode == 0:
        status = result.stdout.strip()
        return {"running": status == "running", "status": status, "container": ENGINE_CONTAINER}
    return {"running": False, "status": "not found", "container": ENGINE_CONTAINER}


def _run(cmd: list) -> subprocess.CompletedProcess:
    """Run a shell command and return the result."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        log.error("engine_mgr: command failed: %s", exc)

        class _Fake:
            returncode = 1
            stdout = ""
            stderr = str(exc)
        return _Fake()  # type: ignore
