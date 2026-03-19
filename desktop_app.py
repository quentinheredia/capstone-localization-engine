#!/usr/bin/env python3
"""
IPS Platform Desktop App
Uses pywebview (Microsoft Edge/WebView2) to embed the frontend in a native
desktop window. No PyQtWebEngine → no subprocess fork-bomb on Windows.
"""

import sys
import os
import socket
import subprocess
import time
import threading
from pathlib import Path

# ── Required for PyInstaller --onefile on Windows ────────────────────────────
# Must be called before anything else so multiprocessing workers spawned by
# the frozen exe (e.g. from uvicorn) exit cleanly instead of re-running main().
import multiprocessing
multiprocessing.freeze_support()
# ─────────────────────────────────────────────────────────────────────────────

import requests
import webview

# ── Config ───────────────────────────────────────────────────────────────────
PLATFORM_PORT = 8080
HYBRID_PORT   = 8000
LOCK_PORT     = 19876   # single-instance guard

# Resolve repo root whether running as .py or PyInstaller .exe
if getattr(sys, "frozen", False):
    # Running as PyInstaller bundle — .exe lives in dist/, repo is one level up
    REPO_ROOT = Path(sys.executable).parent.parent
else:
    REPO_ROOT = Path(__file__).parent

FRONTEND_PATH = REPO_ROOT / "platform" / "frontend" / "index.html"
# ─────────────────────────────────────────────────────────────────────────────


def _acquire_instance_lock():
    """Bind an internal TCP port as a process-level singleton lock."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None


def _find_python():
    """Return a usable python executable path."""
    if getattr(sys, "frozen", False):
        # We're a PyInstaller exe — find the system Python
        import shutil
        py = shutil.which("python") or shutil.which("python3")
        if not py:
            raise RuntimeError(
                "Python not found in PATH.\n"
                "Please install Python and add it to your PATH, then restart."
            )
        return py
    return sys.executable


def _wait_for_server(port: int, proc: subprocess.Popen = None, timeout: int = 30) -> tuple:
    """
    Poll /health until it responds or timeout elapses.
    Returns (success: bool, error_detail: str).
    If the subprocess dies early, captures its stderr immediately.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        # Check if the process crashed before responding
        if proc and proc.poll() is not None:
            stderr = ""
            try:
                _, err = proc.communicate(timeout=2)
                stderr = err.decode(errors="replace").strip()
            except Exception:
                pass
            return False, stderr or f"Process exited with code {proc.returncode}"

        try:
            r = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
            if r.ok:
                return True, ""
        except Exception:
            pass
        time.sleep(1)

    # Timed out — try to grab stderr from the process
    stderr = ""
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            _, err = proc.communicate(timeout=3)
            stderr = err.decode(errors="replace").strip()
        except Exception:
            pass
    return False, stderr or f"No response after {timeout}s"


