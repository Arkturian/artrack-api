"""GET /tracks/nearby — broad/narrow phase track detection.

Used by audio-guide's auto-track detection. Replaces a brute-force
per-tick snap call against every hardcoded track id with a single
spatial-indexed query.

Algorithm:

    Broad phase (cheap):
      Filter tracks where the query point is plausibly inside the
      track's bounding circle (center + radius + caller-supplied
      tolerance). For typical track counts (<200) this is O(N) in
      Python — the heavy SQL filter on lat/lon columns isn't worth
      the index complexity at this scale.

    Narrow phase (more accurate):
      For each candidate, run the existing snap-to-polyline math
      against the track's GPS waypoints. Returns exact perpendicular
      distance to the route.

Result: a list of {track_id, name, visibility, distance_m,
bbox_center_lat, bbox_center_lon}, sorted by distance, filtered to
distance_m <= max_distance_m.

The lazy-recompute defence: if a candidate track has bbox_radius_m
IS NULL (e.g., never written since the migration deployed), we run
recompute_bbox() on it on-demand and re-evaluate. This is the cheap
safety net for the case where the hook chain missed a write path.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Track, Waypoint
from ..services.track_bbox import _haversine_m, recompute_bbox
from .snap_routes import _closest_point_across_polylines

router = APIRouter()


class NearbyTrack(BaseModel):
    track_id: int
    name: str
    visibility: str
    distance_m: float
    bbox_center_lat: Optional[float] = None
    bbox_center_lon: Optional[float] = None


class NearbyResult(BaseModel):
    tracks: List[NearbyTrack]
    candidates_checked: int
    elapsed_ms: int


@router.get("/nearby", response_model=NearbyResult)
def tracks_nearby(
    lat: float = Query(..., description="Query latitude"),
    lon: float = Query(..., description="Query longitude"),
    max_distance_m: float = Query(200.0, description="Filter: only tracks within this snap distance"),
    include_ineligible: bool = Query(False, description="Debug: include tracks with auto_detect_eligible=false"),
    db: Session = Depends(get_db),
):
    """Detect tracks near a GPS position via broad+narrow spatial filter.

    Only tracks with `auto_detect_eligible=true` are returned by default.
    This is the explicit opt-in flag the track owner sets in the artrack
    UI; visibility (public/private) is unrelated. The include_ineligible
    flag is a debug switch — production traffic should leave it false.
    """
    import time
    start = time.monotonic()

    base_q = db.query(Track)
    if not include_ineligible:
        base_q = base_q.filter(Track.auto_detect_eligible.is_(True))
    tracks = base_q.all()

    candidates: list[Track] = []
    for t in tracks:
        # Broad phase: trust the persisted bbox. If never computed, do
        # a lazy recompute so this track becomes detectable from now on.
        if t.bbox_radius_m is None or t.bbox_center_lat is None:
            try:
                recompute_bbox(db, t.id)
                db.refresh(t)
            except Exception:
                continue
            if t.bbox_radius_m is None:
                continue
        d_to_center = _haversine_m(lat, lon, t.bbox_center_lat, t.bbox_center_lon)
        if d_to_center <= t.bbox_radius_m + max_distance_m:
            candidates.append(t)

    # Narrow phase — snap each candidate against its actual polyline.
    results: list[NearbyTrack] = []
    for t in candidates:
        gps_points = (
            db.query(Waypoint)
            .filter(
                Waypoint.track_id == t.id,
                Waypoint.waypoint_type == "gps_track",
                Waypoint.created_by == t.created_by,
            )
            .order_by(Waypoint.timestamp.asc())
            .all()
        )
        if not gps_points or len(gps_points) < 2:
            # Treat a one-point track as its own snap distance from the center.
            if gps_points:
                d = _haversine_m(lat, lon, gps_points[0].latitude, gps_points[0].longitude)
                if d <= max_distance_m:
                    results.append(NearbyTrack(
                        track_id=t.id,
                        name=t.name or f"Track {t.id}",
                        visibility=t.visibility,
                        distance_m=d,
                        bbox_center_lat=t.bbox_center_lat,
                        bbox_center_lon=t.bbox_center_lon,
                    ))
            continue

        # Group waypoints by route_id (same logic as snap_to_track)
        grouped: dict = {}
        for wp in gps_points:
            rid = wp.route_id
            grouped.setdefault(rid, []).append((wp.latitude, wp.longitude))
        polylines = list(grouped.items())

        try:
            _route, best_i, _t, dist_m, _along_m = _closest_point_across_polylines(polylines, lat, lon)
        except Exception:
            continue
        if best_i < 0:
            continue
        if dist_m <= max_distance_m:
            results.append(NearbyTrack(
                track_id=t.id,
                name=t.name or f"Track {t.id}",
                visibility=t.visibility,
                distance_m=dist_m,
                bbox_center_lat=t.bbox_center_lat,
                bbox_center_lon=t.bbox_center_lon,
            ))

    results.sort(key=lambda r: r.distance_m)
    elapsed = int((time.monotonic() - start) * 1000)
    return NearbyResult(tracks=results, candidates_checked=len(candidates), elapsed_ms=elapsed)


@router.post("/bbox/recompute-all", response_model=dict)
def admin_recompute_all_bbox(db: Session = Depends(get_db)):
    """One-shot backfill — recompute bounding-circle for all tracks.

    Safe to re-run anytime. Useful immediately after the schema
    migration so existing tracks become detectable without waiting
    for a fresh waypoint write to trigger the lazy path.
    """
    from ..services.track_bbox import recompute_all
    return recompute_all(db)
