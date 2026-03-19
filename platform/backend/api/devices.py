"""
CRUD routes for Anchors and Tags.

Anchors are scoped to a Floor.  Tags are global.
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from models import Anchor, Tag, Floor, get_db
from api.schemas import (
    AnchorCreate, AnchorUpdate, AnchorOut,
    TagCreate, TagUpdate, TagOut,
)

router = APIRouter(prefix="/api/v1", tags=["devices"])


# ── Anchors ───────────────────────────────────────────────────────────────────

@router.post("/floors/{floor_id}/anchors", response_model=AnchorOut, status_code=201)
def create_anchor(floor_id: int, body: AnchorCreate, db: Session = Depends(get_db)):
    if not db.get(Floor, floor_id):
        raise HTTPException(404, "Floor not found")
    # Validate EnGenius IP subnet
    if body.anchor_type == "engenius_ap" and body.ip_address:
        if not body.ip_address.startswith("192.168.1."):
            raise HTTPException(400, "EnGenius AP IP must be in 192.168.1.0/24")
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
    db: Session = Depends(get_db),
):
    q = db.query(Anchor)
    if anchor_type:
        q = q.filter_by(anchor_type=anchor_type)
    if status:
        q = q.filter_by(status=status)
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
    # Validate IP change for EnGenius
    if "ip_address" in updates and anchor.anchor_type == "engenius_ap":
        if updates["ip_address"] and not updates["ip_address"].startswith("192.168.1."):
            raise HTTPException(400, "EnGenius AP IP must be in 192.168.1.0/24")
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
