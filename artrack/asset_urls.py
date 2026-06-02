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
    out.setdefault("thumbnail_url", _media_url(host, aid, "thumbnail"))
    return out


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
