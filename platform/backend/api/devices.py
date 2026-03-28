"""
CRUD routes for Anchors and Tags.

Anchors can be registered globally (no floor) via POST /anchors,
or placed on a specific floor via POST /floors/{floor_id}/anchors.
Tags are always global.
"""

import asyncio
import logging
import socket
import subprocess
import sys
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query as QParam
from sqlalchemy.orm import Session

from models import Anchor, Tag, Floor, EngineLog, get_db
from api.schemas import (
    AnchorCreate, AnchorUpdate, AnchorOut,
    TagCreate, TagUpdate, TagOut,
)

log = logging.getLogger("devices")


def _poll_log(db: Session, level: str, message: str, meta: dict | None = None) -> None:
    """Write a connectivity EngineLog entry from the poll endpoint."""
    db.add(EngineLog(
        level=level,
        source="connectivity",
        message=message,
        meta=meta or {},
        timestamp=datetime.now(timezone.utc),
    ))

router = APIRouter(prefix="/api/v1", tags=["devices"])


# ── Connectivity helpers ───────────────────────────────────────────────────────

def _build_ping_cmd(ip: str) -> list[str]:
    """Return the platform-correct ping command for one packet with a 1-second timeout."""
    if sys.platform == "win32":
        return ["ping", "-n", "1", "-w", "1000", ip]
    else:
        return ["ping", "-c", "1", "-W", "1", ip]


async def _ping_once(ip: str) -> bool:
    """
    Single ICMP ping using asyncio subprocess.
    Falls back to a blocking subprocess call if the asyncio method raises
    NotImplementedError (happens on Windows when the event loop lacks
    subprocess support — e.g. uvicorn running on SelectorEventLoop).
    """
    cmd = _build_ping_cmd(ip)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        log.debug("ping %s → returncode=%s", ip, proc.returncode)
        return proc.returncode == 0
    except NotImplementedError:
        # Windows SelectorEventLoop doesn't support subprocesses — fall back
        log.warning("asyncio subprocess not available (SelectorEventLoop?), using blocking ping for %s", ip)
        return _ping_blocking(ip)
    except Exception as exc:
        log.warning("asyncio ping %s failed (%s: %s), trying blocking fallback", ip, type(exc).__name__, exc)
        return _ping_blocking(ip)


def _ping_blocking(ip: str) -> bool:
    """Blocking subprocess ping — used as fallback when asyncio subprocess fails."""
    try:
        result = subprocess.run(
            _build_ping_cmd(ip),
            capture_output=True,
            timeout=3,
        )
        log.debug("blocking ping %s → returncode=%s", ip, result.returncode)
        return result.returncode == 0
    except Exception as exc:
        log.warning("blocking ping %s failed: %s", ip, exc)
        return False


async def _ping_verbose(ip: str) -> dict:
    """
    Diagnostic ping that captures full stdout/stderr and reports what happened.
    Used by the debug endpoint only.
    """
    cmd = _build_ping_cmd(ip)
    result = {
        "ip": ip,
        "cmd": " ".join(cmd),
        "platform": sys.platform,
        "method": None,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "reachable": False,
        "error": None,
    }

    # Try asyncio first
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=5)
        result["method"] = "asyncio"
        result["returncode"] = proc.returncode
        result["stdout"] = stdout_b.decode(errors="replace").strip()
        result["stderr"] = stderr_b.decode(errors="replace").strip()
        result["reachable"] = proc.returncode == 0
        return result
    except NotImplementedError as exc:
        result["error"] = f"asyncio subprocess not supported: {exc}"
    except Exception as exc:
        result["error"] = f"asyncio failed: {type(exc).__name__}: {exc}"

    # Fall back to blocking subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=5)
        result["method"] = "blocking_subprocess"
        result["returncode"] = r.returncode
        result["stdout"] = r.stdout.decode(errors="replace").strip()
        result["stderr"] = r.stderr.decode(errors="replace").strip()
        result["reachable"] = r.returncode == 0
        result["error"] = None
    except Exception as exc2:
        result["method"] = "blocking_subprocess_failed"
        result["error"] = f"blocking also failed: {type(exc2).__name__}: {exc2}"

    return result


