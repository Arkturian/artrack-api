"""
GPS Real-time Tracking Routes
Handles real-time GPS point ingestion and batch uploads
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
import os
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
import uuid
import json

from ..database import get_db
from ..auth import get_current_user
from ..models import User, Track, Waypoint, TrackRoute as TrackRouteModel
from ..collaboration_models import get_user_permissions

router = APIRouter()

# === Request/Response Models ===

from pydantic import BaseModel, Field
from datetime import datetime

class GPSPointCreate(BaseModel):
    trackId: int = Field(..., alias="trackId")
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    altitude: Optional[float] = None
    accuracy: float = Field(..., gt=0)
    timestamp: datetime
    speed: Optional[float] = Field(None, ge=0)
    course: Optional[float] = Field(None, ge=0, le=360)
    isFromKalmanFilter: bool = Field(False, alias="isFromKalmanFilter")
    segmentId: Optional[int] = Field(None, alias="segmentId")
    routeId: Optional[int] = Field(None, alias="routeId")

    connectionQuality: Optional[str] = Field(None, alias="connectionQuality")
    
    class Config:
        validate_by_name = True

class GPSPointBatchCreate(BaseModel):
    trackId: int = Field(..., alias="trackId")
    points: List[GPSPointCreate]
    batchId: str = Field(..., alias="batchId")
    
    class Config:
        validate_by_name = True

class GPSPointResponse(BaseModel):
    id: int
    trackId: int = Field(..., alias="trackId")
    latitude: float
    longitude: float
    altitude: Optional[float]
    accuracy: float
    timestamp: datetime
    speed: Optional[float]
    course: Optional[float]
    isFromKalmanFilter: bool = Field(..., alias="isFromKalmanFilter")
    createdAt: datetime = Field(..., alias="createdAt")
    segmentId: Optional[int] = Field(None, alias="segmentId")
    routeId: Optional[int] = Field(None, alias="routeId")
    
    class Config:
        validate_by_name = True
        from_attributes = True

class GPSPointBatchResponse(BaseModel):
    batchId: str = Field(..., alias="batchId")
    trackId: int = Field(..., alias="trackId")
    pointsCreated: int = Field(..., alias="pointsCreated")
    pointsSkipped: int = Field(..., alias="pointsSkipped")
    processingTimeMs: float = Field(..., alias="processingTimeMs")
    
    class Config:
        validate_by_name = True

class GPSTrackStatsResponse(BaseModel):
    trackId: int = Field(..., alias="trackId")
    totalPoints: int = Field(..., alias="totalPoints")
    totalDistance: float = Field(..., alias="totalDistance")  # in meters
    trackDuration: float = Field(..., alias="trackDuration")  # in seconds
    averageSpeed: float = Field(..., alias="averageSpeed")  # m/s
    maxSpeed: Optional[float] = Field(..., alias="maxSpeed")  # m/s
    minAltitude: Optional[float] = Field(..., alias="minAltitude")
    maxAltitude: Optional[float] = Field(..., alias="maxAltitude")
    startTime: Optional[datetime] = Field(..., alias="startTime")
    endTime: Optional[datetime] = Field(..., alias="endTime")
    
    class Config:
        validate_by_name = True

# === Configuration ===
MAX_BATCH_POINTS = int(os.getenv("ARTRACK_GPS_MAX_BATCH", "1000"))

# === Core GPS Endpoints ===

@router.post("/{track_id}/gps-points", response_model=GPSPointResponse)
async def create_gps_point(
    track_id: int,
    gps_point: GPSPointCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a single GPS point for real-time tracking"""
    
    # Verify track exists and permissions
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    
    # Check permissions (track owner or collaborator with waypoint permissions)
    permissions = get_user_permissions(track, current_user.id)
    if not (track.created_by == current_user.id or permissions.can_add_waypoints):
        raise HTTPException(status_code=403, detail="No permission to add GPS points to this track")
    
    # For collaborative tracks, only owner can add GPS track points
    if track.is_collaborative and track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Only track owner can add GPS track points")
    
    # Create GPS waypoint (stored as special waypoint type)
    waypoint = Waypoint(
        track_id=track_id,
        created_by=current_user.id,
        latitude=gps_point.latitude,
        longitude=gps_point.longitude,
        altitude=gps_point.altitude,
        accuracy=gps_point.accuracy,
        recorded_at=gps_point.timestamp,
        timestamp=gps_point.timestamp,
        waypoint_type="gps_track",  # Special type for GPS track points
        is_public=False,
        segment_id=gps_point.segmentId,
        route_id=gps_point.routeId,
        metadata_json={
            "speed": gps_point.speed,
            "course": gps_point.course,
            "isFromKalmanFilter": gps_point.isFromKalmanFilter,
            "realtime_sync": True,
            "connection_quality": gps_point.connectionQuality,
            "route_id": gps_point.routeId
        }
    )
    
    db.add(waypoint)
    db.commit()
    db.refresh(waypoint)

    # Refresh derived bounding-circle (broad-phase index for /tracks/nearby).
    try:
        from ..services.track_bbox import recompute_bbox
        recompute_bbox(db, track_id)
    except Exception:
        pass  # never block the GPS write on bbox maintenance

    response = GPSPointResponse(
        id=waypoint.id,
        trackId=waypoint.track_id,
        latitude=waypoint.latitude,
        longitude=waypoint.longitude,
        altitude=waypoint.altitude,
        accuracy=waypoint.accuracy,
        timestamp=waypoint.timestamp,
        speed=gps_point.speed,
        course=gps_point.course,
        isFromKalmanFilter=gps_point.isFromKalmanFilter,
        createdAt=waypoint.created_at
    )

    return response

