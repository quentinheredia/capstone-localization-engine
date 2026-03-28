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
    print(f"[DESKTOP_APP.PY] Waiting for server on port {port}...")
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
            print(f"[DESKTOP_APP.PY] Server not responding yet on port {port}…")
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
        self._popout_windows: dict = {}   # method -> webview.Window (or None)

    def _on_window_closed(self, method: str):
        """Called when the user closes a pop-out window — removes the stale reference."""
        print(f"[JsApi] Pop-out closed by user: {method}")
        self._popout_windows.pop(method, None)

    def open_map_window(self, method: str):
        """
        Open a native OS window for the given localization method.
        If the window is already alive, bring it to the front.
        If the user previously closed it, create a fresh one.
        """
        print(f"[JsApi] open_map_window called for: {method}")

        # ── Check whether an existing window is still alive ───────────────
        existing = self._popout_windows.get(method)
        if existing is not None:
            # Verify the window is still in pywebview's active list
            alive = existing in webview.windows
            if alive:
                try:
                    existing.on_top = True
                    existing.on_top = False
                    print(f"[JsApi] Focused existing window: {method}")
                    return {"ok": True, "action": "focused"}
                except Exception as e:
                    print(f"[JsApi] Focus failed ({e}), recreating window: {method}")
            # Window was closed — clean up the stale reference
            self._popout_windows.pop(method, None)

        # ── Create a new native window ────────────────────────────────────
        html = _map_window_html(method, HYBRID_PORT)
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

        # Register closed-event so stale reference is auto-removed
        w.events.closed += lambda: self._on_window_closed(method)

        self._popout_windows[method] = w
        print(f"[JsApi] New window created: {method}")
        return {"ok": True, "action": "opened"}

    def close_map_window(self, method: str):
        """Programmatically close a pop-out map window."""
        print(f"[JsApi] close_map_window called for: {method}")
        w = self._popout_windows.pop(method, None)
        if w:
            try:
                w.destroy()
            except Exception as e:
                print(f"[JsApi] Error destroying window {method}: {e}")
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

    @staticmethod
    def _start_pipe_drainer(proc: subprocess.Popen, label: str) -> None:
        """
        Spawn two daemon threads that continuously drain a subprocess's stdout
        and stderr pipes.

        Without draining, the OS pipe buffer (4–64 KB on Windows) fills up once
        the process has emitted that much output.  After it fills, every call to
        write() on the subprocess's end of the pipe blocks until the reader
        consumes data — which never happens unless we drain it here.  That
        blocking write() happens to be a log.warning() call on the asyncio event
        loop thread, so a full pipe silently freezes the HTTP server.

        Daemon threads are used so they die automatically when the desktop app
        exits.  Output is forwarded to the desktop app's own stdout so the
        operator can still read it.
        """
        def _drain(stream, prefix: str):
            try:
                for raw in stream:
                    try:
                        line = raw.decode(errors="replace").rstrip()
                        if line:
                            print(f"[{prefix}] {line}", flush=True)
                    except Exception:
                        pass
            except Exception:
                pass   # process exited — pipe closed

        for stream, kind in [(proc.stdout, f"{label}/out"), (proc.stderr, f"{label}/err")]:
            if stream is None:
                continue
            t = threading.Thread(target=_drain, args=(stream, kind), daemon=True)
            t.start()

    def _server_healthy(self, port: int) -> bool:
        """Return True if a server is already responding on this port."""
        print("[DESKTOP_APP.PY] Checking for existing server on port %d…" % port)
        try:
            r = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
            return r.ok
        except Exception:
            print(f"[DESKTOP_APP.PY] No server responding on port {port}.")
            return False

    @staticmethod
    def _kill_port(port: int) -> None:
        """
        Terminate any process currently bound to *port*.

        Called before launching a fresh server so that stale processes from
        previous (possibly crashed) sessions do not prevent the new one from
        binding or cause the startup check to incorrectly reuse old code.
        """
        try:
            if sys.platform == "win32":
                # netstat -ano lists pid in the last column; taskkill ends it.
                out = subprocess.check_output(
                    f'netstat -ano | findstr ":{port} "',
                    shell=True, stderr=subprocess.DEVNULL,
                ).decode(errors="replace")
                pids = set()
                for line in out.splitlines():
                    parts = line.strip().split()
                    if parts and parts[-1].isdigit():
                        pids.add(parts[-1])
                for pid in pids:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        capture_output=True,
                    )
            else:
                # macOS / Linux: lsof gives us the pids directly.
                out = subprocess.check_output(
                    ["lsof", "-ti", f":{port}"],
                    stderr=subprocess.DEVNULL,
                ).decode(errors="replace").strip()
                if out:
                    for pid in out.split("\n"):
                        pid = pid.strip()
                        if pid.isdigit():
                            subprocess.run(
                                ["kill", "-9", pid],
                                capture_output=True,
                            )
            time.sleep(0.4)   # give the OS a moment to free the port
        except Exception:
            pass  # port was not in use — nothing to do

    def _start_servers(self):
        python = _find_python()
        env = os.environ.copy()

        platform_script = str(REPO_ROOT / "platform" / "backend" / "main.py")
        hybrid_script   = str(REPO_ROOT / "Hybrid" / "src_python" / "app.py")

        # Kill any stale process on each port before starting fresh.
        # This ensures updated code is always loaded even after a crash or
        # forced close that left an orphaned server process behind.
        print(f"[App] Clearing port {PLATFORM_PORT} (any stale process)…")
        self._kill_port(PLATFORM_PORT)
        print(f"[App] Starting Platform Backend  ({platform_script})")
        self._platform_proc = subprocess.Popen(
            [python, platform_script],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Drain stdout/stderr of both subprocesses in background daemon threads.
        # Without this the OS pipe buffers fill up (~4-64 KB), causing every
        # subsequent log.warning() call in the backend to block the event loop
        # indefinitely — which prevents uvicorn from responding to HTTP health
        # checks and produces the periodic "Frontend reconnected for 7s" errors.
        self._start_pipe_drainer(self._platform_proc, "platform")

        print(f"[App] Clearing port {HYBRID_PORT} (any stale process)…")
        self._kill_port(HYBRID_PORT)
        print(f"[App] Starting Hybrid Engine  ({hybrid_script})")
        self._hybrid_proc = subprocess.Popen(
            [python, hybrid_script],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._start_pipe_drainer(self._hybrid_proc, "engine")

    def _stop_servers(self):
        # Always stop the engine poll first — this covers BOTH processes we
        # started this session AND any stale engine process that was reused
        # (where self._hybrid_proc is None).  Without this call the poll loop
        # keeps making Telnet connections to the APs between sessions, filling
        # the log DB with connectivity-failure alerts the user will see on the
        # next open.
        try:
            requests.post(
                f"http://127.0.0.1:{HYBRID_PORT}/poll/stop",
                timeout=3,
            )
            print("[App] Engine poll stopped on shutdown.")
        except Exception as exc:
            print(f"[App] Could not stop engine poll on shutdown (ok if never started): {exc}")

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

            # ── Ensure the engine poll is in a clean stopped state ────────
            # If a stale engine process from a previous session was reused
            # (because _server_healthy() returned True and we skipped launching
            # a new subprocess), it may still have poll_running=True and active
            # Telnet sessions open to the APs.  Always stop the poll here so
            # the user starts from a known clean state.
            try:
                requests.post(
                    f"http://127.0.0.1:{HYBRID_PORT}/poll/stop",
                    timeout=3,
                )
                print("[App] Engine poll stopped — clean startup state.")
            except Exception as exc:
                print(f"[App] Could not stop engine poll (may be normal): {exc}")

            # Write a "session started" sentinel to the engine log so the
            # user can see exactly where the current session begins — any
            # connectivity failures above this entry are from a previous run.
            try:
                requests.post(
                    f"http://127.0.0.1:{PLATFORM_PORT}/api/v1/engine-logs/session-start",
                    timeout=3,
                )
                print("[App] Session-start marker written to engine log.")
            except Exception as exc:
                print(f"[App] Could not write session-start marker: {exc}")

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

        # Hand control to pywebview — blocks this thread until the window closes.
        # Events are wired here (not in __init__) so self._window is always set.
        self._window.events.loaded  += self._on_loaded
        self._window.events.closed  += self._on_closed
        webview.start(debug=False)


if __name__ == "__main__":
    App().run()
