"""Routes from OSM: generate, snap, edit drafts, release (Post #5567/#5568/#5628).

Every OSM operation produces a NEW route with status "entwurf". Drafts are
invisible to apps (route_status.guide_routes, points stored as route_draft)
until released. Releasing onto an existing route keeps that route's id - the
apps hard-code route ids - and keeps the old geometry as a separate route
with status "abgeloest", so a release can be undone by releasing it back.
"""
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..collaboration_models import can_read_track, get_user_permissions
from ..database import get_db
from ..models import Track, TrackRoute, Waypoint
from ..services import osm_network as on
from ..services.route_status import DRAFT_POINT_TYPE, route_art, route_meta, route_status
from ..services import route_transfer

logger = logging.getLogger(__name__)
router = APIRouter()

ART_VALUES = ("wege", "gewaesser")
POINT_TYPES = ("gps_track", DRAFT_POINT_TYPE)
MAX_POINTS = 20000


# --- helpers ----------------------------------------------------------------

def _track_for_read(db: Session, track_id: int, user) -> Track:
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if not can_read_track(track, user):
        raise HTTPException(status_code=403, detail="Access denied")
    return track


def _track_for_write(db: Session, track_id: int, user) -> Track:
    track = _track_for_read(db, track_id, user)
    if track.created_by == user.id or getattr(user, "trust_level", None) in ("admin", "moderator"):
        return track
    if get_user_permissions(track, user.id).can_edit_track:
        return track
    raise HTTPException(status_code=403, detail="Keine Schreibrechte fuer diesen Track")