@router.post("/{track_id}/gps-points/batch", response_model=GPSPointBatchResponse)
async def create_gps_point_batch(
    track_id: int,
    batch: GPSPointBatchCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create multiple GPS points in a batch for efficient sync"""
    
    start_time = datetime.utcnow()
    
    # Verify track exists and permissions
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    
    # Check permissions
    permissions = get_user_permissions(track, current_user.id)
    if not (track.created_by == current_user.id or permissions.can_add_waypoints):
        raise HTTPException(status_code=403, detail="No permission to add GPS points to this track")
    
    # For collaborative tracks, only owner can add GPS track points
    if track.is_collaborative and track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Only track owner can add GPS track points")
    
    # Validate batch size (configurable)
    if len(batch.points) > MAX_BATCH_POINTS:
        raise HTTPException(status_code=400, detail=f"Batch size cannot exceed {MAX_BATCH_POINTS} points")
    
    if len(batch.points) == 0:
        raise HTTPException(status_code=400, detail="Batch cannot be empty")
    
    points_created = 0
    points_skipped = 0
    
    # Check for duplicate timestamps to avoid duplicates
    existing_timestamps = set()
    recent_points = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type == "gps_track",
        Waypoint.created_by == current_user.id
    ).order_by(Waypoint.timestamp.desc()).limit(200).all()
    
    for point in recent_points:
        existing_timestamps.add(point.timestamp)
    
    # Process batch points
    waypoints_to_add = []
    
    for gps_point in batch.points:
        # Skip duplicates
        if gps_point.timestamp in existing_timestamps:
            points_skipped += 1
            continue
        
        # Create waypoint
        waypoint = Waypoint(
            track_id=track_id,
            created_by=current_user.id,
            latitude=gps_point.latitude,
            longitude=gps_point.longitude,
            altitude=gps_point.altitude,
            accuracy=gps_point.accuracy,
            recorded_at=gps_point.timestamp,
            timestamp=gps_point.timestamp,
            waypoint_type="gps_track",
            is_public=False,
            segment_id=gps_point.segmentId,
            route_id=gps_point.routeId,
            metadata_json={
                "speed": gps_point.speed,
                "course": gps_point.course,
                "isFromKalmanFilter": gps_point.isFromKalmanFilter,
                "realtime_sync": True,
                "batch_id": batch.batchId,
                "connection_quality": gps_point.connectionQuality,
                "route_id": gps_point.routeId
            }
        )
        
        waypoints_to_add.append(waypoint)
        existing_timestamps.add(gps_point.timestamp)
        points_created += 1
    
    # Insert batched waypoints (safer than bulk_save_objects for relationships)
    if waypoints_to_add:
        try:
            db.add_all(waypoints_to_add)
            db.commit()
        except Exception as e:
            db.rollback()
            print(f"GPS batch insert failed for track {track_id}: {e}")
            raise HTTPException(status_code=500, detail="Batch insert failed")

    # Calculate processing time
    processing_time = (datetime.utcnow() - start_time).total_seconds() * 1000

    # Schedule background track statistics update
    background_tasks.add_task(update_track_statistics, track_id, db)

    # Refresh derived bounding-circle once per batch (not per point) so
    # /tracks/nearby stays in sync without exploding insert cost.
    if waypoints_to_add:
        try:
            from ..services.track_bbox import recompute_bbox
            recompute_bbox(db, track_id)
        except Exception:
            pass
    
    response = GPSPointBatchResponse(
        batchId=batch.batchId,
        trackId=track_id,
        pointsCreated=points_created,
        pointsSkipped=points_skipped,
        processingTimeMs=processing_time
    )
    
    print(f"📡 GPS batch processed: {points_created} created, {points_skipped} skipped, {processing_time:.1f}ms")
    
    return response

@router.get("/{track_id}/gps-points", response_model=List[GPSPointResponse])
async def get_gps_points(
    track_id: int,
    limit: int = 1000,
    offset: int = 0,
    segment_id: Optional[int] = None,
    route_id: Optional[int] = Query(None, alias="route_id"),
    routeId: Optional[int] = Query(None, alias="routeId"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get GPS track points for a track"""
    
    # Verify track exists and permissions
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    
    # Check permissions
    permissions = get_user_permissions(track, current_user.id)
    if not permissions.can_view:
        raise HTTPException(status_code=403, detail="No permission to view this track")
    
    # Get GPS track points (only from track owner)
    query = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type == "gps_track",
        Waypoint.created_by == track.created_by
    )
    if segment_id is not None:
        query = query.filter(Waypoint.segment_id == segment_id)

    # Apply route filter (supports both route_id and routeId)
    effective_route_id = route_id if route_id is not None else routeId
    if effective_route_id is not None:
        # Fetch all candidates then filter in Python for backward compatibility (metadata_json.route_id)
        all_points = query.order_by(Waypoint.timestamp.asc()).all()
        filtered = []
        for point in all_points:
            meta = point.metadata_json or {}
            point_route_id = getattr(point, 'route_id', None)
            meta_route_id = meta.get('route_id') if isinstance(meta, dict) else None
            if point_route_id == effective_route_id or meta_route_id == effective_route_id:
                filtered.append(point)
        # Apply offset/limit after filtering
        start = max(offset, 0)
        end = start + max(limit, 0)
        gps_points = filtered[start:end]
    else:
        gps_points = query.order_by(Waypoint.timestamp.asc()).offset(offset).limit(limit).all()
    
    responses = []
    for point in gps_points:
        metadata = point.metadata_json or {}
        responses.append(GPSPointResponse(
            id=point.id,
            trackId=point.track_id,
            latitude=point.latitude,
            longitude=point.longitude,
            altitude=point.altitude,
            accuracy=point.accuracy if point.accuracy is not None else 0.0,
            timestamp=point.timestamp or point.recorded_at or point.created_at,
            speed=metadata.get("speed"),
            course=metadata.get("course"),
            isFromKalmanFilter=metadata.get("isFromKalmanFilter", False),
            createdAt=point.created_at,
            segmentId=point.segment_id,
            routeId=(point.metadata_json or {}).get("route_id") or getattr(point, 'route_id', None)
        ))
    
    return responses

