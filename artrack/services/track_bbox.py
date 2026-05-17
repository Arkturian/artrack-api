"""Track bounding-circle derived attribute.

The bounding circle (center + radius) is a cheap, denormalised spatial
index used by GET /tracks/nearby as a broad-phase filter before the
expensive snap-to-polyline call.

Maintenance: Pattern B — explicit recompute hook called by every endpoint
that writes to the `waypoints` table for a track. See `track_bbox.py`
docstring in routes/gps_routes.py for the exact hook points. Lazy
recompute on the read side (when bbox_radius_m IS NULL) acts as defence
in depth so a missed hook never produces "track not detectable" forever.
"""

from math import asin, cos, radians, sin, sqrt
from typing import Optional

from sqlalchemy.orm import Session

from ..models import Track, Waypoint


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in meters between two lat/lon points."""
    r = 6371000.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return r * 2 * asin(sqrt(a))


def recompute_bbox(db: Session, track_id: int) -> Optional[dict]:
    """Recompute and persist (center_lat, center_lon, radius_m) for a track.

    Center is the centroid (mean) of all GPS-track waypoints owned by the
    track creator (matching the snap_to_track query exactly). Radius is
    the maximum Haversine distance from the centroid to any waypoint.

    Edge cases:
      - Track has no GPS waypoints → all three fields are set to None
        (clears any stale value).
      - Track has 1 GPS waypoint → radius = 0, center = that point.

    Returns the {center_lat, center_lon, radius_m} dict that was written,
    or None if the track doesn't exist.
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        return None

    rows = (
        db.query(Waypoint.latitude, Waypoint.longitude)
        .filter(
            Waypoint.track_id == track_id,
            Waypoint.waypoint_type == "gps_track",
            Waypoint.created_by == track.created_by,
        )
        .all()
    )

    if not rows:
        track.bbox_center_lat = None
        track.bbox_center_lon = None
        track.bbox_radius_m = None
        db.commit()
        return {"center_lat": None, "center_lon": None, "radius_m": None}

    sum_lat = 0.0
    sum_lon = 0.0
    for lat, lon in rows:
        sum_lat += lat
        sum_lon += lon
    n = len(rows)
    cx = sum_lat / n
    cy = sum_lon / n

    max_d = 0.0
    for lat, lon in rows:
        d = _haversine_m(cx, cy, lat, lon)
        if d > max_d:
            max_d = d

    track.bbox_center_lat = cx
    track.bbox_center_lon = cy
    track.bbox_radius_m = max_d
    db.commit()
    return {"center_lat": cx, "center_lon": cy, "radius_m": max_d}


def recompute_all(db: Session) -> dict:
    """One-shot backfill — recompute bbox for every track in the DB.

    Called from the admin migration endpoint (or directly from a script
    on first deploy). Safe to re-run anytime; idempotent.
    """
    track_ids = [t.id for t in db.query(Track.id).all()]
    updated = 0
    skipped = 0
    for tid in track_ids:
        result = recompute_bbox(db, tid)
        if result and result.get("radius_m") is not None:
            updated += 1
        else:
            skipped += 1
    return {"updated": updated, "skipped_empty": skipped, "total": len(track_ids)}
