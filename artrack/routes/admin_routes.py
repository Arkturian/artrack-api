from fastapi import APIRouter, Depends, HTTPException, status
import logging
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from sqlalchemy import func, desc
from pathlib import Path
from pydantic import BaseModel
import os

from ..database import get_db
from ..models import User, Track, Waypoint, MediaFile, AnalysisJob, StorageObject
from ..auth import get_current_user
from ..storage_domain import cleanup_storage_refs, find_storage_refs
from clients.storage_client import generic_storage

router = APIRouter()
logger = logging.getLogger("artrack.admin")


def _ensure_admin(user: User):
    if user.trust_level not in ("admin", "moderator"):
        raise HTTPException(status_code=403, detail="Admin privileges required")


# ==========================
# Pydantic response models
# ==========================

class AdminOverview(BaseModel):
    active_users_24h: int
    total_tracks: int
    tracks_today: int
    total_waypoints: int
    waypoints_today: int
    storage_used_bytes: int
    processing_queue_count: int
    last_updated: datetime


class AdminUserItem(BaseModel):
    id: int
    email: str
    display_name: Optional[str]
    trust_level: str
    created_at: datetime
    last_active_at: Optional[datetime] = None
    track_count: int
    waypoint_count: int
    storage_used_bytes: int


class AdminUsersResponse(BaseModel):
    total: int
    items: List[AdminUserItem]


class AdminTrackItem(BaseModel):
    id: int
    name: str
    owner_id: int
    visibility: str
    track_type: Optional[str] = None
    created_at: datetime
    waypoint_count: int
    collaborator_count: int


class AdminTracksResponse(BaseModel):
    total: int
    items: List[AdminTrackItem]


class ModerationQueueItem(BaseModel):
    id: int
    track_id: int
    recorded_at: datetime
    processing_state: Optional[str] = None
    moderation_status: Optional[str] = None


class ModerationQueueResponse(BaseModel):
    total: int
    items: List[ModerationQueueItem]


class ModerationResult(BaseModel):
    status: str
    waypoint_id: int
    action: str
    reason: Optional[str] = None


class SystemHealth(BaseModel):
    db_ok: bool
    storage_ok: bool
    ai_services_ok: bool
    time: datetime


class ActivityUserRef(BaseModel):
    id: Optional[int] = None
    display_name: Optional[str] = None


class ActivityTrackRef(BaseModel):
    id: int
    name: Optional[str] = None


class ActivityItem(BaseModel):
    id: str
    timestamp: datetime
    type: str
    user: Optional[ActivityUserRef] = None
    track: Optional[ActivityTrackRef] = None
    details: Dict[str, Any] = {}


class AnalyticsSeriesPoint(BaseModel):
    date: str
    users: int
    tracks: int
    waypoints: int


class AnalyticsTrackTypeItem(BaseModel):
    name: str
    value: int
    color: str


class AnalyticsStoragePoint(BaseModel):
    date: str
    storage: float


class AnalyticsUsage(BaseModel):
    series: List[AnalyticsSeriesPoint]
    trackTypes: List[AnalyticsTrackTypeItem]
    storage: List[AnalyticsStoragePoint]


class PerformanceMetrics(BaseModel):
    avg_response_time: Optional[float] = None
    uptime_percentage: Optional[float] = None
    error_rate: Optional[float] = None
    requests_per_hour: Optional[int] = None


class ErrorLog(BaseModel):
    id: int
    timestamp: Optional[str] = None
    level: str
    message: str
    endpoint: Optional[str] = None


class MessageResponse(BaseModel):
    message: str


@router.get("/stats/overview", response_model=AdminOverview)
def admin_overview_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    _ensure_admin(current_user)

    now = datetime.utcnow()
    day_ago = now - timedelta(days=1)
    today = datetime(now.year, now.month, now.day)

    active_users_24h = db.query(User).filter(User.last_active_at >= day_ago).count()
    total_tracks = db.query(Track).count()
    tracks_today = db.query(Track).filter(Track.created_at >= today).count()
    total_waypoints = db.query(Waypoint).count()
    waypoints_today = db.query(Waypoint).filter(Waypoint.created_at >= today).count()
    storage_used_bytes = db.query(func.sum(MediaFile.file_size_bytes)).scalar() or 0
    processing_queue_count = db.query(AnalysisJob).filter(AnalysisJob.status.in_(["pending","processing"])) .count()

    return {
        "active_users_24h": active_users_24h,
        "total_tracks": total_tracks,
        "tracks_today": tracks_today,
        "total_waypoints": total_waypoints,
        "waypoints_today": waypoints_today,
        "storage_used_bytes": storage_used_bytes,
        "processing_queue_count": processing_queue_count,
        "last_updated": now,
    }