@router.get("/{track_id}/gps-stats", response_model=GPSTrackStatsResponse)
async def get_gps_track_statistics(
    track_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get comprehensive GPS track statistics"""
    
    # Verify track exists and permissions
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    
    # Check permissions
    permissions = get_user_permissions(track, current_user.id)
    if not permissions.can_view:
        raise HTTPException(status_code=403, detail="No permission to view this track")
    
    # Get all GPS track points
    gps_points = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type == "gps_track",
        Waypoint.created_by == track.created_by
    ).order_by(Waypoint.timestamp.asc()).all()
    
    if not gps_points:
        return GPSTrackStatsResponse(
            trackId=track_id,
            totalPoints=0,
            totalDistance=0.0,
            trackDuration=0.0,
            averageSpeed=0.0,
            maxSpeed=None,
            minAltitude=None,
            maxAltitude=None,
            startTime=None,
            endTime=None
        )
    
    # Calculate statistics
    total_points = len(gps_points)
    start_time = gps_points[0].timestamp
    end_time = gps_points[-1].timestamp
    track_duration = (end_time - start_time).total_seconds()
    
    # Calculate total distance using Haversine formula
    total_distance = 0.0
    speeds = []
    altitudes = []
    
    for i in range(1, len(gps_points)):
        prev_point = gps_points[i-1]
        curr_point = gps_points[i]
        
        # Haversine distance calculation
        distance = calculate_haversine_distance(
            prev_point.latitude, prev_point.longitude,
            curr_point.latitude, curr_point.longitude
        )
        total_distance += distance
        
        # Extract speed from metadata
        metadata = curr_point.metadata_json or {}
        if metadata.get("speed") is not None:
            speeds.append(metadata["speed"])
        
        # Collect altitudes
        if curr_point.altitude is not None:
            altitudes.append(curr_point.altitude)
    
    # Calculate speed statistics
    average_speed = total_distance / track_duration if track_duration > 0 else 0.0
    max_speed = max(speeds) if speeds else None
    
    # Altitude statistics
    min_altitude = min(altitudes) if altitudes else None
    max_altitude = max(altitudes) if altitudes else None
    
    return GPSTrackStatsResponse(
        trackId=track_id,
        totalPoints=total_points,
        totalDistance=total_distance,
        trackDuration=track_duration,
        averageSpeed=average_speed,
        maxSpeed=max_speed,
        minAltitude=min_altitude,
        maxAltitude=max_altitude,
        startTime=start_time,
        endTime=end_time
    )

@router.delete("/{track_id}/gps-points", response_model=dict)
async def clear_gps_points(
    track_id: int,
    route_id: Optional[int] = None,
    routeId: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Clear all GPS points for a track (owner only)"""
    
    # Verify track exists and ownership
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Only track owner can clear GPS points")
    
    effective_route_id = route_id if route_id is not None else routeId
    base_q = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type == "gps_track",
        Waypoint.created_by == current_user.id
    )
    if effective_route_id is not None:
        # Prefer direct column, fall back to metadata_json.route_id filter in Python
        deleted_count = 0
        points = base_q.all()
        for p in points:
            meta = p.metadata_json or {}
            pr = getattr(p, 'route_id', None)
            mr = meta.get('route_id') if isinstance(meta, dict) else None
            if pr == effective_route_id or mr == effective_route_id:
                db.delete(p)
                deleted_count += 1
        db.commit()
    else:
        # Delete all GPS track points
        deleted_count = base_q.delete()
        db.commit()

    # Refresh derived bounding-circle after deletion. If the track is now
    # empty, recompute_bbox clears the three fields back to NULL.
    if deleted_count:
        try:
            from ..services.track_bbox import recompute_bbox
            recompute_bbox(db, track_id)
        except Exception:
            pass

    return {
        "message": f"Cleared {deleted_count} GPS points from track {track_id}",
        "deleted_count": deleted_count
    }