def _validate_engenius_ip(anchor_type: str, ip_address: str):
    if anchor_type == "engenius_ap" and ip_address:
        if not ip_address.startswith("192.168.1."):
            raise HTTPException(400, "EnGenius AP IP must be in 192.168.1.0/24")


# ── Anchors ───────────────────────────────────────────────────────────────────

@router.post("/anchors", response_model=AnchorOut, status_code=201)
def register_anchor(body: AnchorCreate, db: Session = Depends(get_db)):
    """Register an anchor into the device inventory without a floor assignment."""
    _validate_engenius_ip(body.anchor_type, body.ip_address)
    anchor = Anchor(floor_id=None, **body.model_dump())
    db.add(anchor)
    db.commit()
    db.refresh(anchor)
    return anchor


@router.post("/floors/{floor_id}/anchors", response_model=AnchorOut, status_code=201)
def create_anchor(floor_id: int, body: AnchorCreate, db: Session = Depends(get_db)):
    """Place an anchor on a specific floor."""
    if not db.get(Floor, floor_id):
        raise HTTPException(404, "Floor not found")
    _validate_engenius_ip(body.anchor_type, body.ip_address)
    anchor = Anchor(floor_id=floor_id, **body.model_dump())
    db.add(anchor)
    db.commit()
    db.refresh(anchor)
    return anchor


@router.get("/floors/{floor_id}/anchors", response_model=List[AnchorOut])
def list_anchors(floor_id: int, db: Session = Depends(get_db)):
    return db.query(Anchor).filter_by(floor_id=floor_id).order_by(Anchor.anchor_id).all()