def _route(db: Session, track_id: int, route_id: int) -> TrackRoute:
    r = db.query(TrackRoute).filter(TrackRoute.id == route_id, TrackRoute.track_id == track_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Route not found")
    return r


def _route_points(db: Session, track: Track, route_id: int) -> List[Waypoint]:
    return db.query(Waypoint).filter(
        Waypoint.track_id == track.id,
        Waypoint.route_id == route_id,
        Waypoint.waypoint_type.in_(POINT_TYPES),
    ).order_by(Waypoint.recorded_at.asc(), Waypoint.id.asc()).all()


def _network(track: Track, art: str):
    kind = "gewaesser" if art == "gewaesser" else "wege"
    try:
        return on.get_network(track, kind)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


def _set_meta(route: TrackRoute, **changes):
    # JSON columns only persist on reassignment
    meta = dict(route_meta(route))
    meta.update({k: v for k, v in changes.items() if v is not None})
    route.metadata_json = meta


def _write_points(db: Session, track: Track, route_id: int, coords: list, point_type: str):
    """Replace the points of a route in the current transaction."""
    db.query(Waypoint).filter(
        Waypoint.track_id == track.id,
        Waypoint.route_id == route_id,
        Waypoint.waypoint_type.in_(POINT_TYPES),
    ).delete(synchronize_session=False)
    base = datetime.utcnow().replace(microsecond=0)
    db.add_all([
        Waypoint(
            track_id=track.id,
            # gps-points only returns points of the track owner
            created_by=track.created_by,
            latitude=float(lat),
            longitude=float(lon),
            recorded_at=base + timedelta(seconds=i),
            timestamp=base + timedelta(seconds=i),
            waypoint_type=point_type,
            is_public=False,
            route_id=route_id,
            metadata_json={"route_id": route_id, "quelle": "osm-editor"},
        )
        for i, (lat, lon) in enumerate(coords)
    ])


def _move_points(db: Session, track: Track, from_route: int, to_route: int, point_type: str):
    for p in _route_points(db, track, from_route):
        p.route_id = to_route
        p.waypoint_type = point_type
        meta = dict(p.metadata_json or {})
        meta["route_id"] = to_route
        p.metadata_json = meta


def _after_geometry_change(db: Session, track_id: int):
    try:
        from .routes_routes import _invalidate_polyline_cache
        _invalidate_polyline_cache(track_id)
    except Exception:
        pass
    try:
        from ..services.track_bbox import recompute_bbox
        recompute_bbox(db, track_id)
    except Exception:
        pass


def _check_coords(coords) -> list:
    out = []
    for c in coords:
        if not isinstance(c, (list, tuple)) or len(c) < 2:
            raise HTTPException(status_code=400, detail="coords: Liste von [lat, lon]")
        lat, lon = float(c[0]), float(c[1])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise HTTPException(status_code=400, detail=f"Koordinate ausserhalb: {c}")
        out.append([lat, lon])
    if len(out) < 2:
        raise HTTPException(status_code=400, detail="Mindestens 2 Punkte")
    if len(out) > MAX_POINTS:
        raise HTTPException(status_code=400, detail=f"Hoechstens {MAX_POINTS} Punkte")
    return out


def _osm_art_of(net, way_ids: list) -> Optional[str]:
    """Dominant waterway kind by length along the path."""
    weight = {}
    for wid in way_ids:
        w = net.ways.get(wid)
        if w:
            k = w["tags"].get("waterway")
            weight[k] = weight.get(k, 0) + len(w["coords"])
    return max(weight, key=weight.get) if weight else None


def _names_of(net, way_ids: list) -> list:
    names = []
    for wid in way_ids:
        n = (net.ways.get(wid) or {}).get("tags", {}).get("name")
        if n and n not in names:
            names.append(n)
    return names


def _create_draft(db: Session, track: Track, user, name: str, art: str, coords: list, meta: dict, color: Optional[str]) -> TrackRoute:
    route = TrackRoute(
        track_id=track.id,
        created_by=user.id,
        name=name,
        color=color,
        metadata_json={"status": "entwurf", "art": "gewaesser" if art == "gewaesser" else "weg", **meta},
    )
    db.add(route)
    db.flush()
    _write_points(db, track, route.id, coords, DRAFT_POINT_TYPE)
    db.commit()
    db.refresh(route)
    return route


def _route_out(route: TrackRoute, coords: list) -> dict:
    return {"id": route.id, "name": route.name, "color": route.color, "metadata_json": route.metadata_json, "punkte": len(coords)}


# --- OSM data ---------------------------------------------------------------

@router.get("/{track_id}/osm/lines")
def osm_lines(
    track_id: int,
    art: str = Query("wege", description="wege | gewaesser"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """OSM ways of the track area as GeoJSON (full resolution) for the editor."""
    if art not in ART_VALUES:
        raise HTTPException(status_code=400, detail="art: wege oder gewaesser")
    track = _track_for_read(db, track_id, current_user)
    try:
        data = on.load_ways(track)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    out = on.lines_geojson(data, art)
    out["osm_stand"] = data.get("fetched_at")
    return out


@router.post("/{track_id}/osm/refresh")
def osm_refresh(track_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Fetch the track area again from Overpass (admin/moderator)."""
    track = _track_for_read(db, track_id, current_user)
    if getattr(current_user, "trust_level", None) not in ("admin", "moderator"):
        raise HTTPException(status_code=403, detail="Nur admin/moderator")
    try:
        data = on.load_ways(track, refresh=True)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"osm_stand": data.get("fetched_at"), "ways": len(data["ways"]), "radius_m": data.get("radius_m")}


# --- generate / snap --------------------------------------------------------

class GenerateBody(BaseModel):
    punkte: List[List[float]] = Field(..., description="[[lat, lon], ...] Start, Zwischenpunkte, Ziel")
    art: str = Field("wege", description="wege | gewaesser")
    name: Optional[str] = None
    farbe: Optional[str] = None
    vorschau: bool = Field(False, description="nur berechnen, nichts speichern")


@router.post("/{track_id}/routes/osm-generate")
def osm_generate(track_id: int, body: GenerateBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Shortest path along OSM ways (or a water section) through the given points.
    Saves a new draft route unless `vorschau`."""
    if body.art not in ART_VALUES:
        raise HTTPException(status_code=400, detail="art: wege oder gewaesser")
    track = _track_for_write(db, track_id, current_user)
    points = _check_coords(body.punkte)
    net, info = _network(track, body.art)
    try:
        res = on.route_through(net, points)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    names = _names_of(net, res["way_ids"])
    meta = {
        "quelle": "osm",
        "verfahren": "a_b",
        "osm_stand": info["osm_stand"],
        "osm_ids": res["way_ids"],
        "laenge_m": res["length_m"],
        "bearbeitet": False,
    }
    if body.art == "gewaesser":
        meta["osm_art"] = _osm_art_of(net, res["way_ids"])
        meta["von_m"] = 0
        meta["bis_m"] = res["length_m"]
    name = body.name or (names[0] if names else ("Gewaesser-Abschnitt" if body.art == "gewaesser" else "OSM-Route"))
    if body.vorschau:
        return {"vorschau": True, "name": name, "coords": res["coords"], "snapped": res["snapped"], "metadata_json": meta, "namen": names}
    route = _create_draft(db, track, current_user, name, body.art, res["coords"], meta, body.farbe)
    return {**_route_out(route, res["coords"]), "coords": res["coords"], "snapped": res["snapped"], "namen": names}


class SnapBody(BaseModel):
    name: Optional[str] = None
    vorschau: bool = False
    abstand_m: float = Field(50.0, ge=10, le=300, description="Ankerabstand entlang der Route")


@router.post("/{track_id}/routes/{route_id}/osm-snap")
def osm_snap(track_id: int, route_id: int, body: SnapBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Snap an existing route onto OSM ways. Result is a NEW draft; the source
    route stays untouched. Reports deviation (median/p90/max) and gaps."""
    track = _track_for_write(db, track_id, current_user)
    src = _route(db, track_id, route_id)
    art = "gewaesser" if route_art(src) == "gewaesser" else "wege"
    coords = [[p.latitude, p.longitude] for p in _route_points(db, track, route_id)]
    if len(coords) < 2:
        raise HTTPException(status_code=422, detail="Route hat weniger als 2 Punkte")
    net, info = _network(track, art)
    try:
        res = on.snap_route(net, coords, step_m=body.abstand_m)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    meta = {
        "quelle": "osm",
        "verfahren": "einrasten",
        "osm_stand": info["osm_stand"],
        "basis_route_id": route_id,
        "osm_ids": res["way_ids"],
        "laenge_m": res["length_m"],
        "abweichung_m": res["abweichung_m"],
        "luecken": res["luecken"],
        "bearbeitet": False,
    }
    stats = {k: res[k] for k in ("length_m", "laenge_original_m", "abweichung_m", "luecken")}
    if body.vorschau:
        return {"vorschau": True, "coords": res["coords"], **stats}
    name = body.name or f"{src.name} (OSM-Entwurf)"
    route = _create_draft(db, track, current_user, name, art, res["coords"], meta, src.color)
    return {**_route_out(route, res["coords"]), "coords": res["coords"], **stats}


# --- edit / release ---------------------------------------------------------

class PointsBody(BaseModel):
    coords: List[List[float]]
    expected_count: Optional[int] = Field(None, description="Punktzahl, die der Editor zuletzt geladen hat (Schutz vor parallelen Aenderungen)")
    force: bool = Field(False, description="auch aktive Routen ueberschreiben")


@router.put("/{track_id}/routes/{route_id}/points")
def replace_points(track_id: int, route_id: int, body: PointsBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Replace all points of a route in ONE transaction (no half-saved state).
    Drafts only, unless `force` for an active route."""
    track = _track_for_write(db, track_id, current_user)
    route = _route(db, track_id, route_id)
    status = route_status(route)
    if status != "entwurf" and not body.force:
        raise HTTPException(status_code=409, detail="Nur Entwuerfe; fuer aktive Routen force=true")
    coords = _check_coords(body.coords)
    if body.expected_count is not None:
        current = len(_route_points(db, track, route_id))
        if current != body.expected_count:
            raise HTTPException(status_code=409, detail=f"Route wurde inzwischen geaendert ({current} statt {body.expected_count} Punkte), bitte neu laden")
    try:
        _write_points(db, track, route_id, coords, DRAFT_POINT_TYPE if status in ("entwurf", "abgeloest") else "gps_track")
        _set_meta(route, bearbeitet=True, bearbeitet_am=datetime.utcnow().isoformat(timespec="seconds"))
        db.commit()
    except Exception:
        db.rollback()
        raise
    if status == "aktiv":
        _after_geometry_change(db, track_id)
    return {"id": route.id, "punkte": len(coords), "metadata_json": route.metadata_json}


class ReleaseBody(BaseModel):
    ersetzt_route_id: Optional[int] = Field(None, description="aktive Route, deren Geometrie ersetzt wird (ID bleibt)")
    wegpunkte: bool = Field(True, description="Segmentmarker, Story-Points und POI-Snaps auf die neue Linie uebertragen")


def _transfer_plan(db: Session, track: Track, target: TrackRoute, src: TrackRoute) -> dict:
    old_coords = [[p.latitude, p.longitude] for p in _route_points(db, track, target.id)]
    new_coords = [[p.latitude, p.longitude] for p in _route_points(db, track, src.id)]
    wps = db.query(Waypoint).filter(
        Waypoint.track_id == track.id,
        ~Waypoint.waypoint_type.in_(POINT_TYPES),
        Waypoint.archived.isnot(True),
    ).all()
    try:
        return route_transfer.plan_transfer(wps, target.id, old_coords, new_coords), {w.id: w for w in wps}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{track_id}/routes/{route_id}/freigeben-vorschau")
def release_preview(track_id: int, route_id: int, body: ReleaseBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """What a release onto `ersetzt_route_id` would do to the waypoints of that
    route (segment markers moved, story points re-chained, POI snaps recomputed).
    Nothing is written."""
    track = _track_for_write(db, track_id, current_user)
    src = _route(db, track_id, route_id)
    if body.ersetzt_route_id is None:
        return {"route_id": None, "segmentmarker": [], "story_points": [], "pois": [], "zusammenfassung": {}, "hinweis": "Ohne Zielroute werden keine Wegpunkte uebertragen"}
    target = _route(db, track_id, body.ersetzt_route_id)
    plan, _ = _transfer_plan(db, track, target, src)
    return plan


@router.post("/{track_id}/routes/{route_id}/freigeben")
def release(track_id: int, route_id: int, body: ReleaseBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Make a draft (or a replaced old geometry) live.

    With `ersetzt_route_id`: the target route keeps its id, name and assets and
    gets the new geometry; its old geometry becomes a new route with status
    "abgeloest" (undo = release that one onto the target again). The source
    route is removed. Without: the source itself becomes an active route.
    """
    track = _track_for_write(db, track_id, current_user)
    src = _route(db, track_id, route_id)
    if route_status(src) not in ("entwurf", "abgeloest"):
        raise HTTPException(status_code=409, detail="Nur Entwuerfe oder abgeloeste Routen lassen sich freigeben")
    now = datetime.utcnow().isoformat(timespec="seconds")
    try:
        if body.ersetzt_route_id is None:
            _move_points(db, track, src.id, src.id, "gps_track")
            _set_meta(src, status="aktiv", freigegeben_am=now)
            db.commit()
            _after_geometry_change(db, track_id)
            return {"id": src.id, "status": "aktiv"}

        target = _route(db, track_id, body.ersetzt_route_id)
        if target.id == src.id:
            raise HTTPException(status_code=400, detail="Quelle und Ziel sind dieselbe Route")
        if route_status(target) != "aktiv":
            raise HTTPException(status_code=409, detail="Ziel muss eine aktive Route sein")
        if route_art(target) != route_art(src):
            raise HTTPException(status_code=409, detail="Ziel und Entwurf haben verschiedene Art (weg/gewaesser)")
        if len(_route_points(db, track, src.id)) < 2:
            raise HTTPException(status_code=422, detail="Entwurf hat weniger als 2 Punkte")

        transfer = None
        saved_before = None
        if body.wegpunkte:
            plan, by_id = _transfer_plan(db, track, target, src)
            # Exact undo: the replaced line keeps the waypoint positions it had
            saved_before = route_transfer.snapshot(by_id, plan)
            saved = route_meta(src).get("wegpunkte_vorher")
            if route_status(src) == "abgeloest" and route_meta(src).get("ersetzt_durch") == target.id and isinstance(saved, list):
                changed = route_transfer.restore(by_id, saved, target.id, now)
                transfer = {"geaendert": changed, "verfahren": "exakt zurueckgeholt"}
            else:
                changed = route_transfer.apply_transfer(by_id, plan, now)
                transfer = {"geaendert": changed, "verfahren": "uebertragen", **plan["zusammenfassung"]}

        old = TrackRoute(
            track_id=track.id,
            created_by=current_user.id,
            name=f"{target.name} (abgeloest {now[:10]})",
            color=target.color,
            metadata_json={
                **{k: v for k, v in route_meta(target).items() if k not in ("status",)},
                "status": "abgeloest",
                "art": route_art(target),
                "ersetzt_durch": target.id,
                "alte_geometrie_von": target.id,
                "abgeloest_am": now,
                **({"wegpunkte_vorher": saved_before} if saved_before is not None else {}),
            },
        )
        db.add(old)
        db.flush()
        _move_points(db, track, target.id, old.id, DRAFT_POINT_TYPE)
        _move_points(db, track, src.id, target.id, "gps_track")
        src_meta = {k: v for k, v in route_meta(src).items() if k not in ("status", "ersetzt_durch", "alte_geometrie_von", "abgeloest_am", "wegpunkte_vorher")}
        src_meta.update(status="aktiv", freigegeben_am=now, vorgaenger_route_id=old.id)
        _set_meta(target, **src_meta)
        db.delete(src)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Freigeben fehlgeschlagen (track %s, route %s)", track_id, route_id)
        raise HTTPException(status_code=500, detail="Freigeben fehlgeschlagen, nichts geaendert")
    _after_geometry_change(db, track_id)
    return {"id": target.id, "status": "aktiv", "abgeloest_route_id": old.id, "wegpunkte": transfer}


@router.post("/{track_id}/routes/{route_id}/verwerfen")
def discard(track_id: int, route_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Delete a draft or a replaced old geometry with its points."""
    track = _track_for_write(db, track_id, current_user)
    route = _route(db, track_id, route_id)
    if route_status(route) not in ("entwurf", "abgeloest"):
        raise HTTPException(status_code=409, detail="Nur Entwuerfe oder abgeloeste Routen lassen sich verwerfen")
    db.query(Waypoint).filter(Waypoint.track_id == track.id, Waypoint.route_id == route.id, Waypoint.waypoint_type.in_(POINT_TYPES)).delete(synchronize_session=False)
    db.delete(route)
    db.commit()
    return {"id": route_id, "verworfen": True}