class App:
    """Manages backend processes and the pywebview window."""

    def __init__(self):
        self._platform_proc = None
        self._hybrid_proc   = None
        self._window        = None
        self._lock          = None

    # ── Server lifecycle ──────────────────────────────────────────────────

    def _server_healthy(self, port: int) -> bool:
        """Return True if a server is already responding on this port."""
        try:
            r = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
            return r.ok
        except Exception:
            return False

    def _start_servers(self):
        python = _find_python()
        env = os.environ.copy()

        platform_script = str(REPO_ROOT / "platform" / "backend" / "main.py")
        hybrid_script   = str(REPO_ROOT / "Hybrid" / "src_python" / "app.py")

        # Only start if not already running (user may have started them manually)
        if self._server_healthy(PLATFORM_PORT):
            print(f"[App] Platform Backend already running on port {PLATFORM_PORT} — skipping launch.")
        else:
            print(f"[App] Starting Platform Backend  ({platform_script})")
            self._platform_proc = subprocess.Popen(
                [python, platform_script],
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        if self._server_healthy(HYBRID_PORT):
            print(f"[App] Hybrid Engine already running on port {HYBRID_PORT} — skipping launch.")
        else:
            print(f"[App] Starting Hybrid Engine  ({hybrid_script})")
            self._hybrid_proc = subprocess.Popen(
                [python, hybrid_script],
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

    def _stop_servers(self):
        for proc, name in [
            (self._platform_proc, "Platform Backend"),
            (self._hybrid_proc,   "Hybrid Engine"),
        ]:
            if proc and proc.poll() is None:
                print(f"[App] Stopping {name}…")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    # ── pywebview callbacks ───────────────────────────────────────────────

    def _on_loaded(self):
        """Called by pywebview when the page finishes loading."""
        print("[App] Frontend loaded.")

    def _on_closed(self):
        """Called by pywebview when the window is closed."""
        print("[App] Window closed — shutting down servers.")
        self._stop_servers()
        if self._lock:
            self._lock.close()

    # ── Background startup thread ─────────────────────────────────────────

    def _startup_thread(self):
        """
        Runs in a background thread:
        1. Starts both backend servers.
        2. Waits for them to be healthy.
        3. Navigates the webview window to the frontend.
        """
        try:
            self._start_servers()

            print("[App] Waiting for Platform Backend…")
            ok, detail = _wait_for_server(PLATFORM_PORT, self._platform_proc)
            if not ok:
                self._show_error(
                    "Platform Backend failed to start.\n\n"
                    "Most likely cause: PostgreSQL is not running.\n"
                    "Fix: open a terminal and run:\n"
                    "  docker start ips-postgres\n"
                    "or: net start postgresql-x64-15\n\n"
                    f"Server output:\n{detail}"
                )
                return

            print("[App] Waiting for Hybrid Engine…")
            ok, detail = _wait_for_server(HYBRID_PORT, self._hybrid_proc)
            if not ok:
                self._show_error(
                    "Hybrid Engine failed to start.\n\n"
                    "Possible causes:\n"
                    "• capstone_core.pyd not compiled (run CMake build)\n"
                    "• Missing Python dependencies (pip install -r Hybrid/requirements.txt)\n"
                    "• config.yaml not found\n\n"
                    f"Server output:\n{detail}"
                )
                return

            print("[App] Both servers ready — loading frontend.")
            if self._window:
                if FRONTEND_PATH.exists():
                    self._window.load_url(FRONTEND_PATH.as_uri())
                else:
                    self._show_error(
                        f"Frontend file not found:\n{FRONTEND_PATH}\n\n"
                        "Make sure platform/frontend/index.html exists."
                    )

        except Exception as exc:
            self._show_error(f"Startup error:\n{exc}")

    def _show_error(self, message: str):
        """Display an error inside the webview window."""
        print(f"[App ERROR] {message}")
        if self._window:
            safe = message.replace("\\", "\\\\").replace("`", "\\`").replace("\n", "<br>")
            self._window.evaluate_js(
                f"document.body.innerHTML = `"
                f"<div style='font-family:sans-serif;padding:40px;color:#c0392b;'>"
                f"<h2>⚠ Startup Error</h2><p>{safe}</p></div>`;"
            )

    # ── Entry point ───────────────────────────────────────────────────────

    def run(self):
        # Single-instance guard
        self._lock = _acquire_instance_lock()
        if self._lock is None:
            print("[App] Another instance is already running. Exiting.")
            sys.exit(0)

        # Splash screen HTML shown while servers start
        splash = """
        <!DOCTYPE html><html><head><style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{
          font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
          background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
          display:flex;align-items:center;justify-content:center;
          height:100vh;color:#fff;
        }
        .box{text-align:center}
        .logo{font-size:3.5rem;margin-bottom:1rem}
        h1{font-size:1.8rem;margin-bottom:.4rem}
        p{font-size:1rem;opacity:.85;margin-bottom:2rem}
        .spinner{width:36px;height:36px;border:4px solid rgba(255,255,255,.3);
          border-top-color:#fff;border-radius:50%;margin:0 auto;
          animation:spin 1s linear infinite}
        @keyframes spin{to{transform:rotate(360deg)}}
        .status{margin-top:1.5rem;font-size:.85rem;opacity:.75}
        </style></head><body>
        <div class="box">
          <div class="logo">⚡</div>
          <h1>IPS Platform</h1>
          <p>Indoor Positioning System</p>
          <div class="spinner"></div>
          <div class="status" id="s">Starting servers…</div>
        </div>
        <script>
          const msgs=['Starting Platform Backend…','Starting Hybrid Engine…',
                      'Almost ready…','Loading dashboard…'];
          let i=0;
          setInterval(()=>{
            document.getElementById('s').textContent=msgs[i%msgs.length];i++;
          },2500);
        </script></body></html>
        """

        # Create the window (shows splash immediately)
        self._window = webview.create_window(
            title   = "IPS Platform — Indoor Positioning System",
            html    = splash,
            width   = 1600,
            height  = 900,
            min_size= (1200, 700),
            # Allow the page to make requests to localhost backends
        )

        # Start the server startup in a background thread
        t = threading.Thread(target=self._startup_thread, daemon=True)
        t.start()

        # Hand control to pywebview (blocks until window is closed)
        webview.start(
            func        = None,
            debug       = False,
            http_server = False,
        )

        # Cleanup after window closes
        self._on_closed()


def main():
    App().run()


if __name__ == "__main__":
    main()
