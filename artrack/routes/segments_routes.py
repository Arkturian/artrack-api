# DEPRECATED: This file uses the OLD segment system (track_segments table)
# The NEW system uses waypoint pairs with metadata_json.segment = {name, role, routeId}
# This file is kept for backwards compatibility but should NOT be used for new features

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from typing import Optional, List

from ..database import get_db
from ..auth import get_current_user
from ..models import Track, Waypoint
from ..models import TrackSegment as TrackSegmentModel
from ..models import TrackRoute as TrackRouteModel
from pydantic import BaseModel
from artrack.collaboration_models import can_read_track

router = APIRouter()

class SegmentStartRequest(BaseModel):
    name: Optional[str] = None

class SegmentStartResponse(BaseModel):
    segment_id: int
    started_at: datetime

class SegmentEndRequest(BaseModel):
    ended_at: Optional[datetime] = None

class SegmentEndResponse(BaseModel):
    segment_id: int
    ended_at: datetime

class SegmentItem(BaseModel):
    id: int
    started_at: datetime
    ended_at: Optional[datetime]
    points: int
    distance_m: float
    route_id: Optional[int] = None

class SegmentGeometryResponse(BaseModel):
    segment_id: int
    coordinates: list[tuple[float, float]]
    along_meters: list[float]

@router.post("/{track_id}/segments/start", response_model=SegmentStartResponse)
async def start_segment(
    track_id: int,
    body: SegmentStartRequest,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    seg = TrackSegmentModel(
        track_id=track_id,
        created_by=current_user.id,
        started_at=datetime.utcnow(),
        name=body.name
    )
    db.add(seg)
    db.commit()
    db.refresh(seg)
    return SegmentStartResponse(segment_id=seg.id, started_at=seg.started_at)

@router.post("/{track_id}/segments/{segment_id}/end", response_model=SegmentEndResponse)
async def end_segment(
    track_id: int,
    segment_id: int,
    body: SegmentEndRequest,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    seg = db.query(TrackSegmentModel).filter(TrackSegmentModel.id == segment_id, TrackSegmentModel.track_id == track_id).first()
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")
    end_time = body.ended_at or datetime.utcnow()
    seg.ended_at = end_time
    db.commit()
    return SegmentEndResponse(segment_id=seg.id, ended_at=end_time)

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    return 6371000 * c

@router.get("/{track_id}/segments", response_model=List[SegmentItem])
async def list_segments(
    track_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if not can_read_track(track, current_user):
        raise HTTPException(status_code=403, detail="Access denied")

    segments = db.query(TrackSegmentModel).filter(TrackSegmentModel.track_id == track_id).order_by(TrackSegmentModel.started_at.asc()).all()
    items: List[SegmentItem] = []
    for seg in segments:
        seg_points = db.query(Waypoint).filter(Waypoint.track_id == track_id, Waypoint.segment_id == seg.id, Waypoint.waypoint_type == "gps_track").order_by(Waypoint.timestamp.asc()).all()
        points_count = len(seg_points)
        distance = 0.0
        for i in range(1, len(seg_points)):
            a = seg_points[i-1]
            b = seg_points[i]
            distance += _haversine(a.latitude, a.longitude, b.latitude, b.longitude)
        items.append(SegmentItem(id=seg.id, started_at=seg.started_at, ended_at=seg.ended_at, points=points_count, distance_m=distance, route_id=getattr(seg, 'route_id', None)))
    return items

@router.get("/{track_id}/segments/{segment_id}/geometry", response_model=SegmentGeometryResponse)
async def get_segment_geometry(
    track_id: int,
    segment_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if not can_read_track(track, current_user):
        raise HTTPException(status_code=403, detail="Access denied")
    seg = db.query(TrackSegmentModel).filter(TrackSegmentModel.id == segment_id, TrackSegmentModel.track_id == track_id).first()
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")
    seg_points = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.segment_id == segment_id,
        Waypoint.waypoint_type == "gps_track"
    ).order_by(Waypoint.timestamp.asc()).all()
    coords: list[tuple[float, float]] = [(p.longitude, p.latitude) for p in seg_points]
    along: list[float] = []
    total = 0.0
    for i, p in enumerate(seg_points):
        if i == 0:
            along.append(0.0)
        else:
            prev = seg_points[i-1]
            total += _haversine(prev.latitude, prev.longitude, p.latitude, p.longitude)
            along.append(total)
    return SegmentGeometryResponse(segment_id=segment_id, coordinates=coords, along_meters=along)

