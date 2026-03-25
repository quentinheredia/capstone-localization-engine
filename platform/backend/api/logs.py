"""
Routes for boundary crossings, alerts, and the engine event log.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc

from models import BoundaryCrossing, Alert, EngineLog, get_db
from api.schemas import BoundaryCrossingOut, AlertOut, AlertAck, EngineLogOut, EngineLogAck

router = APIRouter(prefix="/api/v1", tags=["logs"])


# ── Boundary Crossings ───────────────────────────────────────────────────────

@router.get("/crossings", response_model=List[BoundaryCrossingOut])
def list_crossings(
    tag_id: Optional[str] = None,
    room: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    q = db.query(BoundaryCrossing)
    if tag_id:
        q = q.filter_by(tag_id=tag_id)
    if room:
        q = q.filter(
            BoundaryCrossing.new_location.contains(room)
            | BoundaryCrossing.previous_location.contains(room)
        )
    return q.order_by(desc(BoundaryCrossing.timestamp)).limit(limit).all()


# ── Alerts ────────────────────────────────────────────────────────────────────

@router.get("/alerts", response_model=List[AlertOut])
def list_alerts(
    tag_id: Optional[str] = None,
    severity: Optional[str] = None,
    acknowledged: Optional[bool] = None,
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    q = db.query(Alert)
    if tag_id:
        q = q.filter_by(tag_id=tag_id)
    if severity:
        q = q.filter_by(severity=severity)
    if acknowledged is not None:
        q = q.filter_by(acknowledged=acknowledged)
    return q.order_by(desc(Alert.timestamp)).limit(limit).all()


@router.post("/alerts/{alert_id}/ack", response_model=AlertOut)
def acknowledge_alert(alert_id: int, body: AlertAck, db: Session = Depends(get_db)):
    alert = db.get(Alert, alert_id)
    if not alert:
        raise HTTPException(404, "Alert not found")
    alert.acknowledged = True
    alert.acknowledged_by = body.acknowledged_by
    alert.acknowledged_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(alert)
    return alert


# ── Engine Logs ───────────────────────────────────────────────────────────────

@router.get("/engine-logs", response_model=List[EngineLogOut])
def list_engine_logs(
    level:   Optional[str] = None,
    tag_id:  Optional[str] = None,
    source:  Optional[str] = None,
    unacked: Optional[bool] = None,
    limit:   int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """
    Return engine log entries, most-recent first.
    Filters: level (info/warn/alert/config/security), tag_id, source, unacked.
    """
    q = db.query(EngineLog)
    if level:
        q = q.filter_by(level=level)
    if tag_id:
        q = q.filter_by(tag_id=tag_id)
    if source:
        q = q.filter_by(source=source)
    if unacked is True:
        q = q.filter_by(acknowledged=False)
    return q.order_by(desc(EngineLog.timestamp)).limit(limit).all()


@router.post("/engine-logs/{log_id}/ack", response_model=EngineLogOut)
def ack_engine_log(log_id: int, body: EngineLogAck, db: Session = Depends(get_db)):
    entry = db.get(EngineLog, log_id)
    if not entry:
        raise HTTPException(404, "Log entry not found")
    entry.acknowledged    = True
    entry.acknowledged_by = body.acknowledged_by
    entry.acknowledged_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(entry)
    return entry


@router.delete("/engine-logs", status_code=204)
def clear_engine_logs(
    older_than_hours: Optional[int] = Query(None, ge=0),
    db: Session = Depends(get_db),
):
    """
    Delete engine log entries.
    - older_than_hours=0 or omitted → delete ALL entries
    - older_than_hours=N (N>0) → delete entries older than N hours
    """
    if older_than_hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        db.query(EngineLog).filter(EngineLog.timestamp < cutoff).delete()
    else:
        db.query(EngineLog).delete()
    db.commit()
