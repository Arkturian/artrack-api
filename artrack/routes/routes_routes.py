from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from typing import List, Optional, Tuple
from datetime import datetime
import math

from ..database import get_db
from ..auth import get_current_user
from ..models import Track, TrackRoute as TrackRouteModel, Waypoint
from pydantic import BaseModel
from .track_report_generator import generate_track_report

router = APIRouter()

# Helper function to calculate distance
def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two GPS coordinates in meters"""
    R = 6371000  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    return R * c


def _closest_point_on_polyline(poly: List[Tuple[float, float]], lat: float, lon: float) -> Tuple[float, float, float, float]:
    """
    Calculate closest point on polyline to given coordinate.
    Returns (distance_meters, along_meters, snap_lat, snap_lon).

    KORREKTE IMPLEMENTIERUNG - Single Source of Truth:

    1. Sammle alle orthogonalen Projektionen auf Segmente (NUR wo 0 ≤ t ≤ 1)
    2. Sammle alle direkten Distanzen zu GPS-Punkten (Vektoren)
    3. Minimum von allen = Ergebnis
    """
    if len(poly) < 2:
        return (float('inf'), 0.0, lat, lon)

    # Convert lat/lon to approximate meters (good enough for small areas)
    # Using equirectangular approximation
    ref_lat = poly[0][0]
    meters_per_deg_lat = 111320.0
    meters_per_deg_lon = 111320.0 * math.cos(math.radians(ref_lat))

    def to_meters(lat_a, lon_a):
        """Convert lat/lon to meters relative to first point"""
        return (
            (lat_a - poly[0][0]) * meters_per_deg_lat,
            (lon_a - poly[0][1]) * meters_per_deg_lon
        )

    # Point P in meters
    px, py = to_meters(lat, lon)

    # Collect all candidate distances
    candidates = []

    total_along = 0.0

    # 1. Check orthogonal projections on segments
    for i in range(len(poly) - 1):
        # Segment endpoints A and B
        seg_start_lat, seg_start_lon = poly[i]
        seg_end_lat, seg_end_lon = poly[i + 1]

        # Convert to meters
        ax, ay = to_meters(seg_start_lat, seg_start_lon)
        bx, by = to_meters(seg_end_lat, seg_end_lon)

        # Vector AB
        abx = bx - ax
        aby = by - ay

        # Vector AP
        apx = px - ax
        apy = py - ay

        # Segment length squared
        ab_len_sq = abx * abx + aby * aby

        if ab_len_sq >= 0.01:  # Ignore very short segments (< 10cm)
            # Projection parameter t = dot(AP, AB) / dot(AB, AB)
            t = (apx * abx + apy * aby) / ab_len_sq

            # ONLY use projection if WITHIN segment (0 ≤ t ≤ 1)
            if 0 <= t <= 1:
                # Projected point Q = A + t * AB
                qx = ax + t * abx
                qy = ay + t * aby

                # Distance from P to Q (orthogonal distance)
                pqx = px - qx
                pqy = py - qy
                dist = math.sqrt(pqx * pqx + pqy * pqy)

                proj_along = total_along + t * math.sqrt(ab_len_sq)

                # Convert projected point back to lat/lon
                snap_x = qx + poly[0][0] * meters_per_deg_lat / meters_per_deg_lat
                snap_y = qy + poly[0][1] * meters_per_deg_lon / meters_per_deg_lon
                snap_lat = poly[0][0] + qx / meters_per_deg_lat
                snap_lon = poly[0][1] + qy / meters_per_deg_lon

                candidates.append((dist, proj_along, snap_lat, snap_lon))

        # Accumulate distance along polyline
        total_along += math.sqrt(ab_len_sq)

    # 2. Check direct distances to all GPS points (Vektoren)
    total_along = 0.0
    for i, (point_lat, point_lon) in enumerate(poly):
        vx, vy = to_meters(point_lat, point_lon)

        # Direct distance from P to this GPS point
        dx = px - vx
        dy = py - vy
        dist = math.sqrt(dx * dx + dy * dy)

        # Snap position is the GPS point itself
        candidates.append((dist, total_along, point_lat, point_lon))

        # Accumulate for next point
        if i < len(poly) - 1:
            next_lat, next_lon = poly[i + 1]
            nx, ny = to_meters(next_lat, next_lon)
            seg_len = math.sqrt((nx - vx)**2 + (ny - vy)**2)
            total_along += seg_len

    # 3. Find minimum
    if not candidates:
        return (float('inf'), 0.0, lat, lon)

    min_dist, best_along, snap_lat, snap_lon = min(candidates, key=lambda x: x[0])
    return (min_dist, best_along, snap_lat, snap_lon)


def _waypoint_belongs_to_route(wp: Waypoint, route_id: int, track_id: int, db: Session, track: Track, max_snap_distance: float = 50.0) -> bool:
    """
    RUNTIME evaluation: Does this waypoint belong to this route?

    Priority:
    1. Manual override: Check segment.routeId (for segment markers)
    2. Manual override: Check fixedRoutes (for POIs)
    3. Auto-snap: Calculate distance to route polyline using SNAP algorithm

    Single Source of Truth: Uses same snap algorithm as snap_routes.py
    """
    meta = wp.metadata_json or {}

    # Priority 1: segment.routeId (old manual assignment for segment markers)
    if meta.get("segment"):
        seg_route_id = meta.get("segment", {}).get("routeId")
        if seg_route_id == route_id:
            return True

    # Priority 2: Manual override via fixedRoutes
    fixed_routes = meta.get("fixedRoutes", [])
    if fixed_routes:
        return f"route_{route_id}" in fixed_routes

    # Priority 3: Runtime SNAP calculation
    # Check distance to ALL routes and assign to CLOSEST one only
    # (Multi-route only if snap positions are very close together, like shared paths)

    # Get all routes for this track
    all_routes = db.query(TrackRouteModel).filter(TrackRouteModel.track_id == track_id).all()

    if not all_routes:
        return False

    # Get all GPS points once
    all_gps = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type == "gps_track",
        Waypoint.created_by == track.created_by
    ).order_by(Waypoint.timestamp.asc()).all()

    # Calculate distance to each route
    route_distances = []

    for r in all_routes:
        # Filter GPS points for this route
        gps_points = []
        for p in all_gps:
            p_route_id = getattr(p, 'route_id', None)
            if p_route_id == r.id:
                gps_points.append(p)
            elif p.metadata_json:
                meta_route = p.metadata_json.get('route_id')
                if meta_route == r.id:
                    gps_points.append(p)

        if len(gps_points) >= 2:
            polyline = [(p.latitude, p.longitude) for p in gps_points]
            dist_meters, along_meters, snap_lat, snap_lon = _closest_point_on_polyline(polyline, wp.latitude, wp.longitude)

            if dist_meters <= max_snap_distance:
                route_distances.append({
                    'route_id': r.id,
                    'distance': dist_meters,
                    'along_meters': along_meters,
                    'snap_lat': snap_lat,
                    'snap_lon': snap_lon
                })

    if not route_distances:
        # No route within threshold
        return False

    # Sort by distance (closest first)
    route_distances.sort(key=lambda x: x['distance'])

    # Check if this is the closest route
    closest_route_id = route_distances[0]['route_id']
    closest_snap_lat = route_distances[0]['snap_lat']
    closest_snap_lon = route_distances[0]['snap_lon']

    # Multi-route detection: Only if distances from POI to routes are equal
    # If POI is 0.5m from Route 1 and 0.5m from Route 2 → routes overlap → POI on both
    if len(route_distances) > 1:
        # Check if other routes have similar distance from POI (< 2m difference)
        closest_distance = route_distances[0]['distance']

        for rd in route_distances[1:]:
            dist_diff = abs(rd['distance'] - closest_distance)

            if dist_diff < 2.0:
                # POI is equally close to both routes → shared path
                # Return true if our route_id is one of them
                if rd['route_id'] == route_id or closest_route_id == route_id:
                    return True

    # Otherwise: waypoint only belongs to the single closest route
    return closest_route_id == route_id

class RouteCreate(BaseModel):
    name: str
    color: Optional[str] = None
    description: Optional[str] = None
    storage_object_ids: Optional[list[int]] = None
    storage_collection: Optional[dict] = None  # { name: string, owner_email: string }

class RouteUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None
    storage_object_ids: Optional[list[int]] = None
    storage_collection: Optional[dict] = None

class RouteItem(BaseModel):
    id: int
    track_id: int
    name: str
    color: Optional[str]
    description: Optional[str] = None
    storage_object_ids: Optional[list[int]] = None
    storage_collection: Optional[dict] = None
    created_at: datetime

# Detailed route data response
class WaypointPoint(BaseModel):
    latitude: float
    longitude: float
    altitude: Optional[float] = None
    recorded_at: datetime
    along_meters: float  # Cumulative distance along route

class SegmentDetail(BaseModel):
    id: int
    name: Optional[str]
    started_at: datetime
    ended_at: Optional[datetime]
    waypoints: List[WaypointPoint]
    distance_meters: float
    metadata_json: Optional[dict] = None

class RouteDetail(BaseModel):
    id: int
    track_id: int
    name: str
    color: Optional[str]
    description: Optional[str]
    storage_object_ids: Optional[list[int]]
    storage_collection: Optional[dict]
    created_at: datetime
    segments: List[SegmentDetail]
    total_distance_meters: float
    total_waypoints: int

# Simplified route overview response
class WaypointOverview(BaseModel):
    id: int
    waypoint_type: str
    latitude: float
    longitude: float
    user_description: Optional[str]
    metadata_json: Optional[dict]

class SegmentOverview(BaseModel):
    name: str
    start_waypoint: WaypointOverview
    end_waypoint: WaypointOverview

class RouteOverview(BaseModel):
    id: int
    track_id: int
    name: str
    color: Optional[str]
    description: Optional[str]
    segments: List[SegmentOverview]
    waypoints: List[WaypointOverview]  # All waypoints (POIs) for this route
    total_segments: int
    total_waypoints: int

@router.get("/{track_id}/routes", response_model=List[RouteItem])
async def list_routes(track_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    routes = db.query(TrackRouteModel).filter(TrackRouteModel.track_id == track_id).order_by(TrackRouteModel.id.asc()).all()
    return [RouteItem(id=r.id, track_id=r.track_id, name=r.name, color=r.color, description=r.description, storage_object_ids=getattr(r, 'storage_object_ids', None), storage_collection=getattr(r, 'storage_collection', None), created_at=r.created_at) for r in routes]

@router.get("/{track_id}/routes/{route_id}/overview", response_model=RouteOverview)
async def get_route_overview(
    track_id: int,
    route_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Get route overview with all waypoints (POIs) and segments.

    Returns:
    - Route metadata
    - All segments (start/end waypoint pairs)
    - All POIs/waypoints assigned to this route
    """
    # Verify track access
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Get route
    route = db.query(TrackRouteModel).filter(
        TrackRouteModel.id == route_id,
        TrackRouteModel.track_id == track_id
    ).first()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    # Load all waypoints (we'll filter GPS track points later)
    # Note: Can't use waypoint_type != "gps_track" because NULL != 'gps_track' returns NULL in SQL
    all_waypoints = db.query(Waypoint).filter(
        Waypoint.track_id == track_id
    ).all()

    # Filter out GPS track points in Python
    all_waypoints = [wp for wp in all_waypoints if wp.waypoint_type != "gps_track"]

    # Filter segment markers for this route
    # Priority: 1) segment.routeId (manual), 2) fixedRoutes (manual), 3) RUNTIME SNAP
    segment_waypoints = []
    for wp in all_waypoints:
        if not (wp.metadata_json and wp.metadata_json.get("segment")):
            continue

        # Check if segment belongs to route (same logic as POIs)
        if _waypoint_belongs_to_route(wp, route_id, track_id, db, track):
            segment_waypoints.append(wp)

    # Group segments by name
    segments_by_name = {}
    for wp in segment_waypoints:
        segment_meta = wp.metadata_json.get("segment", {})
        seg_name = segment_meta.get("name")
        role = segment_meta.get("role")

        if not seg_name or not role:
            continue

        if seg_name not in segments_by_name:
            segments_by_name[seg_name] = {}

        segments_by_name[seg_name][role] = wp

    # Build segment overview
    segments_overview = []
    for seg_name, wps in sorted(segments_by_name.items()):
        if "start" not in wps or "end" not in wps:
            continue

        start_wp = wps["start"]
        end_wp = wps["end"]

        segments_overview.append(SegmentOverview(
            name=seg_name,
            start_waypoint=WaypointOverview(
                id=start_wp.id,
                waypoint_type=start_wp.waypoint_type,
                latitude=start_wp.latitude,
                longitude=start_wp.longitude,
                user_description=start_wp.user_description,
                metadata_json=start_wp.metadata_json
            ),
            end_waypoint=WaypointOverview(
                id=end_wp.id,
                waypoint_type=end_wp.waypoint_type,
                latitude=end_wp.latitude,
                longitude=end_wp.longitude,
                user_description=end_wp.user_description,
                metadata_json=end_wp.metadata_json
            )
        ))

    # Filter POIs/waypoints for this route
    # Priority: 1) fixedRoutes (manual override), 2) RUNTIME SNAP calculation
    route_waypoints = []
    for wp in all_waypoints:
        # Skip segment markers
        if wp.metadata_json and wp.metadata_json.get("segment"):
            continue

        # Check if belongs to route (runtime evaluation)
        if _waypoint_belongs_to_route(wp, route_id, track_id, db, track):
            route_waypoints.append(WaypointOverview(
                id=wp.id,
                waypoint_type=wp.waypoint_type,
                latitude=wp.latitude,
                longitude=wp.longitude,
                user_description=wp.user_description,
                metadata_json=wp.metadata_json
            ))

    return RouteOverview(
        id=route.id,
        track_id=route.track_id,
        name=route.name,
        color=route.color,
        description=route.description,
        segments=segments_overview,
        waypoints=route_waypoints,
        total_segments=len(segments_overview),
        total_waypoints=len(route_waypoints)
    )

