"""
Google Places Nearby — server-side proxy with persistent SQLite cache.

Endpoints:
  GET /places/nearby?lat=...&lng=...&radius_m=...
  GET /places/nearby/compact?lat=...&lng=...&radius_m=...

Cache persists across API restarts via SQLite. ~110m grid cells, 24h TTL.
"""

import json
import math
import os
import sqlite3
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Query, HTTPException

router = APIRouter()

GOOGLE_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "AIzaSyDAZnfJ30Hrs0LmBpNrGdzBvs9DV3_82BY")
PLACES_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"

# ── SQLite Cache ──────────────────────────────────────────────────

_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "places_cache.db")
_CACHE_TTL = 2592000  # 30 days
_RADIUS_BUCKETS = [50, 100, 200, 500, 1000, 2000]


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS places_cache (
            key TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    return conn


def _cache_key(lat: float, lng: float, radius_m: int, type_filter: Optional[str] = None) -> str:
    bucket = min(_RADIUS_BUCKETS, key=lambda b: abs(b - radius_m))
    key = f"{lat:.3f},{lng:.3f},{bucket}"
    if type_filter:
        key += f":{type_filter}"
    return key


def _cache_get(key: str) -> Optional[dict]:
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT data, created_at FROM places_cache WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        if row and (time.time() - row[1]) < _CACHE_TTL:
            return json.loads(row[0])
    except Exception:
        pass
    return None


def _cache_set(key: str, data: dict):
    try:
        conn = _get_db()
        conn.execute(
            "INSERT OR REPLACE INTO places_cache (key, data, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(data), time.time()),
        )
        conn.commit()
        # Cleanup old entries (keep max 2000)
        conn.execute("""
            DELETE FROM places_cache WHERE key NOT IN (
                SELECT key FROM places_cache ORDER BY created_at DESC LIMIT 1000000
            )
        """)
        conn.commit()
        conn.close()
    except Exception:
        pass


def _cache_stats() -> dict:
    try:
        conn = _get_db()
        total = conn.execute("SELECT COUNT(*) FROM places_cache").fetchone()[0]
        valid = conn.execute(
            "SELECT COUNT(*) FROM places_cache WHERE created_at > ?",
            (time.time() - _CACHE_TTL,)
        ).fetchone()[0]
        conn.close()
        return {"total_entries": total, "valid_entries": valid, "ttl_hours": _CACHE_TTL // 3600}
    except Exception:
        return {"error": "cache db unavailable"}


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
    skip = {"point_of_interest", "establishment", "political", "locality",
            "sublocality", "sublocality_level_1", "route", "street_address",
            "premise", "geocode"}
    for t in types:
        if t not in skip:
            return t
    return types[0] if types else "place"


def _parse_results(lat: float, lng: float, results: list) -> list[dict]:
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
            "name": name, "category": category, "distance_m": dist,
            "lat": round(f_lat, 6), "lng": round(f_lng, 6),
        }
        if rating is not None: entry["rating"] = rating
        if reviews is not None: entry["reviews"] = reviews
        if price is not None: entry["price_level"] = price
        if vicinity: entry["address"] = vicinity
        if open_now is not None: entry["open_now"] = open_now

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
    type: Optional[str] = Query(None, description="Filter by Google Place type"),
):
    """
    Query nearby places from Google Places API.
    Cached in SQLite — persists across API restarts. 24h TTL.
    """
    key = _cache_key(lat, lng, radius_m, type)

    # Cache hit
    cached = _cache_get(key)
    if cached:
        return {**cached, "cached": True}

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

    _cache_set(key, result)
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


@router.get("/cache/stats")
async def places_cache_stats():
    """Cache statistics — how many entries, how many valid."""
    return _cache_stats()
