"""
Routes for boundary crossings, alerts, and the log viewer.
"""

from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc

from models import BoundaryCrossing, Alert, get_db
from api.schemas import BoundaryCrossingOut, AlertOut, AlertAck

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
