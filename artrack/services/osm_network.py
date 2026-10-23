"""OSM path and water network of a track, for generating and snapping routes.

Source: Overpass, all highway ways and waterway=river|stream|canal around the
track's bbox, with node ids and full geometry (no simplification). Cached per
track as a JSON file under OSM_CACHE_DIR; refresh is explicit.

Routing: undirected graph over OSM node ids, Dijkstra (heapq) with cost =
length * factor by highway type, so foot paths win over roads. Port of the
approach in Tschepp's tools/osm_routen.py (Post #5567), without scipy.
"""
import heapq
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
import numpy as np

logger = logging.getLogger(__name__)

OSM_CACHE_DIR = os.getenv("ARTRACK_OSM_CACHE_DIR", "/var/lib/artrack/osm")
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
MARGIN_M = 600
MAX_RADIUS_M = 8000

WATER_KINDS = ("river", "stream", "canal")
# Cost factors per highway type (Post #5568): foot paths 1.0, small roads 1.5, bigger 3.0
_FOOT = {"path", "footway", "track", "bridleway", "steps", "pedestrian", "cycleway"}
_SMALL = {"service", "residential", "unclassified", "living_street", "road"}
_EXCLUDED = {"motorway", "motorway_link", "trunk", "trunk_link", "construction", "proposed", "platform", "corridor"}


def cost_factor(highway: str) -> float:
    if highway in _FOOT:
        return 1.0
    if highway in _SMALL:
        return 1.5
    return 3.0


def _haversine(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# --- data -------------------------------------------------------------------

def _cache_path(track_id: int) -> str:
    return os.path.join(OSM_CACHE_DIR, f"track_{track_id}.json")


def _query(lat: float, lon: float, radius_m: int) -> str:
    return f"""[out:json][timeout:90];
(
  way["highway"](around:{radius_m},{lat},{lon});
  way["waterway"~"^(river|stream|canal)$"](around:{radius_m},{lat},{lon});
);
out body geom;"""


def ways_from_elements(elements: list) -> list:
    """Overpass `out body geom` ways -> [{id, tags, nodes, coords}], deduplicated."""
    seen, ways = set(), []
    for el in elements:
        if el.get("type") != "way" or el.get("id") in seen:
            continue
        if not el.get("geometry") or not el.get("nodes") or len(el["geometry"]) != len(el["nodes"]):
            continue
        seen.add(el["id"])
        ways.append({
            "id": el["id"],
            "tags": el.get("tags", {}),
            "nodes": el["nodes"],
            "coords": [[g["lat"], g["lon"]] for g in el["geometry"]],
        })
    return ways


def save_ways(track, ways: list, stand: str, radius_m: Optional[int] = None) -> dict:
    data = {
        "fetched_at": stand,
        "center": [track.bbox_center_lat, track.bbox_center_lon],
        "radius_m": radius_m,
        "ways": ways,
    }
    os.makedirs(OSM_CACHE_DIR, exist_ok=True)
    path = _cache_path(track.id)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return data


def fetch_ways(lat: float, lon: float, radius_m: int) -> list:
    """Overpass ways with node ids + geometry. Raises RuntimeError on failure."""
    query = _query(lat, lon, radius_m)
    last = None
    for url in OVERPASS_URLS:
        try:
            r = httpx.post(url, data={"data": query}, headers={"User-Agent": "artrack-api/1.0 (route editor)"}, timeout=120)
            if r.status_code != 200:
                last = f"{url}: HTTP {r.status_code}"
                continue
            data = r.json()
            if data.get("remark"):
                # Overpass reports server timeouts as 200 + remark with truncated data
                last = f"{url}: {str(data['remark'])[:100]}"
                continue
            return ways_from_elements(data.get("elements", []))
        except Exception as e:  # network errors: try the next mirror
            last = f"{url}: {type(e).__name__} {e}"
    raise RuntimeError(f"Overpass nicht erreichbar ({last})")


def load_ways(track, refresh: bool = False) -> dict:
    """{fetched_at, center, radius_m, ways} for a track, from cache or Overpass."""
    path = _cache_path(track.id)
    if not refresh and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    if track.bbox_center_lat is None or track.bbox_center_lon is None:
        raise RuntimeError("Track hat noch keine Ausdehnung (bbox), erst Punkte anlegen")
    radius = int(min(MAX_RADIUS_M, (track.bbox_radius_m or 1000) + MARGIN_M))
    ways = fetch_ways(track.bbox_center_lat, track.bbox_center_lon, radius)
    return save_ways(track, ways, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), radius)


def way_kind(tags: dict) -> Optional[str]:
    """'wege' | 'gewaesser' | None (not routable)."""
    if tags.get("waterway") in WATER_KINDS:
        return "gewaesser"
    hw = tags.get("highway")
    if hw and hw not in _EXCLUDED and tags.get("area") != "yes":
        return "wege"
    return None


