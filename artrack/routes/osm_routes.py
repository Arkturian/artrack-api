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

import asyncio
import json
import logging
import math
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Query, HTTPException

logger = logging.getLogger("artrack.osm")

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
    # NO [name] filter in the query — it's paradoxically slower on Overpass
    # because it forces a per-element tag scan. We filter for name in Python
    # (_parse_elements checks for name). Without [name], Overpass uses fast
    # spatial indices only → 1-2s instead of 5-8s.
    return f"""[out:json][timeout:8];
(
  node(around:{r},{lat},{lng})["amenity"];
  node(around:{r},{lat},{lng})["shop"];
  node(around:{r},{lat},{lng})["tourism"];
  node(around:{r},{lat},{lng})["historic"];
  node(around:{r},{lat},{lng})["leisure"];
  way(around:{r},{lat},{lng})["building"];
  // Areas, not just points. A monument mapped as a way or relation — the Arc
  // du Cinquantenaire is one — could never appear before, no matter how close
  // it was: only `node` was asked for. That made "nearby sights" a promise the
  // endpoint could not keep. (GuideDevBot2, Brussels, 2026-09-04.)
  //
  // Deliberately ONLY historic+tourism as areas. Adding leisure and amenity too
  // made the query so heavy it timed out (502 after 34 s, measured): every park,
  // parking lot and school ground came back as a polygon. Those are containment,
  // not sights — /osm/within answers "which area am I in" properly instead.
  way(around:{r},{lat},{lng})["historic"];
  way(around:{r},{lat},{lng})["tourism"];
  relation(around:{r},{lat},{lng})["historic"];
  relation(around:{r},{lat},{lng})["tourism"];
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

        # Get coordinates (centroid for ways AND relations — `out center` sets
        # `center` for both; treating only `way` here silently dropped every
        # relation, e.g. the Royal Military Museum at the Cinquantenaire.)
        if el["type"] in ("way", "relation"):
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
    return result  # caller caps; see _apply_kinds


def _apply_kinds(rows: list[dict], kinds: Optional[str], limit: int = 20) -> list[dict]:
    """Optional category filter, applied BEFORE the 20-item cap.

    Filtering after the cap would be useless: the whole point is that a monument
    must not be pushed out by twenty nearer bicycle stands. With `kinds` the
    caller decides what competes for the twenty slots.
    """
    if kinds:
        wanted = [k.strip().lower() for k in kinds.split(",") if k.strip()]
        if wanted:
            rows = [r for r in rows
                    if any(w in (r.get("category") or "").lower() for w in wanted)]
    return rows[:limit]


# ── Endpoint ──────────────────────────────────────────────────────

@router.get("/nearby")
async def osm_nearby(
    lat: float = Query(..., description="Latitude"),
    lng: float = Query(..., description="Longitude"),
    radius_m: int = Query(200, ge=10, le=2000, description="Search radius in meters (max 2000)"),
    budget_s: float = Query(12.0, ge=0.5, le=30.0, description="per-mirror time budget; the realtime guide waits synchronously, so it passes a short one"),
    kinds: Optional[str] = Query(None, description="comma-separated category filter, e.g. 'monument,attraction,museum,artwork,park'; substring match on the classified category"),
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
    async with httpx.AsyncClient(timeout=budget_s) as client:
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
        # NOT a 5xx. The endpoint did its job; the external service did not, and
        # the caller can act on that — "I could not look" is usable, an error is
        # not. A realtime guide that gets a 500 goes down its error path and says
        # nothing at all, which is worse than narrating without surroundings.
        logger.warning(f"osm/nearby: all mirrors failed at {lat},{lng}. Last: {last_error}")
        return {"features": [], "count": 0, "cached": False, "degraded": True,
                "degraded_reason": f"all Overpass mirrors failed: {str(last_error)[:160]}",
                "query": {"lat": lat, "lng": lng, "radius_m": radius_m}}

    # Overpass signals a server-side timeout with HTTP 200 and a `remark`, not
    # with an error status: the body then carries few or no elements. Without
    # this flag that arrives as "there is nothing here", which is the one answer
    # a guide must not give confidently. (GuideDevBot measured 0 results after
    # 24 s at radius 800; a second run returned 20 results in 8 s — same place.)
    remark = data.get("remark")
    degraded = bool(remark)
    if degraded:
        logger.warning(f"osm/nearby degraded at {lat},{lng} r={radius_m}: {remark}")

    # Parse + cache
    features = _apply_kinds(_parse_elements(lat, lng, data.get("elements", [])), kinds)
    result = {
        "features": features,
        "count": len(features),
        "query": {"lat": lat, "lng": lng, "radius_m": radius_m},
    }
    if degraded:
        result["degraded"] = True
        result["degraded_reason"] = str(remark)[:200]

    if not degraded:
        # A truncated result must never be cached: it would freeze "nothing here"
        # for an hour at a place that is actually full of sights.
        _cache[key] = result
        _cache_ts[key] = time.time()
        _evict_oldest()

    return {**result, "cached": False}


@router.get("/nearby/compact")
async def osm_nearby_compact(
    lat: float = Query(..., description="Latitude"),
    lng: float = Query(..., description="Longitude"),
    radius_m: int = Query(200, ge=10, le=2000, description="Search radius in meters"),
    budget_s: float = Query(2.0, ge=0.5, le=30.0, description="time budget per mirror; short by default because a realtime model waits synchronously on this call"),
    kinds: Optional[str] = Query(None, description="comma-separated category filter, same as /nearby"),
):
    """
    Same as /nearby but returns a single-line string for IACP messages.

    Format: "Café Mozart (cafe, 15m) | Stadtpark (park, 45m) | ..."

    Two deliberate differences from /nearby, both because a realtime guide waits
    on this call while the visitor stands there:
    * the default budget is 2 s, not 12 — a slow answer is worse than none;
    * it NEVER raises. Any failure comes back as an empty text with
      `degraded: true`, so the model narrates without surroundings instead of
      falling into its error path. (GuideDevBot2: a 500 here cost 5 s per turn
      and pushed time-to-first-audio from ~1.5 s to 6-7 s.)
    """
    try:
        data = await osm_nearby(lat=lat, lng=lng, radius_m=radius_m,
                                budget_s=budget_s, kinds=kinds)
    except Exception as e:
        logger.warning(f"osm/nearby/compact failed at {lat},{lng}: {e}")
        return {"text": "", "count": 0, "degraded": True,
                "degraded_reason": str(e)[:160]}

    features = data.get("features") or []
    out = {
        "text": " | ".join(
            f"{f['name']} ({f['category']}, {f['distance_m']}m)" for f in features
        ),
        "count": len(features),
        "cached": data.get("cached", False),
    }
    if data.get("degraded"):
        out["degraded"] = True
        out["degraded_reason"] = data.get("degraded_reason")
    return out


# ── /osm/within — "which areas am I standing IN?" ────────────────────
#
# The nearby endpoint answers "what is around me" and returns points. It cannot
# answer "I am inside a large park": a park's centroid may be hundreds of metres
# away, so it looks like a distant object — or drops out entirely. Overpass'
# `is_in` answers the containment question directly.
#
# Nominatim is not a substitute: reverse-geocoding Alex' position in the Parc du
# Cinquantenaire returns the Army Museum at zoom 18 and the street "Avenue de la
# Renaissance" at zoom 17/16 — never the park. That street is exactly what the
# guide currently announces while the visitor is cycling through the park.

_WITHIN_CACHE: dict[str, tuple[float, list]] = {}
_WITHIN_TTL = 7 * 24 * 3600          # areas essentially never move
_WITHIN_BUDGET = 3.5                 # hard cap for the REQUEST the caller waits on
_WITHIN_FILL_BUDGET = 25.0           # background fill may take as long as Overpass needs
_WITHIN_FILLING: set[str] = set()    # cells currently being fetched, so we ask once
_WITHIN_FAILS: dict[str, int] = {}   # consecutive failed fills per cell
_WITHIN_MAX_FAILS = 3                # after this, say "degraded" instead of "filling"
_WITHIN_STATS = {"started": 0, "completed": 0, "failed": 0}
_WITHIN_REDIS_PREFIX = "artrack:osm:within:"


async def _within_cache_get(key: str):
    """Read a cell from the SHARED cache, falling back to this worker's memory.

    A per-process dict is not good enough here: the service runs four gunicorn
    workers, so a warm cell answered only about one request in four while the
    other three re-reported "filling". Measured before this was added. Redis is
    already configured for the event bus, so the shared cache costs nothing new.
    """
    try:
        from ..event_bus import _get_client  # lazy: Redis must never break OSM
        client = _get_client()
        if client is not None:
            raw = await client.get(_WITHIN_REDIS_PREFIX + key)
            if raw:
                return json.loads(raw)
    except Exception as e:
        logger.debug(f"osm/within shared cache read failed: {e}")
    hit = _WITHIN_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _WITHIN_TTL:
        return hit[1]
    return None


async def _within_cache_put(key: str, areas: list) -> None:
    _WITHIN_CACHE[key] = (time.time(), areas)      # local copy stays as fallback
    try:
        from ..event_bus import _get_client
        client = _get_client()
        if client is not None:
            await client.setex(_WITHIN_REDIS_PREFIX + key, _WITHIN_TTL, json.dumps(areas))
    except Exception as e:
        logger.debug(f"osm/within shared cache write failed: {e}")
_WITHIN_GRID = 3                     # decimals ≈ 110 m lat / ~70 m lon at 50°N

# Tags that make an enclosing area worth telling a visitor about.
_WITHIN_KINDS = ("leisure", "landuse", "historic", "tourism", "amenity")


def _within_cell(lat: float, lng: float) -> str:
    return f"{round(lat, _WITHIN_GRID)},{round(lng, _WITHIN_GRID)}"


def _within_parse(elements: list, include_boundaries: bool) -> list[dict]:
    """Keep meaningful enclosing areas, smallest first.

    Smallest-first is computed from the bounding box Overpass returns with
    `out tags bb` — not guessed from the tag. At the Cinquantenaire that puts
    the park (~67 units) ahead of the city (~6.264) and the region (~35.829).
    """
    rows = []
    for el in elements:
        tags = el.get("tags") or {}
        name = tags.get("name")
        if not name:
            continue
        if "boundary" in tags and not include_boundaries:
            continue
        kind = next((f"{k}={tags[k]}" for k in _WITHIN_KINDS if k in tags), None)
        if kind is None:
            if tags.get("building"):
                kind = "building"
            elif include_boundaries and "boundary" in tags:
                kind = f"boundary={tags['boundary']}"
            else:
                continue
        b = el.get("bounds") or {}
        try:
            size = (b["maxlat"] - b["minlat"]) * (b["maxlon"] - b["minlon"])
        except KeyError:
            size = float("inf")     # unknown size sorts last, never first
        rows.append((size, {
            "name": name,
            "kind": kind,
            "osm_type": el.get("type"),
            "osm_id": el.get("id"),
        }))
    rows.sort(key=lambda r: r[0])
    return [r[1] for r in rows]


@router.get("/within")
async def osm_within(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    include_boundaries: bool = Query(False, description="also return administrative areas (country, city, postal code …)"),
):
    """Areas the point lies INSIDE, smallest first.

    Administrative boundaries are dropped by default — without that filter the
    honest answer to "where am I" includes "in Benelux", which is true and
    useless. Results are cached per ~110 m grid cell for a week; a cold cell is
    capped at 3.5 s and falls back to an EMPTY list rather than an error, so a
    slow or unreachable Overpass can never hold up a walking guide.
    """
    cell = _within_cell(lat, lng)
    key = f"{cell}|{int(include_boundaries)}"
    hit = await _within_cache_get(key)
    if hit is not None:
        return {"lat": lat, "lng": lng, "cell": cell, "cached": True, "areas": hit}

    # A cold cell is answered EMPTY and filled in the background.
    #
    # Measured against Overpass, `is_in` takes 2.3-6.2 s — routinely more than
    # the 3.5 s a walking guide can wait. Capping the request alone would mean
    # the first visitor in a cell essentially never gets an answer, which is the
    # very case the endpoint exists for. So the caller is never held up, and the
    # fill runs with a budget Overpass can actually meet; the guide polls again
    # a few seconds later and hits a warm cache.
    # A cell whose fill keeps failing must stop claiming to be "filling". After
    # 20 minutes that is simply not a true statement any more, and a consumer
    # that treats it as "not yet known" waits forever. (GuideDevBot2, 2026-09-05.)
    if _WITHIN_FAILS.get(key, 0) >= _WITHIN_MAX_FAILS:
        return {"lat": lat, "lng": lng, "cell": cell, "cached": False,
                "areas": [], "degraded": True,
                "degraded_reason": f"{_WITHIN_FAILS[key]} consecutive fill failures",
                "stats": dict(_WITHIN_STATS)}

    if key not in _WITHIN_FILLING and await _within_claim(key):
        _WITHIN_FILLING.add(key)
        _WITHIN_STATS["started"] += 1
        asyncio.create_task(_within_fill(key, lat, lng, include_boundaries))

    return {"lat": lat, "lng": lng, "cell": cell, "cached": False,
            "areas": [], "filling": True, "stats": dict(_WITHIN_STATS)}


async def _within_claim(key: str) -> bool:
    """Claim the right to fetch one cell, across ALL workers.

    The in-process guard is not enough: four gunicorn workers each fired their
    own Overpass request for the same cell, and Overpass answered with 429 and
    504. A short shared lock means one request per cell instead of four, which
    is also simply polite towards a free public service. Without Redis we fall
    back to the per-process guard — worse, but never blocking.
    """
    try:
        from ..event_bus import _get_client
        client = _get_client()
        if client is not None:
            return bool(await client.set(_WITHIN_REDIS_PREFIX + "lock:" + key,
                                         "1", nx=True, ex=60))
    except Exception as e:
        logger.debug(f"osm/within claim failed, falling back to local guard: {e}")
    return True


async def _within_fill(key: str, lat: float, lng: float, include_boundaries: bool) -> None:
    """Fetch one cell's enclosing areas and put them in the cache."""
    query = (f"[out:json][timeout:20];is_in({lat},{lng})->.a;"
             f"way(pivot.a);out tags bb;relation(pivot.a);out tags bb;")
    # ALL mirrors, not just the first. On 2026-09-05 overpass-api.de was
    # unreachable from arkserver while the other two answered fine — /nearby
    # survived because it iterates, /within died silently because it did not.
    # Every fill failed for hours and the endpoint kept answering "filling".
    try:
        async with httpx.AsyncClient(timeout=_WITHIN_FILL_BUDGET) as client:
            # User-Agent is NOT optional: overpass-api.de answers a request
            # without one with 406 Not Acceptable — which arrives as a fast,
            # silent empty result rather than an error, so it looks like "no
            # areas here" instead of "you asked wrong". The /nearby path already
            # sends one; leaving it off here cost a deploy cycle to find.
            last = None
            for url in OVERPASS_URLS:
                try:
                    resp = await client.post(
                        url,
                        data={"data": query},
                        headers={"User-Agent": "artrack-api/1.0 (audio-guide)"},
                    )
                    if resp.status_code == 200:
                        areas = _within_parse(resp.json().get("elements", []), include_boundaries)
                        await _within_cache_put(key, areas)
                        _WITHIN_FAILS.pop(key, None)
                        _WITHIN_STATS["completed"] += 1
                        logger.info(f"osm/within filled {key}: {len(areas)} areas via {url}")
                        return
                    last = f"{url}: HTTP {resp.status_code}"
                except Exception as e:
                    last = f"{url}: {e}"
            _WITHIN_FAILS[key] = _WITHIN_FAILS.get(key, 0) + 1
            _WITHIN_STATS["failed"] += 1
            logger.warning(f"osm/within fill {key} failed on all mirrors (attempt "
                           f"{_WITHIN_FAILS[key]}). Last: {last}")
    except Exception as e:
        _WITHIN_FAILS[key] = _WITHIN_FAILS.get(key, 0) + 1
        _WITHIN_STATS["failed"] += 1
        logger.warning(f"osm/within fill {key} failed: {e}")
    finally:
        _WITHIN_FILLING.discard(key)