@router.get("/anchors", response_model=List[AnchorOut])
def list_all_anchors(
    anchor_type: Optional[str] = None,
    status: Optional[str] = None,
    device_status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(Anchor)
    if anchor_type:
        q = q.filter_by(anchor_type=anchor_type)
    if status:
        q = q.filter_by(status=status)
    if device_status:
        q = q.filter_by(device_status=device_status)
    return q.order_by(Anchor.anchor_id).all()


@router.get("/anchors/{anchor_pk}", response_model=AnchorOut)
def get_anchor(anchor_pk: int, db: Session = Depends(get_db)):
    anchor = db.get(Anchor, anchor_pk)
    if not anchor:
        raise HTTPException(404, "Anchor not found")
    return anchor


@router.patch("/anchors/{anchor_pk}", response_model=AnchorOut)
def update_anchor(anchor_pk: int, body: AnchorUpdate, db: Session = Depends(get_db)):
    anchor = db.get(Anchor, anchor_pk)
    if not anchor:
        raise HTTPException(404, "Anchor not found")
    updates = body.model_dump(exclude_unset=True)
    effective_type = updates.get("anchor_type", anchor.anchor_type)
    effective_ip   = updates.get("ip_address",   anchor.ip_address)
    _validate_engenius_ip(effective_type, effective_ip)
    for key, val in updates.items():
        setattr(anchor, key, val)
    db.commit()
    db.refresh(anchor)
    return anchor


@router.delete("/anchors/{anchor_pk}", status_code=204)
def delete_anchor(anchor_pk: int, db: Session = Depends(get_db)):
    anchor = db.get(Anchor, anchor_pk)
    if not anchor:
        raise HTTPException(404, "Anchor not found")
    db.delete(anchor)
    db.commit()


@router.post("/anchors/poll")
async def poll_anchors_now(db: Session = Depends(get_db)):
    """
    Manually trigger an immediate ICMP ping of all enabled anchors with an IP.
    Returns per-anchor results: {anchor_id, ip, reachable, status}.
    Updates anchor.status, anchor.last_polled, and anchor.last_reached in DB.
    Writes a summary + per-anchor status changes to EngineLog.
    """
    anchors = (
        db.query(Anchor)
        .filter_by(enabled=True)
        .filter(Anchor.ip_address != "")
        .all()
    )
    if not anchors:
        _poll_log(db, "info", "Manual poll triggered — no anchors with IP addresses configured")
        db.commit()
        return {"polled": 0, "results": []}

    now = datetime.now(timezone.utc)
    results_raw = await asyncio.gather(
        *[_ping_once(a.ip_address) for a in anchors],
        return_exceptions=True,
    )

    results = []
    online_ids, offline_ids, changed = [], [], []

    for anchor, ok in zip(anchors, results_raw):
        if isinstance(ok, Exception):
            ok = False
        prev_status = anchor.status
        anchor.last_polled = now

        if ok:
            anchor.status = "online"
            anchor.last_reached = now
            online_ids.append(anchor.anchor_id)
            if prev_status != "online":
                changed.append((anchor.anchor_id, prev_status, "online"))
                _poll_log(db, "info",
                    f"[Manual poll] {anchor.anchor_id} ({anchor.ip_address}) — ONLINE",
                    {"anchor_id": anchor.anchor_id, "ip": anchor.ip_address,
                     "prev_status": prev_status, "trigger": "manual"})
        else:
            anchor.status = "offline"
            offline_ids.append(anchor.anchor_id)
            if prev_status != "offline":
                changed.append((anchor.anchor_id, prev_status, "offline"))
                _poll_log(db, "alert",
                    f"[Manual poll] {anchor.anchor_id} ({anchor.ip_address}) — OFFLINE (no ping response)",
                    {"anchor_id": anchor.anchor_id, "ip": anchor.ip_address,
                     "prev_status": prev_status, "trigger": "manual"})

        results.append({
            "anchor_id": anchor.anchor_id,
            "ip": anchor.ip_address,
            "reachable": bool(ok),
            "status": anchor.status,
        })

    # Write summary log entry
    total = len(anchors)
    n_online  = len(online_ids)
    n_offline = len(offline_ids)
    summary = (
        f"Manual poll complete — {n_online}/{total} online"
        + (f", {n_offline} unreachable" if n_offline else "")
        + (f" | Changes: {', '.join(f'{a} {p}→{s}' for a, p, s in changed)}" if changed else " | No status changes")
    )
    _poll_log(db, "info", summary, {
        "trigger": "manual",
        "total": total,
        "online": n_online,
        "offline": n_offline,
        "online_ids": online_ids,
        "offline_ids": offline_ids,
    })

    db.commit()
    return {"polled": total, "results": results}


@router.get("/anchors/ping-debug")
async def ping_debug(ip: str = QParam(..., description="IP address to test")):
    """
    Debug endpoint — runs a verbose ping against a single IP and returns
    the raw stdout, stderr, return code, and method used.
    Helps diagnose why ICMP pings are failing in the background poller.
    """
    result = await _ping_verbose(ip)

    # Also test basic TCP reachability (port 80 / 443 / any open port)
    tcp_results = {}
    for port in [80, 443, 22, 23, 8080]:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            tcp_ok = sock.connect_ex((ip, port)) == 0
            sock.close()
            tcp_results[port] = tcp_ok
        except Exception as e:
            tcp_results[port] = f"error: {e}"

    result["tcp_probe"] = tcp_results
    result["any_tcp_open"] = any(v is True for v in tcp_results.values())
    return result


# ── Tags ──────────────────────────────────────────────────────────────────────

@router.post("/tags", response_model=TagOut, status_code=201)
def create_tag(body: TagCreate, db: Session = Depends(get_db)):
    tag = Tag(**body.model_dump())
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag


@router.get("/tags", response_model=List[TagOut])
def list_tags(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(Tag)
    if status:
        q = q.filter_by(status=status)
    return q.order_by(Tag.tag_id).all()


@router.get("/tags/{tag_pk}", response_model=TagOut)
def get_tag(tag_pk: int, db: Session = Depends(get_db)):
    tag = db.get(Tag, tag_pk)
    if not tag:
        raise HTTPException(404, "Tag not found")
    return tag


@router.patch("/tags/{tag_pk}", response_model=TagOut)
def update_tag(tag_pk: int, body: TagUpdate, db: Session = Depends(get_db)):
    tag = db.get(Tag, tag_pk)
    if not tag:
        raise HTTPException(404, "Tag not found")
    for key, val in body.model_dump(exclude_unset=True).items():
        setattr(tag, key, val)
    db.commit()
    db.refresh(tag)
    return tag


@router.delete("/tags/{tag_pk}", status_code=204)
def delete_tag(tag_pk: int, db: Session = Depends(get_db)):
    tag = db.get(Tag, tag_pk)
    if not tag:
        raise HTTPException(404, "Tag not found")
    db.delete(tag)
    db.commit()
