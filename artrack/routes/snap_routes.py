from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List, Tuple
from sqlalchemy.orm import Session
from math import cos, radians, sqrt, asin, sin
from datetime import datetime

from ..database import get_db
from ..auth import get_current_user
from ..models import User, Track, Waypoint
from ..collaboration_models import get_user_permissions

from ..services.route_status import guide_routes
router = APIRouter()


class SnapRequest(BaseModel):
    lat: float
    lon: float
    routeId: Optional[int] = Field(None, description="Optional Route/Segment group id to constrain snapping; if omitted, uses whole track or auto-route.")
    maxSnapDistance: Optional[float] = Field(None, description="Maximum allowed snap distance in meters; if exceeded, no snap.")


class SnapResult(BaseModel):
    snappedLat: float
    snappedLon: float
    distanceMeters: float
    alongMeters: float
    lateralOffsetMeters: float
    routeId: Optional[int] = Field(None, description="Route/segment id that was used for snapping (auto-selected if request omitted it)")


def _meters_per_degree(lat_deg: float) -> Tuple[float, float]:
    # Approximate meters per degree at given latitude (WGS84)
    meters_per_deg_lat = 111320.0
    meters_per_deg_lon = 111320.0 * cos(radians(lat_deg))
    return meters_per_deg_lat, meters_per_deg_lon


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great circle distance between two points
    on the earth (specified in decimal degrees) in meters.
    """
    # convert decimal degrees to radians
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])

    # haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    r = 6371000 # Radius of earth in meters.
    return c * r


def _project_to_local_xy(points: List[Tuple[float, float]], ref_lat: float) -> List[Tuple[float, float]]:
    mlat, mlon = _meters_per_degree(ref_lat)
    # Use first point as origin to keep numbers small
    lat0, lon0 = points[0]
    origin_x = (lon0 - lon0) * mlon
    origin_y = (lat0 - lat0) * mlat
    xy: List[Tuple[float, float]] = []
    for (lat, lon) in points:
        x = (lon - lon0) * mlon
        y = (lat - lat0) * mlat
        xy.append((x - origin_x, y - origin_y))
    return xy


def _closest_point_on_polyline(points_latlon: List[Tuple[float, float]], p_lat: float, p_lon: float) -> Tuple[int, float, float, float]:
    """
    Returns (best_segment_index, t_in_segment, distance_meters, along_meters)
    - best_segment_index: index i for segment (points[i] -> points[i+1])
    - t_in_segment in [0,1]
    - distance_meters: XY distance to snapped point
    - along_meters: cumulative length from start to snapped point
    """
    if len(points_latlon) < 2:
        return (-1, 0.0, float('inf'), 0.0)
    ref_lat = points_latlon[0][0]
    mlat, mlon = _meters_per_degree(ref_lat)
    # origin
    lat0, lon0 = points_latlon[0]
    def to_xy(lat: float, lon: float) -> Tuple[float, float]:
        return ((lon - lon0) * mlon, (lat - lat0) * mlat)
    poly_xy = [to_xy(lat, lon) for (lat, lon) in points_latlon]
    px, py = to_xy(p_lat, p_lon)
    best_d2 = float('inf')
    best_i = -1
    best_t = 0.0
    # cumulative lengths per vertex
    seg_lens: List[float] = []
    cum = 0.0
    for i in range(len(poly_xy) - 1):
        ax, ay = poly_xy[i]
        bx, by = poly_xy[i+1]
        dx = bx - ax
        dy = by - ay
        seg_len2 = dx*dx + dy*dy
        seg_len = sqrt(seg_len2) if seg_len2 > 0 else 0.0
        seg_lens.append(seg_len)
    # Find closest projection
    for i in range(len(poly_xy) - 1):
        ax, ay = poly_xy[i]
        bx, by = poly_xy[i+1]
        dx = bx - ax
        dy = by - ay
        denom = dx*dx + dy*dy
        if denom <= 1e-12:
            # degenerate; use vertex
            qx, qy = ax, ay
            d2 = (px - qx)*(px - qx) + (py - qy)*(py - qy)
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((px - ax)*dx + (py - ay)*dy) / denom))
            qx = ax + t * dx
            qy = ay + t * dy
            d2 = (px - qx)*(px - qx) + (py - qy)*(py - qy)
        if d2 < best_d2:
            best_d2 = d2
            best_i = i
            best_t = t
    # Compute along meters
    along = 0.0
    for j in range(best_i):
        along += seg_lens[j]
    along += seg_lens[best_i] * best_t if best_i >= 0 and best_i < len(seg_lens) else 0.0
    return (best_i, best_t, sqrt(best_d2), along)


def _closest_point_across_polylines(
    polylines: List[Tuple[Optional[int], List[Tuple[float, float]]]],
    p_lat: float,
    p_lon: float
) -> Tuple[Optional[int], int, float, float, float]:
    """
    Evaluate the closest projection across multiple polylines.

    Returns (chosen_route_id, best_segment_index, t_in_segment, distance_meters, along_meters)
    - chosen_route_id: the segment/group id if available (can be None)
    - others as in _closest_point_on_polyline for the chosen polyline
    """
    best_route: Optional[int] = None
    best_idx: int = -1
    best_t: float = 0.0
    best_d: float = float('inf')
    best_along: float = 0.0
    for route_id, pts in polylines:
        if not pts or len(pts) < 2:
            continue
        idx, t, d, along = _closest_point_on_polyline(pts, p_lat, p_lon)
        if d < best_d:
            best_route = route_id
            best_idx = idx
            best_t = t
            best_d = d
            best_along = along
    return best_route, best_idx, best_t, best_d, best_along


class SegmentFromPoisRequest(BaseModel):
    startPoiId: int
    endPoiId: int
    routeId: Optional[int] = Field(None, description="Optional route/segment id; if omitted, best route is auto-selected based on both points")
    maxSnapDistance: Optional[float] = Field(None, description="If provided, and either endpoint is farther than this, polyline will fall back to original start/end points")


class LatLon(BaseModel):
    lat: float
    lon: float


class SegmentPolylineResult(BaseModel):
    routeId: Optional[int]
    polyline: List[LatLon]
    start: SnapResult
    end: SnapResult


def _interpolate_latlon(a: Tuple[float, float], b: Tuple[float, float], t: float) -> Tuple[float, float]:
    return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))


def _build_subpolyline(poly: List[Tuple[float, float]], i1: int, t1: float, i2: int, t2: float) -> List[Tuple[float, float]]:
    # compute snapped endpoints
    s1 = _interpolate_latlon(poly[i1], poly[i1 + 1], t1)
    s2 = _interpolate_latlon(poly[i2], poly[i2 + 1], t2)
    # Determine direction using segment indices primarily
    # If along of end is before start (i2 < i1 or equal with t2 < t1), build reverse
    forward = (i2 > i1) or (i2 == i1 and t2 >= t1)
    points: List[Tuple[float, float]] = []
    if forward:
        points.append(s1)
        # include intermediate vertices
        for k in range(i1 + 1, i2 + 1):
            points.append(poly[k])
        points.append(s2)
    else:
        points.append(s1)
        # move backward including vertices on the way back
        for k in range(i1, i2, -1):
            points.append(poly[k])
        points.append(s2)
    return points


@router.post("/{track_id}/snap", response_model=SnapResult)
async def snap_to_track(
    track_id: int,
    req: SnapRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    perms = get_user_permissions(track, current_user.id)
    if not perms.can_view:
        raise HTTPException(status_code=403, detail="No permission to view this track")
    base_query = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type == "gps_track",
        Waypoint.created_by == track.created_by
    )
    # If a specific route is requested, stay constrained; otherwise evaluate all routes
    if req.routeId is not None:
        # NEW SYSTEM: Use route_id instead of segment_id
        base_query = base_query.filter(Waypoint.route_id == req.routeId)
        gps_points = base_query.order_by(Waypoint.timestamp.asc()).all()
        if not gps_points or len(gps_points) < 2:
            raise HTTPException(status_code=404, detail="No GPS polyline available for this route/track")
        poly = [(wp.latitude, wp.longitude) for wp in gps_points]
        best_i, best_t, dist_m, along_m = _closest_point_on_polyline(poly, req.lat, req.lon)
        if best_i < 0:
            raise HTTPException(status_code=500, detail="Failed to compute nearest point")
        # Interpolate snapped lat/lon between best_i and best_i+1
        a_lat, a_lon = poly[best_i]
        b_lat, b_lon = poly[best_i + 1]
        chosen_poly = poly
        chosen_route: Optional[int] = req.routeId
    else:
        # NEW SYSTEM: Group by route_id instead of segment_id
        gps_points = base_query.order_by(Waypoint.timestamp.asc()).all()
        if not gps_points or len(gps_points) < 2:
            raise HTTPException(status_code=404, detail="No GPS polylines available for this track")
        # Build polylines per route_id (None allowed for ungrouped GPS points)
        grouped: dict[Optional[int], List[Tuple[float, float]]] = {}
        for wp in gps_points:
            rid = wp.route_id
            if rid not in grouped:
                grouped[rid] = []
            grouped[rid].append((wp.latitude, wp.longitude))
        polylines = list(grouped.items())
        chosen_route, best_i, best_t, dist_m, along_m = _closest_point_across_polylines(polylines, req.lat, req.lon)
        if best_i < 0:
            raise HTTPException(status_code=500, detail="Failed to compute nearest point across polylines")
        chosen_poly = grouped[chosen_route]
        a_lat, a_lon = chosen_poly[best_i]
        b_lat, b_lon = chosen_poly[best_i + 1]
    snapped_lat = a_lat + best_t * (b_lat - a_lat)
    snapped_lon = a_lon + best_t * (b_lon - a_lon)
    # lateral offset (signed) can be approximated via left/right using 2D cross
    # Compute local XY for sign based on chosen polyline
    ref_lat = chosen_poly[0][0]
    mlat, mlon = _meters_per_degree(ref_lat)
    def to_xy(lat, lon):
        lat0, lon0 = chosen_poly[0]
        return ((lon - lon0) * mlon, (lat - lat0) * mlat)
    ax, ay = to_xy(a_lat, a_lon)
    bx, by = to_xy(b_lat, b_lon)
    px, py = to_xy(req.lat, req.lon)
    qx, qy = to_xy(snapped_lat, snapped_lon)
    abx, aby = (bx - ax), (by - ay)
    aqx, aqy = (qx - ax), (qy - ay)
    apx, apy = (px - ax), (py - ay)
    # perpendicular distance signed: use 2D cross of AB and AP normalized by |AB|
    ab_len = sqrt(abx*abx + aby*aby) or 1.0
    cross = (abx * apy - aby * apx) / ab_len
    lateral_offset = cross
    if req.maxSnapDistance is not None and dist_m > req.maxSnapDistance:
        # No snap if too far
        return SnapResult(
            snappedLat=req.lat,
            snappedLon=req.lon,
            distanceMeters=dist_m,
            alongMeters=along_m,
            lateralOffsetMeters=lateral_offset,
            routeId=chosen_route,
        )
    return SnapResult(
        snappedLat=snapped_lat,
        snappedLon=snapped_lon,
        distanceMeters=dist_m,
        alongMeters=along_m,
        lateralOffsetMeters=lateral_offset,
        routeId=chosen_route,
    )


class PoiSnapRequest(BaseModel):
    enabled: bool = True
    routeId: Optional[int] = None
    maxSnapDistance: Optional[float] = None


@router.post("/{track_id}/pois/{poi_id}/snap", response_model=dict)
async def snap_poi(
    track_id: int,
    poi_id: int,
    req: PoiSnapRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Snap a POI to route(s) using intelligent multi-route detection.

    If multiple routes are within similar distance (< 10m difference),
    the POI will be assigned to all of them (shared path detection).
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    perms = get_user_permissions(track, current_user.id)
    if not (track.created_by == current_user.id or perms.can_add_waypoints):
        raise HTTPException(status_code=403, detail="No permission to modify waypoints for this track")
    wp = db.query(Waypoint).filter(Waypoint.id == poi_id, Waypoint.track_id == track_id).first()
    if not wp:
        raise HTTPException(status_code=404, detail="Waypoint not found")
    if not req.enabled:
        meta = wp.metadata_json or {}
        if "snap" in meta:
            try:
                del meta["snap"]
                del meta["fixedRoutes"]
            except Exception:
                pass
            wp.metadata_json = meta

            # Mark JSON field as modified for SQLAlchemy
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(wp, "metadata_json")

            db.commit()
            db.refresh(wp)
        return {"status": "unsnapped", "waypoint_id": wp.id}

    # Multi-route snap: Check distance to ALL routes
    from ..models import TrackRoute

    routes = guide_routes(db.query(TrackRoute).filter(TrackRoute.track_id == track_id).all())
    route_snaps = []

    for route in routes:
        try:
            snap_result = await snap_to_track(
                track_id,
                SnapRequest(lat=wp.latitude, lon=wp.longitude, routeId=route.id, maxSnapDistance=req.maxSnapDistance),
                current_user=current_user,
                db=db
            )
            route_snaps.append({
                "route_id": route.id,
                "distance": snap_result.distanceMeters,
                "snapped_lat": snap_result.snappedLat,
                "snapped_lon": snap_result.snappedLon,
                "along_meters": snap_result.alongMeters,
                "lateral_offset_meters": snap_result.lateralOffsetMeters
            })
        except:
            # Route has no GPS data or snap failed
            continue

    if not route_snaps:
        raise HTTPException(status_code=404, detail="Could not snap to any route")

    # Sort by distance
    route_snaps.sort(key=lambda x: x["distance"])

    # Intelligent multi-route detection
    snap_threshold = req.maxSnapDistance or 50.0
    position_threshold = 10.0  # If snap positions are within 10m, routes share the same location

    closest_snap = route_snaps[0]
    assigned_routes = []

    # Add closest route
    if closest_snap["distance"] <= snap_threshold:
        assigned_routes.append(closest_snap)

        # Check if other routes snap to nearly the same position (shared path)
        for snap in route_snaps[1:]:
            if snap["distance"] <= snap_threshold:
                # Calculate distance between the two snap positions
                snap_pos_distance = _haversine(
                    closest_snap["snapped_lat"], closest_snap["snapped_lon"],
                    snap["snapped_lat"], snap["snapped_lon"]
                )
                if snap_pos_distance <= position_threshold:
                    # Routes are at the same location → shared path
                    assigned_routes.append(snap)

    if not assigned_routes:
        raise HTTPException(status_code=400, detail=f"POI too far from routes (closest: {closest_snap['distance']:.1f}m)")

    # Update metadata with multi-route snap info
    meta = wp.metadata_json or {}
    meta["snap"] = {
        "routes": assigned_routes,  # Array of route snap data
        "primary_route_id": assigned_routes[0]["route_id"],  # Closest route
        "max_snap_distance": req.maxSnapDistance,
        "updated_at": datetime.utcnow().isoformat()
    }

    # Add fixedRoutes for Guide Export compatibility
    meta["fixedRoutes"] = [f"route_{r['route_id']}" for r in assigned_routes]

    wp.metadata_json = meta

    # Mark JSON field as modified for SQLAlchemy
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(wp, "metadata_json")

    db.commit()
    db.refresh(wp)

    return {
        "status": "snapped",
        "waypoint_id": wp.id,
        "routes": len(assigned_routes),
        "route_ids": [r["route_id"] for r in assigned_routes],
        "snap": meta["snap"]
    }


@router.post("/{track_id}/resnap-waypoints", response_model=dict)
async def resnap_all_waypoints(
    track_id: int,
    route_id: Optional[int] = None,  # Optional: nur für eine bestimmte Route
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Re-snap all waypoints (POIs) for a track to their assigned routes.

    Useful when GPS track data has changed and snap positions need to be recalculated.

    Parameters:
    - route_id (optional): Only resnap waypoints for this specific route

    Returns:
    - total: Total waypoints found
    - resnapped: Successfully resnapped
    - skipped: Skipped (no snap info or no route assigned)
    - errors: Failed to resnap
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    perms = get_user_permissions(track, current_user.id)
    if not (track.created_by == current_user.id or perms.can_add_waypoints):
        raise HTTPException(status_code=403, detail="No permission to modify waypoints for this track")

    # Load all waypoints (exclude GPS track points)
    waypoints = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type != "gps_track"
    ).all()

    total = 0
    resnapped = 0
    skipped = 0
    errors = 0

    for wp in waypoints:
        total += 1

        # Get current snap info
        meta = wp.metadata_json or {}
        snap_info = meta.get("snap", {})

        # Skip segment markers (they have manual route assignment via segment.routeId)
        if meta.get("segment"):
            skipped += 1
            continue

        # If route_id filter is provided, only process waypoints assigned to that route
        if route_id is not None:
            fixed_routes = meta.get("fixedRoutes", [])
            if f"route_{route_id}" not in fixed_routes:
                skipped += 1
                continue

        try:
            # Re-snap using multi-route logic (same as snap_poi endpoint)
            from ..models import TrackRoute

            routes = guide_routes(db.query(TrackRoute).filter(TrackRoute.track_id == track_id).all())
            route_snaps = []

            for route_obj in routes:
                try:
                    snap_result = await snap_to_track(
                        track_id,
                        SnapRequest(
                            lat=wp.latitude,
                            lon=wp.longitude,
                            routeId=route_obj.id,
                            maxSnapDistance=snap_info.get("max_snap_distance")
                        ),
                        current_user=current_user,
                        db=db
                    )
                    route_snaps.append({
                        "route_id": route_obj.id,
                        "distance": snap_result.distanceMeters,
                        "snapped_lat": snap_result.snappedLat,
                        "snapped_lon": snap_result.snappedLon,
                        "along_meters": snap_result.alongMeters,
                        "lateral_offset_meters": snap_result.lateralOffsetMeters
                    })
                except:
                    continue

            if not route_snaps:
                errors += 1
                continue

            # Sort by distance
            route_snaps.sort(key=lambda x: x["distance"])

            # Multi-route detection
            snap_threshold = snap_info.get("max_snap_distance") or 50.0
            position_threshold = 10.0  # If snap positions are within 10m, routes share the same location

            closest_snap = route_snaps[0]
            assigned_routes = []

            if closest_snap["distance"] <= snap_threshold:
                assigned_routes.append(closest_snap)
                for snap in route_snaps[1:]:
                    if snap["distance"] <= snap_threshold:
                        # Calculate distance between snap positions
                        snap_pos_distance = _haversine(
                            closest_snap["snapped_lat"], closest_snap["snapped_lon"],
                            snap["snapped_lat"], snap["snapped_lon"]
                        )
                        if snap_pos_distance <= position_threshold:
                            # Routes are at the same location → shared path
                            assigned_routes.append(snap)

            if not assigned_routes:
                errors += 1
                continue

            # Update metadata with new multi-route snap info
            meta["snap"] = {
                "routes": assigned_routes,
                "primary_route_id": assigned_routes[0]["route_id"],
                "max_snap_distance": snap_info.get("max_snap_distance"),
                "updated_at": datetime.utcnow().isoformat()
            }
            meta["fixedRoutes"] = [f"route_{r['route_id']}" for r in assigned_routes]
            wp.metadata_json = meta

            # Mark JSON field as modified for SQLAlchemy
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(wp, "metadata_json")

            resnapped += 1

        except Exception as e:
            print(f"❌ Failed to resnap waypoint {wp.id}: {e}")
            errors += 1

    # Commit all changes
    if resnapped > 0:
        db.commit()

    return {
        "status": "completed",
        "total": total,
        "resnapped": resnapped,
        "skipped": skipped,
        "errors": errors,
        "message": f"Resnapped {resnapped}/{total} waypoints"
    }


@router.post("/{track_id}/segment/from-pois", response_model=SegmentPolylineResult)
async def segment_from_pois(
    track_id: int,
    req: SegmentFromPoisRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    perms = get_user_permissions(track, current_user.id)
    if not perms.can_view:
        raise HTTPException(status_code=403, detail="No permission to view this track")

    # Load start/end waypoints
    wp_start = db.query(Waypoint).filter(Waypoint.id == req.startPoiId, Waypoint.track_id == track_id).first()
    wp_end = db.query(Waypoint).filter(Waypoint.id == req.endPoiId, Waypoint.track_id == track_id).first()
    if not wp_start or not wp_end:
        raise HTTPException(status_code=404, detail="Start or end waypoint not found")

    # Build polylines per segment for this track
    base_query = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type == "gps_track",
        Waypoint.created_by == track.created_by
    ).order_by(Waypoint.timestamp.asc())
    gps_points = base_query.all()
    if not gps_points or len(gps_points) < 2:
        raise HTTPException(status_code=404, detail="No GPS polylines available for this track")

    grouped: dict[Optional[int], List[Tuple[float, float]]] = {}
    for wp in gps_points:
        sid = wp.segment_id
        if sid not in grouped:
            grouped[sid] = []
        grouped[sid].append((wp.latitude, wp.longitude))

    # Determine chosen route
    chosen_route: Optional[int] = req.routeId
    if chosen_route is None:
        # Evaluate sum of start+end distances per route and choose min
        best_sum = float('inf')
        best_sid: Optional[int] = None
        for sid, poly in grouped.items():
            if not poly or len(poly) < 2:
                continue
            i1, t1, d1, _ = _closest_point_on_polyline(poly, wp_start.latitude, wp_start.longitude)
            i2, t2, d2, _ = _closest_point_on_polyline(poly, wp_end.latitude, wp_end.longitude)
            s = d1 + d2
            if s < best_sum:
                best_sum = s
                best_sid = sid
        chosen_route = best_sid

    if chosen_route not in grouped:
        # fallback: take the longest polyline
        chosen_route = max(grouped.keys(), key=lambda k: len(grouped[k]) if grouped[k] is not None else -1)  # type: ignore
        if chosen_route not in grouped:
            raise HTTPException(status_code=404, detail="No usable route found for segment computation")

    poly = grouped[chosen_route]
    # Snap start and end to chosen polyline
    i1, t1, d1, along1 = _closest_point_on_polyline(poly, wp_start.latitude, wp_start.longitude)
    i2, t2, d2, along2 = _closest_point_on_polyline(poly, wp_end.latitude, wp_end.longitude)
    if i1 < 0 or i2 < 0:
        raise HTTPException(status_code=500, detail="Failed to compute nearest points on chosen route")

    s1_lat, s1_lon = _interpolate_latlon(poly[i1], poly[i1 + 1], t1)
    s2_lat, s2_lon = _interpolate_latlon(poly[i2], poly[i2 + 1], t2)

    # lateral offsets for both endpoints (signed)
    # Build local XY based on chosen polyline origin
    mlat, mlon = _meters_per_degree(poly[0][0])
    lat0, lon0 = poly[0]
    def to_xy(lat, lon):
        return ((lon - lon0) * mlon, (lat - lat0) * mlat)
    ax1, ay1 = to_xy(poly[i1][0], poly[i1][1])
    bx1, by1 = to_xy(poly[i1 + 1][0], poly[i1 + 1][1])
    px1, py1 = to_xy(wp_start.latitude, wp_start.longitude)
    qx1, qy1 = to_xy(s1_lat, s1_lon)
    abx1, aby1 = (bx1 - ax1), (by1 - ay1)
    apx1, apy1 = (px1 - ax1), (py1 - ay1)
    ab_len1 = sqrt(abx1 * abx1 + aby1 * aby1) or 1.0
    lateral1 = (abx1 * apy1 - aby1 * apx1) / ab_len1

    ax2, ay2 = to_xy(poly[i2][0], poly[i2][1])
    bx2, by2 = to_xy(poly[i2 + 1][0], poly[i2 + 1][1])
    px2, py2 = to_xy(wp_end.latitude, wp_end.longitude)
    qx2, qy2 = to_xy(s2_lat, s2_lon)
    abx2, aby2 = (bx2 - ax2), (by2 - ay2)
    apx2, apy2 = (px2 - ax2), (py2 - ay2)
    ab_len2 = sqrt(abx2 * abx2 + aby2 * aby2) or 1.0
    lateral2 = (abx2 * apy2 - aby2 * apx2) / ab_len2

    # Build sub-polyline
    poly_points = _build_subpolyline(poly, i1, t1, i2, t2)

    # Respect maxSnapDistance if provided
    if req.maxSnapDistance is not None and (d1 > req.maxSnapDistance or d2 > req.maxSnapDistance):
        poly_points = [(wp_start.latitude, wp_start.longitude), (wp_end.latitude, wp_end.longitude)]
        # Adjust snapped points to originals as well
        s1_lat, s1_lon = wp_start.latitude, wp_start.longitude
        s2_lat, s2_lon = wp_end.latitude, wp_end.longitude

    start_snap = SnapResult(
        snappedLat=s1_lat,
        snappedLon=s1_lon,
        distanceMeters=d1,
        alongMeters=along1,
        lateralOffsetMeters=lateral1,
        routeId=chosen_route,
    )
    end_snap = SnapResult(
        snappedLat=s2_lat,
        snappedLon=s2_lon,
        distanceMeters=d2,
        alongMeters=along2,
        lateralOffsetMeters=lateral2,
        routeId=chosen_route,
    )

    return SegmentPolylineResult(
        routeId=chosen_route,
        polyline=[LatLon(lat=p[0], lon=p[1]) for p in poly_points],
        start=start_snap,
        end=end_snap,
    )

