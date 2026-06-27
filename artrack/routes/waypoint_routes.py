from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Header, BackgroundTasks, Query
from fastapi.responses import JSONResponse
import logging
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy import or_, select
from typing import List, Optional
from datetime import datetime, timedelta
import uuid
import asyncio
import httpx
import base64

from ..database import get_db
from ..models import (
    Track, Waypoint, MediaFile, WaypointCreate, WaypointBatch,
    WaypointBatchResponse, WaypointCreateResponse, UploadSession,
    MediaUploadUrl, WaypointStatusResponse, MediaFileResponse,
    MediaAnalysis, User, WaypointDetailResponse, WaypointListItem, WaypointLocation, SimpleUserRef, StorageObject
)
from ..auth import get_current_user
from ..asset_urls import enrich_assets_in_metadata
from ..services.track_bbox import _haversine_m
from artrack.storage_domain import save_file_and_record
from clients.storage_client import generic_storage, enqueue_ai_safety_and_transcoding
from ..analysis import analysis_service
from pydantic import BaseModel
from typing import Optional, List
import os
from ..config import settings
import sqlite3

router = APIRouter()
logger = logging.getLogger("artrack.waypoints")

# --- Per-asset storage host auto-resolution -------------------------------
# Some assets live ONLY on the alternate (arkserver) storage and are NOT in this
# service's storage_objects mirror, so we cannot resolve their host from the DB.
# When a client attaches such an asset without a storage_host marker, enrichment
# would fall back to the default (arkturian) host and 404. As a best-effort, we
# HEAD-probe the alternate host once (cached) so consumers don't have to know
# where an asset lives. Graceful: any failure → no stamp → default behavior.
import time as _time
_ASSET_HOST_CACHE: dict = {}            # asset_id(int) -> (expiry_epoch, host_or_None)
_ASSET_HOST_TTL = 600                    # 10 min
_STORAGE_ALT_HOST = os.getenv("ARTRACK_STORAGE_ALT_HOST", "https://api-storage.arkserver.arkturian.com")

async def _resolve_asset_host(asset_id: int) -> Optional[str]:
    """Return the alternate storage host if the asset is served there (HTTP 200),
    else None. Cached with a TTL; never raises."""
    try:
        now = _time.time()
        ent = _ASSET_HOST_CACHE.get(asset_id)
        if ent and ent[0] > now:
            return ent[1]
        host = None
        try:
            url = f"{_STORAGE_ALT_HOST}/storage/media/{asset_id}?variant=thumbnail&format=jpg"
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.head(url)
                if r.status_code == 200:
                    host = _STORAGE_ALT_HOST
        except Exception:
            host = None
        _ASSET_HOST_CACHE[asset_id] = (now + _ASSET_HOST_TTL, host)
        return host
    except Exception:
        return None

def _is_admin(user: "User") -> bool:
    try:
        return getattr(user, "trust_level", None) in ("admin", "moderator")
    except Exception:
        return False

