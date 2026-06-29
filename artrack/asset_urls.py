"""
Asset URL resolution for waypoint metadata.

Waypoint assets are stored as bare references in
``waypoint.metadata_json["assets"]`` — e.g. ``[{"id": 108866, "role": "main"}]``.
Historically clients (tile-map / tschepp-ar-web, guide-api, dashboard) built the
storage URL themselves by hard-coding the storage host. That breaks the moment an
asset is migrated to a different storage host (arkturian -> arkserver).

This module resolves a full, host-correct ``file_url`` (+ ``thumbnail_url``) per
asset, so clients can consume the URL 1:1 and never construct a host. The host is
taken from a per-asset ``storage_host`` marker; assets without one fall back to
``settings.STORAGE_DEFAULT_HOST`` (arkturian today), so existing tracks are
unaffected — the enrichment is purely additive.

Single source of truth used by waypoints/detail, get_waypoint_detail and
context-at.
"""
from typing import Any, Optional

from .config import settings


def _media_url(host: str, asset_id: Any, variant: Optional[str] = None) -> str:
    base = f"{host.rstrip('/')}/storage/media/{asset_id}"
    if variant == "thumbnail":
        # format=jpg is REQUIRED for video assets (the storage endpoint extracts a
        # frame; without it videos return the raw stream / content-length 0 and the
        # client's loadImage fails). It is harmless for images (forces a jpg thumb).
        return f"{base}?variant=thumbnail&format=jpg"
    return f"{base}?variant={variant}" if variant else base


def enrich_asset(asset: dict) -> dict:
    """Return a copy of one asset dict with resolved file_url + thumbnail_url.

    - Keeps id/role and any existing keys untouched (backward compatible).
    - Host = asset["storage_host"] if present, else STORAGE_DEFAULT_HOST.
    - Does not overwrite a file_url/thumbnail_url that is already set.
    - Leaves the asset unchanged if it has no usable id.
    """
    if not isinstance(asset, dict):
        return asset
    aid = asset.get("id")
    if aid is None:
        return dict(asset)
    out = dict(asset)
    host = out.get("storage_host") or settings.STORAGE_DEFAULT_HOST
    out.setdefault("file_url", _media_url(host, aid))
    # thumbnail_url is ALWAYS recomputed from storage_host (not setdefault): stored
    # thumbnail_urls were frequently host-stale and/or missing format=jpg (videos
    # then 404/empty → broken thumbs). Recomputing makes them host-correct + jpg.
    out["thumbnail_url"] = _media_url(host, aid, "thumbnail")
    return out


def resolve_audio_url(audio_storage_id: Any, storage_host: Optional[str] = None) -> Optional[str]:
    """Resolve a host-correct media URL for a TTS audio cue.

    Audio cues live in knowledge.<slot>.cues[].audio_storage_id (not in assets[]),
    so they need their own resolver. An optional per-cue ``audio_storage_host``
    marks a migrated audio; without it we fall back to STORAGE_DEFAULT_HOST
    (arkturian today), which is correct as long as the audio still lives there
    (copy-not-move migration keeps the original reachable).
    """
    if audio_storage_id is None:
        return None
    host = storage_host or settings.STORAGE_DEFAULT_HOST
    return _media_url(host, audio_storage_id)


def enrich_assets_in_metadata(metadata: Optional[dict]) -> Optional[dict]:
    """Return a shallow copy of waypoint metadata_json with metadata["assets"]
    asset entries enriched (file_url/thumbnail_url). No-op if there are no
    dict-shaped assets. Non-dict asset entries (legacy bare-int) are left as-is.
    """
    if not isinstance(metadata, dict):
        return metadata
    assets = metadata.get("assets")
    if not isinstance(assets, list) or not assets:
        return metadata
    new_assets = [enrich_asset(a) if isinstance(a, dict) else a for a in assets]
    out = dict(metadata)
    out["assets"] = new_assets
    return out


# --- HLS resolution for video assets ----------------------------------------
# The storage media endpoint serves the raw mp4 (file_url) AND exposes a
# transcoded HLS manifest via the ``X-HLS-URL`` response header (when
# ``X-Transcoding-Status: completed``). The HLS URL is an opaque transcoded path,
# NOT derivable from the asset id, so we read it from the header. Cached per asset.
import time as _time
_HLS_CACHE: dict = {}            # asset_id(int) -> (expiry_epoch, hls_url_or_None)
_HLS_TTL = 600                    # 10 min


async def resolve_hls_url(asset_id: Any, storage_host: Optional[str] = None) -> Optional[str]:
    """Return the asset's transcoded HLS manifest URL (X-HLS-URL header) if
    transcoding is completed, else None. Cached with a TTL; never raises."""
    if asset_id is None:
        return None
    try:
        now = _time.time()
        ent = _HLS_CACHE.get(asset_id)
        if ent and ent[0] > now:
            return ent[1]
        hls = None
        try:
            import httpx
            host = (storage_host or settings.STORAGE_DEFAULT_HOST).rstrip("/")
            url = f"{host}/storage/media/{asset_id}"
            async with httpx.AsyncClient(timeout=2.5) as client:
                # range GET (cheap) — the X-HLS-URL header rides on the media response
                r = await client.get(url, headers={"Range": "bytes=0-1"})
                if str(r.headers.get("x-transcoding-status", "")).lower() == "completed":
                    h = r.headers.get("x-hls-url")
                    if h:
                        hls = h
        except Exception:
            hls = None
        _HLS_CACHE[asset_id] = (now + _HLS_TTL, hls)
        return hls
    except Exception:
        return None


async def attach_hls_to_assets(metadata: Optional[dict]) -> Optional[dict]:
    """In-place: set ``hls_url`` on each video asset (role=='video' or mime
    video/*) in metadata['assets'] when a transcoded HLS manifest exists. Pass an
    already-enriched metadata dict (assets carry storage_host/file_url)."""
    if not isinstance(metadata, dict):
        return metadata
    assets = metadata.get("assets")
    if not isinstance(assets, list):
        return metadata
    for a in assets:
        if not isinstance(a, dict) or a.get("id") is None or a.get("hls_url"):
            continue
        is_video = a.get("role") == "video" or str(a.get("mime_type") or "").startswith("video")
        if not is_video:
            continue
        hls = await resolve_hls_url(a["id"], a.get("storage_host"))
        if hls:
            a["hls_url"] = hls
    return metadata
