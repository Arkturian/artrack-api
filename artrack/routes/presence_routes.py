"""
Visitor presence & position history (GPS pings).

Feature (Alex, 2026-07-05, enabled by the Postgres cutover — multi-writer):
- the guide app sends anonymised position pings every ~10-15s per visitor
- "wo war ich": the client keeps its own session_id and can fetch its path
- "hier sind Leute": live presence = last position of every session that
  pinged within a time window

Privacy by design: session_id is client-generated and opaque; the server
stores NO user linkage. Presence output rounds coordinates to ~11 m so live
positions of strangers are never centimeter-precise. A session owner gets
its own exact path back (it sent the data in the first place).

Auth: same model as the other track reads — X-API-KEY, private tracks only
for creator/admin/moderator. The guide app already sends a key.
"""
import random
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import User, get_current_user
from ..database import get_db
from ..models import GpsPing, Track

router = APIRouter()

MAX_BATCH = 500
RETENTION_DAYS = 90


class PingIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    recorded_at: datetime
    altitude: Optional[float] = None
    accuracy: Optional[float] = None
    speed_kmh: Optional[float] = None
    heading: Optional[float] = None


class PingBatch(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    points: List[PingIn] = Field(min_length=1, max_length=MAX_BATCH)


def _track_or_403(db: Session, track_id: int, user: User) -> Track:
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.visibility == "private" and track.created_by != user.id \
            and getattr(user, "trust_level", None) not in ("admin", "moderator"):
        raise HTTPException(status_code=403, detail="Access denied")
    return track


@router.post("/{track_id}/pings")
def ingest_pings(
    track_id: int,
    batch: PingBatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Batch-ingest visitor pings (app buffers ~10-15s pings, sends periodically)."""
    _track_or_403(db, track_id, current_user)
    now = datetime.utcnow()
    db.bulk_save_objects([
        GpsPing(
            track_id=track_id,
            session_id=batch.session_id,
            latitude=p.lat,
            longitude=p.lon,
            altitude=p.altitude,
            accuracy=p.accuracy,
            speed_kmh=p.speed_kmh,
            heading=p.heading,
            recorded_at=p.recorded_at.replace(tzinfo=None),
            received_at=now,
        )
        for p in batch.points
    ])
    db.commit()

    # Opportunistic retention sweep (~1% of ingests): raw pings are transient
    # telemetry; long-term insight comes from aggregates, not raw traces.
    if random.random() < 0.01:
        cutoff = now - timedelta(days=RETENTION_DAYS)
        db.query(GpsPing).filter(GpsPing.received_at < cutoff).delete()
        db.commit()

    return {"accepted": len(batch.points), "session_id": batch.session_id}


@router.get("/{track_id}/presence")
def live_presence(
    track_id: int,
    window_s: int = Query(600, ge=30, le=86400, description="Sitzung gilt als 'da', wenn letzter Ping jünger als window_s ist"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Live: who is on the track right now (last position per active session)."""
    _track_or_403(db, track_id, current_user)
    since = datetime.utcnow() - timedelta(seconds=window_s)
    # newest ping per session inside the window (portable group-by-max-id)
    latest_ids = select(func.max(GpsPing.id)).where(
        GpsPing.track_id == track_id,
        GpsPing.received_at >= since,
    ).group_by(GpsPing.session_id)
    rows = db.query(GpsPing).filter(GpsPing.id.in_(latest_ids)).all()
    return {
        "track_id": track_id,
        "window_s": window_s,
        "active_sessions": len(rows),
        "positions": [
            {
                "session_id": r.session_id,
                # ~11 m grid — live positions of strangers stay fuzzy
                "lat": round(r.latitude, 4),
                "lon": round(r.longitude, 4),
                "last_seen": r.received_at.isoformat() + "Z",
            }
            for r in rows
        ],
    }


@router.get("/{track_id}/pings/{session_id}")
def session_path(
    track_id: int,
    session_id: str,
    since: Optional[datetime] = Query(None, description="nur Punkte nach diesem Zeitpunkt (recorded_at)"),
    limit: int = Query(10000, ge=1, le=50000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """'Wo war ich': full exact path of one session (the client knows its own id)."""
    _track_or_403(db, track_id, current_user)
    q = db.query(GpsPing).filter(
        GpsPing.track_id == track_id,
        GpsPing.session_id == session_id,
    )
    if since is not None:
        q = q.filter(GpsPing.recorded_at >= since.replace(tzinfo=None))
    rows = q.order_by(GpsPing.recorded_at.asc()).limit(limit).all()
    return {
        "track_id": track_id,
        "session_id": session_id,
        "count": len(rows),
        "points": [
            {
                "lat": r.latitude,
                "lon": r.longitude,
                "recorded_at": r.recorded_at.isoformat() + "Z",
                "altitude": r.altitude,
                "accuracy": r.accuracy,
                "speed_kmh": r.speed_kmh,
                "heading": r.heading,
            }
            for r in rows
        ],
    }
