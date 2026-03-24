from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Header, BackgroundTasks
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
    if track.created_by != current_user.id and track.visibility == "private":
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
        if track.created_by != current_user.id and track.visibility == "private":
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
            metadata_json=wp.metadata_json,
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
            if track.created_by != current_user.id and track.visibility == "private":
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
    if not track or (track.created_by != current_user.id and track.visibility == "private"):
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
        metadata_json=waypoint.metadata_json,
        media=media
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
    if not track or (track.created_by != current_user.id and track.visibility == "private"):
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
        for key, value in update.metadata_json.items():
            if isinstance(value, dict) and isinstance(meta.get(key), dict):
                # Deep merge for nested dicts (e.g., segment, snap)
                meta[key] = {**meta[key], **value}
            else:
                meta[key] = value
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
    if not track or (track.created_by != current_user.id and track.visibility == "private"):
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
    if not track or (track.created_by != current_user.id and track.visibility == "private"):
        raise HTTPException(status_code=403, detail="Access denied")

    # Delete media file records referencing the waypoint
    db.query(MediaFile).filter(MediaFile.waypoint_id == waypoint_id).delete()
    db.delete(waypoint)
    db.commit()

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
    if track.created_by != current_user.id and track.visibility == "private":
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