@router.post("/{track_id}/waypoints", response_model=WaypointBatchResponse)
async def create_waypoints(
    track_id: int,
    waypoint_batch: WaypointBatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create waypoints for a track"""
    
    # Verify track exists and user has permission
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    results = []
    
    for waypoint_data in waypoint_batch.waypoints:
        # Check if waypoint already exists
        existing_waypoint = db.query(Waypoint).filter(
            Waypoint.client_waypoint_id == waypoint_data.client_waypoint_id,
            Waypoint.track_id == track_id
        ).first()
        
        if existing_waypoint:
            # Return existing waypoint
            result = WaypointCreateResponse(
                client_waypoint_id=waypoint_data.client_waypoint_id,
                waypoint_id=existing_waypoint.id,
                status="existing"
            )
        else:
            # Create new waypoint
            db_waypoint = Waypoint(
                client_waypoint_id=waypoint_data.client_waypoint_id,
                track_id=track_id,
                latitude=waypoint_data.latitude,
                longitude=waypoint_data.longitude,
                altitude=waypoint_data.altitude,
                accuracy=waypoint_data.accuracy,
                recorded_at=waypoint_data.recorded_at,
                user_description=waypoint_data.user_description,
                processing_state="pending",
                waypoint_type=waypoint_data.waypoint_type,
                metadata_json=waypoint_data.metadata_json or {},
                segment_id=waypoint_data.segment_id
            )
            
            db.add(db_waypoint)
            db.commit()
            db.refresh(db_waypoint)
            
            # Create upload session if media is expected
            upload_session = None
            if waypoint_data.media_count > 0:
                session_id = str(uuid.uuid4())
                expires_at = datetime.utcnow() + timedelta(hours=2)
                
                # Create upload URLs for each media slot
                media_upload_urls = []
                for slot in range(waypoint_data.media_count):
                    upload_url = f"/artrack/upload/{session_id}/{slot}"
                    media_upload_urls.append(MediaUploadUrl(
                        media_slot=slot,
                        upload_url=upload_url,
                        max_size_bytes=50 * 1024 * 1024  # 50MB
                    ))
                
                upload_session = UploadSession(
                    session_id=session_id,
                    media_upload_urls=media_upload_urls,
                    expires_at=expires_at
                )
            
            result = WaypointCreateResponse(
                client_waypoint_id=waypoint_data.client_waypoint_id,
                waypoint_id=db_waypoint.id,
                status="created",
                upload_session=upload_session
            )
        
        results.append(result)
    
    # Update track waypoint count
    track.total_waypoints = db.query(Waypoint).filter(Waypoint.track_id == track_id).count()
    track.updated_at = datetime.utcnow()
    db.commit()
    
    return WaypointBatchResponse(results=results)

@router.get("/waypoints/{waypoint_id}/status", response_model=WaypointStatusResponse)
async def get_waypoint_status(
    waypoint_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get waypoint processing status"""
    
    waypoint = db.query(Waypoint).filter(Waypoint.id == waypoint_id).first()
    if not waypoint:
        raise HTTPException(status_code=404, detail="Waypoint not found")
    
    # Check permissions
    track = db.query(Track).filter(Track.id == waypoint.track_id).first()
    if track.created_by != current_user.id and track.visibility == "private" and current_user.trust_level not in ("admin", "moderator"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get media files and their analysis results
    media_files = db.query(MediaFile).filter(MediaFile.waypoint_id == waypoint_id).all()

    media_responses = []
    for media_file in media_files:
        # Get analysis result
        from ..models import AnalysisResult
        analysis_result = db.query(AnalysisResult).filter(
            AnalysisResult.media_file_id == media_file.id
        ).first()
        
        analysis = None
        if analysis_result:
            analysis = MediaAnalysis(
                description=analysis_result.description,
                categories=analysis_result.categories,
                safety_rating=analysis_result.safety_rating,
                quality_score=analysis_result.quality_score,
                confidence=analysis_result.confidence
            )
        
        media_response = MediaFileResponse(
            media_id=media_file.id,
            type=media_file.media_type,
            processing_state=media_file.processing_state,
            analysis=analysis,
            thumbnail_url=media_file.thumbnail_url,
            url=media_file.file_url,
            storage_object_id=getattr(media_file, 'storage_object_id', None)
        )
        media_responses.append(media_response)
    
    safe_processing = waypoint.processing_state or "pending"
    safe_moderation = waypoint.moderation_status or "pending"
    return WaypointStatusResponse(
        waypoint_id=waypoint.id,
        processing_state=safe_processing,
        media=media_responses,
        moderation_status=safe_moderation,
        published_at=waypoint.updated_at if safe_processing == "published" else None,
        metadata_json=waypoint.metadata_json,
    )

@router.get("/tracks/{track_id}/waypoints/detail", response_model=List[WaypointDetailResponse])
async def list_waypoints_detail(
    track_id: int,
    segment_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = 200,
    offset: int = 0
):
    """Return full waypoint details for a given track (coords, times, media summary)."""
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    # Admins may access any track; otherwise enforce privacy
    if not _is_admin(current_user):
        if track.created_by != current_user.id and track.visibility == "private" and current_user.trust_level not in ("admin", "moderator"):
            raise HTTPException(status_code=403, detail="Access denied")

    query = db.query(Waypoint).filter(Waypoint.track_id == track_id)
    if segment_id is not None:
        query = query.filter(Waypoint.segment_id == segment_id)
    waypoints = query.offset(offset).limit(limit).all()
    result: list[WaypointDetailResponse] = []
    for wp in waypoints:
        media_files = db.query(MediaFile).filter(MediaFile.waypoint_id == wp.id).all()
        media = [
            {
                "media_id": mf.id,
                "type": mf.media_type,
                "processing_state": mf.processing_state,
                "thumbnail_url": mf.thumbnail_url,
                "url": mf.file_url,
                "storage_object_id": getattr(mf, 'storage_object_id', None)
            } for mf in media_files
        ]
        result.append(WaypointDetailResponse(
            id=wp.id,
            track_id=wp.track_id,
            latitude=wp.latitude,
            longitude=wp.longitude,
            altitude=wp.altitude,
            accuracy=wp.accuracy,
            recorded_at=wp.recorded_at,
            user_description=wp.user_description,
            processing_state=wp.processing_state,
            moderation_status=wp.moderation_status,
            waypoint_type=wp.waypoint_type,
            metadata_json=enrich_assets_in_metadata(wp.metadata_json),
            segment_id=wp.segment_id,
            priority=getattr(wp, 'priority', None),
            media=[MediaFileResponse(
                media_id=m["media_id"],
                type=m["type"],
                processing_state=m["processing_state"],
                analysis=None,
                thumbnail_url=m.get("thumbnail_url"),
                url=m.get("url")
            , storage_object_id=m.get("storage_object_id")) for m in media]
        ))
    return result

@router.get("/tracks/{track_id}/narration-points")
async def list_narration_points(
    track_id: int,
    generation_id: str | None = None,
    all_generations: bool = Query(False, alias="all"),
    near: str | None = Query(None, description="geo filter 'lat,lon' — returns points within radius_m, sorted by distance"),
    radius_m: float | None = None,
    image_status: str | None = Query(None, description="filter by metadata_json.image_status: provisional|approved|rejected"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read the narration corpus for a track (Phase-2 consumer read path).

    narration_point waypoints are excluded from the bot-facing selector
    (get_pois_near -> pois-near/context-at/pretty) to avoid an Output->Input
    loop, so the corpus is read HERE instead. Server-side filtered by type
    (+ optional generation_id) and sorted by order_id — no pagination over the
    full waypoint set (which is why a plain waypoints/detail?limit=N can miss
    high-id narration_points on a large track).

    Read modes:
    - no params  → LATEST generation only (one coherent corpus; right for a
      single sim-run track like Track 30).
    - ?generation_id=<id> → exactly that batch.
    - ?all=true  (or ?generation_id=all) → the FULL accumulated corpus across
      ALL generations, sorted chronologically (insertion order). Needed for the
      World track, which accumulates narrations over many sessions — latest-only
      would truncate it. Per-batch generation_id is preserved either way, so the
      by-generation wipe can still drop a single bad batch selectively.
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if not _is_admin(current_user):
        if track.created_by != current_user.id and track.visibility == "private" and current_user.trust_level not in ("admin", "moderator"):
            raise HTTPException(status_code=403, detail="Access denied")

    wps = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type == "narration_point",
    ).all()
    # geo-near: parse "lat,lon" → distance filter (replaces interactions/nearby).
    near_lat = near_lon = None
    if near:
        try:
            _p = near.split(",")
            near_lat = float(_p[0].strip()); near_lon = float(_p[1].strip())
        except Exception:
            raise HTTPException(status_code=422, detail="near must be 'lat,lon'")
    geo_mode = near_lat is not None and near_lon is not None
    # all-mode: every generation (accumulating corpus). geo-near implies all-gens
    # (search the whole corpus in radius). Sentinel generation_id="all" also works.
    # An image_status filter WITHOUT an explicit generation_id also implies all-gens:
    # status filtering means "all approved/provisional/rejected across the corpus"
    # (Stufe-2 backstop, frontend gallery) — scoping it to just the latest generation
    # would silently miss points in older generations on an accumulating track.
    want_all = (
        all_generations
        or geo_mode
        or (image_status is not None and generation_id is None)
        or (generation_id is not None and str(generation_id).lower() == "all")
    )
    # No generation_id (and not all/geo) → default to the LATEST generation
    # (most recently persisted; waypoint id is monotonic with insertion order).
    effective_gen = None if want_all else generation_id
    if not want_all and effective_gen is None and wps:
        latest_wp = max(wps, key=lambda w: w.id)
        effective_gen = (latest_wp.metadata_json or {}).get("generation_id")
    out = []
    for wp in wps:
        meta = wp.metadata_json or {}
        if effective_gen is not None and str(meta.get("generation_id")) != str(effective_gen):
            continue
        if image_status is not None and str(meta.get("image_status") or "") != image_status:
            continue
        dist = None
        if geo_mode:
            dist = _haversine_m(near_lat, near_lon, wp.latitude, wp.longitude)
            if radius_m is not None and dist > radius_m:
                continue
        rec = {
            "id": wp.id,
            "lat": wp.latitude,
            "lon": wp.longitude,
            "order_id": meta.get("order_id"),
            "generation_id": meta.get("generation_id"),
            "text": meta.get("text"),
            "title": meta.get("title"),
            "subtitle": meta.get("subtitle"),
            "narrator": meta.get("narrator"),
            "audio_storage_id": meta.get("audio_storage_id"),
            "image_status": meta.get("image_status"),
            "recorded_at": wp.recorded_at,
            "metadata_json": enrich_assets_in_metadata(meta),
        }
        if dist is not None:
            rec["distance_m"] = round(dist, 1)
        if isinstance(meta.get("settings"), dict) and meta.get("settings"):
            rec["settings"] = meta["settings"]
        out.append(rec)
    if geo_mode:
        out.sort(key=lambda x: x.get("distance_m") if x.get("distance_m") is not None else float("inf"))
    elif want_all:
        # chronological across generations (each batch persisted in order_id order)
        out.sort(key=lambda x: x["id"])
    else:
        out.sort(key=lambda x: x["order_id"] if isinstance(x.get("order_id"), (int, float)) else float("inf"))
    return {
        "track_id": track_id,
        "generation_id": (None if want_all else effective_gen),
        "all": want_all,
        "is_latest": (not want_all and generation_id is None),
        "near": ([near_lat, near_lon] if geo_mode else None),
        "radius_m": radius_m if geo_mode else None,
        "image_status": image_status,
        "count": len(out),
        "narration_points": out,
    }


@router.get("/tracks/{track_id}/narration-generations")
async def list_narration_generations(
    track_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List the narration-corpus generations of a track (review/tooling).

    Returns one entry per generation_id with count + latest waypoint id/time,
    sorted newest-first. Lets a UI pick a generation without scanning all points.
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if not _is_admin(current_user):
        if track.created_by != current_user.id and track.visibility == "private" and current_user.trust_level not in ("admin", "moderator"):
            raise HTTPException(status_code=403, detail="Access denied")

    wps = db.query(Waypoint).filter(
        Waypoint.track_id == track_id,
        Waypoint.waypoint_type == "narration_point",
    ).all()
    gens: dict = {}
    for wp in wps:
        g = (wp.metadata_json or {}).get("generation_id")
        if g is None:
            continue
        e = gens.setdefault(str(g), {"generation_id": str(g), "count": 0, "latest_id": 0, "latest_recorded_at": None})
        e["count"] += 1
        if wp.id > e["latest_id"]:
            e["latest_id"] = wp.id
            e["latest_recorded_at"] = wp.recorded_at
    out = sorted(gens.values(), key=lambda x: x["latest_id"], reverse=True)
    return {"track_id": track_id, "count": len(out), "generations": out}

@router.post("/upload/{session_id}/complete")
async def complete_upload_session(
    session_id: str,
    waypoint_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Complete upload session and finalize waypoint"""
    
    waypoint = db.query(Waypoint).filter(Waypoint.id == waypoint_id).first()
    if not waypoint:
        raise HTTPException(status_code=404, detail="Waypoint not found")
    
    track = db.query(Track).filter(Track.id == waypoint.track_id).first()
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get all media files for this upload session
    media_files = db.query(MediaFile).filter(
        MediaFile.waypoint_id == waypoint_id,
        MediaFile.upload_session_id == session_id
    ).all()
    
    # Get analysis jobs for these media files
    analysis_jobs = []
    for media_file in media_files:
        from ..models import AnalysisJob
        job = db.query(AnalysisJob).filter(
            AnalysisJob.media_file_id == media_file.id
        ).first()
        if job:
            analysis_jobs.append({
                "job_id": job.job_id,
                "media_id": media_file.id,
                "type": job.analysis_type,
                "estimated_completion": datetime.utcnow() + timedelta(seconds=30)
            })
    
    # Update waypoint processing state
    if media_files:
        waypoint.processing_state = "analysing"
    else:
        waypoint.processing_state = "published"
        waypoint.moderation_status = "approved"
    
    db.commit()
    
    return {
        "waypoint_id": waypoint.id,
        "status": "media_uploaded",
        "analysis_jobs": analysis_jobs
    }

@router.post("/upload/{session_id}/{media_slot}")
async def upload_media_file(
    session_id: str,
    media_slot: int,
    file: UploadFile = File(...),
    waypoint_id: int = Form(...),
    media_type: str = Form(...),
    metadata_json: Optional[str] = Form(None),
    x_content_hash: Optional[str] = Header(None, alias="X-Content-Hash"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    background_tasks: BackgroundTasks = None
):
    """Upload media file for a waypoint"""
    
    # Verify waypoint exists and user has permission
    waypoint = db.query(Waypoint).filter(Waypoint.id == waypoint_id).first()
    if not waypoint:
        raise HTTPException(status_code=404, detail="Waypoint not found")
    
    track = db.query(Track).filter(Track.id == waypoint.track_id).first()
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        # Read file content
        file_content = await file.read()
        
        # Compute SHA-256 content hash for idempotency if provided via header
        from hashlib import sha256
        content_hash_header = x_content_hash or None
        if not content_hash_header:
            try:
                computed_hash = sha256(file_content).hexdigest()
                content_hash_header = computed_hash
            except Exception:
                content_hash_header = None

        # If hash present, check for existing media on this waypoint
        if content_hash_header:
            # Same-waypoint idempotency
            existing = db.query(MediaFile).filter(
                MediaFile.waypoint_id == waypoint_id,
                MediaFile.content_hash == content_hash_header
            ).first()
            if existing:
                return {
                    "media_id": existing.id,
                    "status": "uploaded",
                    "processing_state": existing.processing_state or "pending_analysis"
                }
            # Cross-waypoint conflict
            cross = db.query(MediaFile).filter(
                MediaFile.content_hash == content_hash_header,
                MediaFile.waypoint_id != waypoint_id
            ).first()
            if cross:
                raise HTTPException(status_code=409, detail={
                    "existing_media_id": cross.id,
                    "waypoint_id": cross.waypoint_id
                })

        # Save media file via shared storage helper (creates StorageObject)
        storage_obj = await save_file_and_record(
            db,
            owner_user_id=current_user.id,
            data=file_content,
            original_filename=file.filename,
            context=f"waypoint_{waypoint_id}",
            is_public=False,
        )
        # Enqueue AI safety and transcoding jobs for all supported media types
        await enqueue_ai_safety_and_transcoding(storage_obj.id)

        # --- Robust GLB→USDZ queued retry when Mac is offline ---
        async def _mac_available(timeout: float = 3.0) -> bool:
            try:
                async with httpx.AsyncClient(timeout=timeout) as c:
                    r = await c.get("http://arkturian.com:8087/health")
                    return r.status_code == 200 and (r.json().get("status") == "healthy")
            except Exception:
                return False

        async def _convert_glb_with_retry(src_url: str, reference_id: str):
            backoff = (60, 300, 900, 3600)
            idx = 0
            while True:
                if await _mac_available():
                    try:
                        async with httpx.AsyncClient(timeout=600.0) as c:
                            payload = {"download_url": src_url, "reference_id": reference_id}
                            await c.post("http://arkturian.com:8087/convert_glb", json=payload)
                    except Exception:
                        pass
                    return
                wait_s = backoff[min(idx, len(backoff)-1)]
                await asyncio.sleep(wait_s)
                idx += 1

        # Fire-and-forget background task only for GLB/GLTF
        if (storage_obj.original_filename or "").lower().endswith((".glb", ".gltf")) and background_tasks is not None:
            background_tasks.add_task(_convert_glb_with_retry, storage_obj.file_url, str(storage_obj.id))

        # --- Robust HLS video transcoding with retry ---
        async def _start_hls_with_retry(src_url: str, original_filename: str, file_size_bytes: int, storage_object_id: str):
            backoff = (60, 300, 900, 3600)
            idx = 0
            async def _mac_available(timeout: float = 3.0) -> bool:
                try:
                    async with httpx.AsyncClient(timeout=timeout) as c:
                        r = await c.get("http://arkturian.com:8087/health")
                        return r.status_code == 200 and (r.json().get("status") == "healthy")
                except Exception:
                    return False
            while True:
                if await _mac_available():
                    try:
                        payload = {
                            "job_id": str(uuid.uuid4()),
                            "source_url": src_url,
                            "callback_url": "https://api.arkturian.com/transcode/callback",  # legacy, not used
                            "file_size_bytes": int(file_size_bytes or 0),
                            "original_filename": original_filename,
                            "storage_object_id": storage_object_id,
                        }
                        async with httpx.AsyncClient(timeout=600.0) as c:
                            await c.post("http://arkturian.com:8087/transcode", json=payload)
                    except Exception:
                        pass
                    return
                wait_s = backoff[min(idx, len(backoff)-1)]
                await asyncio.sleep(wait_s)
                idx += 1

        # Video? schedule HLS transcoding retry flow
        _name_lower = (storage_obj.original_filename or "").lower()
        _mime = (storage_obj.mime_type or "").lower()
        if background_tasks is not None and (
            _mime.startswith("video/") or _name_lower.endswith((".mp4", ".mov", ".m4v"))
        ):
            background_tasks.add_task(
                _start_hls_with_retry,
                storage_obj.file_url,
                storage_obj.original_filename or "video.mp4",
                int(storage_obj.file_size_bytes or 0),
                str(storage_obj.id)
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            conn.close()
        except Exception:
            pass
        
@router.post("/admin/retry_hls/{storage_id}")
async def admin_retry_hls(storage_id: int, background_tasks: BackgroundTasks):
    """Force HLS retry for an existing storage object id (admin tool)."""
    # Lookup storage object directly via DB (consistent with upload_results usage)
    try:
        db_path = "/var/lib/api-arkturian/artrack.db"
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, original_filename, file_url, file_size_bytes
            FROM storage_objects
            WHERE id = ?
            LIMIT 1
            """,
            (storage_id,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="storage object not found")
        _id, _name, _url, _size = row
        if not _url:
            raise HTTPException(status_code=400, detail="storage object missing file_url")

        async def _mac_available(timeout: float = 3.0) -> bool:
            try:
                async with httpx.AsyncClient(timeout=timeout) as c:
                    r = await c.get("http://arkturian.com:8087/health")
                    return r.status_code == 200 and (r.json().get("status") == "healthy")
            except Exception:
                return False

        async def _start_hls_with_retry_once(src_url: str, original_filename: str, file_size_bytes: int, storage_object_id: str):
            if await _mac_available():
                try:
                    payload = {
                        "job_id": str(uuid.uuid4()),
                        "source_url": src_url,
                        "callback_url": "https://api.arkturian.com/transcode/callback",
                        "file_size_bytes": int(file_size_bytes or 0),
                        "original_filename": original_filename,
                        "storage_object_id": storage_object_id,
                    }
                    async with httpx.AsyncClient(timeout=600.0) as c:
                        await c.post("http://arkturian.com:8087/transcode", json=payload)
                except Exception:
                    pass
                return
            # Fallback to scheduled retries
            backoff = (60, 300, 900, 3600)
            idx = 0
            while True:
                if await _mac_available():
                    try:
                        payload = {
                            "job_id": str(uuid.uuid4()),
                            "source_url": src_url,
                            "callback_url": "https://api.arkturian.com/transcode/callback",
                            "file_size_bytes": int(file_size_bytes or 0),
                            "original_filename": original_filename,
                            "storage_object_id": storage_object_id,
                        }
                        async with httpx.AsyncClient(timeout=600.0) as c:
                            await c.post("http://arkturian.com:8087/transcode", json=payload)
                    except Exception:
                        pass
                    return
                wait_s = backoff[min(idx, len(backoff)-1)]
                await asyncio.sleep(wait_s)
                idx += 1

        background_tasks.add_task(_start_hls_with_retry_once, _url, _name or "video.mp4", int(_size or 0), str(_id))
        return {"status": "queued", "storage_id": storage_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/waypoints/", response_model=List[WaypointListItem])
async def list_waypoints(
    track_id: int = None,
    segment_id: int = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = 50,
    offset: int = 0
):
    """List waypoints"""
    
    query = db.query(Waypoint)
    
    if track_id:
        # Verify track access
        track = db.query(Track).filter(Track.id == track_id).first()
        if not track:
            raise HTTPException(status_code=404, detail="Track not found")
        # Admins may access any track; otherwise enforce privacy
        if not _is_admin(current_user):
            if track.created_by != current_user.id and track.visibility == "private" and current_user.trust_level not in ("admin", "moderator"):
                raise HTTPException(status_code=403, detail="Access denied")
        
        query = query.filter(Waypoint.track_id == track_id)
    if segment_id is not None:
        query = query.filter(Waypoint.segment_id == segment_id)
    else:
        # Only show waypoints from user's own tracks or public tracks
        # Use explicit select() to avoid SAWarning about coercing Subquery into select()
        if not _is_admin(current_user):
            user_tracks = select(Track.id).where(Track.created_by == current_user.id)
            public_tracks = select(Track.id).where(Track.visibility == "public")
            query = query.filter(
                or_(
                    Waypoint.track_id.in_(user_tracks),
                    Waypoint.track_id.in_(public_tracks)
                )
            )
    
    try:
        waypoints = query.offset(offset).limit(limit).all()
    except Exception:
        logger.exception("list_waypoints: query failed")
        return []

    # Convert to response format
    waypoint_responses: list[WaypointListItem] = []
    for waypoint in waypoints:
        try:
            media_files = db.query(MediaFile).filter(MediaFile.waypoint_id == waypoint.id).all()
        except Exception:
            logger.exception("list_waypoints: media query failed for waypoint_id=%s", waypoint.id)
            media_files = []

        media_responses = []
        for media_file in media_files:
            try:
                from ..models import AnalysisResult
                analysis_result = db.query(AnalysisResult).filter(
                    AnalysisResult.media_file_id == media_file.id
                ).first()
            except Exception:
                analysis_result = None
            analysis = None
            if analysis_result:
                try:
                    analysis = MediaAnalysis(
                        description=analysis_result.description,
                        categories=analysis_result.categories,
                        safety_rating=analysis_result.safety_rating,
                        quality_score=analysis_result.quality_score,
                        confidence=analysis_result.confidence
                    )
                except Exception:
                    analysis = None

            media_response = MediaFileResponse(
                media_id=media_file.id,
                type=media_file.media_type,
                processing_state=media_file.processing_state,
                analysis=analysis,
                thumbnail_url=media_file.thumbnail_url,
                url=media_file.file_url,
                storage_object_id=getattr(media_file, 'storage_object_id', None)
            )
            media_responses.append(media_response)

        safe_processing = waypoint.processing_state or "pending"
        safe_moderation = waypoint.moderation_status or "pending"
        # Populate extended fields used by the dashboard table
        try:
            track = db.query(Track).filter(Track.id == waypoint.track_id).first()
        except Exception:
            track = None
        # Resolve simple creator reference
        creator_ref: SimpleUserRef | None = None
        try:
            if waypoint.created_by:
                u = db.query(User).filter(User.id == waypoint.created_by).first()
                if u:
                    creator_ref = SimpleUserRef(id=u.id, display_name=u.display_name)
        except Exception:
            creator_ref = None
        waypoint_response = WaypointListItem(
            waypoint_id=waypoint.id,
            processing_state=safe_processing,
            media=media_responses,
            moderation_status=safe_moderation,
            published_at=(waypoint.updated_at or datetime.utcnow()) if safe_processing == "published" else None,
            track_id=waypoint.track_id,
            track_name=(track.name if track and getattr(track, 'name', None) else None),
            creator_id=waypoint.created_by,
            creator=creator_ref,
            location=WaypointLocation(latitude=waypoint.latitude, longitude=waypoint.longitude) if waypoint.latitude is not None and waypoint.longitude is not None else None,
            created_at=waypoint.created_at,
            media_count=len(media_responses),
            metadata_json=waypoint.metadata_json,
            waypoint_type=waypoint.waypoint_type,
            segment_id=getattr(waypoint, 'segment_id', None),
            priority=getattr(waypoint, 'priority', None),
        )
        waypoint_responses.append(waypoint_response)

    return waypoint_responses

class WaypointUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    priority: Optional[float] = None  # -1.0 to 1.0, higher = more important
    metadata_json: Optional[dict] = None  # Full metadata override (merges with existing)

# --- Chunked Uploads (optional) ---

class ChunkInitRequest(BaseModel):
    waypoint_id: int
    media_type: str
    size_bytes: int
    metadata_json: Optional[dict] = None

class ChunkInitResponse(BaseModel):
    upload_id: str

@router.post("/upload/{session_id}/{media_slot}/init", response_model=ChunkInitResponse)
async def init_chunked_upload(
    session_id: str,
    media_slot: int,
    body: ChunkInitRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    waypoint = db.query(Waypoint).filter(Waypoint.id == body.waypoint_id).first()
    if not waypoint:
        raise HTTPException(status_code=404, detail="Waypoint not found")
    track = db.query(Track).filter(Track.id == waypoint.track_id).first()
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Create temp directory for parts
    chunk_dir = settings.CHUNK_UPLOAD_DIR
    os.makedirs(chunk_dir, exist_ok=True)
    upload_id = f"{session_id}_{media_slot}_{uuid.uuid4()}"
    meta_path = os.path.join(chunk_dir, f"{upload_id}.json")
    with open(meta_path, "w") as f:
        import json
        json.dump({
            "waypoint_id": body.waypoint_id,
            "media_type": body.media_type,
            "size_bytes": body.size_bytes,
            "metadata_json": body.metadata_json or {}
        }, f)
    return ChunkInitResponse(upload_id=upload_id)

@router.put("/upload/{session_id}/{media_slot}/parts/{index}")
async def upload_chunk_part(
    session_id: str,
    media_slot: int,
    index: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    chunk_dir = settings.CHUNK_UPLOAD_DIR
    os.makedirs(chunk_dir, exist_ok=True)
    # Find upload_id by scanning metadata files that start with session-slot
    prefix = f"{session_id}_{media_slot}_"
    candidates = [fn[:-5] for fn in os.listdir(chunk_dir) if fn.startswith(prefix) and fn.endswith('.json')]
    if not candidates:
        raise HTTPException(status_code=404, detail="Upload not initialized")
    upload_id = candidates[0]
    part_path = os.path.join(chunk_dir, f"{upload_id}.part.{index}")
    # Validate Content-Range header
    content_range = file.headers.get('content-range') or file.headers.get('Content-Range')
    if not content_range:
        raise HTTPException(status_code=411, detail="Content-Range header required for chunked part")
    # Expected format: bytes start-end/total (we only validate presence and numeric parts)
    try:
        units, rng = content_range.split(' ')
        start_end, total = rng.split('/')
        start, end = start_end.split('-')
        int(start); int(end); int(total)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Content-Range format")

    content = await file.read()
    if len(content) > settings.CHUNK_PART_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"Chunk too large (>{settings.CHUNK_PART_MAX_BYTES} bytes)")
    with open(part_path, 'wb') as f:
        f.write(content)
    return JSONResponse(status_code=202, content={"status": "ok", "part": index})

class ChunkCompleteRequest(BaseModel):
    upload_id: str

@router.post("/upload/{session_id}/{media_slot}/complete-chunked")
async def complete_chunked_upload(
    session_id: str,
    media_slot: int,
    body: ChunkCompleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    chunk_dir = settings.CHUNK_UPLOAD_DIR
    meta_path = os.path.join(chunk_dir, f"{body.upload_id}.json")
    if not os.path.exists(meta_path):
        raise HTTPException(status_code=404, detail="Upload not found")
    import json
    with open(meta_path, 'r') as f:
        meta = json.load(f)
    waypoint_id = meta["waypoint_id"]
    waypoint = db.query(Waypoint).filter(Waypoint.id == waypoint_id).first()
    if not waypoint:
        raise HTTPException(status_code=404, detail="Waypoint not found")
    track = db.query(Track).filter(Track.id == waypoint.track_id).first()
    if track.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Assemble parts by index order
    part_files = sorted([fn for fn in os.listdir(chunk_dir) if fn.startswith(f"{body.upload_id}.part.")], key=lambda s: int(s.split('.')[-1]))
    data = bytearray()
    for pf in part_files:
        with open(os.path.join(chunk_dir, pf), 'rb') as f:
            data.extend(f.read())

    # Compute content hash for idempotency
    from hashlib import sha256
    content_hash_header = sha256(bytes(data)).hexdigest()

    # If existing on this waypoint, return 200; 409 if under different waypoint
    if content_hash_header:
        existing = db.query(MediaFile).filter(
            MediaFile.waypoint_id == waypoint_id,
            MediaFile.content_hash == content_hash_header
        ).first()
        if existing:
            return {
                "media_id": existing.id,
                "status": "uploaded",
                "processing_state": existing.processing_state or "pending_analysis"
            }
        cross = db.query(MediaFile).filter(
            MediaFile.content_hash == content_hash_header,
            MediaFile.waypoint_id != waypoint_id
        ).first()
        if cross:
            raise HTTPException(status_code=409, detail={
                "existing_media_id": cross.id,
                "waypoint_id": cross.waypoint_id
            })

    # Save via existing storage helper
    storage_obj = await save_file_and_record(
        db,
        owner_user_id=current_user.id,
        data=bytes(data),
        original_filename=f"chunked_{body.upload_id}",
        context=f"waypoint_{waypoint_id}",
        is_public=False,
    )
    # Enqueue AI safety and transcoding jobs for all supported media types
    await enqueue_ai_safety_and_transcoding(storage_obj.id)

    media_file = MediaFile(
        waypoint_id=waypoint_id,
        media_type=meta["media_type"],
        original_filename=storage_obj.original_filename,
        file_path=str(generic_storage.absolute_path_for_key(storage_obj.object_key)),
        file_url=storage_obj.file_url,
        thumbnail_url=storage_obj.thumbnail_url,
        file_size_bytes=storage_obj.file_size_bytes,
        mime_type=storage_obj.mime_type,
        checksum=storage_obj.checksum,
        upload_session_id=session_id,
        processing_state="uploaded",
        storage_object_id=storage_obj.id,
        metadata_json=meta.get("metadata_json") or {},
        content_hash=content_hash_header,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)

    # Cleanup parts
    for pf in part_files:
        try:
            os.remove(os.path.join(chunk_dir, pf))
        except Exception:
            pass
    try:
        os.remove(meta_path)
    except Exception:
        pass

    # Update waypoint state and kick analysis
    waypoint.processing_state = "uploaded"
    db.commit()
    job_id = await analysis_service.start_analysis_job(media_file.id, db)
    return {
        "media_id": media_file.id,
        "status": "uploaded",
        "processing_state": "pending_analysis",
        "analysis_job_id": job_id
    }

@router.get("/waypoints/{waypoint_id}", response_model=WaypointDetailResponse)
async def get_waypoint_detail(
    waypoint_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    waypoint = db.query(Waypoint).filter(Waypoint.id == waypoint_id).first()
    if not waypoint:
        raise HTTPException(status_code=404, detail="Waypoint not found")
    track = db.query(Track).filter(Track.id == waypoint.track_id).first()
    if not track or (track.created_by != current_user.id and track.visibility == "private" and current_user.trust_level not in ("admin", "moderator")):
        raise HTTPException(status_code=403, detail="Access denied")
    media_files = db.query(MediaFile).filter(MediaFile.waypoint_id == waypoint_id).all()
    media = [MediaFileResponse(media_id=m.id, type=m.media_type, processing_state=m.processing_state, thumbnail_url=m.thumbnail_url, url=m.file_url, storage_object_id=getattr(m, 'storage_object_id', None)) for m in media_files]
    return WaypointDetailResponse(
        id=waypoint.id,
        track_id=waypoint.track_id,
        latitude=waypoint.latitude,
        longitude=waypoint.longitude,
        altitude=waypoint.altitude,
        accuracy=waypoint.accuracy,
        recorded_at=waypoint.recorded_at,
        user_description=waypoint.user_description,
        processing_state=waypoint.processing_state,
        moderation_status=waypoint.moderation_status,
        waypoint_type=waypoint.waypoint_type,
        metadata_json=enrich_assets_in_metadata(waypoint.metadata_json),
        media=media
    )

# --- Per-POI generic settings (metadata_json.settings) ----------------------
# Consuming apps (tschepp-ar etc.) read a generic per-POI settings block for
# display/behaviour. Hybrid namespace: shared `display`/`audio`/`ar` blocks +
# optional per-app overrides under `apps.<app>` (app-specific overrides shared,
# merge applied consumer-side). Iteration 1: display.{pin_style,priority,
# featured,enabled,min_zoom}. metadata_json is free JSON → no migration.
_SETTINGS_PIN_STYLES = {"balloon", "teardrop", "card", "spotlight"}

def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge patch into a copy of base. Nested dicts merge key-by-key
    (so updating settings.display.pin_style preserves settings.display.priority);
    non-dict values replace. Needed because the generic metadata merge is only
    one level deep and would clobber sibling settings on a partial update."""
    out = dict(base or {})
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out

def _prune_empty(d):
    """Recursively drop None values + emptied dicts so cleared settings fields
    don't linger as null-spam (a consumer treats an absent key as 'use default').
    Lets a client clear a field by sending it as null."""
    if not isinstance(d, dict):
        return d
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            pv = _prune_empty(v)
            if pv:
                out[k] = pv
        elif v is not None:
            out[k] = v
    return out

def _validate_settings(settings) -> None:
    """Validate the parts of a settings patch that have a fixed contract.
    Only pin_style is enum-constrained; everything else is free-form. Checks both
    the shared display block and any per-app override. Raises 400 on a bad value."""
    if not isinstance(settings, dict):
        return
    blocks = []
    if isinstance(settings.get("display"), dict):
        blocks.append(settings["display"])
    apps = settings.get("apps")
    if isinstance(apps, dict):
        for app_cfg in apps.values():
            if isinstance(app_cfg, dict) and isinstance(app_cfg.get("display"), dict):
                blocks.append(app_cfg["display"])
    for blk in blocks:
        ps = blk.get("pin_style")
        if ps is not None and ps not in _SETTINGS_PIN_STYLES:
            raise HTTPException(
                status_code=400,
                detail=f"invalid display.pin_style '{ps}' — allowed: {sorted(_SETTINGS_PIN_STYLES)} or null",
            )


# Update a waypoint's user_description and metadata
@router.put("/waypoints/{waypoint_id}")
async def update_waypoint(
    waypoint_id: int,
    update: WaypointUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    waypoint = db.query(Waypoint).filter(Waypoint.id == waypoint_id).first()
    if not waypoint:
        raise HTTPException(status_code=404, detail="Waypoint not found")

    track = db.query(Track).filter(Track.id == waypoint.track_id).first()
    if not track or (track.created_by != current_user.id and track.visibility == "private" and current_user.trust_level not in ("admin", "moderator")):
        raise HTTPException(status_code=403, detail="Access denied")

    if update.description is not None:
        waypoint.user_description = update.description

    # Store title/tags in metadata_json for flexibility
    meta = dict(waypoint.metadata_json or {})  # Create new dict to ensure mutation detection
    if update.title is not None:
        meta["title"] = update.title
    if update.tags is not None:
        meta["tags"] = update.tags
    # Merge metadata_json if provided (deep merge for nested objects like segment)
    if update.metadata_json is not None:
        incoming = dict(update.metadata_json)
        # Defensive: preserve each asset's storage_host across an assets-list
        # replacement. Clients (dashboard editor, MCP waypoint_update) frequently
        # resend assets as {id, role} WITHOUT the host marker; without this carry-
        # over the merge would strip storage_host and migrated (arkserver) assets
        # would 404 again. Match by id and only fill when the incoming entry omits it.
        if isinstance(incoming.get("assets"), list):
            old_hosts = {
                a["id"]: a["storage_host"]
                for a in (meta.get("assets") or [])
                if isinstance(a, dict) and a.get("id") is not None and a.get("storage_host")
            }
            for a in incoming["assets"]:
                if isinstance(a, dict) and a.get("id") in old_hosts and not a.get("storage_host"):
                    a["storage_host"] = old_hosts[a["id"]]
            # Genuinely new asset with no host and no prior record → best-effort
            # probe the alternate (arkserver) host so consumers needn't know it.
            for a in incoming["assets"]:
                if isinstance(a, dict) and a.get("id") is not None and not a.get("storage_host"):
                    try:
                        resolved = await _resolve_asset_host(int(a["id"]))
                    except Exception:
                        resolved = None
                    if resolved:
                        a["storage_host"] = resolved
        # Validate the settings patch (pin_style enum) before merging.
        if "settings" in incoming:
            _validate_settings(incoming.get("settings"))
        for key, value in incoming.items():
            if key == "settings" and isinstance(value, dict) and isinstance(meta.get(key), dict):
                # Recursive partial-merge so a client can update one nested
                # settings field without clobbering its siblings.
                meta[key] = _deep_merge(meta[key], value)
            elif isinstance(value, dict) and isinstance(meta.get(key), dict):
                # Deep merge for nested dicts (e.g., segment, snap)
                meta[key] = {**meta[key], **value}
            else:
                meta[key] = value
        # Prune null/empty out of settings so cleared fields disappear (no null-spam)
        if isinstance(meta.get("settings"), dict):
            pruned = _prune_empty(meta["settings"])
            if pruned:
                meta["settings"] = pruned
            else:
                meta.pop("settings", None)
    waypoint.metadata_json = meta
    flag_modified(waypoint, "metadata_json")  # Explicitly mark JSON field as modified for SQLAlchemy

    # Priority (-1.0 to 1.0)
    if update.priority is not None:
        waypoint.priority = max(-1.0, min(1.0, update.priority))  # Clamp to valid range

    waypoint.updated_at = datetime.utcnow()
    db.commit()

    return {"message": "Waypoint updated"}

# --- Attach existing storage objects (media) to a waypoint ---
class StorageAttachRequest(BaseModel):
    storageIds: List[int]
    mediaType: Optional[str] = None  # photo, audio, video (optional hint)

@router.post("/waypoints/{waypoint_id}/attach-storage", response_model=dict)
async def attach_storage_to_waypoint(
    waypoint_id: int,
    body: StorageAttachRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    waypoint = db.query(Waypoint).filter(Waypoint.id == waypoint_id).first()
    if not waypoint:
        raise HTTPException(status_code=404, detail="Waypoint not found")
    track = db.query(Track).filter(Track.id == waypoint.track_id).first()
    if not track or (track.created_by != current_user.id and track.visibility == "private" and current_user.trust_level not in ("admin", "moderator")):
        raise HTTPException(status_code=403, detail="Access denied")
    attached = 0
    skipped = 0
    for sid in (body.storageIds or []):
        try:
            so = db.query(StorageObject).filter(StorageObject.id == int(sid)).first()
            if not so:
                skipped += 1
                continue
            # Skip if already attached
            existing = db.query(MediaFile).filter(MediaFile.waypoint_id == waypoint_id, MediaFile.storage_object_id == so.id).first()
            if existing:
                skipped += 1
                continue
            mf = MediaFile(
                waypoint_id=waypoint_id,
                media_type=body.mediaType or (so.mime_type.split('/')[0] if (so.mime_type or '').find('/')>0 else 'photo'),
                original_filename=so.original_filename,
                file_path=str(generic_storage.absolute_path_for_key(so.object_key)) if getattr(so, 'object_key', None) else None,
                file_url=so.file_url,
                thumbnail_url=so.thumbnail_url,
                file_size_bytes=so.file_size_bytes,
                mime_type=so.mime_type,
                checksum=so.checksum,
                processing_state="uploaded",
                storage_object_id=so.id,
                metadata_json={}
            )
            db.add(mf)
            attached += 1
        except Exception:
            skipped += 1
    db.commit()
    return {"attached": attached, "skipped": skipped}

# Delete a waypoint and its media records (files left intact for now)
@router.delete("/waypoints/{waypoint_id}")
async def delete_waypoint(
    waypoint_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    waypoint = db.query(Waypoint).filter(Waypoint.id == waypoint_id).first()
    if not waypoint:
        raise HTTPException(status_code=404, detail="Waypoint not found")

    track = db.query(Track).filter(Track.id == waypoint.track_id).first()
    if not track or (track.created_by != current_user.id and track.visibility == "private" and current_user.trust_level not in ("admin", "moderator")):
        raise HTTPException(status_code=403, detail="Access denied")

    # Capture before delete (waypoint is expired after commit).
    track_id = waypoint.track_id

    # Delete media file records referencing the waypoint
    db.query(MediaFile).filter(MediaFile.waypoint_id == waypoint_id).delete()
    db.delete(waypoint)
    db.commit()

    # Producer-cascade: announce the deletion on the IACP event-bus so swfme-api
    # triggers CascadeDeleteWaypoint → Knowledge cleans its dangling
    # locations[].waypoint_id refs. Fire-and-forget: must never break the delete.
    try:
        from ..event_bus import publish_event
        await publish_event(
            "artrack.waypoint_deleted",
            {"waypoint_id": waypoint_id, "tenant_id": "arkturian", "track_id": track_id},
        )
    except Exception:
        logging.getLogger("artrack.waypoints").debug(
            "waypoint_deleted event publish skipped (non-fatal)", exc_info=True
        )

    return {"message": "Waypoint deleted"}

@router.delete("/tracks/{track_id}/waypoints/bulk")
async def bulk_delete_waypoints(
    track_id: int,
    min_id: int | None = None,
    ids: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Bulk delete waypoints for a track.
    - If `ids` is provided (comma-separated), deletes those IDs (only if they belong to track_id)
    - Else if `min_id` is provided, deletes all waypoints with id >= min_id belonging to track_id
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id and track.visibility == "private" and current_user.trust_level not in ("admin", "moderator"):
        raise HTTPException(status_code=403, detail="Access denied")

    q = db.query(Waypoint).filter(Waypoint.track_id == track_id)
    targets = []
    if ids:
        try:
            id_list = [int(x) for x in ids.split(',') if x.strip()]
        except Exception:
            raise HTTPException(status_code=400, detail="ids must be comma-separated integers")
        targets = q.filter(Waypoint.id.in_(id_list)).all()
    elif min_id is not None:
        targets = q.filter(Waypoint.id >= int(min_id)).all()
    else:
        raise HTTPException(status_code=400, detail="Provide ids=... or min_id=")

    deleted = 0
    for w in targets:
        db.query(MediaFile).filter(MediaFile.waypoint_id == w.id).delete()
        db.delete(w)
        deleted += 1
    db.commit()
    return {"deleted": deleted}

@router.delete("/tracks/{track_id}/waypoints/by-generation/{generation_id}")
async def delete_waypoints_by_generation(
    track_id: int,
    generation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Wipe a whole narration-corpus generation (Phase B).

    Deletes all narration_point waypoints of the given generation_id on the track
    (+ their MediaFile rows). Scoped STRICTLY to waypoint_type=='narration_point',
    so it can never touch curated POIs/screen_points. Returns the freed TTS
    audio_storage_ids so the caller sees what is now unreferenced. The audio
    storage objects are not hard-deleted here yet (narration audio is a follow-up;
    audio_storage_id is null today) — the storage-delete of freed audios gets
    wired + verified once TTS audios exist (Alex: audio-mitlöschen = JA).
    """
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    if track.created_by != current_user.id and track.visibility == "private" and current_user.trust_level not in ("admin", "moderator"):
        raise HTTPException(status_code=403, detail="Access denied")

    targets = [
        wp for wp in db.query(Waypoint).filter(
            Waypoint.track_id == track_id,
            Waypoint.waypoint_type == "narration_point",
        ).all()
        if str((wp.metadata_json or {}).get("generation_id")) == str(generation_id)
    ]
    freed_audio = []
    deleted = 0
    for wp in targets:
        aid = (wp.metadata_json or {}).get("audio_storage_id")
        if aid is not None:
            freed_audio.append(aid)
        db.query(MediaFile).filter(MediaFile.waypoint_id == wp.id).delete()
        db.delete(wp)
        deleted += 1
    db.commit()
    logging.getLogger("artrack.narration").info(
        "by-generation wipe track=%s gen=%s deleted=%s freed_audio=%s",
        track_id, generation_id, deleted, freed_audio,
    )
    return {
        "track_id": track_id,
        "generation_id": generation_id,
        "deleted": deleted,
        "freed_audio_storage_ids": freed_audio,
    }