"""
IPS Management Platform — Backend Entry Point

Launches the FastAPI application with:
  - Full CRUD for the Campus → Building → Floor → Room hierarchy
  - Anchor and Tag device management
  - Boundary crossing, alert, and unified engine-log viewer
  - Config YAML generation from database state
  - Docker engine lifecycle management
  - Background tasks:
      * connectivity_poller  — ICMP ping every 1.5 s; generates EngineLog entries
      * scope_checker        — polls engine decisions; checks tag scopes every 10 s

Usage:
  python main.py                                # dev mode
  uvicorn main:app --host 0.0.0.0 --port 8080  # production

The localization engine (Hybrid) runs as a separate container on port 8001.
This platform API runs on port 8080.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import text, inspect

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from models import Base, engine as db_engine, SessionLocal
from models import Anchor, Tag, EngineLog
from models.tag import PRIORITY_LOG_LEVELS
from api.hierarchy import router as hierarchy_router
from api.devices import router as devices_router
from api.logs import router as logs_router
from api.config import router as config_router
from api.floorplan_editor import router as floorplan_editor_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
# Silence noisy third-party loggers (routine HTTP polling floods the console)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("watchfiles").setLevel(logging.WARNING)
log = logging.getLogger("platform")

# ─── Engine API URL (configurable so it works outside Docker Desktop too) ────
ENGINE_API_URL = os.environ.get("ENGINE_API_URL", "http://host.docker.internal:8001")

PLATFORM_PORT = int(os.environ.get("PLATFORM_PORT", 8080))
ENGINE_PORT   = int(os.environ.get("ENGINE_PORT", 8000))

# Consecutive ping-failure counters per anchor IP
_anchor_fail_counts: dict[str, int] = {}

# Last known room per tag (for scope-change dedup)
_tag_last_room: dict[str, str] = {}


def _write_system_log(level: str, message: str, meta: dict | None = None) -> None:
    """Write a system-level EngineLog entry (source='system')."""
    try:
        db = SessionLocal()
        try:
            db.add(EngineLog(
                level=level,
                source="system",
                message=message,
                meta=meta or {},
                timestamp=datetime.now(timezone.utc),
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        log.warning("Failed to write system log: %s", exc)


def _write_survey_log(level: str, message: str, meta: dict | None = None) -> None:
    """Write a survey-level EngineLog entry (source='survey')."""
    try:
        db = SessionLocal()
        try:
            db.add(EngineLog(
                level=level,
                source="survey",
                message=message,
                meta=meta or {},
                timestamp=datetime.now(timezone.utc),
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        log.warning("Failed to write survey log: %s", exc)


# ─── Migrations ──────────────────────────────────────────────────────────────

def _migrate_columns(engine):
    """
    Add columns introduced after initial table creation.
    create_all() only creates NEW tables — it will not ALTER existing ones.
    Each migration is idempotent (skipped if column already exists).
    """
    migrations = [
        # Floor table — editor fields
        ("floors", "floorplan_data",      "JSONB DEFAULT '{}'"),
        ("floors", "pdf_path",            "VARCHAR(500) DEFAULT ''"),
        ("floors", "default_grid_size_m", "FLOAT DEFAULT 1.0"),
        # Room table — access control
        ("rooms",  "access_level_id",     "INTEGER REFERENCES access_levels(id) ON DELETE SET NULL"),
        ("rooms",  "grid_size_override",  "FLOAT"),
        # Anchor table — device lifecycle
        ("anchors", "device_status",      "VARCHAR(50) DEFAULT 'in_stock'"),
        ("anchors", "flags",              "JSONB DEFAULT '[]'"),
        ("anchors", "last_reached",       "TIMESTAMPTZ"),
        # Tag table — identity, priority, scope  (new in this release)
        ("tags",   "name",                "VARCHAR(255) DEFAULT ''"),
        ("tags",   "device_type",         "VARCHAR(100) DEFAULT ''"),
        ("tags",   "security_level",      "INTEGER DEFAULT 1"),
        ("tags",   "priority",            "INTEGER DEFAULT 1"),
        ("tags",   "floor_scope",         "JSONB DEFAULT '[\"ALL\"]'"),
        ("tags",   "room_scope",          "JSONB DEFAULT '[]'"),
    ]
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, column, col_type in migrations:
            if not insp.has_table(table):
                continue
            existing = [c["name"] for c in insp.get_columns(table)]
            if column not in existing:
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                    log.info("  Added column %s.%s", table, column)
                except Exception as e:
                    log.warning("  Column %s.%s migration skipped: %s", table, column, e)

        # Make anchors.floor_id nullable (in-stock anchors have no floor)
        if insp.has_table("anchors"):
            try:
                conn.execute(text("ALTER TABLE anchors ALTER COLUMN floor_id DROP NOT NULL"))
                log.info("  Made anchors.floor_id nullable")
            except Exception:
                pass  # Already nullable — safe to ignore


import time as _time

# ─── Asyncio Loop Watchdog ────────────────────────────────────────────────────

async def _loop_watchdog() -> None:
    """
    Detects event-loop stalls.  Wakes every 0.1 s; if the actual sleep
    exceeds THRESHOLD the loop was blocked — log the duration so we can
    trace which background task is responsible.
    """
    THRESHOLD = 0.4   # seconds — anything longer than this is logged
    INTERVAL  = 0.1   # probe frequency (seconds)

    while True:
        t0 = _time.monotonic()
        await asyncio.sleep(INTERVAL)
        blocked_for = _time.monotonic() - t0 - INTERVAL
        if blocked_for >= THRESHOLD:
            log.warning(
                "[WATCHDOG] Event loop blocked for %.3f s  "
                "(health checks will fail if this exceeds 3 s)",
                blocked_for,
            )


# ─── Background Task: ICMP Connectivity Poller ───────────────────────────────

import sys as _sys
import subprocess as _subprocess

# Warn once per process if asyncio subprocesses are unavailable (SelectorEventLoop).
# We deliberately do NOT repeat this warning on every ping — with 10+ anchors
# retrying every 6 s, repeating it floods the log and fills the stderr pipe when
# the backend runs as a subprocess (desktop app), which in turn blocks the event
# loop on every subsequent log.warning() call.
_subprocess_warned: bool = False


def _ping_cmd(ip: str) -> list[str]:
    if _sys.platform == "win32":
        return ["ping", "-n", "1", "-w", "1000", ip]
    return ["ping", "-c", "1", "-W", "1", ip]


def _ping_blocking(ip: str) -> bool:
    """Blocking ICMP ping — always call via asyncio.to_thread(), never directly
    from the event loop, or it will freeze the entire server."""
    cmd = _ping_cmd(ip)
    try:
        r = _subprocess.run(cmd, capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception as exc:
        log.warning("blocking ping %s failed: %s", ip, exc)
        return False


async def _ping(ip: str) -> bool:
    """Non-blocking single ICMP ping. Returns True if host responded.

    On Windows (SelectorEventLoop), asyncio.create_subprocess_exec raises
    NotImplementedError *synchronously* — before any await — which blocks the
    event loop for every anchor in the gather() batch.  We detect Windows at
    import time and skip straight to the thread-pool path, eliminating the
    startup watchdog block entirely.
    """
    if _sys.platform == "win32":
        return await asyncio.to_thread(_ping_blocking, ip)

    # Non-Windows: use asyncio subprocess (truly non-blocking).
    global _subprocess_warned
    cmd = _ping_cmd(ip)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0
    except Exception as exc:
        if not _subprocess_warned:
            _subprocess_warned = True
            log.warning(
                "asyncio ping failed (%s) — using thread-pool fallback "
                "(this message appears once)", type(exc).__name__
            )
        return await asyncio.to_thread(_ping_blocking, ip)


def _write_connectivity_log(db, anchor: Anchor, event: str, extra: dict | None = None) -> None:
    """Emit an EngineLog entry on a connectivity state change."""
    level_map = {"offline": "alert", "online": "info", "trying": "warn"}
    msg_map = {
        "offline": f"Anchor {anchor.anchor_id} ({anchor.ip_address}) went OFFLINE — 5 consecutive ping failures",
        "online":  f"Anchor {anchor.anchor_id} ({anchor.ip_address}) is back ONLINE",
        "trying":  f"Anchor {anchor.anchor_id} ({anchor.ip_address}) — first ping failure, retrying…",
    }
    entry = EngineLog(
        level=level_map.get(event, "info"),
        source="connectivity",
        message=msg_map.get(event, event),
        meta={"anchor_id": anchor.anchor_id, "ip": anchor.ip_address, **(extra or {})},
        timestamp=datetime.now(timezone.utc),
    )
    db.add(entry)

""" Polls continously for anchor connectivity and logs state changes to the database."""
async def connectivity_poller() -> None:
    """
    Background anchor reachability monitor.

    Baseline schedule: ping all anchors every 150 s (2.5 min).

    State machine per anchor (tracked by _anchor_fail_counts):
      0            → "online"   (ping succeeded)
      1            → "trying"   (first failure; schedule 4 retries every 6 s)
      2–4 (retry)  → still "trying" while retrying
      5 (final)    → "offline"  (all retries exhausted)

    last_reached is updated ONLY when a ping succeeds.
    last_polled   is updated on every attempt.

    When engine poll is active, log state transitions. In standby mode,
    status is still updated silently (no log spam).
    """
    BASELINE_INTERVAL = 150.0   # 2.5 min between normal sweeps
    RETRY_INTERVAL    =   6.0   # 6 s between retries after first failure
    MAX_RETRIES       =   4     # 4 retries = 5 total attempts before "offline"

    log.info("Connectivity poller started (baseline=%.0fs, retry=%ds×%d) — pinging 'in_use' anchors only",
             BASELINE_INTERVAL, int(RETRY_INTERVAL), MAX_RETRIES)

    # Per-IP: number of consecutive failures (0 = last ping was OK)
    _fail_counts: dict[str, int] = {}
    # Per-IP: number of retry attempts still outstanding (0 = not in retry mode)
    _retry_remaining: dict[str, int] = {}

    _engine_poll_running: bool = False
    _engine_check_counter: int = 10  # force immediate check

    # Persistent HTTP client for engine health checks inside this task
    _http = httpx.AsyncClient(timeout=2.0)

    async def _sweep_anchors() -> None:
        """Ping all enabled anchors, update DB state, and log all transitions.

        All Postgres I/O runs in a thread-pool worker so the event loop stays
        free to answer /health during the sweep.
        """
        # Phase 1 — load enabled anchor IPs off the event loop
        def _load_ips():
            db = SessionLocal()
            try:
                return [
                    row[0]
                    for row in db.query(Anchor.ip_address)
                                 .filter_by(enabled=True)
                                 .filter(Anchor.ip_address != "")
                                 .filter(Anchor.device_status == "in_use")
                                 .all()
                ]
            finally:
                db.close()

        ips = await asyncio.to_thread(_load_ips)
        if not ips:
            return

        now = datetime.now(timezone.utc)

        # Phase 2 — ping all anchors concurrently (event-loop-friendly)
        results = await asyncio.gather(
            *[_ping(ip) for ip in ips],
            return_exceptions=True,
        )

        # Phase 3 — commit state changes off the event loop
        def _update_and_commit():
            db = SessionLocal()
            try:
                anchor_map = {
                    a.ip_address: a
                    for a in db.query(Anchor)
                               .filter_by(enabled=True)
                               .filter(Anchor.ip_address.in_(ips))
                               .all()
                }
                for ip, ok in zip(ips, results):
                    anchor = anchor_map.get(ip)
                    if not anchor:
                        continue
                    if isinstance(ok, Exception):
                        ok = False
                    prev_status = anchor.status
                    prev_fails  = _fail_counts.get(ip, 0)
                    anchor.last_polled = now
                    if ok:
                        # Success — reset failure tracking
                        _fail_counts[ip]     = 0
                        _retry_remaining[ip] = 0
                        anchor.status        = "online"
                        anchor.last_reached  = now
                        if prev_fails > 0 and prev_status != "online":
                            _write_connectivity_log(db, anchor, "online")
                    else:
                        # Failure
                        _fail_counts[ip] = prev_fails + 1
                        fails = _fail_counts[ip]
                        if fails == 1:
                            _retry_remaining[ip] = MAX_RETRIES
                            anchor.status = "trying"
                            _write_connectivity_log(db, anchor, "trying")
                        elif fails > MAX_RETRIES + 1:
                            anchor.status        = "offline"
                            _retry_remaining[ip] = 0
                            if prev_status != "offline":
                                _write_connectivity_log(db, anchor, "offline")
                        # else: still in retry window — keep "trying"
                db.commit()
            finally:
                db.close()

        await asyncio.to_thread(_update_and_commit)

    async def _retry_sweep(anchors_in_retry: list) -> None:
        """Ping only anchors currently in retry mode.

        DB commit runs in a thread-pool worker — same pattern as _sweep_anchors.
        """
        if not anchors_in_retry:
            return

        now = datetime.now(timezone.utc)

        # Ping retry anchors concurrently (event-loop-friendly)
        results = await asyncio.gather(
            *[_ping(ip) for ip in anchors_in_retry],
            return_exceptions=True,
        )

        # Commit results off the event loop
        def _update_and_commit():
            db = SessionLocal()
            try:
                for ip, ok in zip(anchors_in_retry, results):
                    if isinstance(ok, Exception):
                        ok = False
                    anchor = db.query(Anchor).filter_by(ip_address=ip, enabled=True).first()
                    if not anchor:
                        continue
                    anchor.last_polled = now
                    if ok:
                        _fail_counts[ip]     = 0
                        _retry_remaining[ip] = 0
                        anchor.status        = "online"
                        anchor.last_reached  = now
                        _write_connectivity_log(db, anchor, "online")
                    else:
                        _fail_counts[ip]     = _fail_counts.get(ip, 0) + 1
                        remaining            = _retry_remaining.get(ip, 0) - 1
                        _retry_remaining[ip] = max(0, remaining)
                        if remaining <= 0:
                            anchor.status = "offline"
                            _write_connectivity_log(db, anchor, "offline")
                        # else: still "trying" — no log (avoid retry spam)
                db.commit()
            finally:
                db.close()

        await asyncio.to_thread(_update_and_commit)

    # ── Main loop ─────────────────────────────────────────────────────────────
    # Small startup delay so the watchdog and other tasks get a few event-loop
    # cycles before the first anchor sweep hits the thread pool simultaneously
    # with engine_monitor and survey_monitor.  Prevents the thundering-herd
    # startup block that the watchdog detected as ~2.4 s.
    await asyncio.sleep(2.0)
    next_sweep_in = 0.0   # trigger first sweep right after the delay

    while True:
        try:
            # Periodically check engine poll status
            _engine_check_counter += 1
            if _engine_check_counter >= 10:
                _engine_check_counter = 0
                try:
                    r = await _http.get(f"http://localhost:{ENGINE_PORT}/health")
                    _engine_poll_running = (
                        r.status_code == 200
                        and bool(r.json().get("poll_running", False))
                    )
                except Exception:
                    _engine_poll_running = False

            # Check if any anchors are in retry mode
            retrying_ips = [ip for ip, rem in _retry_remaining.items() if rem > 0]
            if retrying_ips:
                await _retry_sweep(retrying_ips)
                await asyncio.sleep(RETRY_INTERVAL)
                continue

            # Baseline sweep
            next_sweep_in -= RETRY_INTERVAL
            if next_sweep_in <= 0:
                await _sweep_anchors()
                next_sweep_in = BASELINE_INTERVAL

        except Exception as exc:
            log.warning("Connectivity poller error: %s", exc)

        await asyncio.sleep(RETRY_INTERVAL)


# ─── Background Task: Scope Violation Checker ────────────────────────────────

def _priority_to_level(priority: int) -> Optional[str]:
    return PRIORITY_LOG_LEVELS.get(priority)


async def scope_checker() -> None:
    """
    Every 10 s, fetch latest engine decisions and check each tag's
    current room against its allowed floor_scope / room_scope.

    Scope violation severity is determined by tag.priority:
        1 → no log (free-roaming)
        2 → warn
        3 → alert
        4 → config
        5 → security

    Also logs Info entries for any room transition (movement).
    """
    # Use localhost:ENGINE_PORT (same as all other tasks) — ENGINE_API_URL
    # targets Docker port 8001 which doesn't exist in desktop mode.
    _engine_url = f"http://localhost:{ENGINE_PORT}"
    _http = httpx.AsyncClient(timeout=3.0)

    log.info("Scope checker started (interval=10s, engine=%s)", _engine_url)
    await asyncio.sleep(15)  # Give engine time to start before first check

    while True:
        try:
            resp = await _http.get(f"{_engine_url}/decisions", params={"limit": 50})
            if resp.status_code != 200:
                await asyncio.sleep(10)
                continue
            decisions = resp.json().get("items", [])
        except Exception:
            # Engine not running — check again later
            await asyncio.sleep(10)
            continue

        try:
            # Run all DB work in a thread so the event loop stays free
            def _process_decisions():
                db = SessionLocal()
                try:
                    tags = {t.tag_id: t for t in db.query(Tag).filter_by(enabled=True).all()}
                    seen: set[str] = set()

                    for d in decisions:
                        ssid    = d.get("device_id", "")
                        room_id = d.get("room_id", "")
                        method  = d.get("localization_method", "trilateration")
                        conf    = float(d.get("confidence", 0.0))

                        tag = tags.get(ssid)
                        if not tag or ssid in seen:
                            continue
                        seen.add(ssid)

                        prev_room = _tag_last_room.get(ssid)

                        # ── Info: movement (room changed) ────────────────────
                        if prev_room is not None and room_id and room_id != prev_room:
                            db.add(EngineLog(
                                level="info",
                                tag_id=ssid,
                                source="boundary",
                                message=f"{tag.name or ssid} moved: {prev_room} → {room_id}",
                                meta={"from": prev_room, "to": room_id,
                                      "method": method, "confidence": conf},
                                timestamp=datetime.now(timezone.utc),
                            ))

                        _tag_last_room[ssid] = room_id

                        # ── Scope violation check ────────────────────────────
                        level = _priority_to_level(tag.priority)
                        if level is None:
                            continue  # Priority 1 — no scope restrictions

                        # Room scope check
                        if tag.room_scope and room_id and room_id not in tag.room_scope:
                            dedup_key = f"{ssid}:{room_id}"
                            if _tag_last_room.get(f"_scope_{dedup_key}") != room_id:
                                _tag_last_room[f"_scope_{dedup_key}"] = room_id
                                db.add(EngineLog(
                                    level=level,
                                    tag_id=ssid,
                                    source="boundary",
                                    message=(
                                        f"{tag.name or ssid} (priority {tag.priority}) "
                                        f"outside allowed rooms — currently in '{room_id}', "
                                        f"scope: {tag.room_scope}"
                                    ),
                                    meta={
                                        "current_room":  room_id,
                                        "allowed_rooms": tag.room_scope,
                                        "priority":      tag.priority,
                                        "method":        method,
                                        "confidence":    conf,
                                    },
                                    timestamp=datetime.now(timezone.utc),
                                ))

                    db.commit()
                finally:
                    db.close()

            await asyncio.to_thread(_process_decisions)
        except Exception as exc:
            log.warning("Scope checker error: %s", exc)

        await asyncio.sleep(10)


# ─── Background Task: Survey Monitor ─────────────────────────────────────────

async def survey_monitor() -> None:
    """
    Poll /survey/status on the engine every 3 s and write an EngineLog entry
    (source='survey') whenever the survey state changes.

    Detected transitions:
      idle → running    → "Survey started"
      running → done    → "Survey complete"
      running → error   → "Survey error"
      running → cancelled → "Survey cancelled"
    """
    CHECK_INTERVAL = 3.0
    _last_status: str | None = None   # None = first poll not done yet
    _http = httpx.AsyncClient(timeout=5.0)

    log.info("Survey monitor started (interval=%.0fs)", CHECK_INTERVAL)

    # Stagger startup — let connectivity_poller's 2s delay go first, then start
    # survey polling 1s later so the first HTTP batches don't all land at once.
    await asyncio.sleep(3.0)

    while True:
        try:
            r = await _http.get(f"http://localhost:{ENGINE_PORT}/survey/status")
            if r.status_code == 200:
                    st        = r.json()
                    status    = st.get("status", "idle")
                    room      = st.get("room_label") or "unknown room"
                    ssid      = st.get("target_ssid") or "?"
                    collected = st.get("collected_samples", 0)
                    total     = st.get("total_samples", 0)

                    if _last_status is None:
                        _last_status = status   # first check, no transition to log

                    elif status != _last_status:
                        if status == "running":
                            _write_survey_log("info",
                                f"Survey started — room '{room}', SSID '{ssid}', "
                                f"{total} samples requested",
                                {"room_label": room, "target_ssid": ssid,
                                 "total_samples": total, "event": "survey_start"},
                            )
                            log.info("[survey] Started: room=%r ssid=%r n=%d", room, ssid, total)

                        elif status == "done":
                            _write_survey_log("info",
                                f"Survey complete — room '{room}', "
                                f"{collected}/{total} samples collected",
                                {"room_label": room, "collected_samples": collected,
                                 "total_samples": total, "event": "survey_done"},
                            )
                            log.info("[survey] Done: room=%r %d/%d samples", room, collected, total)

                        elif status == "cancelled":
                            _write_survey_log("warn",
                                f"Survey cancelled — room '{room}', "
                                f"{collected}/{total} samples saved",
                                {"room_label": room, "collected_samples": collected,
                                 "total_samples": total, "event": "survey_cancelled"},
                            )
                            log.info("[survey] Cancelled: room=%r %d/%d saved", room, collected, total)

                        elif status == "error":
                            err = st.get("error") or "unknown error"
                            _write_survey_log("alert",
                                f"Survey error — room '{room}': {err}",
                                {"room_label": room, "error": err,
                                 "collected_samples": collected, "event": "survey_error"},
                            )
                            log.error("[survey] Error: room=%r err=%s", room, err)

                        _last_status = status

        except Exception:
            pass   # engine not running yet — silent skip

        await asyncio.sleep(CHECK_INTERVAL)


# ─── Background Task: Engine Connectivity Monitor ────────────────────────────

async def engine_monitor() -> None:
    """
    Poll the Hybrid engine health endpoint every 10 s and log every
    connect / disconnect transition so there is a permanent record of
    when the engine went down and when it came back.

    Tracks cumulative downtime so the reconnect log entry includes
    "engine was unreachable for X seconds".

    IMPORTANT: Only writes to the DB on state TRANSITIONS (up→down or
    down→up), NOT on every tick.  Logging every tick floods the DB and
    causes the very disconnection issues we're trying to detect.
    """
    CHECK_INTERVAL = 10.0   # seconds between checks
    HTTP_TIMEOUT   =  5.0   # per-request timeout (generous for mid-survey load)

    _engine_up: bool | None = None   # None = not yet known
    _went_down_at: datetime | None = None
    _last_error: str | None = None   # raw exception from most recent failed probe

    # Persistent client — reused across all iterations, no churn
    _http = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

    log.info("Engine monitor started (interval=%.0fs, timeout=%.0fs)", CHECK_INTERVAL, HTTP_TIMEOUT)

    while True:
        raw_error: str | None = None
        try:
            r = await _http.get(f"http://localhost:{ENGINE_PORT}/health")
            now_up = r.status_code == 200
            if not now_up:
                raw_error = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            now_up = False
            raw_error = f"{type(exc).__name__}: {exc}"

        now = datetime.now(timezone.utc)

        if _engine_up is None:
            # First check — just record current state, don't log (startup already did it)
            _engine_up = now_up
            if not now_up:
                _went_down_at = now
                _last_error   = raw_error

        elif now_up and not _engine_up:
            # Transition: down → up
            downtime_s = int((now - _went_down_at).total_seconds()) if _went_down_at else "?"
            await asyncio.to_thread(
                _write_system_log,
                "info",
                f"Engine reconnected on port {ENGINE_PORT} (was unreachable for {downtime_s}s)",
                {"port": ENGINE_PORT, "downtime_s": downtime_s, "event": "engine_reconnect",
                 "last_error": _last_error},
            )
            log.info("Engine reconnected after %ss", downtime_s)
            _engine_up  = True
            _went_down_at = None
            _last_error   = None

        elif not now_up and _engine_up:
            # Transition: up → down — include raw error so the log shows *why*
            _went_down_at = now
            _last_error   = raw_error
            await asyncio.to_thread(
                _write_system_log,
                "alert",
                f"Engine on port {ENGINE_PORT} became unreachable"
                + (f" — {raw_error}" if raw_error else ""),
                {"port": ENGINE_PORT, "event": "engine_disconnect", "raw_error": raw_error},
            )
            log.warning("Engine on port %s became unreachable: %s", ENGINE_PORT, raw_error)
            _engine_up = False

        elif not now_up and not _engine_up:
            # Still down — update last_error in case it changes
            _last_error = raw_error

        await asyncio.sleep(CHECK_INTERVAL)


# ─── Background Task: Engine Log Forwarder ───────────────────────────────────

# Cursor used for incremental scraping — ISO timestamp of last forwarded entry
_engine_log_state: dict = {"last_ts": ""}


def _write_engine_decision_log(level: str, message: str, meta: dict) -> None:
    """Write a decision/engine event to EngineLog with source='engine'."""
    try:
        db = SessionLocal()
        try:
            db.add(EngineLog(
                level=level,
                tag_id=meta.get("device_id"),
                source="engine",
                message=message,
                meta=meta,
                timestamp=datetime.now(timezone.utc),
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        log.warning("Failed to write engine decision log: %s", exc)


async def engine_log_forwarder() -> None:
    """
    Every 5 s, scrape GET /logs from the engine and forward new entries to the
    platform EngineLog DB (source='engine').  Uses ISO timestamp cursoring so
    only unseen entries are written — no duplicates.

    Engine log severity → EngineLog level mapping:
        INFO  → "info"
        WARN  → "warn"
        ERROR → "alert"
        (other) → "info"
    """
    INTERVAL = 5.0
    _http = httpx.AsyncClient(timeout=5.0)
    _sev_map = {"INFO": "info", "CONFIG": "config", "WARN": "warn",
                "WARNING": "warn", "ERROR": "alert", "CRITICAL": "alert"}

    log.info("Engine log forwarder started (interval=%.0fs)", INTERVAL)
    await asyncio.sleep(20)   # wait for engine to fully start

    while True:
        try:
            r = await _http.get(
                f"http://localhost:{ENGINE_PORT}/logs",
                params={"limit": 200},
            )
            if r.status_code == 200:
                items = r.json().get("items", [])
                # Only forward entries newer than our cursor
                new_items = [
                    e for e in items
                    if e.get("timestamp", "") > _engine_log_state["last_ts"]
                ]
                if new_items:
                    _engine_log_state["last_ts"] = max(
                        e["timestamp"] for e in new_items
                    )

                    def _ingest(entries):
                        db = SessionLocal()
                        try:
                            for e in entries:
                                sev = e.get("severity", "INFO").upper()
                                db.add(EngineLog(
                                    level=_sev_map.get(sev, "info"),
                                    tag_id=e.get("device_id") or None,
                                    source="engine",
                                    message=e.get("message", ""),
                                    meta={"severity": sev,
                                          "device_id": e.get("device_id")},
                                    timestamp=datetime.now(timezone.utc),
                                ))
                            db.commit()
                        finally:
                            db.close()

                    await asyncio.to_thread(_ingest, new_items)
        except Exception:
            pass   # engine not running — silent skip

        await asyncio.sleep(INTERVAL)


# ─── Background Task: Tag Presence Poller ────────────────────────────────────

# Last known online/offline status per tag SSID (for transition logging)
_tag_online: dict[str, str] = {}   # {ssid: "online" | "offline"}


async def tag_poller() -> None:
    """
    Every 30 s, call GET /seen_ssids on the engine (which returns every SSID
    visible in the last 120 s across all AP apscans) and update each enabled
    tag's status in the database.

    State changes are written to EngineLog (source='connectivity'):
        "online"  → info
        "offline" → warn
    """
    INTERVAL   = 30.0
    MAX_AGE_S  = 120      # match the engine's default window
    _http = httpx.AsyncClient(timeout=5.0)

    log.info("Tag poller started (interval=%.0fs, max_age=%ds)", INTERVAL, MAX_AGE_S)
    await asyncio.sleep(25)   # stagger after engine_log_forwarder startup

    while True:
        try:
            r = await _http.get(
                f"http://localhost:{ENGINE_PORT}/seen_ssids",
                params={"max_age_s": MAX_AGE_S},
            )
            if r.status_code != 200:
                await asyncio.sleep(INTERVAL)
                continue

            seen: dict = r.json().get("ssids", {})
            now = datetime.now(timezone.utc)

            def _update_tags():
                db = SessionLocal()
                try:
                    tags = db.query(Tag).filter_by(enabled=True).all()
                    for tag in tags:
                        ssid = tag.ssid
                        if not ssid:
                            continue

                        presence = seen.get(ssid)
                        if presence:
                            try:
                                last_seen_dt = datetime.fromisoformat(
                                    presence["last_seen"]
                                )
                                # Treat as online if seen within MAX_AGE_S
                                is_online = (
                                    (now - last_seen_dt).total_seconds() < MAX_AGE_S
                                )
                            except Exception:
                                is_online = False
                        else:
                            is_online = False

                        new_status = "online" if is_online else "offline"
                        prev       = _tag_online.get(ssid)

                        # Persist status change
                        if new_status != (tag.status or "offline"):
                            tag.status      = new_status
                            tag.last_polled = now

                        # Log on transition
                        if prev is not None and prev != new_status:
                            signals_str = ""
                            if is_online and presence:
                                sigs = presence.get("signals", {})
                                signals_str = "  " + "  ".join(
                                    f"{ap}:{rssi:+.0f}"
                                    for ap, rssi in sorted(sigs.items())
                                )
                            level = "info" if is_online else "warn"
                            msg = (
                                f"Tag '{tag.name or ssid}' ({ssid}) came "
                                f"{'ONLINE' if is_online else 'OFFLINE'}"
                                + signals_str
                            )
                            db.add(EngineLog(
                                level=level,
                                tag_id=ssid,
                                source="connectivity",
                                message=msg,
                                meta={
                                    "ssid":     ssid,
                                    "tag_id":   tag.tag_id,
                                    "status":   new_status,
                                    "signals":  (presence or {}).get("signals", {}),
                                },
                                timestamp=now,
                            ))
                            log.info("[tag_poller] %s", msg)

                        _tag_online[ssid] = new_status

                    db.commit()
                finally:
                    db.close()

            await asyncio.to_thread(_update_tags)

        except Exception as exc:
            log.warning("Tag poller error: %s", exc)

        await asyncio.sleep(INTERVAL)


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──────────────────────────────────────────────────────────────
    # Run all blocking startup I/O in a thread pool so the event loop (and
    # therefore uvicorn's HTTP server) stays responsive from the very first
    # moment.  create_all, migrations, and the initial _write_system_log calls
    # each require synchronous Postgres round-trips that previously caused a
    # 2-second event-loop stall on cold start.
    def _blocking_startup() -> str | None:
        """Returns the engine health note (or None) to be logged after."""
        log.info("Creating database tables (if not exist)...")
        Base.metadata.create_all(bind=db_engine)
        log.info("Running column migrations...")
        _migrate_columns(db_engine)
        log.info("Platform API ready.")

        _write_system_log("info", f"Platform backend started on port {PLATFORM_PORT}", {
            "port": PLATFORM_PORT,
            "engine_url": ENGINE_API_URL,
            "engine_port": ENGINE_PORT,
        })
        _write_system_log("info", "Database connected — tables verified", {
            "db_url": str(db_engine.url).split("@")[-1],
        })
        return None   # engine check done async below

    await asyncio.to_thread(_blocking_startup)

    # Check engine reachability at startup (async — doesn't block)
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"http://localhost:{ENGINE_PORT}/health")
            if resp.status_code == 200:
                await asyncio.to_thread(
                    _write_system_log,
                    "info",
                    f"Hybrid engine reachable on port {ENGINE_PORT}",
                    {"port": ENGINE_PORT, "response": resp.json()},
                )
            else:
                await asyncio.to_thread(
                    _write_system_log,
                    "warn",
                    f"Hybrid engine returned {resp.status_code} on port {ENGINE_PORT}",
                    None,
                )
    except Exception:
        await asyncio.to_thread(
            _write_system_log,
            "warn",
            f"Hybrid engine not reachable on port {ENGINE_PORT} (will retry when needed)",
            {"port": ENGINE_PORT},
        )

    # Start background tasks
    watchdog_task   = asyncio.create_task(_loop_watchdog(),        name="loop_watchdog")
    poller_task     = asyncio.create_task(connectivity_poller(),   name="connectivity_poller")
    scope_task      = asyncio.create_task(scope_checker(),         name="scope_checker")
    monitor_task    = asyncio.create_task(engine_monitor(),        name="engine_monitor")
    survey_task     = asyncio.create_task(survey_monitor(),        name="survey_monitor")
    log_fwd_task    = asyncio.create_task(engine_log_forwarder(),  name="engine_log_forwarder")
    tag_poll_task   = asyncio.create_task(tag_poller(),            name="tag_poller")

    await asyncio.to_thread(
        _write_system_log,
        "info",
        "Background tasks started — loop_watchdog, connectivity_poller, scope_checker, "
        "engine_monitor, survey_monitor, engine_log_forwarder, tag_poller",
        None,
    )

    yield  # ── app is running; block here until shutdown ──────────────────

    # ── Shutdown: cancel all background tasks ──────────────────────────────
    log.info("Shutting down background tasks...")
    for task in (watchdog_task, poller_task, scope_task, monitor_task,
                 survey_task, log_fwd_task, tag_poll_task):
        task.cancel()
    await asyncio.gather(
        watchdog_task, poller_task, scope_task, monitor_task,
        survey_task, log_fwd_task, tag_poll_task,
        return_exceptions=True,
    )
    log.info("All background tasks stopped.")


# ─── FastAPI Application ──────────────────────────────────────────────────────

app = FastAPI(title="IPS Management Platform", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(hierarchy_router)
app.include_router(devices_router)
app.include_router(logs_router)
app.include_router(config_router)
app.include_router(floorplan_editor_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PLATFORM_PORT, reload=True)