# --- graph ------------------------------------------------------------------

@dataclass
class Network:
    kind: str
    coord: dict = field(default_factory=dict)       # node id -> (lat, lon)
    adj: dict = field(default_factory=dict)         # node id -> [(nbr, length_m, cost, way_id)]
    ways: dict = field(default_factory=dict)        # way id -> way dict
    node_ids: Optional[np.ndarray] = None
    node_xy: Optional[np.ndarray] = None            # local metres, for nearest search
    _origin: tuple = (0.0, 0.0)

    def _xy(self, lat, lon):
        lat0, lon0 = self._origin
        return ((lon - lon0) * 111320.0 * math.cos(math.radians(lat0)), (lat - lat0) * 111320.0)

    def nearest_node(self, lat: float, lon: float):
        """(node id, distance m) of the closest network node."""
        x, y = self._xy(lat, lon)
        d = np.hypot(self.node_xy[:, 0] - x, self.node_xy[:, 1] - y)
        i = int(np.argmin(d))
        return int(self.node_ids[i]), float(d[i])

    def shortest_path(self, a: int, b: int, max_cost: float = float("inf")):
        """(node list, length m, way ids) or None. Dijkstra on cost."""
        if a == b:
            return [a], 0.0, []
        dist = {a: 0.0}
        prev = {}
        heap = [(0.0, a)]
        while heap:
            c, u = heapq.heappop(heap)
            if u == b:
                break
            if c > dist.get(u, float("inf")) or c > max_cost:
                continue
            for v, length, cost, wid in self.adj.get(u, ()):
                nc = c + cost
                if nc < dist.get(v, float("inf")):
                    dist[v] = nc
                    prev[v] = (u, length, wid)
                    heapq.heappush(heap, (nc, v))
        if b not in prev:
            return None
        nodes, length, way_ids = [b], 0.0, []
        cur = b
        while cur != a:
            u, l, wid = prev[cur]
            nodes.append(u)
            length += l
            if not way_ids or way_ids[-1] != wid:
                way_ids.append(wid)
            cur = u
        nodes.reverse()
        way_ids.reverse()
        return nodes, length, way_ids


def build_network(data: dict, kind: str) -> Network:
    net = Network(kind=kind)
    lat0, lon0 = data.get("center") or (0.0, 0.0)
    net._origin = (lat0, lon0)
    for w in data["ways"]:
        if way_kind(w["tags"]) != kind:
            continue
        net.ways[w["id"]] = w
        factor = 1.0 if kind == "gewaesser" else cost_factor(w["tags"].get("highway", ""))
        prev = None
        for nid, (lat, lon) in zip(w["nodes"], w["coords"]):
            net.coord[nid] = (lat, lon)
            if prev is not None and prev != nid:
                plat, plon = net.coord[prev]
                length = _haversine(plat, plon, lat, lon)
                net.adj.setdefault(prev, []).append((nid, length, length * factor, w["id"]))
                net.adj.setdefault(nid, []).append((prev, length, length * factor, w["id"]))
            prev = nid
    ids = list(net.adj.keys())
    if not ids:
        raise RuntimeError(f"Kein OSM-Netz der Art {kind} im Bereich des Tracks")
    net.node_ids = np.array(ids, dtype=np.int64)
    net.node_xy = np.array([net._xy(*net.coord[i]) for i in ids])
    return net


_NET_CACHE: dict = {}


def get_network(track, kind: str) -> tuple:
    """(Network, data meta) memoized per track/kind/file version."""
    data = load_ways(track)
    key = (track.id, kind, data.get("fetched_at"))
    net = _NET_CACHE.get(key)
    if net is None:
        net = build_network(data, kind)
        if len(_NET_CACHE) > 8:
            _NET_CACHE.clear()
        _NET_CACHE[key] = net
    return net, {"osm_stand": data.get("fetched_at"), "radius_m": data.get("radius_m")}


# --- operations -------------------------------------------------------------

SNAP_MAX_M = 60.0


def _polyline_length(coords) -> float:
    return sum(_haversine(a[0], a[1], b[0], b[1]) for a, b in zip(coords, coords[1:]))


