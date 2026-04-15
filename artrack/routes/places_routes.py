"""
Google Places Nearby — server-side proxy with caching.

Endpoints:
  GET /places/nearby?lat=...&lng=...&radius_m=...
  GET /places/nearby/compact?lat=...&lng=...&radius_m=...

Richer data than OSM: ratings, reviews, opening hours, price level, photos.
Costs ~$32/1000 requests — aggressive caching essential.

Cache: same strategy as OSM — 3-decimal grid (~110m), 1h TTL, 500 max.
"""

import math
import os
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Query, HTTPException

router = APIRouter()

GOOGLE_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "AIzaSyDAZnfJ30Hrs0LmBpNrGdzBvs9DV3_82BY")
PLACES_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"

# ── Cache ─────────────────────────────────────────────────────────

_cache: dict[str, dict] = {}
_cache_ts: dict[str, float] = {}
_CACHE_TTL = 3600  # 1 hour
_CACHE_MAX = 500
_RADIUS_BUCKETS = [100, 200, 500, 1000, 2000]


def _cache_key(lat: float, lng: float, radius_m: int) -> str:
    bucket = min(_RADIUS_BUCKETS, key=lambda b: abs(b - radius_m))
    return f"gp:{lat:.3f},{lng:.3f},{bucket}"


def _evict_oldest():
    if len(_cache) <= _CACHE_MAX:
        return
    oldest_key = min(_cache_ts, key=_cache_ts.get)  # type: ignore
    _cache.pop(oldest_key, None)
    _cache_ts.pop(oldest_key, None)


# ── Helpers ───────────────────────────────────────────────────────

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000
    to_rad = math.radians
    d_lat = to_rad(lat2 - lat1)
    d_lon = to_rad(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2 +
         math.cos(to_rad(lat1)) * math.cos(to_rad(lat2)) *
         math.sin(d_lon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _classify(types: list[str]) -> str:
    """Pick the most informative type from Google's type list."""
    skip = {"point_of_interest", "establishment", "political", "locality",
            "sublocality", "sublocality_level_1", "route", "street_address",
            "premise", "geocode"}
    for t in types:
        if t not in skip:
            return t
    return types[0] if types else "place"


def _parse_results(lat: float, lng: float, results: list) -> list[dict]:
    """Parse Google Places results into a clean sorted list."""
    seen: set[str] = set()
    parsed: list[dict] = []

    for r in results:
        name = r.get("name", "")
        if not name or name in seen:
            continue
        seen.add(name)

        loc = r.get("geometry", {}).get("location", {})
        f_lat = loc.get("lat")
        f_lng = loc.get("lng")
        if f_lat is None or f_lng is None:
            continue

        dist = round(_haversine(lat, lng, f_lat, f_lng))
        category = _classify(r.get("types", []))
        rating = r.get("rating")
        reviews = r.get("user_ratings_total")
        price = r.get("price_level")
        vicinity = r.get("vicinity", "")
        open_now = r.get("opening_hours", {}).get("open_now")

        entry: dict = {
            "name": name,
            "category": category,
            "distance_m": dist,
            "lat": round(f_lat, 6),
            "lng": round(f_lng, 6),
        }
        if rating is not None:
            entry["rating"] = rating
        if reviews is not None:
            entry["reviews"] = reviews
        if price is not None:
            entry["price_level"] = price
        if vicinity:
            entry["address"] = vicinity
        if open_now is not None:
            entry["open_now"] = open_now

        # Photo reference (first photo only — can be resolved via Places Photo API)
        photos = r.get("photos", [])
        if photos:
            entry["photo_ref"] = photos[0].get("photo_reference", "")

        parsed.append(entry)

    parsed.sort(key=lambda x: x["distance_m"])
    return parsed[:20]


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("/nearby")
async def places_nearby(
    lat: float = Query(..., description="Latitude"),
    lng: float = Query(..., description="Longitude"),
    radius_m: int = Query(200, ge=10, le=2000, description="Search radius (max 2000m)"),
    type: Optional[str] = Query(None, description="Filter by Google Place type (e.g. restaurant, cafe, museum)"),
):
    """
    Query nearby places from Google Places API.

    Returns richer data than OSM: ratings, reviews, price level,
    opening hours, photo references. Cached 1h per ~110m grid cell.
    """
    key = _cache_key(lat, lng, radius_m)
    if type:
        key += f":{type}"

    # Cache hit
    if key in _cache and (time.time() - _cache_ts.get(key, 0)) < _CACHE_TTL:
        return {**_cache[key], "cached": True}

    # Query Google
    params: dict = {
        "location": f"{lat},{lng}",
        "radius": radius_m,
        "key": GOOGLE_API_KEY,
    }
    if type:
        params["type"] = type

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(PLACES_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Google Places API error: {str(e)}")

    status = data.get("status", "UNKNOWN")
    if status not in ("OK", "ZERO_RESULTS"):
        raise HTTPException(status_code=502, detail=f"Google Places status: {status}")

    features = _parse_results(lat, lng, data.get("results", []))
    result = {
        "features": features,
        "count": len(features),
        "query": {"lat": lat, "lng": lng, "radius_m": radius_m, "type": type},
        "source": "google_places",
    }

    _cache[key] = result
    _cache_ts[key] = time.time()
    _evict_oldest()

    return {**result, "cached": False}


@router.get("/nearby/compact")
async def places_nearby_compact(
    lat: float = Query(..., description="Latitude"),
    lng: float = Query(..., description="Longitude"),
    radius_m: int = Query(200, ge=10, le=2000, description="Search radius"),
    type: Optional[str] = Query(None, description="Filter by type"),
):
    """
    Compact single-line format for IACP messages.

    Format: "Bar Carmen (bar, 60m, ★4.0) | Testcenter (establishment, 137m, ★3.2)"
    """
    data = await places_nearby(lat=lat, lng=lng, radius_m=radius_m, type=type)
    features = data["features"]
    if not features:
        return {"text": "", "count": 0, "source": "google_places"}

    parts = []
    for f in features:
        rating = f"★{f['rating']}" if "rating" in f else ""
        price = "€" * f["price_level"] if "price_level" in f else ""
        extras = ", ".join(x for x in [rating, price] if x)
        suffix = f", {extras}" if extras else ""
        parts.append(f"{f['name']} ({f['category']}, {f['distance_m']}m{suffix})")

    return {
        "text": " | ".join(parts),
        "count": len(features),
        "cached": data.get("cached", False),
        "source": "google_places",
    }
