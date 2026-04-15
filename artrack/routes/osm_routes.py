"""
OSM Nearby — server-side Overpass proxy with aggressive caching.

Endpoint: GET /osm/nearby?lat=...&lng=...&radius_m=...

Why server-side instead of browser-direct:
- Central cache: 100 users at the same spot = 1 Overpass query
- Rate-limit compliance: Overpass allows ~1 req/10s per client
- Post-processing: classify, dedupe, sort by distance
- No CORS issues for the frontend

Cache strategy:
- Coordinates rounded to 3 decimals (~110m grid) as cache key
- Radius bucketed to nearest preset (100, 200, 500, 1000, 2000)
- TTL: 1 hour (OSM data doesn't change fast)
- Max cache size: 500 entries (LRU eviction)
"""

import math
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Query, HTTPException

router = APIRouter()

# ── Cache ─────────────────────────────────────────────────────────

_cache: dict[str, dict] = {}
_cache_ts: dict[str, float] = {}
_CACHE_TTL = 3600  # 1 hour
_CACHE_MAX = 500
_RADIUS_BUCKETS = [100, 200, 500, 1000, 2000]

# Overpass rate limit: track last request time globally
_last_overpass_request = 0.0
_OVERPASS_MIN_INTERVAL = 2.0  # seconds between requests


def _cache_key(lat: float, lng: float, radius_m: int) -> str:
    """Round coords to ~110m grid + bucket radius for cache hit rate."""
    bucket = min(_RADIUS_BUCKETS, key=lambda b: abs(b - radius_m))
    return f"{lat:.3f},{lng:.3f},{bucket}"


def _evict_oldest():
    """Remove oldest entry if cache exceeds max size."""
    if len(_cache) <= _CACHE_MAX:
        return
    oldest_key = min(_cache_ts, key=_cache_ts.get)  # type: ignore
    _cache.pop(oldest_key, None)
    _cache_ts.pop(oldest_key, None)


# ── Overpass Query ────────────────────────────────────────────────

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]


def _build_query(lat: float, lng: float, radius_m: int) -> str:
    """Overpass QL query for named features in radius."""
    r = radius_m
    return f"""[out:json][timeout:10];
(
  node(around:{r},{lat},{lng})["name"]["amenity"];
  node(around:{r},{lat},{lng})["name"]["shop"];
  node(around:{r},{lat},{lng})["name"]["tourism"];
  node(around:{r},{lat},{lng})["name"]["historic"];
  node(around:{r},{lat},{lng})["name"]["leisure"];
  node(around:{r},{lat},{lng})["name"]["natural"];
  way(around:{r},{lat},{lng})["name"]["building"];
  way(around:{r},{lat},{lng})["name"]["landuse"]["landuse"!="residential"];
);
out center tags;"""


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in meters between two points."""
    R = 6371000
    to_rad = math.radians
    d_lat = to_rad(lat2 - lat1)
    d_lon = to_rad(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2 +
         math.cos(to_rad(lat1)) * math.cos(to_rad(lat2)) *
         math.sin(d_lon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _classify(tags: dict) -> str:
    """Classify an OSM element into a short category string."""
    for key in ("amenity", "shop", "tourism", "historic", "leisure", "natural", "landuse"):
        if key in tags:
            val = tags[key]
            return val if val != "yes" else key
    if "building" in tags:
        val = tags["building"]
        return f"building:{val}" if val != "yes" else "building"
    return "feature"


def _parse_elements(lat: float, lng: float, elements: list) -> list[dict]:
    """Parse Overpass elements into a sorted, deduped list."""
    seen: set[str] = set()
    result: list[dict] = []

    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name or name in seen:
            continue
        seen.add(name)

        # Get coordinates (centroid for ways)
        if el["type"] == "way":
            center = el.get("center", {})
            f_lat = center.get("lat")
            f_lng = center.get("lon")
        else:
            f_lat = el.get("lat")
            f_lng = el.get("lon")

        if f_lat is None or f_lng is None:
            continue

        dist = round(_haversine(lat, lng, f_lat, f_lng))
        category = _classify(tags)

        result.append({
            "name": name,
            "category": category,
            "distance_m": dist,
            "lat": round(f_lat, 6),
            "lng": round(f_lng, 6),
            "osm_id": el.get("id"),
        })

    result.sort(key=lambda x: x["distance_m"])
    return result[:20]  # cap at 20


# ── Endpoint ──────────────────────────────────────────────────────

@router.get("/nearby")
async def osm_nearby(
    lat: float = Query(..., description="Latitude"),
    lng: float = Query(..., description="Longitude"),
    radius_m: int = Query(200, ge=10, le=2000, description="Search radius in meters (max 2000)"),
):
    """
    Query nearby named features from OpenStreetMap via Overpass.

    Returns buildings, amenities, shops, tourism POIs, historic sites,
    leisure facilities, and natural features within the given radius.

    Results are cached server-side (1h TTL, ~110m grid) for efficiency.
    """
    global _last_overpass_request

    key = _cache_key(lat, lng, radius_m)

    # Cache hit
    if key in _cache and (time.time() - _cache_ts.get(key, 0)) < _CACHE_TTL:
        cached = _cache[key]
        return {**cached, "cached": True}

    # Rate limit
    now = time.time()
    wait = _OVERPASS_MIN_INTERVAL - (now - _last_overpass_request)
    if wait > 0:
        import asyncio
        await asyncio.sleep(wait)

    _last_overpass_request = time.time()

    # Query Overpass — try multiple mirrors with failover
    query = _build_query(lat, lng, radius_m)
    data = None
    last_error = ""
    async with httpx.AsyncClient(timeout=12.0) as client:
        for url in OVERPASS_URLS:
            try:
                resp = await client.post(
                    url,
                    data={"data": query},
                    headers={"User-Agent": "artrack-api/1.0 (audio-guide)"},
                )
                resp.raise_for_status()
                data = resp.json()
                break  # success
            except (httpx.TimeoutException, httpx.HTTPStatusError, Exception) as e:
                last_error = f"{url}: {e}"
                continue  # try next mirror

    if data is None:
        raise HTTPException(status_code=502, detail=f"All Overpass mirrors failed. Last: {last_error}")

    # Parse + cache
    features = _parse_elements(lat, lng, data.get("elements", []))
    result = {
        "features": features,
        "count": len(features),
        "query": {"lat": lat, "lng": lng, "radius_m": radius_m},
    }

    _cache[key] = result
    _cache_ts[key] = time.time()
    _evict_oldest()

    return {**result, "cached": False}


@router.get("/nearby/compact")
async def osm_nearby_compact(
    lat: float = Query(..., description="Latitude"),
    lng: float = Query(..., description="Longitude"),
    radius_m: int = Query(200, ge=10, le=2000, description="Search radius in meters"),
):
    """
    Same as /nearby but returns a single-line string for IACP messages.

    Format: "Café Mozart (cafe, 15m) | Stadtpark (park, 45m) | ..."
    """
    data = await osm_nearby(lat=lat, lng=lng, radius_m=radius_m)
    features = data["features"]
    if not features:
        return {"text": "", "count": 0}

    text = " | ".join(
        f"{f['name']} ({f['category']}, {f['distance_m']}m)"
        for f in features
    )
    return {"text": text, "count": len(features), "cached": data.get("cached", False)}
