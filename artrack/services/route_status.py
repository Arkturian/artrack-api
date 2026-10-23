"""Lifecycle of a route (OSM routes, Post #5567/#5628).

metadata_json.status: "entwurf" (draft, editor only), "aktiv" (default for
every existing route) or "abgeloest" (old geometry kept after a release).
metadata_json.art: "weg" (default) or "gewaesser" (water section for the map).

Only active walking routes are routes in the sense of the guide: membership,
snapping, POI lists, exports. Drafts and water sections must never leak there.
Draft points are stored as waypoint_type DRAFT_POINT_TYPE so that every reader
filtering on "gps_track" skips them without further changes.
"""

DRAFT_POINT_TYPE = "route_draft"


def route_meta(route) -> dict:
    meta = getattr(route, "metadata_json", None)
    return meta if isinstance(meta, dict) else {}


def route_status(route) -> str:
    return route_meta(route).get("status") or "aktiv"


def route_art(route) -> str:
    return route_meta(route).get("art") or "weg"


def is_guide_route(route) -> bool:
    """Active walking route: what apps, membership and exports may see."""
    return route_status(route) == "aktiv" and route_art(route) == "weg"


def guide_routes(routes) -> list:
    return [r for r in routes if is_guide_route(r)]