@router.get("/{track_id}/routes/{route_id}/detail", response_model=RouteDetail)
async def get_route_detail(
    track_id: int,
    route_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Get complete route data including all segments and waypoints.

    Returns:
    - Route metadata (name, color, description, storage objects)
    - All segments belonging to this route
    - All GPS waypoints for each segment (ordered by timestamp)
    - Distance calculations
    """
    # Verify track access
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Get route
    route = db.query(TrackRouteModel).filter(
        TrackRouteModel.id == route_id,
        TrackRouteModel.track_id == track_id
    ).first()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    # NEW SEGMENT SYSTEM: Load segments from waypoint pairs
    # Segments are defined by two waypoints (start/end) with metadata_json.segment = {name, role, routeId}

    segment_details = []
    total_distance = 0.0
    total_waypoints = 0
    cumulative_distance = 0.0

    # Load all non-GPS waypoints to find segment markers
    all_waypoints = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type != "gps_track"
    ).all()

    # Filter segment markers (waypoints with metadata_json.segment)
    segment_waypoints = [
        wp for wp in all_waypoints
        if wp.metadata_json and wp.metadata_json.get("segment")
    ]

    # Filter segment waypoints for this route
    # Priority: 1) segment.routeId (manual assignment), 2) fixedRoutes (auto-snap)
    route_segment_wps = [
        wp for wp in segment_waypoints
        if (wp.metadata_json.get("segment", {}).get("routeId") == route_id
            or f"route_{route_id}" in wp.metadata_json.get("fixedRoutes", []))
    ]

    # Group by segment name
    segments_by_name = {}
    for wp in route_segment_wps:
        segment_meta = wp.metadata_json.get("segment", {})
        seg_name = segment_meta.get("name")
        role = segment_meta.get("role")

        if not seg_name or not role:
            continue

        if seg_name not in segments_by_name:
            segments_by_name[seg_name] = {}

        segments_by_name[seg_name][role] = wp

    # Check if route has segments defined
    if segments_by_name:
        # Route has segments - load GPS waypoints for the route
        all_route_waypoints = db.query(Waypoint).filter(
            Waypoint.track_id == track_id,
            Waypoint.route_id == route_id,
            Waypoint.waypoint_type == "gps_track"
        ).order_by(Waypoint.recorded_at.asc()).all()

        # Build segments from waypoint pairs
        for seg_name, wps in sorted(segments_by_name.items()):
            if "start" not in wps or "end" not in wps:
                continue

            start_wp = wps["start"]
            end_wp = wps["end"]

            # Find waypoints between start and end
            # Simple approach: use timestamps
            segment_waypoints = [
                wp for wp in all_route_waypoints
                if start_wp.recorded_at <= wp.recorded_at <= end_wp.recorded_at
            ]

            waypoint_points = []
            segment_distance = 0.0

            for i, wp in enumerate(segment_waypoints):
                if i > 0:
                    prev = segment_waypoints[i - 1]
                    dist = _haversine(prev.latitude, prev.longitude, wp.latitude, wp.longitude)
                    segment_distance += dist
                    cumulative_distance += dist

                waypoint_points.append(WaypointPoint(
                    latitude=wp.latitude,
                    longitude=wp.longitude,
                    altitude=wp.altitude,
                    recorded_at=wp.recorded_at,
                    along_meters=cumulative_distance
                ))

            total_distance += segment_distance
            total_waypoints += len(segment_waypoints)

            segment_details.append(SegmentDetail(
                id=hash(seg_name) % 10000,  # Virtual ID based on name
                name=seg_name,
                started_at=start_wp.recorded_at,
                ended_at=end_wp.recorded_at,
                waypoints=waypoint_points,
                distance_meters=segment_distance,
                metadata_json={"start_waypoint_id": start_wp.id, "end_waypoint_id": end_wp.id}
            ))
    else:
        # No segments - load all waypoints directly assigned to this route
        waypoints = db.query(Waypoint).filter(
            Waypoint.track_id == track_id,
            Waypoint.route_id == route_id,
            Waypoint.waypoint_type == "gps_track"
        ).order_by(Waypoint.recorded_at.asc()).all()

        if waypoints:
            # Create a virtual segment containing all route waypoints
            waypoint_points = []
            route_distance = 0.0

            for i, wp in enumerate(waypoints):
                if i > 0:
                    prev = waypoints[i - 1]
                    dist = _haversine(prev.latitude, prev.longitude, wp.latitude, wp.longitude)
                    route_distance += dist
                    cumulative_distance += dist

                waypoint_points.append(WaypointPoint(
                    latitude=wp.latitude,
                    longitude=wp.longitude,
                    altitude=wp.altitude,
                    recorded_at=wp.recorded_at,
                    along_meters=cumulative_distance
                ))

            total_distance = route_distance
            total_waypoints = len(waypoints)

            # Create virtual segment with all route waypoints
            segment_details.append(SegmentDetail(
                id=0,  # Virtual segment ID
                name=f"{route.name} (full route)",
                started_at=waypoints[0].recorded_at,
                ended_at=waypoints[-1].recorded_at,
                waypoints=waypoint_points,
                distance_meters=route_distance,
                metadata_json={"virtual": True, "note": "Route has no segments, all waypoints grouped together"}
            ))

    return RouteDetail(
        id=route.id,
        track_id=route.track_id,
        name=route.name,
        color=route.color,
        description=route.description,
        storage_object_ids=getattr(route, 'storage_object_ids', None),
        storage_collection=getattr(route, 'storage_collection', None),
        created_at=route.created_at,
        segments=segment_details,
        total_distance_meters=total_distance,
        total_waypoints=total_waypoints
    )

@router.post("/{track_id}/routes", response_model=RouteItem)
async def create_route(track_id: int, body: RouteCreate, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    r = TrackRouteModel(track_id=track_id, created_by=current_user.id, name=body.name, color=body.color, description=body.description, storage_object_ids=body.storage_object_ids or [], storage_collection=body.storage_collection or {})
    db.add(r)
    db.commit()
    db.refresh(r)
    return RouteItem(id=r.id, track_id=r.track_id, name=r.name, color=r.color, description=r.description, storage_object_ids=getattr(r, 'storage_object_ids', None), storage_collection=getattr(r, 'storage_collection', None), created_at=r.created_at)

@router.patch("/{track_id}/routes/{route_id}", response_model=RouteItem)
async def update_route(track_id: int, route_id: int, body: RouteUpdate, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    r = db.query(TrackRouteModel).filter(TrackRouteModel.id == route_id, TrackRouteModel.track_id == track_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Route not found")
    if body.name is not None:
        r.name = body.name
    if body.color is not None:
        r.color = body.color
    if body.description is not None:
        r.description = body.description
    if body.storage_object_ids is not None:
        r.storage_object_ids = body.storage_object_ids
    if body.storage_collection is not None:
        # minimal validation
        try:
            sc = body.storage_collection or {}
            name = (sc.get('name') or '').strip()
            owner = (sc.get('owner_email') or '').strip()
            r.storage_collection = {'name': name, 'owner_email': owner}
        except Exception:
            r.storage_collection = body.storage_collection
    db.commit()
    db.refresh(r)
    return RouteItem(id=r.id, track_id=r.track_id, name=r.name, color=r.color, description=r.description, storage_object_ids=getattr(r, 'storage_object_ids', None), storage_collection=getattr(r, 'storage_collection', None), created_at=r.created_at)

@router.delete("/{track_id}/routes/{route_id}", response_model=dict)
async def delete_route(track_id: int, route_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    r = db.query(TrackRouteModel).filter(TrackRouteModel.id == route_id, TrackRouteModel.track_id == track_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Route not found")
    # Also delete GPS points belonging to this route to avoid orphan gps_track dots
    deleted_count = 0
    try:
        points = db.query(Waypoint).filter(
            Waypoint.track_id == track_id,
            Waypoint.waypoint_type == "gps_track",
            Waypoint.created_by == track.created_by
        ).all()
        for p in points:
            meta = p.metadata_json or {}
            pr = getattr(p, 'route_id', None)
            mr = meta.get('route_id') if isinstance(meta, dict) else None
            if pr == route_id or mr == route_id:
                db.delete(p)
                deleted_count += 1
        # Finally delete the route record
        db.delete(r)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"deleted": True, "gps_points_deleted": deleted_count}


@router.get("/{track_id}/structure-report")
async def get_track_structure_report(
    track_id: int,
    full: bool = False,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Get a text-based structure report for a track showing routes, segments, POIs, and overlaps.

    Query parameters:
    - full: If true, includes descriptions for track, routes, segments, and POIs
    """
    # Check track permissions
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Generate report using shared logic
    report_text = generate_track_report(
        track_id=track_id,
        show_descriptions=full,
        api_key="Inetpass1",
        base_url="https://api.arkturian.com/artrack"
    )

    return PlainTextResponse(report_text)


@router.get("/{track_id}/routes-overview")
async def get_track_routes_overview(
    track_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Get complete routes overview with POI assignments for a track.

    Returns JSON with all routes and their assigned POIs based on runtime snap algorithm.
    This is the single source of truth for POI-to-route assignments.
    """
    # Check track permissions
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Get all routes for this track
    routes = db.query(TrackRouteModel).filter(TrackRouteModel.track_id == track_id).all()

    routes_data = []

    for route in routes:
        route_id = route.id

        # Get GPS points count
        gps_count = db.query(Waypoint).filter(
            Waypoint.track_id == track_id,
            Waypoint.route_id == route_id,
            Waypoint.waypoint_type == "gps_track"
        ).count()

        # Get route overview (internally uses the same logic as /overview endpoint)
        all_waypoints = db.query(Waypoint).filter(
            Waypoint.track_id == track_id
        ).all()

        # Filter out GPS track points
        all_waypoints = [wp for wp in all_waypoints if wp.waypoint_type != "gps_track"]

        # Get POIs for this route (using _waypoint_belongs_to_route logic)
        route_pois = []
        for wp in all_waypoints:
            if wp.metadata_json and wp.metadata_json.get("segment"):
                continue  # Skip segment markers
            if _waypoint_belongs_to_route(wp, route_id, track_id, db, track):
                # Get GPS points for distance calculation
                gps_points = db.query(Waypoint).filter(
                    Waypoint.track_id == track_id,
                    Waypoint.route_id == route_id,
                    Waypoint.waypoint_type == "gps_track"
                ).order_by(Waypoint.timestamp.asc()).all()

                polyline = [(p.latitude, p.longitude) for p in gps_points]
                along_meters = 0

                if len(polyline) >= 2:
                    dist, along_meters, _, _ = _closest_point_on_polyline(
                        polyline, wp.latitude, wp.longitude
                    )

                meta = wp.metadata_json or {}
                route_pois.append({
                    "id": wp.id,
                    "title": meta.get("title", "N/A"),
                    "latitude": wp.latitude,
                    "longitude": wp.longitude,
                    "distance_along_route_m": round(along_meters, 1),
                    "user_description": wp.user_description,
                    "metadata": meta
                })

        # Sort POIs by distance along route
        route_pois.sort(key=lambda x: x["distance_along_route_m"])

        # Get segments count
        segment_waypoints = [wp for wp in all_waypoints
                           if wp.metadata_json and wp.metadata_json.get("segment")
                           and _waypoint_belongs_to_route(wp, route_id, track_id, db, track)]

        segments_by_name = {}
        for wp in segment_waypoints:
            segment_meta = wp.metadata_json.get("segment", {})
            seg_name = segment_meta.get("name")
            role = segment_meta.get("role")
            if seg_name and role:
                if seg_name not in segments_by_name:
                    segments_by_name[seg_name] = {}
                segments_by_name[seg_name][role] = wp

        complete_segments = sum(1 for wps in segments_by_name.values()
                              if "start" in wps and "end" in wps)

        routes_data.append({
            "route_id": route_id,
            "route_name": route.name,
            "route_description": route.description,
            "route_color": route.color,
            "gps_points_count": gps_count,
            "segments_count": complete_segments,
            "pois_count": len(route_pois),
            "pois": route_pois
        })

    return {
        "track_id": track_id,
        "track_name": track.name,
        "track_description": track.description,
        "routes_count": len(routes),
        "routes": routes_data
    }


class GenerateIntroRequest(BaseModel):
    dry_run: bool = False
    custom_text: Optional[str] = None

@router.post("/{track_id}/routes/{route_id}/generate-intro")
async def generate_route_intro(
    track_id: int,
    route_id: int,
    body: GenerateIntroRequest = GenerateIntroRequest(),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Generate intro audio for a route.
    
    Uses the track's guide config to generate a welcome/intro audio that plays when
    the user selects this route in the app.
    """
    # Imports inside function to avoid module-level crashes
    try:
        import httpx
        from datetime import datetime
        import os
        import traceback
        import json
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Configuration Error: Missing required module. {str(e)}")

    # Wrap entire logic in try-except
    try:
        # Check track permissions
        track = db.query(Track).filter(Track.id == track_id).first()
        if not track:
            raise HTTPException(status_code=404, detail="Track not found")
        if track.created_by != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied")

        # Get route
        route = db.query(TrackRouteModel).filter(
            TrackRouteModel.id == route_id,
            TrackRouteModel.track_id == track_id
        ).first()
        if not route:
            raise HTTPException(status_code=404, detail="Route not found")

        # Get guide config from metadata_json
        metadata = track.metadata_json or {}
        guide_config = metadata.get('guide', metadata.get('guide_config', {}))

        # AI Service Configuration
        AI_SERVICE_URL = "https://api-ai.arkturian.com"
        AI_API_KEY = "Inetpass1" # Using internal key
        HEADERS = {
            "X-API-KEY": AI_API_KEY,
            "Content-Type": "application/json"
        }

        # Generate intro text
        if body.custom_text:
            intro_text = body.custom_text
        else:
            # Generate intro text using AI Service (ChatGPT)
            prompt = f"""Du bist ein Wanderführer. Generiere eine kurze, freundliche Begrüßung (max 30 Sekunden Sprechzeit) für die Route "{route.name}".

Track: {track.name}
Route: {route.name}
{f'Beschreibung: {route.description}' if route.description else ''}

Die Begrüßung soll:
- Den Wanderer willkommen heißen
- Die Route kurz vorstellen
- Motivation geben
- Freundlich und einladend sein

Sprich direkt den Wanderer an. Keine Metainformationen, nur den gesprochenen Text."""

            async with httpx.AsyncClient(timeout=60.0) as client:
                # Use prompt field as required by api-ai Prompt model
                chat_payload = {
                    "prompt": prompt,
                    "system": "Du bist ein erfahrener, freundlicher Wanderführer. Antworte immer auf Deutsch."
                }
                
                response = await client.post(
                    f"{AI_SERVICE_URL}/ai/chatgpt?model=gpt-4",
                    json=chat_payload,
                    headers=HEADERS
                )
                
                if response.status_code != 200:
                    raise Exception(f"AI Text Generation failed: {response.text}")
                
                ai_data = response.json()
                # Handle flexible response formats from AI service
                if 'response' in ai_data:
                    intro_text = ai_data['response']
                elif 'message' in ai_data:
                    intro_text = ai_data['message']
                elif 'choices' in ai_data and len(ai_data['choices']) > 0:
                    intro_text = ai_data['choices'][0]['message']['content']
                else:
                    intro_text = str(ai_data) # Fallback

        if body.dry_run:
            return {
                "route_id": route_id,
                "route_name": route.name,
                "intro_text": intro_text,
                "audio_url": None
            }

        # Generate audio using AI Service (Generate Speech)
        voice = guide_config.get('voice', {}).get('style', 'nova')
        if voice == 'warm_guide':
            voice = 'nova'
            
        speed = guide_config.get('voice', {}).get('speed', 0.9)
        
        user_email = getattr(current_user, 'email', 'system@arkturian.com')

        speech_payload = {
            "id": f"route-intro-{route_id}-{int(datetime.now().timestamp())}",
            "timestamp": datetime.now().isoformat(), # Required field
            "collection_id": "route_intros",         # Required at root level
            "content": {
                "text": intro_text,
                "language": guide_config.get('language', 'de-DE'),
                "voice": voice,
                "speed": speed
            },
            "config": {
                "provider": "openai", 
                "output_format": "mp3"
            },
            "save_options": {
                "is_public": True,
                "owner_email": user_email
            }
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{AI_SERVICE_URL}/ai/generate_speech",
                json=speech_payload,
                headers=HEADERS
            )
            
            if response.status_code != 200:
                error_detail = response.text
                # Try to parse detail from JSON
                try:
                    error_json = response.json()
                    if 'detail' in error_json:
                        error_detail = error_json['detail']
                except:
                    pass
                raise Exception(f"AI Audio Generation failed: {error_detail}")
            
            result = response.json()
            
            if not result.get('file_url'):
                raise Exception("AI Service did not return a file URL")
                
            audio_url = result['file_url']

        # Store intro_audio_url in route metadata
        route.intro_audio_url = audio_url
        db.commit()

        return {
            "route_id": route_id,
            "route_name": route.name,
            "intro_text": intro_text,
            "audio_url": audio_url
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Audio Generation Error: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Process Error: {str(e)}")


# ============================================================================
# PRETTY ENDPOINTS - Human-readable overview without technical details
# ============================================================================

class PrettyPOI(BaseModel):
    id: int
    name: Optional[str]
    description: Optional[str]
    type: Optional[str]
    km: float  # Position along route in kilometers

class PrettySegment(BaseModel):
    id: int
    name: Optional[str]
    description: Optional[str]
    start_km: float
    end_km: float
    length_km: float

class PrettyRoute(BaseModel):
    id: int
    name: str
    description: Optional[str]
    color: Optional[str]
    total_km: float
    poi_count: int
    segment_count: int

class PrettyTrackOverview(BaseModel):
    track_id: int
    track_name: str
    track_description: Optional[str]
    routes: List[PrettyRoute]

class PrettyRouteDetail(BaseModel):
    route_id: int
    route_name: str
    route_description: Optional[str]
    total_km: float
    pois: List[PrettyPOI]
    segments: List[PrettySegment]


@router.get("/{track_id}/pois-pretty")
async def get_all_pois_pretty(
    track_id: int,
    route_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Human-readable list of all POIs for a track.

    Optional filter by route_id.
    Returns: id, name, description, type, km position, route assignment.
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Get all routes for distance calculations
    routes = db.query(TrackRouteModel).filter(TrackRouteModel.track_id == track_id).all()

    # Build polylines per route
    route_polylines = {}
    route_names = {}
    for route in routes:
        gps_points = db.query(Waypoint).filter(
            Waypoint.track_id == track_id,
            Waypoint.route_id == route.id,
            Waypoint.waypoint_type == "gps_track"
        ).order_by(Waypoint.recorded_at.asc()).all()
        route_polylines[route.id] = [(p.latitude, p.longitude) for p in gps_points]
        route_names[route.id] = route.name

    # Get all non-GPS, non-segment waypoints
    all_waypoints = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type != "gps_track"
    ).all()

    pois = []
    for wp in all_waypoints:
        meta = wp.metadata_json or {}

        # Skip segment markers
        if meta.get("segment"):
            continue

        # Find which route this POI belongs to
        assigned_route_id = None
        assigned_route_name = None
        km_position = 0.0

        for r_id, polyline in route_polylines.items():
            if len(polyline) >= 2 and _waypoint_belongs_to_route(wp, r_id, track_id, db, track):
                assigned_route_id = r_id
                assigned_route_name = route_names.get(r_id)
                _, along_meters, _, _ = _closest_point_on_polyline(polyline, wp.latitude, wp.longitude)
                km_position = along_meters / 1000
                break

        # Filter by route if specified
        if route_id is not None and assigned_route_id != route_id:
            continue

        poi_name = meta.get("title") or wp.user_description or f"POI {wp.id}"
        pois.append({
            "id": wp.id,
            "name": poi_name[:100] if poi_name else None,
            "description": wp.user_description[:300] if wp.user_description else None,
            "type": wp.waypoint_type,
            "km": round(km_position, 2),
            "route_id": assigned_route_id,
            "route_name": assigned_route_name
        })

    # Sort by route, then by km
    pois.sort(key=lambda x: (x["route_id"] or 0, x["km"]))

    return {
        "track_id": track_id,
        "track_name": track.name,
        "total_pois": len(pois),
        "pois": pois
    }


@router.get("/{track_id}/pois-near")
async def get_pois_near(
    track_id: int,
    lat: float,
    lng: float,
    radius_m: int = 200,
    limit: int = 10,
    route_id: Optional[int] = None,
    direction: Optional[str] = None,
    include_text: bool = True,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    GPS-based POI lookup with optional route-snapping.

    Returns POIs near the given GPS position, sorted by distance.
    Includes route snapping (snapped position + along_meters) and rich
    metadata (knowledge text, audio_id, illustration_id).

    Designed for AI audio guides that need fast, low-cost POI lookups
    without burning tokens on filtering.

    Parameters:
    - lat, lng: User's current GPS position
    - radius_m: Max distance in meters (default 200)
    - limit: Max number of POIs to return (default 10)
    - route_id: Optional - constrain to a specific route
    - direction: Optional - "ahead" (only POIs ahead on route) | "behind" |
      None (all directions). Requires route_id to work.
    - include_text: Include knowledge text (approaching/at_poi) in response
                    (default true; set false for smaller payload)

    Returns:
    {
      track_id, query: {lat, lng, radius_m},
      snap: {snapped_lat, snapped_lng, along_meters, route_id, lateral_offset_m} | null,
      pois: [{id, name, type, lat, lng, distance_m, along_meters, ahead,
              category, route_id, knowledge: {approaching, at_poi}, audio_id,
              illustration_id}]
    }
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # 1. Build polylines per route (for snapping)
    routes = db.query(TrackRouteModel).filter(TrackRouteModel.track_id == track_id).all()
    route_polylines = {}
    for route in routes:
        gps_points = db.query(Waypoint).filter(
            Waypoint.track_id == track_id,
            Waypoint.route_id == route.id,
            Waypoint.waypoint_type == "gps_track"
        ).order_by(Waypoint.recorded_at.asc()).all()
        if len(gps_points) >= 2:
            route_polylines[route.id] = [(p.latitude, p.longitude) for p in gps_points]

    # 2. Snap user position to closest route (or constrained route)
    user_snap = None
    user_along = None
    user_route_id = route_id
    snap_candidates = []
    for r_id, polyline in route_polylines.items():
        if route_id is not None and r_id != route_id:
            continue
        dist, along, snap_lat, snap_lon = _closest_point_on_polyline(polyline, lat, lng)
        snap_candidates.append((r_id, dist, along, snap_lat, snap_lon))

    if snap_candidates:
        # Pick closest snap
        snap_candidates.sort(key=lambda x: x[1])
        best = snap_candidates[0]
        # Cutoff: if user is more than 400m off any route, don't snap (user is not on the track)
        if best[1] <= 400:
            user_route_id = best[0]
            user_snap = {
                "snapped_lat": best[3],
                "snapped_lng": best[4],
                "along_meters": round(best[2], 1),
                "route_id": best[0],
                "lateral_offset_m": round(best[1], 1),
            }
            user_along = best[2]
        else:
            # User is too far from any route — return null snap
            user_route_id = None
            user_along = None

    # 3. Get all POIs (manual waypoints + story_points etc., not gps_track)
    all_waypoints = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type != "gps_track"
    ).all()

    # 4. For each POI: calculate distance, snap to route, filter
    pois_with_distance = []
    for wp in all_waypoints:
        meta = wp.metadata_json or {}

        # Skip segment markers
        if meta.get("segment"):
            continue

        # Direct GPS distance from user to POI
        dist_to_user = _haversine(lat, lng, wp.latitude, wp.longitude)
        if dist_to_user > radius_m:
            continue

        # Find POI's along_meters on the user's route (for direction filter)
        poi_along = None
        if user_route_id and user_route_id in route_polylines:
            polyline = route_polylines[user_route_id]
            _, poi_along_calc, _, _ = _closest_point_on_polyline(polyline, wp.latitude, wp.longitude)
            poi_along = poi_along_calc

        # Direction filter (requires user_along + poi_along)
        ahead = None
        if poi_along is not None and user_along is not None:
            ahead = poi_along >= user_along
            if direction == "ahead" and not ahead:
                continue
            if direction == "behind" and ahead:
                continue

        # Build POI dict
        poi_name = meta.get("title") or wp.user_description or f"POI {wp.id}"
        category = meta.get("category")
        knowledge = meta.get("knowledge") or {}

        poi_data = {
            "id": wp.id,
            "name": poi_name[:100] if poi_name else None,
            "type": wp.waypoint_type,
            "lat": wp.latitude,
            "lng": wp.longitude,
            "distance_m": round(dist_to_user, 1),
            "along_meters": round(poi_along, 1) if poi_along is not None else None,
            "ahead": ahead,
            "category": category,
            "subcategory": meta.get("subcategory"),
            "route_id": user_route_id,
        }

        if include_text:
            # Knowledge texts (approaching + at_poi)
            approaching = knowledge.get("approaching", {})
            at_poi = knowledge.get("at_poi", {})
            poi_data["knowledge"] = {
                "approaching": approaching.get("text") if isinstance(approaching, dict) else None,
                "at_poi": at_poi.get("text") if isinstance(at_poi, dict) else None,
            }
            poi_data["radius_m"] = meta.get("radiusMeters")

            # Stories at this POI (knowledge.story.{story_id} from POIs,
            # OR direct fields on story_point waypoints)
            stories = []
            story_dict = knowledge.get("story") if isinstance(knowledge, dict) else None
            if isinstance(story_dict, dict):
                for story_id, scene_data in story_dict.items():
                    if isinstance(scene_data, dict):
                        stories.append({
                            "story_id": story_id,
                            "scene": scene_data.get("scene"),
                            "role": scene_data.get("role"),
                            "trigger": scene_data.get("trigger"),
                            "trigger_radius": scene_data.get("trigger_radius"),
                            "character": scene_data.get("character"),
                            "text": scene_data.get("text"),
                        })
            # story_point waypoints have story info as direct metadata fields
            if wp.waypoint_type == "story_point" and meta.get("story_id"):
                stories.append({
                    "story_id": meta.get("story_id"),
                    "scene": meta.get("scene_order"),
                    "role": "story_point",
                    "character": meta.get("character"),
                    "along_meters_planned": meta.get("along_meters"),
                    "text": (knowledge.get("at_poi") or {}).get("text") if isinstance(knowledge, dict) else None,
                })
            poi_data["stories"] = stories if stories else None

            # Long-form description from waypoint
            if wp.user_description:
                poi_data["description"] = wp.user_description[:2000]

            # Audio + illustration IDs (from assets array if available)
            assets = meta.get("assets") or []
            audio_id = None
            illustration_id = None
            icon_id = None
            for a in assets:
                if isinstance(a, dict):
                    role = a.get("role")
                    aid = a.get("id")
                    if role == "audio" and not audio_id:
                        audio_id = aid
                    elif role == "illustration" and not illustration_id:
                        illustration_id = aid
                    elif role == "icon" and not icon_id:
                        icon_id = aid
            poi_data["audio_id"] = audio_id
            poi_data["illustration_id"] = illustration_id
            poi_data["icon_id"] = icon_id

        pois_with_distance.append(poi_data)

    # 5. Sort by distance, limit
    pois_with_distance.sort(key=lambda p: p["distance_m"])
    pois_with_distance = pois_with_distance[:limit]

    return {
        "track_id": track_id,
        "track_name": track.name,
        "query": {
            "lat": lat,
            "lng": lng,
            "radius_m": radius_m,
            "limit": limit,
            "route_id": route_id,
            "direction": direction,
        },
        "snap": user_snap,
        "total_found": len(pois_with_distance),
        "pois": pois_with_distance,
    }


@router.get("/{track_id}/segments-pretty")
async def get_all_segments_pretty(
    track_id: int,
    route_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Human-readable list of all segments for a track.

    Optional filter by route_id.
    Returns: id, name, description, start_km, end_km, length_km, route assignment.
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Get all routes for distance calculations
    routes = db.query(TrackRouteModel).filter(TrackRouteModel.track_id == track_id).all()

    # Build polylines per route
    route_polylines = {}
    route_names = {}
    for route in routes:
        gps_points = db.query(Waypoint).filter(
            Waypoint.track_id == track_id,
            Waypoint.route_id == route.id,
            Waypoint.waypoint_type == "gps_track"
        ).order_by(Waypoint.recorded_at.asc()).all()
        route_polylines[route.id] = [(p.latitude, p.longitude) for p in gps_points]
        route_names[route.id] = route.name

    # Get all segment marker waypoints
    all_waypoints = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type != "gps_track"
    ).all()

    # Group segment markers by name and route
    segments_data = {}  # key: (route_id, segment_name)

    for wp in all_waypoints:
        meta = wp.metadata_json or {}
        seg_info = meta.get("segment")

        if not seg_info:
            continue

        seg_name = seg_info.get("name", "Unnamed")
        role = seg_info.get("role")

        if not role:
            continue

        # Find which route this segment belongs to
        assigned_route_id = None
        for r_id, polyline in route_polylines.items():
            if len(polyline) >= 2 and _waypoint_belongs_to_route(wp, r_id, track_id, db, track):
                assigned_route_id = r_id
                break

        # Filter by route if specified
        if route_id is not None and assigned_route_id != route_id:
            continue

        key = (assigned_route_id, seg_name)
        if key not in segments_data:
            segments_data[key] = {
                "route_id": assigned_route_id,
                "route_name": route_names.get(assigned_route_id),
                "name": seg_name,
                "description": seg_info.get("description"),
                "start_id": None,
                "end_id": None,
                "start_km": None,
                "end_km": None
            }

        # Calculate km position
        km_position = 0.0
        if assigned_route_id and assigned_route_id in route_polylines:
            polyline = route_polylines[assigned_route_id]
            if len(polyline) >= 2:
                _, along_meters, _, _ = _closest_point_on_polyline(polyline, wp.latitude, wp.longitude)
                km_position = along_meters / 1000

        if role == "start":
            segments_data[key]["start_id"] = wp.id
            segments_data[key]["start_km"] = round(km_position, 2)
        elif role == "end":
            segments_data[key]["end_id"] = wp.id
            segments_data[key]["end_km"] = round(km_position, 2)

    # Build final segments list
    segments = []
    for key, data in segments_data.items():
        start_km = data["start_km"] or 0
        end_km = data["end_km"] or 0
        segments.append({
            "id": data["start_id"] or data["end_id"],
            "name": data["name"],
            "description": data["description"],
            "start_km": start_km,
            "end_km": end_km,
            "length_km": round(abs(end_km - start_km), 2),
            "route_id": data["route_id"],
            "route_name": data["route_name"],
            "complete": data["start_id"] is not None and data["end_id"] is not None
        })

    # Sort by route, then by start_km
    segments.sort(key=lambda x: (x["route_id"] or 0, x["start_km"]))

    return {
        "track_id": track_id,
        "track_name": track.name,
        "total_segments": len(segments),
        "segments": segments
    }


@router.get("/{track_id}/routes-ids")
async def get_route_ids(
    track_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Get list of route IDs for a track.

    Simple endpoint that returns just the route IDs - useful for iteration.
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    routes = db.query(TrackRouteModel).filter(TrackRouteModel.track_id == track_id).order_by(TrackRouteModel.id.asc()).all()

    return {
        "track_id": track_id,
        "route_ids": [r.id for r in routes]
    }


@router.get("/{track_id}/pretty")
async def get_track_pretty(
    track_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Human-readable track overview.

    Returns a clean summary of all routes with their total length,
    POI count, and segment count - without coordinates or technical IDs.
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    routes = db.query(TrackRouteModel).filter(TrackRouteModel.track_id == track_id).order_by(TrackRouteModel.id.asc()).all()

    pretty_routes = []

    for route in routes:
        # Calculate total distance
        gps_points = db.query(Waypoint).filter(
            Waypoint.track_id == track_id,
            Waypoint.route_id == route.id,
            Waypoint.waypoint_type == "gps_track"
        ).order_by(Waypoint.recorded_at.asc()).all()

        total_distance = 0.0
        for i in range(1, len(gps_points)):
            total_distance += _haversine(
                gps_points[i-1].latitude, gps_points[i-1].longitude,
                gps_points[i].latitude, gps_points[i].longitude
            )

        # Count POIs (non-GPS, non-segment waypoints)
        all_waypoints = db.query(Waypoint).filter(
            Waypoint.track_id == track_id,
            Waypoint.waypoint_type != "gps_track"
        ).all()

        poi_count = 0
        segment_names = set()

        for wp in all_waypoints:
            if _waypoint_belongs_to_route(wp, route.id, track_id, db, track):
                meta = wp.metadata_json or {}
                if meta.get("segment"):
                    seg_name = meta.get("segment", {}).get("name")
                    if seg_name:
                        segment_names.add(seg_name)
                else:
                    poi_count += 1

        pretty_routes.append(PrettyRoute(
            id=route.id,
            name=route.name,
            description=route.description,
            color=route.color,
            total_km=round(total_distance / 1000, 2),
            poi_count=poi_count,
            segment_count=len(segment_names)
        ))

    return PrettyTrackOverview(
        track_id=track.id,
        track_name=track.name,
        track_description=track.description,
        routes=pretty_routes
    )


@router.get("/{track_id}/routes/{route_id}/pretty")
async def get_route_pretty(
    track_id: int,
    route_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Human-readable route detail.

    Returns:
    - Route info with total length in km
    - POIs with name, description, type, and position (km)
    - Segments with name, description, start/end km, and length

    No coordinates, no technical metadata - just the essentials.
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    route = db.query(TrackRouteModel).filter(
        TrackRouteModel.id == route_id,
        TrackRouteModel.track_id == track_id
    ).first()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    # Get GPS points for distance calculations
    gps_points = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.route_id == route_id,
        Waypoint.waypoint_type == "gps_track"
    ).order_by(Waypoint.recorded_at.asc()).all()

    # Build polyline for snap calculations
    polyline = [(p.latitude, p.longitude) for p in gps_points]

    # Calculate total distance
    total_distance = 0.0
    for i in range(1, len(gps_points)):
        total_distance += _haversine(
            gps_points[i-1].latitude, gps_points[i-1].longitude,
            gps_points[i].latitude, gps_points[i].longitude
        )

    # Get all non-GPS waypoints
    all_waypoints = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type != "gps_track"
    ).all()

    pretty_pois = []
    segments_by_name = {}

    for wp in all_waypoints:
        if not _waypoint_belongs_to_route(wp, route_id, track_id, db, track):
            continue

        meta = wp.metadata_json or {}

        # Calculate position along route
        along_meters = 0.0
        if len(polyline) >= 2:
            _, along_meters, _, _ = _closest_point_on_polyline(polyline, wp.latitude, wp.longitude)

        if meta.get("segment"):
            # Segment marker
            seg_info = meta.get("segment", {})
            seg_name = seg_info.get("name", "Unnamed")
            role = seg_info.get("role")

            if seg_name not in segments_by_name:
                segments_by_name[seg_name] = {"id": wp.id, "name": seg_name, "description": seg_info.get("description")}

            if role == "start":
                segments_by_name[seg_name]["start_km"] = along_meters / 1000
                segments_by_name[seg_name]["start_id"] = wp.id
            elif role == "end":
                segments_by_name[seg_name]["end_km"] = along_meters / 1000
                segments_by_name[seg_name]["end_id"] = wp.id
        else:
            # Regular POI
            poi_name = meta.get("title") or wp.user_description or f"POI {wp.id}"
            pretty_pois.append(PrettyPOI(
                id=wp.id,
                name=poi_name[:50] if poi_name else None,
                description=wp.user_description[:200] if wp.user_description else None,
                type=wp.waypoint_type,
                km=round(along_meters / 1000, 2)
            ))

    # Sort POIs by km
    pretty_pois.sort(key=lambda x: x.km)

    # Build segments list
    pretty_segments = []
    for seg_name, seg_data in segments_by_name.items():
        start_km = seg_data.get("start_km", 0)
        end_km = seg_data.get("end_km", 0)
        pretty_segments.append(PrettySegment(
            id=seg_data.get("start_id", 0),
            name=seg_name,
            description=seg_data.get("description"),
            start_km=round(start_km, 2),
            end_km=round(end_km, 2),
            length_km=round(abs(end_km - start_km), 2)
        ))

    # Sort segments by start_km
    pretty_segments.sort(key=lambda x: x.start_km)

    return PrettyRouteDetail(
        route_id=route.id,
        route_name=route.name,
        route_description=route.description,
        total_km=round(total_distance / 1000, 2),
        pois=pretty_pois,
        segments=pretty_segments
    )


class NarrativeCaptureRequest(BaseModel):
    element_id: str  # Expects "wp_123" or just "123"
    text: str
    overwrite: bool = True

@router.post("/{track_id}/narratives/capture")
async def capture_narrative(
    track_id: int,
    body: NarrativeCaptureRequest,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Capture generated narrative from App Simulation and save to Waypoint.
    
    This enables 'Recorder Mode': App generates text -> Server saves it -> Dashboard shows it.
    """
    # Check track permissions
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Parse ID (strip "wp_" prefix if present)
    # Also handle pure segment IDs if possible, but for now rely on Waypoint IDs
    wp_id_str = body.element_id.replace("wp_", "")
    
    # Safety check: ensure it's a number
    if not wp_id_str.isdigit():
        raise HTTPException(status_code=400, detail=f"Invalid element_id format: {body.element_id}. Expected numeric ID or 'wp_ID'.")
    
    waypoint_id = int(wp_id_str)

    # Find Waypoint (POI or Segment Marker)
    waypoint = db.query(Waypoint).filter(
        Waypoint.id == waypoint_id,
        Waypoint.track_id == track_id
    ).first()

    if not waypoint:
        raise HTTPException(status_code=404, detail=f"Waypoint {waypoint_id} not found in track {track_id}")

    # Update description
    if body.overwrite or not waypoint.user_description:
        waypoint.user_description = body.text
        db.commit()
        return {"success": True, "id": waypoint.id, "updated": True, "text_preview": body.text[:50]}
    
    return {"success": True, "id": waypoint.id, "updated": False, "reason": "overwrite_disabled"}

