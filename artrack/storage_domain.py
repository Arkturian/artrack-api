from __future__ import annotations

import re
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from .models import StorageObject, MediaFile, Track, TrackRoute, Waypoint
from clients.storage_client import generic_storage


def cleanup_storage_refs(db: Session, storage_id: int) -> dict[str, int]:
    """Cascade-clean every reference to a StorageObject before it's deleted.

    Background: the FK relationship between artrack and storage objects is
    not enforced everywhere — denormalized refs live in five places:

      1. media_files.storage_object_id        (relational FK)
      2. tracks.storage_object_ids            (JSON list)
      3. track_routes.storage_object_ids      (JSON list)
      4. waypoints.metadata_json.assets       (JSON list of ints or {id,role})
      5. waypoints.metadata_json.{thumbnail_url, file_url, hls_url}
         (URL strings containing the storage id segment)

    Deleting a storage object without sweeping these leaves dangling
    references that surface as 404s in consuming apps (POIs without icons,
    screen_points without videos). This helper is the cascade glue.

    Call BEFORE ``db.delete(storage_object)`` — both should happen in the
    same transaction so the cleanup either commits whole or rolls back.

    Returns a summary dict so callers can log / return the impact:
        {media_files, tracks, track_routes, waypoints_assets, waypoints_urls}
    """
    summary = {
        "media_files": 0,
        "tracks": 0,
        "track_routes": 0,
        "waypoints_assets": 0,
        "waypoints_urls": 0,
        "waypoints_audio_cues": 0,
    }

    # 1. media_files: delete junction rows
    summary["media_files"] = (
        db.query(MediaFile)
        .filter(MediaFile.storage_object_id == storage_id)
        .delete(synchronize_session=False)
    )

    # 2-3. JSON lists on tracks + track_routes
    for model, field in [(Track, "tracks"), (TrackRoute, "track_routes")]:
        for row in db.query(model).all():
            ids = getattr(row, "storage_object_ids", None) or []
            if storage_id in ids:
                row.storage_object_ids = [i for i in ids if i != storage_id]
                flag_modified(row, "storage_object_ids")
                summary[field] += 1

    # 4-5. waypoints.metadata_json — assets[] and URL fields
    url_pat = re.compile(rf"/storage/(?:media|objects)/{storage_id}(?:[?/&#]|$)")

    def _asset_id(a):
        if isinstance(a, int):
            return a
        if isinstance(a, dict):
            return a.get("id")
        return None

    for wp in db.query(Waypoint).all():
        md = wp.metadata_json or {}
        changed = False

        assets = md.get("assets") or []
        new_assets = [a for a in assets if _asset_id(a) != storage_id]
        if len(new_assets) != len(assets):
            md["assets"] = new_assets
            changed = True
            summary["waypoints_assets"] += 1

        for fld in ("thumbnail_url", "file_url", "hls_url"):
            v = md.get(fld)
            if isinstance(v, str) and url_pat.search(v):
                md[fld] = None
                changed = True
                summary["waypoints_urls"] += 1

        # 6. TTS audio cues — knowledge.<slot>.cues[].audio_storage_id
        kn = md.get("knowledge")
        if isinstance(kn, dict):
            for slot in ("approaching", "at_poi"):
                blk = kn.get(slot)
                if not isinstance(blk, dict):
                    continue
                for cue in (blk.get("cues") or []):
                    if isinstance(cue, dict) and cue.get("audio_storage_id") == storage_id:
                        cue["audio_storage_id"] = None
                        changed = True
                        summary["waypoints_audio_cues"] += 1

        if changed:
            wp.metadata_json = md
            flag_modified(wp, "metadata_json")

    return summary


