"""Carry waypoints over when a route gets new geometry (OSM release).

Nearest-point snapping is not enough: where the old line cut straight and the
real path winds through the gorge, a marker would land on the wrong bend and
segment start/end could swap. Instead the position ALONG the route is carried
over:

1. Correspondence old -> new: walk the old line in 5 m steps and project each
   step onto the new line, only forward from the previous match (window), so
   the mapping of chainage old -> new is monotonic and order is preserved.
2. Segment markers (path-defined points) move to the mapped chainage on the
   new line, refined to the nearest point of the new line within +-WINDOW_M.
3. Story points (narration by distance along the route) get the mapped
   along_meters and the matching position on the new line.
4. POIs / screen points are real places: they stay where they are, only their
   snap entry for this route (along, offset, snapped point) is recomputed,
   searched around the mapped chainage.

Releasing back (undo) runs the same transfer in the other direction.
"""
import math
from typing import Optional

import numpy as np

STEP_M = 5.0
FORWARD_M = 250.0
WINDOW_M = 40.0
REVIEW_M = 25.0


class Polyline:
    def __init__(self, coords: list, lat0: float):
        self.coords = coords  # [[lat, lon], ...]
        self.kx = 111320.0 * math.cos(math.radians(lat0))
        self.xy = np.array([[c[1] * self.kx, c[0] * 111320.0] for c in coords])
        seg = np.hypot(*(self.xy[1:] - self.xy[:-1]).T) if len(coords) > 1 else np.zeros(0)
        self.cum = np.concatenate([[0.0], np.cumsum(seg)])
        self.length = float(self.cum[-1])

    def to_xy(self, lat: float, lon: float) -> np.ndarray:
        return np.array([lon * self.kx, lat * 111320.0])

    def to_latlon(self, p: np.ndarray) -> list:
        return [float(p[1] / 111320.0), float(p[0] / self.kx)]

    def point_at(self, s: float) -> list:
        s = min(max(s, 0.0), self.length)
        i = int(np.searchsorted(self.cum, s, side="right") - 1)
        i = min(max(i, 0), len(self.coords) - 2)
        span = self.cum[i + 1] - self.cum[i]
        t = 0.0 if span <= 0 else (s - self.cum[i]) / span
        return self.to_latlon(self.xy[i] + (self.xy[i + 1] - self.xy[i]) * t)

    def project(self, q: np.ndarray, s_lo: float = -1e18, s_hi: float = 1e18):
        """Nearest point on the line with chainage in [s_lo, s_hi]:
        (distance m, chainage m, point xy, signed lateral offset m)."""
        a, b = self.xy[:-1], self.xy[1:]
        mask = (self.cum[1:] >= s_lo) & (self.cum[:-1] <= s_hi)
        if not mask.any():
            mask = np.ones(len(a), dtype=bool)
        idx = np.nonzero(mask)[0]
        a, b = a[idx], b[idx]
        ab = b - a
        t = np.clip(((q - a) * ab).sum(1) / np.maximum((ab ** 2).sum(1), 1e-9), 0, 1)
        proj = a + ab * t[:, None]
        d = np.hypot(*(proj - q).T)
        s = self.cum[idx] + t * np.hypot(*ab.T)
        # keep inside the window even when the clamped segment pokes out of it
        inside = (s >= s_lo - 1e-6) & (s <= s_hi + 1e-6)
        if inside.any():
            d = np.where(inside, d, np.inf)
        k = int(np.argmin(d))
        side = float(np.sign(ab[k, 0] * (q[1] - a[k, 1]) - ab[k, 1] * (q[0] - a[k, 0])))
        return float(d[k]), float(s[k]), proj[k], side * float(d[k])