# --- Maintenance: Cleanup orphan gps_track points (no or missing route) ---

class GPSCleanupResult(BaseModel):
    deleted_orphans: int
    normalized: int

@router.post("/{track_id}/gps-cleanup-orphans", response_model=GPSCleanupResult)
async def cleanup_orphan_gps_points(
    track_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    # Only owner can perform destructive cleanup
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Only track owner can cleanup GPS points")

    # Build set of valid route ids for this track
    route_rows = db.query(TrackRouteModel).filter(TrackRouteModel.track_id == track_id).all()
    valid_route_ids = set(r.id for r in route_rows)

    # Load gps_track waypoints for this track
    points = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type == "gps_track",
        Waypoint.created_by == track.created_by
    ).all()

    deleted = 0
    normalized = 0
    try:
        for p in points:
            meta = p.metadata_json or {}
            pr = getattr(p, 'route_id', None)
            mr = meta.get('route_id') if isinstance(meta, dict) else None
            rid = pr if pr is not None else mr
            # Delete if no route, or route no longer exists
            if rid is None or (isinstance(rid, int) and rid not in valid_route_ids):
                db.delete(p)
                deleted += 1
                continue
            # Normalize: ensure both column and metadata reflect the same valid route id
            if pr != rid:
                p.route_id = rid
                normalized += 1
            if isinstance(meta, dict) and meta.get('route_id') != rid:
                meta['route_id'] = rid
                p.metadata_json = meta
                if pr == rid:
                    normalized += 1
        db.commit()
    except Exception:
        db.rollback()
        raise

    # Refresh derived bounding-circle if cleanup actually deleted anything.
    if deleted:
        try:
            from ..services.track_bbox import recompute_bbox
            recompute_bbox(db, track_id)
        except Exception:
            pass

    return GPSCleanupResult(deleted_orphans=deleted, normalized=normalized)