@router.get("/users", response_model=AdminUsersResponse)
def admin_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    _ensure_admin(current_user)
    query = db.query(User)
    if q:
        like = f"%{q}%"
        query = query.filter((User.email.like(like)) | (User.display_name.like(like)))
    total = query.count()
    users = query.order_by(User.created_at.desc()).limit(limit).offset(offset).all()

    # Aggregate per user
    data = []
    for u in users:
        track_count = db.query(Track).filter(Track.created_by == u.id).count()
        waypoint_count = db.query(Waypoint).join(Track).filter(Track.created_by == u.id).count()
        storage_used = db.query(func.sum(MediaFile.file_size_bytes)).join(Waypoint).join(Track).filter(Track.created_by == u.id).scalar() or 0
        data.append({
            "id": u.id,
            "email": u.email,
            "display_name": u.display_name,
            "trust_level": u.trust_level,
            "created_at": u.created_at,
            "last_active_at": u.last_active_at,
            "track_count": track_count,
            "waypoint_count": waypoint_count,
            "storage_used_bytes": storage_used,
        })

    return {"total": total, "items": data}


@router.get("/tracks", response_model=AdminTracksResponse)
def admin_tracks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    visibility: Optional[str] = None,
    track_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    _ensure_admin(current_user)
    try:
        query = db.query(Track)
        if visibility:
            query = query.filter(Track.visibility == visibility)
        if track_type:
            query = query.filter(Track.track_type == track_type)
        total = query.count()
        tracks = query.order_by(Track.created_at.desc()).limit(limit).offset(offset).all()

        items = []
        for t in tracks:
            try:
                wp_count = db.query(Waypoint).filter(Waypoint.track_id == t.id).count()
            except Exception:
                logger.exception("admin_tracks: waypoint count failed for track_id=%s", t.id)
                wp_count = 0
            try:
                # Safe collaborator count even if relationship not loaded or misconfigured
                collab_count = len(getattr(t, "collaborators", []) or [])
            except Exception:
                logger.exception("admin_tracks: collaborator count failed for track_id=%s", t.id)
                collab_count = 0
            # Guard against nulls to avoid response_model validation 500s
            items.append({
                "id": t.id,
                "name": t.name or "",
                "owner_id": t.created_by or 0,
                "visibility": t.visibility or "private",
                "track_type": t.track_type,
                "created_at": t.created_at or datetime.utcnow(),
                "waypoint_count": wp_count,
                "collaborator_count": collab_count,
            })

        return {"total": total, "items": items}
    except Exception:
        logger.exception("admin_tracks: unhandled error")
        return {"total": 0, "items": []}


@router.get("/waypoints/moderation", response_model=ModerationQueueResponse)
def admin_moderation_queue(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    status: str = "pending",
    limit: int = 50,
    offset: int = 0,
):
    _ensure_admin(current_user)
    query = db.query(Waypoint).filter(Waypoint.moderation_status == status)
    total = query.count()
    wps = query.order_by(Waypoint.created_at.desc()).limit(limit).offset(offset).all()
    items = [
        {
            "id": w.id,
            "track_id": w.track_id,
            "recorded_at": w.recorded_at,
            "processing_state": w.processing_state,
            "moderation_status": w.moderation_status,
        }
        for w in wps
    ]
    return {"total": total, "items": items}