def find_storage_refs(db: Session, storage_id: int) -> dict:
    """Read-only: who references this StorageObject? For a delete-cascade
    workflow's 'who is affected' step BEFORE the bytes are removed (e.g. sWFME).

    Mirrors cleanup_storage_refs' five+1 ref locations but mutates nothing.
    Returns {storage_id, total, waypoints:[{id,track_id,via}], tracks:[id],
    track_routes:[id], media_files:[id]}.
    """
    url_pat = re.compile(rf"/storage/(?:media|objects)/{storage_id}(?:[?/&#]|$)")

    def _asset_id(a):
        if isinstance(a, int):
            return a
        if isinstance(a, dict):
            return a.get("id")
        return None

    wp_hits = []
    for wp in db.query(Waypoint).all():
        md = wp.metadata_json or {}
        via = []
        if any(_asset_id(a) == storage_id for a in (md.get("assets") or [])):
            via.append("assets")
        for fld in ("thumbnail_url", "file_url", "hls_url"):
            v = md.get(fld)
            if isinstance(v, str) and url_pat.search(v):
                via.append(fld)
        kn = md.get("knowledge")
        if isinstance(kn, dict):
            for slot in ("approaching", "at_poi"):
                blk = kn.get(slot)
                if isinstance(blk, dict) and any(
                    isinstance(c, dict) and c.get("audio_storage_id") == storage_id
                    for c in (blk.get("cues") or [])
                ):
                    via.append(f"knowledge.{slot}.cues")
        if via:
            wp_hits.append({"id": wp.id, "track_id": wp.track_id, "via": via})

    track_hits = [t.id for t in db.query(Track).all()
                  if storage_id in (getattr(t, "storage_object_ids", None) or [])]
    route_hits = [r.id for r in db.query(TrackRoute).all()
                  if storage_id in (getattr(r, "storage_object_ids", None) or [])]
    media_hits = [m.id for m in db.query(MediaFile).filter(
        MediaFile.storage_object_id == storage_id).all()]

    total = len(wp_hits) + len(track_hits) + len(route_hits) + len(media_hits)
    return {
        "storage_id": storage_id,
        "total": total,
        "waypoints": wp_hits,
        "tracks": track_hits,
        "track_routes": route_hits,
        "media_files": media_hits,
    }


async def save_file_and_record(
    db: Session,
    *,
    owner_user_id: int,
    data: bytes,
    original_filename: str,
    context: Optional[str] = None,
    is_public: bool = False,
    collection_id: Optional[str] = None,
    link_id: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    storage_mode: str = "copy",
    reference_path: Optional[str] = None,
    ai_context_metadata: Optional[dict] = None,
) -> StorageObject:
    """Save file via GenericStorageService and create a StorageObject row.

    Args:
        storage_mode: "copy" (default) saves file to storage, "reference" only creates DB entry pointing to existing filesystem path
        reference_path: Required when storage_mode="reference" - path to existing file (e.g., "/mnt/oneal/2026/Helmets/Airframe.jpg")
        ai_context_metadata: Optional context for AI analysis (file_path, brand, year, category, etc.)

    Returns the persisted StorageObject.
    """
    # In reference mode, skip saving the original file but still generate thumbnails
    if storage_mode == "reference":
        if not reference_path:
            raise ValueError("reference_path is required when storage_mode='reference'")

        # Generate thumbnails/variants from the referenced file, but don't copy original
        saved = await generic_storage.save_reference(
            data=data,
            original_filename=original_filename,
            owner_user_id=owner_user_id,
            context=context,
            reference_path=reference_path,
        )
    else:
        # Normal copy mode - save everything
        saved = await generic_storage.save(
            data=data,
            original_filename=original_filename,
            owner_user_id=owner_user_id,
            context=context,
        )

    storage_obj = StorageObject(
        owner_user_id=owner_user_id,
        object_key=saved["object_key"],
        original_filename=saved["original_filename"],
        file_url=saved["file_url"],
        thumbnail_url=saved.get("thumbnail_url"),
        webview_url=saved.get("webview_url"),
        mime_type=saved["mime_type"],
        file_size_bytes=saved["file_size_bytes"],
        checksum=saved["checksum"],
        is_public=is_public,
        context=context,
        collection_id=collection_id,
        link_id=link_id,
        title=title,
        description=description,
        width=saved.get("width"),
        height=saved.get("height"),
        duration_seconds=saved.get("duration_seconds"),
        bit_rate=saved.get("bit_rate"),
        latitude=saved.get("latitude"),
        longitude=saved.get("longitude"),
        metadata_json={},
        storage_mode=storage_mode,
        reference_path=reference_path,
        ai_context_metadata=ai_context_metadata or {},
    )
    db.add(storage_obj)
    db.commit()
    db.refresh(storage_obj)
    return storage_obj


async def update_file_and_record(
    db: Session,
    *,
    storage_obj: StorageObject,
    data: bytes,
    context: Optional[str] = None,
) -> StorageObject:
    """Overwrite file via GenericStorageService and update StorageObject row."""
    updated = await generic_storage.update_file(storage_obj.object_key, data)

    storage_obj.file_url = updated["file_url"]
    storage_obj.thumbnail_url = updated.get("thumbnail_url")
    storage_obj.webview_url = updated.get("webview_url")
    storage_obj.mime_type = updated["mime_type"]
    storage_obj.file_size_bytes = updated["file_size_bytes"]
    storage_obj.checksum = updated["checksum"]
    storage_obj.width = updated.get("width")
    storage_obj.height = updated.get("height")
    storage_obj.duration_seconds = updated.get("duration_seconds")
    storage_obj.bit_rate = updated.get("bit_rate")
    if context is not None:
        storage_obj.context = context
    db.commit()
    db.refresh(storage_obj)
    return storage_obj