# === Route utilities: fuse, split, copy ===

class RouteFuseRequest(BaseModel):
    routeA: int
    routeB: int
    name: Optional[str] = None

@router.post("/{track_id}/routes/fuse", response_model=dict)
async def fuse_routes(track_id: int, body: RouteFuseRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    pts = db.query(Waypoint).filter(Waypoint.track_id==track_id, Waypoint.waypoint_type=="gps_track", Waypoint.created_by==track.created_by).order_by(Waypoint.timestamp.asc()).all()
    coordsA=[(p.longitude,p.latitude) for p in pts if (getattr(p,'route_id',None) or (p.metadata_json or {}).get('route_id'))==body.routeA]
    coordsB=[(p.longitude,p.latitude) for p in pts if (getattr(p,'route_id',None) or (p.metadata_json or {}).get('route_id'))==body.routeB]
    if len(coordsA)<1 or len(coordsB)<1:
        raise HTTPException(status_code=400, detail="Both routes must have points")
    fused = coordsA + coordsB
    # clear A and B
    for p in pts:
        rid = getattr(p,'route_id',None) or (p.metadata_json or {}).get('route_id')
        if rid in (body.routeA, body.routeB):
            db.delete(p)
    db.commit()
    # reinsert into routeA
    from datetime import timedelta
    base_ts = datetime.utcnow()
    to_add=[]
    for i,(lon,lat) in enumerate(fused):
        w=Waypoint(track_id=track_id, created_by=track.created_by, latitude=lat, longitude=lon, altitude=None, accuracy=5.0, recorded_at=base_ts+timedelta(seconds=i), timestamp=base_ts+timedelta(seconds=i), waypoint_type="gps_track", route_id=body.routeA, metadata_json={"route_id":body.routeA})
        to_add.append(w)
    if to_add:
        db.add_all(to_add)
        db.commit()
    # If a new name is provided, rename routeA here for consistency
    if body.name is not None and isinstance(body.name, str) and body.name.strip():
        r = db.query(TrackRouteModel).filter(TrackRouteModel.id == body.routeA, TrackRouteModel.track_id == track_id).first()
        if r:
            r.name = body.name.strip()
            db.commit()
    return {"message":"fused","routeId": body.routeA, "points": len(fused), "nameUpdated": bool(body.name and body.name.strip())}

class RouteSplitRequest(BaseModel):
    routeId: int
    index: int
    nameA: Optional[str] = None
    nameB: Optional[str] = None

@router.post("/{track_id}/routes/split", response_model=dict)
async def split_route(track_id: int, body: RouteSplitRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    pts = db.query(Waypoint).filter(Waypoint.track_id==track_id, Waypoint.waypoint_type=="gps_track", Waypoint.created_by==track.created_by).order_by(Waypoint.timestamp.asc()).all()
    coords=[(p.id,p.longitude,p.latitude) for p in pts if (getattr(p,'route_id',None) or (p.metadata_json or {}).get('route_id'))==body.routeId]
    if len(coords)<2 or body.index<=0 or body.index>=len(coords)-1:
        raise HTTPException(status_code=400, detail="Invalid split index")
    a=coords[:body.index+1]
    b=coords[body.index+1:]
    # clear original
    for pid,_,_ in coords:
        db.query(Waypoint).filter(Waypoint.id==pid).delete()
    db.commit()
    # reinsert A into same route
    from datetime import timedelta
    base_ts = datetime.utcnow()
    for i,(_,lon,lat) in enumerate(a):
        w=Waypoint(track_id=track_id, created_by=track.created_by, latitude=lat, longitude=lon, altitude=None, accuracy=5.0, recorded_at=base_ts+timedelta(seconds=i), timestamp=base_ts+timedelta(seconds=i), waypoint_type="gps_track", route_id=body.routeId, metadata_json={"route_id":body.routeId})
        db.add(w)
    db.commit()
    # create new route by copying routeId meta into new id via client can create one; here we return coords for client to create
    # Include the split vertex as start of the second route to keep continuity
    split_lon = coords[body.index][1]
    split_lat = coords[body.index][2]
    coordsB = [(split_lon, split_lat)] + [(lon,lat) for _,lon,lat in b]
    return {"message":"split","routeA": body.routeId, "pointsA": len(a), "pointsB": len(coordsB), "newRouteNeeded": True, "coordsB": coordsB}

class RouteCopyRequest(BaseModel):
    routeId: int
    newName: Optional[str] = None

@router.post("/{track_id}/routes/copy", response_model=dict)
async def copy_route(track_id: int, body: RouteCopyRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    pts = db.query(Waypoint).filter(Waypoint.track_id==track_id, Waypoint.waypoint_type=="gps_track", Waypoint.created_by==track.created_by).order_by(Waypoint.timestamp.asc()).all()
    coords=[(p.longitude,p.latitude) for p in pts if (getattr(p,'route_id',None) or (p.metadata_json or {}).get('route_id'))==body.routeId]
    if len(coords)<2:
        raise HTTPException(status_code=400, detail="Route has not enough points")
    # client should have a create route endpoint; here we just return coords to be saved via existing batch
    return {"message":"copy","coords": coords}

# === Helper Functions ===

def calculate_haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great circle distance between two points on Earth in meters"""
    import math
    
    # Convert latitude and longitude from degrees to radians
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    
    # Haversine formula
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    
    # Radius of earth in meters
    r = 6371000
    
    return c * r

def update_track_statistics(track_id: int, db: Session):
    """Background task to update track statistics after GPS batch upload"""
    try:
        # This could calculate and cache track statistics
        # For now, just log that we would update stats
        print(f"📊 Background task: Update statistics for track {track_id}")
        
        # Could store in track.metadata:
        # - Total distance
        # - Average speed
        # - Elevation gain/loss
        # - Duration
        # - Last updated timestamp
        
    except Exception as e:
        print(f"⚠️ Failed to update track statistics: {e}")

# === Real-time Streaming (WebSocket support) ===

@router.websocket("/{track_id}/gps-stream")
async def gps_realtime_stream(websocket, track_id: int):
    """WebSocket endpoint for real-time GPS streaming (future enhancement)"""
    # This would enable real-time GPS streaming to multiple clients
    # For now, return not implemented
    await websocket.accept()
    await websocket.send_text("GPS streaming not yet implemented")
    await websocket.close()

# === Batch Processing Status ===

@router.get("/batch-status/{batch_id}", response_model=dict)
async def get_batch_processing_status(
    batch_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get the processing status of a GPS point batch"""
    
    # Find waypoints with this batch_id
    # SQLite JSON query portability: filter via LIKE on stored JSON
    batch_points = db.query(Waypoint).filter(
        Waypoint.waypoint_type == "gps_track",
        Waypoint.metadata_json.is_not(None)
    ).all()
    batch_points = [p for p in batch_points if (p.metadata_json or {}).get("batch_id") == batch_id]
    
    if not batch_points:
        raise HTTPException(status_code=404, detail="Batch not found")
    
    # Check if user has access to this batch (via track permissions)
    track_id = batch_points[0].track_id
    track = db.query(Track).filter(Track.id == track_id).first()
    
    permissions = get_user_permissions(track, current_user.id)
    if not permissions.can_view:
        raise HTTPException(status_code=403, detail="No permission to view this batch")
    
    return {
        "batch_id": batch_id,
        "track_id": track_id,
        "points_processed": len(batch_points),
        "status": "completed",
        "created_at": batch_points[0].created_at,
        "processing_complete": True
    }