def route_through(net: Network, points: list) -> dict:
    """Shortest network path through the given [lat, lon] points in order.

    Every point is snapped to its nearest network node (max SNAP_MAX_M).
    Returns {coords, length_m, way_ids, snapped: [{lat, lon, abstand_m}]}.
    """
    if len(points) < 2:
        raise ValueError("Mindestens Start und Ziel")
    nodes, snapped = [], []
    for lat, lon in points:
        nid, d = net.nearest_node(lat, lon)
        if d > SNAP_MAX_M:
            raise ValueError(f"Punkt {lat:.6f},{lon:.6f} liegt {d:.0f} m vom naechsten {net.kind}-Netz entfernt (max {SNAP_MAX_M:.0f} m)")
        nodes.append(nid)
        snapped.append({"lat": net.coord[nid][0], "lon": net.coord[nid][1], "abstand_m": round(d, 1)})
    path_nodes, total, way_ids = [nodes[0]], 0.0, []
    for i, (a, b) in enumerate(zip(nodes, nodes[1:])):
        res = net.shortest_path(a, b)
        if res is None:
            raise ValueError(f"Keine Verbindung im {net.kind}-Netz zwischen Punkt {i + 1} und {i + 2}")
        seg, length, wids = res
        path_nodes.extend(seg[1:])
        total += length
        for w in wids:
            if not way_ids or way_ids[-1] != w:
                way_ids.append(w)
    coords = [list(net.coord[n]) for n in path_nodes]
    return {"coords": coords, "length_m": round(total, 1), "way_ids": way_ids, "snapped": snapped}


def _resample(coords, step_m: float) -> list:
    """Points every step_m along the polyline, always incl. first and last."""
    out = [coords[0]]
    acc = 0.0
    for a, b in zip(coords, coords[1:]):
        seg = _haversine(a[0], a[1], b[0], b[1])
        while seg > 0 and acc + seg >= step_m:
            t = (step_m - acc) / seg
            a = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
            out.append(a)
            seg = _haversine(a[0], a[1], b[0], b[1])
            acc = 0.0
        acc += seg
    if out[-1] != coords[-1]:
        out.append(coords[-1])
    return out


def deviation_stats(original, result) -> dict:
    """Distance of every original point to the result polyline (metres)."""
    if len(result) < 2 or not original:
        return {}
    lat0 = original[0][0]
    kx = 111320.0 * math.cos(math.radians(lat0))
    r = np.array([[p[1] * kx, p[0] * 111320.0] for p in result])
    a, b = r[:-1], r[1:]
    ab = b - a
    ab2 = np.maximum((ab ** 2).sum(1), 1e-9)
    ds = []
    for p in original:
        q = np.array([p[1] * kx, p[0] * 111320.0])
        t = np.clip(((q - a) * ab).sum(1) / ab2, 0, 1)
        proj = a + ab * t[:, None]
        ds.append(float(np.min(np.hypot(*(proj - q).T))))
    ds = np.array(ds)
    return {"median": round(float(np.median(ds)), 1), "p90": round(float(np.percentile(ds, 90)), 1), "max": round(float(ds.max()), 1)}


def snap_route(net: Network, coords: list, step_m: float = 50.0) -> dict:
    """Snap an existing route onto the network.

    Anchors every step_m along the route are snapped to network nodes and
    joined by shortest paths. A leg whose network path is far longer than the
    original (detour) or whose anchors are off the network is kept as drawn and
    reported as a gap, so the result never jumps to a parallel road.
    """
    if len(coords) < 2:
        raise ValueError("Route hat weniger als 2 Punkte")
    anchors = _resample(coords, step_m)
    snaps = []
    for lat, lon in anchors:
        nid, d = net.nearest_node(lat, lon)
        snaps.append((nid if d <= SNAP_MAX_M * 0.6 else None, [lat, lon]))
    out: list = []
    way_ids: list = []
    gaps: list = []
    along = 0.0
    for (na, pa), (nb, pb) in zip(snaps, snaps[1:]):
        leg_len = _haversine(pa[0], pa[1], pb[0], pb[1])
        res = net.shortest_path(na, nb) if na is not None and nb is not None else None
        if res is not None and res[1] <= max(3.0 * leg_len, leg_len + 80.0):
            seg = [list(net.coord[n]) for n in res[0]]
            for w in res[2]:
                if not way_ids or way_ids[-1] != w:
                    way_ids.append(w)
        else:
            seg = [pa, pb]
            if not gaps or gaps[-1]["bis_m"] < along - 1:
                gaps.append({"von_m": round(along), "bis_m": round(along + leg_len)})
            else:
                gaps[-1]["bis_m"] = round(along + leg_len)
        for p in seg:
            if not out or out[-1] != p:
                out.append(p)
        along += leg_len
    return {
        "coords": out,
        "length_m": round(_polyline_length(out), 1),
        "laenge_original_m": round(_polyline_length(coords), 1),
        "way_ids": way_ids,
        "luecken": gaps,
        "abweichung_m": deviation_stats(coords, out),
    }


def lines_geojson(data: dict, kind: str) -> dict:
    feats = []
    for w in data["ways"]:
        if way_kind(w["tags"]) != kind:
            continue
        t = w["tags"]
        feats.append({
            "type": "Feature",
            "id": w["id"],
            "properties": {"osm_id": w["id"], "name": t.get("name"), "art": t.get("waterway") or t.get("highway")},
            "geometry": {"type": "LineString", "coordinates": [[c[1], c[0]] for c in w["coords"]]},
        })
    return {"type": "FeatureCollection", "features": feats}
