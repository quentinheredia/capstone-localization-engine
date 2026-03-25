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


# ─── Background Task: ICMP Connectivity Poller ───────────────────────────────

async def _ping(ip: str) -> bool:
    """Non-blocking single ICMP ping. Returns True if host responded."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", "1", ip,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0
    except Exception:
        return False


def _write_connectivity_log(db, anchor: Anchor, fail_count: int) -> None:
    """Emit an EngineLog entry when an anchor crosses a fail threshold."""
    # Only log at threshold boundaries (1, 3, 5) to avoid flooding
    if fail_count not in (1, 3, 5):
        return
    level = "alert" if fail_count <= 2 else ("config" if fail_count <= 4 else "security")
    msg = (
        f"Anchor {anchor.anchor_id} ({anchor.ip_address}) unreachable — "
        f"{fail_count} consecutive failure{'s' if fail_count > 1 else ''}"
    )
    entry = EngineLog(
        level=level,
        source="connectivity",
        message=msg,
        meta={
            "anchor_id":     anchor.anchor_id,
            "ip":            anchor.ip_address,
            "cycles_missed": fail_count,
        },
        timestamp=datetime.now(timezone.utc),
    )
    db.add(entry)


async def connectivity_poller() -> None:
    """
    Poll every 1.5 s (half the default engine poll interval).
    Pings all enabled anchors that have an IP address.
    Updates anchor.status and generates EngineLog entries on failures.
    """
    log.info("Connectivity poller started (interval=1.5s)")
    while True:
        try:
            db = SessionLocal()
            try:
                anchors = (
                    db.query(Anchor)
                    .filter_by(enabled=True)
                    .filter(Anchor.ip_address != "")
                    .all()
                )
                # Ping all anchors concurrently
                results = await asyncio.gather(
                    *[_ping(a.ip_address) for a in anchors],
                    return_exceptions=True,
                )
                for anchor, ok in zip(anchors, results):
                    if isinstance(ok, Exception):
                        ok = False
                    ip = anchor.ip_address
                    if ok:
                        _anchor_fail_counts[ip] = 0
                        anchor.status = "online"
                    else:
                        _anchor_fail_counts[ip] = _anchor_fail_counts.get(ip, 0) + 1
                        anchor.status = "offline"
                        _write_connectivity_log(db, anchor, _anchor_fail_counts[ip])
                    anchor.last_polled = datetime.now(timezone.utc)
                db.commit()
            finally:
                db.close()
        except Exception as exc:
            log.warning("Connectivity poller error: %s", exc)

        await asyncio.sleep(1.5)


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
    log.info("Scope checker started (interval=10s, engine=%s)", ENGINE_API_URL)
    await asyncio.sleep(15)  # Give engine time to start before first check

    while True:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{ENGINE_API_URL}/decisions", params={"limit": 50})
                if resp.status_code != 200:
                    await asyncio.sleep(10)
                    continue
                decisions = resp.json().get("items", [])
        except Exception:
            # Engine not running — check again later
            await asyncio.sleep(10)
            continue

        try:
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

                    # ── Info: movement (room changed) ───────────────────────
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

                    # ── Scope violation check ────────────────────────────────
                    level = _priority_to_level(tag.priority)
                    if level is None:
                        continue  # Priority 1 — no scope restrictions

                    # Room scope check
                    if tag.room_scope and room_id and room_id not in tag.room_scope:
                        # Deduplicate: only log once per (tag, room) pair
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
        except Exception as exc:
            log.warning("Scope checker error: %s", exc)

        await asyncio.sleep(10)


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──────────────────────────────────────────────────────────────
    log.info("Creating database tables (if not exist)...")
    Base.metadata.create_all(bind=db_engine)
    log.info("Running column migrations...")
    _migrate_columns(db_engine)
    log.info("Platform API ready.")

    # Write system log entries so they appear in the Logs page
    _write_system_log("info", f"Platform backend started on port {PLATFORM_PORT}", {
        "port": PLATFORM_PORT,
        "engine_url": ENGINE_API_URL,
        "engine_port": ENGINE_PORT,
    })
    _write_system_log("info", f"Database connected — tables verified", {
        "db_url": str(db_engine.url).split("@")[-1],  # host/db only, no creds
    })

    # Check engine reachability at startup
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"http://localhost:{ENGINE_PORT}/health")
            if resp.status_code == 200:
                _write_system_log("info", f"Hybrid engine reachable on port {ENGINE_PORT}", {
                    "port": ENGINE_PORT, "response": resp.json(),
                })
            else:
                _write_system_log("warn", f"Hybrid engine returned {resp.status_code} on port {ENGINE_PORT}")
    except Exception:
        _write_system_log("warn", f"Hybrid engine not reachable on port {ENGINE_PORT} (will retry when needed)", {
            "port": ENGINE_PORT,
        })

    # Start background tasks
    poller_task = asyncio.create_task(connectivity_poller(), name="connectivity_poller")
    scope_task  = asyncio.create_task(scope_checker(),       name="scope_checker")

    _write_system_log("info", "Background tasks started: connectivity poller (1.5s), scope checker (10s)")

    yield

    # ── shutdown ─────────────────────────────────────────────────────────────
    _write_system_log("info", "Platform backend shutting down")
    poller_task.cancel()
    scope_task.cancel()
    for t in (poller_task, scope_task):
        try:
            await t
        except asyncio.CancelledError:
            pass
    log.info("Shutting down.")


# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="IPS Management Platform",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register route modules
app.include_router(hierarchy_router)
app.include_router(devices_router)
app.include_router(logs_router)
app.include_router(config_router)
app.include_router(floorplan_editor_router)


@app.get("/health")
def health():
    return {"ok": True, "service": "ips-platform"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