class Correspondence:
    """Monotonic mapping of chainage on the old line to chainage on the new.

    Dynamic Time Warping over both lines sampled every few metres: the globally
    best monotonic alignment. A greedy forward search jumped to a later part of
    the line where a winding path passes close to a switchback (up to 100 m off
    in the 2026-10-23 test); the global alignment cannot do that.
    """

    def __init__(self, old: Polyline, new: Polyline):
        step = max(STEP_M, max(old.length, new.length) / 3000.0)
        so = np.linspace(0.0, old.length, max(2, int(old.length // step) + 1))
        sn = np.linspace(0.0, new.length, max(2, int(new.length // step) + 1))
        po = np.array([old.to_xy(*old.point_at(float(x))) for x in so])
        pn = np.array([new.to_xy(*new.point_at(float(x))) for x in sn])
        n, m = len(so), len(sn)
        inf = np.inf
        D = np.empty((n, m))
        move = np.empty((n, m), dtype=np.int8)  # 0 diag, 1 from above, 2 from left
        for i in range(n):
            c = np.hypot(pn[:, 0] - po[i, 0], pn[:, 1] - po[i, 1])
            if i == 0:
                D[0] = np.cumsum(c)
                move[0] = 2
                move[0, 0] = 0
                continue
            up = D[i - 1]
            diag = np.concatenate([[inf], D[i - 1, :-1]])
            a = np.minimum(up, diag)
            from_up = up <= diag
            # D[i,j] = min(a[j], D[i,j-1]) + c[j], solved with prefix sums:
            # D[i,j] = S[j] + min_{k<=j}(a[k] - S[k-1])
            S = np.cumsum(c)
            Sprev = np.concatenate([[0.0], S[:-1]])
            best = np.minimum.accumulate(a - Sprev)
            row = S + best
            D[i] = row
            # direction: left if the running minimum did not start at j
            started_here = (a - Sprev) <= best + 1e-9
            move[i] = np.where(started_here, np.where(from_up, 1, 0), 2)
        # backtrack
        i, j = n - 1, m - 1
        pairs = [(i, j)]
        while i > 0 or j > 0:
            mv = move[i, j]
            if i == 0:
                j -= 1
            elif j == 0:
                i -= 1
            elif mv == 0:
                i, j = i - 1, j - 1
            elif mv == 1:
                i -= 1
            else:
                j -= 1
            pairs.append((i, j))
        pairs.reverse()
        # chainage on the new line per old sample: mean of its matched new samples
        acc = {}
        for a_i, b_j in pairs:
            acc.setdefault(a_i, []).append(sn[b_j])
        self.old_s = so
        self.new_s = np.maximum.accumulate(np.array([float(np.mean(acc[k])) for k in range(n)]))

    def map(self, s_old: float) -> float:
        return float(np.interp(s_old, self.old_s, self.new_s))


def _segment_of(meta: dict, route_id: int) -> Optional[dict]:
    seg = meta.get("segment")
    if isinstance(seg, dict) and seg.get("routeId") == route_id:
        return seg
    return None


def _snap_entry(meta: dict, route_id: int) -> Optional[dict]:
    snap = meta.get("snap")
    if not isinstance(snap, dict):
        return None
    for r in snap.get("routes") or []:
        if isinstance(r, dict) and r.get("route_id") == route_id:
            return r
    return None


def plan_transfer(waypoints: list, route_id: int, old_coords: list, new_coords: list) -> dict:
    """What would change for waypoints of `route_id` if its geometry went from
    old_coords to new_coords. Pure computation, nothing is written."""
    if len(old_coords) < 2 or len(new_coords) < 2:
        raise ValueError("Alte und neue Linie brauchen je mindestens 2 Punkte")
    lat0 = old_coords[0][0]
    old, new = Polyline(old_coords, lat0), Polyline(new_coords, lat0)
    corr = Correspondence(old, new)
    markers, stories, pois = [], [], []
    for w in waypoints:
        meta = w.metadata_json if isinstance(w.metadata_json, dict) else {}
        q = old.to_xy(w.latitude, w.longitude)
        if _segment_of(meta, route_id):
            # Path-defined point: carry the chainage, then settle on the new line
            _, s_old, _, _ = old.project(q)
            s_map = corr.map(s_old)
            _, s_new, p, _ = new.project(new.to_xy(*new.point_at(s_map)), s_map - WINDOW_M, s_map + WINDOW_M)
            dest = new.to_latlon(p)
            shift = float(np.hypot(*(new.to_xy(*dest) - q)))
            markers.append({
                "id": w.id, "title": meta.get("title") or _segment_of(meta, route_id).get("name"),
                "rolle": _segment_of(meta, route_id).get("role"),
                "von": [w.latitude, w.longitude], "nach": dest,
                "along_alt": round(s_old, 1), "along_neu": round(s_new, 1),
                "verschiebung_m": round(shift, 1), "pruefen": shift > REVIEW_M,
            })
        elif w.waypoint_type == "story_point" and w.route_id == route_id and isinstance(meta.get("along_meters"), (int, float)):
            s_old = float(meta["along_meters"])
            s_new = corr.map(s_old)
            dest = new.point_at(s_new)
            shift = float(np.hypot(*(new.to_xy(*dest) - q)))
            stories.append({"id": w.id, "along_alt": round(s_old, 1), "along_neu": round(s_new, 1), "nach": dest, "verschiebung_m": round(shift, 1)})
        elif _snap_entry(meta, route_id) is not None:
            entry = _snap_entry(meta, route_id)
            s_old = entry.get("along_meters")
            if not isinstance(s_old, (int, float)):
                _, s_old, _, _ = old.project(q)
            s_map = corr.map(float(s_old))
            d, s_new, p, lateral = new.project(q, s_map - 3 * WINDOW_M, s_map + 3 * WINDOW_M)
            pois.append({
                "id": w.id, "typ": w.waypoint_type, "title": meta.get("title"),
                "abstand_alt_m": round(float(entry.get("distance") or 0.0), 1), "abstand_neu_m": round(d, 1),
                "along_alt": round(float(s_old), 1), "along_neu": round(s_new, 1),
                "snapped": new.to_latlon(p), "lateral_m": round(lateral, 2),
                "pruefen": d > REVIEW_M,
            })

    def _stats(vals):
        if not vals:
            return {"anzahl": 0}
        a = np.array(vals)
        return {"anzahl": len(vals), "median": round(float(np.median(a)), 1), "max": round(float(a.max()), 1)}

    return {
        "route_id": route_id,
        "laenge_alt_m": round(old.length, 1),
        "laenge_neu_m": round(new.length, 1),
        "segmentmarker": markers,
        "story_points": stories,
        "pois": pois,
        "zusammenfassung": {
            "segmentmarker": _stats([m["verschiebung_m"] for m in markers]),
            "story_points": _stats([s["verschiebung_m"] for s in stories]),
            "pois_abstand_neu": _stats([p["abstand_neu_m"] for p in pois]),
            "zu_pruefen": sum(1 for m in markers if m["pruefen"]) + sum(1 for p in pois if p["pruefen"]),
        },
    }


def snapshot(waypoints_by_id: dict, plan: dict) -> list:
    """Current state of every waypoint a plan touches, for an exact undo."""
    rid = plan["route_id"]
    out = []
    for key in ("segmentmarker", "story_points", "pois"):
        for item in plan[key]:
            w = waypoints_by_id.get(item["id"])
            if not w:
                continue
            meta = w.metadata_json if isinstance(w.metadata_json, dict) else {}
            out.append({"id": w.id, "lat": w.latitude, "lon": w.longitude,
                        "along_meters": meta.get("along_meters"), "snap": _snap_entry(meta, rid)})
    return out


def restore(waypoints_by_id: dict, saved: list, route_id: int, now_iso: str) -> int:
    """Put waypoints back exactly as recorded by snapshot()."""
    changed = 0
    for item in saved:
        w = waypoints_by_id.get(item.get("id"))
        if not w:
            continue
        w.latitude, w.longitude = item["lat"], item["lon"]
        meta = dict(w.metadata_json or {})
        if item.get("along_meters") is not None:
            meta["along_meters"] = item["along_meters"]
        if item.get("snap") is not None and isinstance(meta.get("snap"), dict):
            snap = dict(meta["snap"])
            snap["routes"] = [dict(item["snap"]) if isinstance(r, dict) and r.get("route_id") == route_id else r for r in snap.get("routes") or []]
            snap["updated_at"] = now_iso
            meta["snap"] = snap
        w.metadata_json = meta
        changed += 1
    return changed


def apply_transfer(waypoints_by_id: dict, plan: dict, now_iso: str) -> int:
    """Write a plan onto the waypoint rows (caller commits). Returns rows changed."""
    rid = plan["route_id"]
    changed = 0
    for m in plan["segmentmarker"]:
        w = waypoints_by_id.get(m["id"])
        if not w:
            continue
        w.latitude, w.longitude = m["nach"]
        meta = dict(w.metadata_json or {})
        entry = _snap_entry(meta, rid)
        if entry is not None:
            snap = dict(meta["snap"])
            snap["routes"] = [dict(r, distance=0.0, lateral_offset_meters=0.0, snapped_lat=m["nach"][0], snapped_lon=m["nach"][1], along_meters=m["along_neu"])
                              if isinstance(r, dict) and r.get("route_id") == rid else r for r in snap.get("routes") or []]
            snap["updated_at"] = now_iso
            meta["snap"] = snap
        w.metadata_json = meta
        changed += 1
    for s in plan["story_points"]:
        w = waypoints_by_id.get(s["id"])
        if not w:
            continue
        w.latitude, w.longitude = s["nach"]
        meta = dict(w.metadata_json or {})
        meta["along_meters"] = s["along_neu"]
        w.metadata_json = meta
        changed += 1
    for p in plan["pois"]:
        w = waypoints_by_id.get(p["id"])
        if not w:
            continue
        meta = dict(w.metadata_json or {})
        snap = dict(meta.get("snap") or {})
        snap["routes"] = [dict(r, distance=p["abstand_neu_m"], lateral_offset_meters=p["lateral_m"], snapped_lat=p["snapped"][0], snapped_lon=p["snapped"][1], along_meters=p["along_neu"])
                          if isinstance(r, dict) and r.get("route_id") == rid else r for r in snap.get("routes") or []]
        snap["updated_at"] = now_iso
        meta["snap"] = snap
        w.metadata_json = meta
        changed += 1
    return changed