@router.post("/waypoints/{waypoint_id}/moderate", response_model=ModerationResult)
def admin_moderate_waypoint(
    waypoint_id: int,
    action: str,  # "approve" | "reject" | "quarantine"
    reason: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _ensure_admin(current_user)
    wp = db.query(Waypoint).filter(Waypoint.id == waypoint_id).first()
    if not wp:
        raise HTTPException(status_code=404, detail="Waypoint not found")
    if action == "approve":
        wp.moderation_status = "approved"
    elif action == "reject":
        wp.moderation_status = "rejected"
    elif action == "quarantine":
        wp.moderation_status = "auto_quarantine"
    else:
        raise HTTPException(status_code=400, detail="Invalid action")
    wp.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok", "waypoint_id": waypoint_id, "action": action, "reason": reason}


@router.get("/system/health", response_model=SystemHealth)
def admin_system_health(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _ensure_admin(current_user)
    # Basic DB/Storage/AI health checks (expand as needed)
    db_ok = True
    try:
        db.query(User).first()
    except Exception:
        db_ok = False
    return {
        "db_ok": db_ok,
        "storage_ok": True,  # TODO: check uploads dir / bucket
        "ai_services_ok": True,  # TODO: ping analysis_service if async worker present
        "time": datetime.utcnow(),
    }


# === New endpoints expected by Admin Dashboard ===

@router.get("/activity/feed", response_model=List[ActivityItem])
def admin_activity_feed(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return a mixed feed of recent activities (waypoints, tracks, users)."""
    _ensure_admin(current_user)

    events: List[Dict[str, Any]] = []

    # Waypoint created
    wps = (
        db.query(Waypoint)
        .order_by(Waypoint.created_at.desc())
        .limit(limit)
        .all()
    )
    for w in wps:
        events.append({
            "id": f"wp_{w.id}",
            "timestamp": (w.created_at or w.updated_at or datetime.utcnow()),
            "type": "waypoint_created",
            "user": {"id": w.created_by if hasattr(w, "created_by") else None, "display_name": ""},
            "track": {"id": w.track_id, "name": ""},
            "details": {"waypoint_id": w.id}
        })

    # Track created
    tracks = (
        db.query(Track)
        .order_by(Track.created_at.desc())
        .limit(limit)
        .all()
    )
    for t in tracks:
        events.append({
            "id": f"tr_{t.id}",
            "timestamp": (t.created_at or datetime.utcnow()),
            "type": "track_created",
            "user": {"id": t.created_by, "display_name": ""},
            "track": {"id": t.id, "name": t.name},
            "details": {}
        })

    # User registered
    users = (
        db.query(User)
        .order_by(User.created_at.desc())
        .limit(limit)
        .all()
    )
    for u in users:
        events.append({
            "id": f"us_{u.id}",
            "timestamp": (u.created_at or datetime.utcnow()),
            "type": "user_registered",
            "user": {"id": u.id, "display_name": u.display_name or u.email},
            "details": {}
        })

    # Sort by timestamp desc and limit
    def _parse_ts(e):
        ts = e["timestamp"]
        return ts if isinstance(ts, datetime) else datetime.utcnow()

    events.sort(key=_parse_ts, reverse=True)
    return events[:limit]


@router.get("/analytics/usage", response_model=AnalyticsUsage)
def admin_analytics_usage(
    timeframe: str = "7d",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return usage analytics series and breakdowns for charts.

    Response shape:
    {
      "series": [ { "date": "YYYY-MM-DD", "users": int, "tracks": int, "waypoints": int } ],
      "trackTypes": [ { "name": str, "value": int, "color": str } ],
      "storage": [ { "date": "YYYY-MM-DD", "storage": float } ]  # GB
    }
    """
    _ensure_admin(current_user)

    # Determine date range
    days = 7 if timeframe == "7d" else 1 if timeframe == "1d" else 30 if timeframe == "30d" else 90
    now = datetime.utcnow()
    start = now - timedelta(days=days)

    # Helper for day string
    def day_col(col):
        return func.date(col)  # works for SQLite/Postgres

    # Users per day
    users_q = (
        db.query(day_col(User.created_at).label("d"), func.count(User.id))
        .filter(User.created_at >= start)
        .group_by("d")
        .order_by("d")
        .all()
    )
    users_by_day = {d: c for d, c in users_q}

    # Tracks per day
    tracks_q = (
        db.query(day_col(Track.created_at).label("d"), func.count(Track.id))
        .filter(Track.created_at >= start)
        .group_by("d")
        .order_by("d")
        .all()
    )
    tracks_by_day = {d: c for d, c in tracks_q}

    # Waypoints per day
    wps_q = (
        db.query(day_col(Waypoint.created_at).label("d"), func.count(Waypoint.id))
        .filter(Waypoint.created_at >= start)
        .group_by("d")
        .order_by("d")
        .all()
    )
    wps_by_day = {d: c for d, c in wps_q}

    # Build continuous series across the date range
    series: List[Dict[str, Any]] = []
    for i in range(days + 1):
        d = (start + timedelta(days=i)).date().isoformat()
        series.append({
            "date": d,
            "users": int(users_by_day.get(d, 0)),
            "tracks": int(tracks_by_day.get(d, 0)),
            "waypoints": int(wps_by_day.get(d, 0)),
        })

    # Track type distribution
    type_rows = (
        db.query(Track.track_type, func.count(Track.id))
        .group_by(Track.track_type)
        .all()
    )
    palette = ["#8884d8", "#82ca9d", "#ffc658", "#ff7300", "#00bcd4", "#9c27b0"]
    trackTypes = [
        {"name": (t or "unknown"), "value": c, "color": palette[i % len(palette)]}
        for i, (t, c) in enumerate(type_rows)
    ]

    # Storage growth (sum of media sizes per day)
    storage_rows = (
        db.query(day_col(MediaFile.created_at).label("d"), func.sum(MediaFile.file_size_bytes))
        .group_by("d")
        .order_by("d")
        .all()
    )
    storage = [
        {"date": d, "storage": round(((s or 0) / (1024 * 1024 * 1024)), 3)}  # GB
        for d, s in storage_rows
    ]

    return {"series": series, "trackTypes": trackTypes, "storage": storage}


@router.get("/analytics/performance", response_model=PerformanceMetrics)
def admin_analytics_performance(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return performance metrics. If unavailable, return null values so UI shows N/A."""
    _ensure_admin(current_user)
    return {
        "avg_response_time": None,
        "uptime_percentage": None,
        "error_rate": None,
        "requests_per_hour": None,
    }


@router.get("/logs/errors", response_model=List[ErrorLog])
def admin_logs_errors(
    limit: int = 50,
    current_user: User = Depends(get_current_user),
):
    """Return recent error/warning log lines from api.log if present."""
    _ensure_admin(current_user)
    candidates = [
        Path(os.getcwd()) / "api.log",
        Path(__file__).resolve().parents[2] / "api.log",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if not path:
        return []
    lines: List[str] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    # Filter recent ERROR/WARNING-like lines
    filtered = [
        {
            "id": i,
            "timestamp": "",  # timestamp parsing depends on log format; leave empty
            "level": ("error" if ("ERROR" in ln or "Error" in ln) else "warning" if ("WARN" in ln or "Warning" in ln) else "info"),
            "message": ln.strip(),
            "endpoint": "",
        }
        for i, ln in enumerate(reversed(lines))
        if any(k in ln for k in ["ERROR", "Error", "WARN", "Warning"])
    ]
    return filtered[:limit]


@router.delete("/tracks/{track_id}", response_model=MessageResponse)
def admin_delete_track(
    track_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a track as admin (bypasses ownership)."""
    _ensure_admin(current_user)
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    # TODO: cascade delete related entities if needed
    db.delete(track)
    db.commit()
    return {"message": "Track deleted"}


# Storage administration
class AdminStorageItem(BaseModel):
    id: int
    owner_user_id: int
    object_key: str
    original_filename: str
    mime_type: str
    file_size_bytes: int
    is_public: bool
    context: Optional[str] = None
    download_count: int
    created_at: datetime


class AdminStorageResponse(BaseModel):
    total: int
    items: List[AdminStorageItem]


@router.get("/storage", response_model=AdminStorageResponse)
def admin_storage_list(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    q: Optional[str] = None,
    owner_id: Optional[int] = None,
    context: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """List stored files across all users for administration."""
    _ensure_admin(current_user)
    query = db.query(StorageObject)
    if owner_id:
        query = query.filter(StorageObject.owner_user_id == owner_id)
    if context:
        query = query.filter(StorageObject.context == context)
    if q:
        like = f"%{q}%"
        query = query.filter((StorageObject.original_filename.like(like)) | (StorageObject.object_key.like(like)))
    total = query.count()
    rows = query.order_by(StorageObject.created_at.desc()).limit(limit).offset(offset).all()
    items = [
        AdminStorageItem(
            id=r.id,
            owner_user_id=r.owner_user_id,
            object_key=r.object_key,
            original_filename=r.original_filename,
            mime_type=r.mime_type,
            file_size_bytes=r.file_size_bytes,
            is_public=r.is_public,
            context=r.context,
            download_count=r.download_count or 0,
            created_at=r.created_at,
        ) for r in rows
    ]
    return {"total": total, "items": items}


@router.delete("/storage/{object_id}", response_model=MessageResponse)
def admin_storage_delete(
    object_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a storage object and its file (admin-only). Also cancels Mac transcoding if active."""
    _ensure_admin(current_user)
    obj = db.query(StorageObject).filter(StorageObject.id == object_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Storage object not found")
    
    # Cancel Mac transcoding job if exists
    mac_job_cancelled = False
    if obj.metadata_json and obj.metadata_json.get('mac_job_id'):
        mac_job_id = obj.metadata_json.get('mac_job_id')
        print(f"🛑 Attempting to cancel Mac transcoding job: {mac_job_id}")
        try:
            from mac_transcoding_client import mac_transcoding_client
            if mac_transcoding_client.is_available():
                cancel_result = mac_transcoding_client.cancel_job(mac_job_id)
                if cancel_result:
                    print(f"✅ Mac transcoding job {mac_job_id} cancelled")
                    mac_job_cancelled = True
                else:
                    print(f"⚠️ Failed to cancel Mac job {mac_job_id} (may already be finished)")
            else:
                print(f"⚠️ Mac API not available to cancel job {mac_job_id}")
        except Exception as e:
            print(f"❌ Error cancelling Mac transcoding job {mac_job_id}: {e}")
    
    # Delete HLS files if they exist
    hls_deleted = False
    if obj.hls_url:
        try:
            # Extract job ID from HLS URL to delete HLS directory
            # HLS URL format: https://api.arkturian.com/uploads/storage/media/{job_id}/master.m3u8
            import re
            match = re.search(r'/media/([^/]+)/master\.m3u8', obj.hls_url)
            if match:
                hls_job_id = match.group(1)
                hls_dir = f"/mnt/backup-disk/uploads/media/{hls_job_id}"
                import shutil
                from pathlib import Path
                hls_path = Path(hls_dir)
                if hls_path.exists():
                    shutil.rmtree(hls_path)
                    print(f"🗑️ Deleted HLS directory: {hls_dir}")
                    hls_deleted = True
        except Exception as e:
            print(f"⚠️ Could not delete HLS files: {e}")
    
    # Delete original file(s)
    try:
        generic_storage.delete(obj.object_key)
        print(f"🗑️ Deleted original file: {obj.object_key}")
    except Exception as e:
        print(f"⚠️ Could not delete original file: {e}")

    # Cascade-cleanup: sweep all references across waypoints, tracks, routes,
    # media_files BEFORE deleting the row. Same transaction so it's atomic.
    # Without this, dangling refs become 404s in consuming apps (POIs without
    # icons, screen_points without videos) — exactly what we just had to
    # manually clean for 12 orphan IDs.
    ref_cleanup = cleanup_storage_refs(db, object_id)
    print(f"🧹 Ref cleanup for storage {object_id}: {ref_cleanup}")

    # Delete DB row
    db.delete(obj)
    db.commit()

    # Build response message
    message_parts = ["Storage object deleted"]
    if mac_job_cancelled:
        message_parts.append("Mac transcoding cancelled")
    if hls_deleted:
        message_parts.append("HLS files deleted")
    ref_total = sum(ref_cleanup.values())
    if ref_total:
        message_parts.append(f"{ref_total} refs cleaned")

    return {"message": " + ".join(message_parts), "refs_cleaned": ref_cleanup}


@router.post("/storage/{object_id}/cleanup-refs", response_model=dict)
def admin_storage_cleanup_refs(
    object_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cascade-clean references to a deleted storage object.

    Called by storage-api after a successful DELETE to keep artrack's
    denormalized refs (media_files, JSON lists on tracks/track_routes,
    waypoint metadata) in sync — closes the foreign-key gap between the
    two services. Safe to call idempotently: if the object_id is already
    not referenced anywhere, returns all-zero counters.

    Does NOT delete the StorageObject row itself (storage-api owns that
    table for live objects; artrack only mirrors). The mirror row, if
    any, gets pruned naturally by the next sync.
    """
    _ensure_admin(current_user)
    summary = cleanup_storage_refs(db, object_id)
    db.commit()
    logger.info(
        "[admin_storage_cleanup_refs] storage=%s refs_cleaned=%s",
        object_id, summary,
    )
    return {"storage_id": object_id, "refs_cleaned": summary}


@router.get("/storage/{object_id}/refs", response_model=dict)
def admin_storage_find_refs(
    object_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read-only: which artrack entities reference this storage object?

    The 'who is affected' step a cross-service delete workflow (e.g. sWFME)
    runs BEFORE removing the bytes — so the orchestrator knows the blast
    radius and can decide/Saga-rollback. Mutates nothing. Mirror of
    cleanup_storage_refs' ref locations.

    Returns {storage_id, total, waypoints:[{id,track_id,via}], tracks:[],
    track_routes:[], media_files:[]}.
    """
    _ensure_admin(current_user)
    return find_storage_refs(db, object_id)

