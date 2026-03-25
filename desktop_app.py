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


def _map_window_html(method: str, engine_port: int) -> str:
    """
    Self-contained HTML page for a pop-out map window.
    Polls the engine /map?method=... endpoint and renders rooms + devices
    on a full-window canvas.  No external dependencies.
    """
    labels = {
        "trilateration": "Trilateration",
        "fingerprinting": "Fingerprinting",
        "ble": "BLE",
        "tof": "Time-of-Flight",
    }
    label = labels.get(method, method)
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>{label} — IPS Map</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#111;color:#e4e4e7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;overflow:hidden}}
#header{{display:flex;align-items:center;justify-content:space-between;padding:8px 16px;background:#1a1a2e;border-bottom:1px solid #333}}
#header h2{{font-size:14px;font-weight:600}}
#header .info{{font-size:11px;color:#a1a1aa}}
canvas{{display:block}}
#footer{{position:fixed;bottom:0;left:0;right:0;padding:6px 16px;background:#1a1a2e;border-top:1px solid #333;font:11px monospace;color:#a1a1aa;white-space:nowrap;overflow-x:auto}}
</style>
</head><body>
<div id="header">
  <h2>{label} Map</h2>
  <span class="info" id="status">Connecting…</span>
</div>
<canvas id="c"></canvas>
<div id="footer" id="devlist"></div>
<script>
const ENGINE = "http://localhost:{engine_port}";
const METHOD = "{method}";
const canvas = document.getElementById("c");
const ctx    = canvas.getContext("2d");
const status = document.getElementById("status");
const footer = document.getElementById("footer");
let rooms = [], devices = [], fw = 20, fh = 20;

function resize() {{
  canvas.width  = window.innerWidth;
  canvas.height = window.innerHeight - 60;  // header + footer
  canvas.style.marginTop = "0";
  render();
}}
window.addEventListener("resize", resize);

function render() {{
  const W = canvas.width, H = canvas.height;
  const sx = W / fw, sy = H / fh;
  ctx.fillStyle = "#111";
  ctx.fillRect(0, 0, W, H);

  // Grid
  ctx.strokeStyle = "#222";
  ctx.lineWidth = 0.5;
  for (let x = 0; x <= fw; x++) {{
    ctx.beginPath(); ctx.moveTo(x*sx, 0); ctx.lineTo(x*sx, H); ctx.stroke();
  }}
  for (let y = 0; y <= fh; y++) {{
    ctx.beginPath(); ctx.moveTo(0, y*sy); ctx.lineTo(W, y*sy); ctx.stroke();
  }}

  // Rooms
  rooms.forEach(r => {{
    if (!r.polygon || !r.polygon.length) return;
    ctx.beginPath();
    r.polygon.forEach(([px,py],i) => i===0 ? ctx.moveTo(px*sx,py*sy) : ctx.lineTo(px*sx,py*sy));
    ctx.closePath();
    ctx.fillStyle = "rgba(99,102,241,0.12)";
    ctx.fill();
    ctx.strokeStyle = "#6366f1";
    ctx.lineWidth = 1.5;
    ctx.stroke();
    // Label
    const cx = r.polygon.reduce((s,[px])=>s+px,0)/r.polygon.length;
    const cy = r.polygon.reduce((s,[,py])=>s+py,0)/r.polygon.length;
    ctx.fillStyle = "#a5b4fc";
    ctx.font = Math.max(12, Math.min(18, W/30)) + "px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(r.name, cx*sx, cy*sy);
  }});

  // Devices
  const dotR = Math.max(8, Math.min(16, W/60));
  devices.forEach(d => {{
    const dx = (d.x||0)*sx, dy = (d.y||0)*sy;
    // Glow
    ctx.beginPath(); ctx.arc(dx, dy, dotR+4, 0, Math.PI*2);
    ctx.fillStyle = d.reachable ? "rgba(34,197,94,0.25)" : "rgba(248,113,113,0.25)";
    ctx.fill();
    // Dot
    ctx.beginPath(); ctx.arc(dx, dy, dotR, 0, Math.PI*2);
    ctx.fillStyle = d.reachable ? "#22c55e" : "#f87171";
    ctx.fill();
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5; ctx.stroke();
    // Label
    ctx.fillStyle = "#fff";
    ctx.font = "bold " + Math.max(10, dotR) + "px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(d.device_id, dx, dy - dotR - 6);
    // Room label below
    if (d.room_id) {{
      ctx.fillStyle = "#a1a1aa";
      ctx.font = (dotR - 2) + "px sans-serif";
      ctx.fillText(d.room_id, dx, dy + dotR + 10);
    }}
  }});
}}

async function poll() {{
  try {{
    const res = await fetch(ENGINE + "/map?method=" + METHOD);
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    rooms   = data.rooms   || [];
    devices = data.devices || [];
    if (data.floor) {{
      fw = data.floor.width_m  || 20;
      fh = data.floor.height_m || 20;
    }}
    status.textContent = devices.length + " device(s) · " + new Date().toLocaleTimeString();
    footer.textContent = devices.map(d =>
      d.device_id + " → " + (d.room_id||"?") + " (" + (d.x||0).toFixed(1) + ", " + (d.y||0).toFixed(1) + ")"
    ).join("   ·   ") || "No devices";
    render();
  }} catch(e) {{
    status.textContent = "Engine offline: " + e.message;
  }}
}}

resize();
poll();
setInterval(poll, 2000);
</script>
</body></html>"""


class JsApi:
    """
    Exposed to the frontend via pywebview's js_api.
    Allows the React app to open native OS windows for pop-out maps.
    """
    def __init__(self):
        print("[JsApi] Initialized")
        self._popout_windows = {}

    def open_map_window(self, method: str):
        print("Creating window for method:", method)
        """Open (or focus) a native window for the given localization method."""
        if method in self._popout_windows:
            print("Window already exists for method, trying to focus:", method)
            try:
                # If window still exists, bring to front
                #w = self._popout_windows[method]
                #w.on_top = True
                #w.on_top = False
                w.destroy()  # Temporary workaround to reliably bring to front on Windows
                self.open_map_window(method)  # Recreate immediately after destroy
                print(f"Focused window for {method}")
                return {"ok": True, "action": "focused"}
            except Exception as e:
                print (f"Error focusing window for {method}, recreating:", e)
                pass  # Window was closed, recreate

        html = _map_window_html(method, HYBRID_PORT)
        print("HTML Window created for method:", method, "On port:", HYBRID_PORT)
        labels = {
            "trilateration": "Trilateration",
            "fingerprinting": "Fingerprinting",
            "ble": "BLE",
            "tof": "Time-of-Flight",
        }
        title = f"{labels.get(method, method)} — IPS Map"
        w = webview.create_window(
            title    = title,
            html     = html,
            width    = 800,
            height   = 600,
            min_size = (400, 300),
        )
        self._popout_windows[method] = w
        print("Window created for method:", method)
        return {"ok": True, "action": "opened"}

    def close_map_window(self, method: str):
        print("Closing window for method:", method)
        """Close a pop-out map window."""
        w = self._popout_windows.pop(method, None)
        if w:
            try:
                print("trying to destroy window for method:", method)
                w.destroy()
            except Exception as e:
                print("Error closing window for method:", method, "Error:", e)
                pass
        print("Window closed for method:", method)
        return {"ok": True}


class App:
    """Manages backend processes and the pywebview window."""

    def __init__(self):
        self._platform_proc = None
        self._hybrid_proc   = None
        self._window        = None
        self._lock          = None
        self._js_api        = JsApi()

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
            js_api  = self._js_api,
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
