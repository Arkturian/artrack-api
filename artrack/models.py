from sqlalchemy import BigInteger, Column, Integer, String, Float, Text, DateTime, Boolean, JSON, ForeignKey, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, backref
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Union

Base = declarative_base()

# Database Models
class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    display_name = Column(String)
    password_hash = Column(String)
    api_key = Column(String, unique=True, index=True)
    trust_level = Column(String, default="new_user")  # new_user, trusted, moderator, admin
    device_ids = Column(JSON, default=list)  # List of device IDs
    
    # Quotas
    storage_bytes_used = Column(BigInteger, default=0)
    storage_bytes_limit = Column(BigInteger, default=5368709120)  # 5GB
    uploads_this_month = Column(Integer, default=0)
    upload_limit_per_month = Column(Integer, default=1000)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    tracks = relationship("Track", back_populates="creator")

class Track(Base):
    __tablename__ = "tracks"
    
    id = Column(Integer, primary_key=True, index=True)
    client_track_id = Column(String, index=True)  # For client sync
    name = Column(String)
    description = Column(Text)
    visibility = Column(String, default="private")  # public, followers, private
    track_type = Column(String, default="hiking")
    tags = Column(JSON, default=list)
    
    # Collaboration settings
    is_collaborative = Column(Boolean, default=False)
    collaboration_mode = Column(String, default="invite_only")  # invite_only, open, link_share
    invite_code = Column(String, unique=True, nullable=True)
    max_collaborators = Column(Integer, default=10)
    
    # Creator
    created_by = Column(Integer, ForeignKey("users.id"))
    creator = relationship("User", back_populates="tracks")
    
    # Statistics
    total_waypoints = Column(Integer, default=0)
    distance_meters = Column(Float, default=0.0)
    duration_seconds = Column(Integer, default=0)
    elevation_gain_meters = Column(Float, default=0.0)
    
    # Metadata
    version = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Optional JSON metadata used by various features
    metadata_json = Column(JSON, default=dict)
    # Assets for the whole track (e.g., guide audio, images)
    storage_object_ids = Column(JSON, default=list)
    storage_collection = Column(JSON, default=dict)

    # Bounding circle — derived attribute, maintained by services/track_bbox.py
    # after any waypoint insert/delete/cleanup. Used as a cheap broad-phase
    # filter in GET /tracks/nearby so we don't snap-to-polyline every track
    # on every GPS tick. NULL until first recompute; the /nearby endpoint
    # treats NULL as "not in this circle" and the read-side falls back to
    # a lazy recompute.
    bbox_center_lat = Column(Float, nullable=True)
    bbox_center_lon = Column(Float, nullable=True)
    bbox_radius_m = Column(Float, nullable=True)

    # Wanderlaut auto-detect eligibility — opt-in flag set by the track
    # owner in the artrack UI. Decoupled from `visibility` so a track can
    # stay private (creator-only access via X-API-KEY) while still being
    # eligible to auto-activate the audio-guide when a user walks near it.
    # GET /tracks/nearby filters on this flag exclusively.
    auto_detect_eligible = Column(Boolean, default=False, nullable=False)

    # Relationships
    waypoints = relationship("Waypoint", back_populates="track")
    collaborators = relationship("TrackCollaborator", back_populates="track")
    invitations = relationship("TrackInvitation", back_populates="track")

class TrackSegment(Base):
    __tablename__ = "track_segments"
    id = Column(Integer, primary_key=True, index=True)
    track_id = Column(Integer, ForeignKey("tracks.id"), index=True)
    created_by = Column(Integer, ForeignKey("users.id"))
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime, nullable=True)
    name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Optional route grouping
    route_id = Column(Integer, ForeignKey("track_routes.id"), nullable=True, index=True)
    metadata_json = Column(JSON, default=dict)

class TrackRoute(Base):
    __tablename__ = "track_routes"
    id = Column(Integer, primary_key=True, index=True)
    track_id = Column(Integer, ForeignKey("tracks.id"), index=True)
    created_by = Column(Integer, ForeignKey("users.id"))
    name = Column(String, nullable=False)
    color = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    # Optional list of storage object ids associated with the route (audio guide, images, etc.)
    storage_object_ids = Column(JSON, default=list)
    # Optional storage collection reference: { name: string, owner_email: string }
    storage_collection = Column(JSON, default=dict)
    # Route intro audio URL (generated welcome message when route is selected)
    intro_audio_url = Column(String, nullable=True)
    # Metadata JSON for route-specific data (e.g., knowledge for audio guides)
    metadata_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)

class Waypoint(Base):
    __tablename__ = "waypoints"
    
    id = Column(Integer, primary_key=True, index=True)
    client_waypoint_id = Column(String, index=True)  # For client sync
    
    # Track relationship
    track_id = Column(Integer, ForeignKey("tracks.id"))
    track = relationship("Track", back_populates="waypoints")
    
    # Location data
    latitude = Column(Float)
    longitude = Column(Float)
    altitude = Column(Float, nullable=True)
    accuracy = Column(Float, nullable=True)
    
    # Creator and timing
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    recorded_at = Column(DateTime)
    # For GPS routes compatibility
    timestamp = Column(DateTime, nullable=True)
    user_description = Column(Text)
    
    # Processing state
    processing_state = Column(String, default="pending")  # pending, uploading, uploaded, analysing, moderated, published, quarantined, failed
    moderation_status = Column(String, default="pending")  # pending, approved, rejected, auto_quarantine
    
    # Waypoint type and metadata JSON
    waypoint_type = Column(String, nullable=True)  # gps_track, photo, video, audio, manual
    # Visibility flag (public listing eligibility)
    is_public = Column(Boolean, default=False)
    # Metadata
    version = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    metadata_json = Column(JSON, default=dict)
    # Priority for display ordering (-1.0 to 1.0, higher = more important)
    priority = Column(Float, default=0.0, nullable=True)
    # Optional segment association (track segment polyline grouping)
    segment_id = Column(Integer, ForeignKey("track_segments.id"), nullable=True, index=True)
    # Optional route association for polyline grouping without segments
    route_id = Column(Integer, ForeignKey("track_routes.id"), nullable=True, index=True)
    
    # Relationships
    media_files = relationship("MediaFile", back_populates="waypoint")
    analysis_results = relationship("AnalysisResult", back_populates="waypoint")

class MediaFile(Base):
    __tablename__ = "media_files"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Waypoint relationship
    waypoint_id = Column(Integer, ForeignKey("waypoints.id"))
    waypoint = relationship("Waypoint", back_populates="media_files")
    
    # File info
    media_type = Column(String)  # photo, audio, video
    original_filename = Column(String)
    file_path = Column(String)  # Local storage path
    file_url = Column(String)   # Public URL
    thumbnail_url = Column(String, nullable=True)
    
    # Upload info
    file_size_bytes = Column(BigInteger)
    mime_type = Column(String)
    checksum = Column(String)
    # Idempotency
    content_hash = Column(String, nullable=True, index=True)
    
    # Processing state
    processing_state = Column(String, default="pending")
    upload_session_id = Column(String, nullable=True)

    # Optional link to generic storage object for unified storage handling
    storage_object_id = Column(Integer, ForeignKey("storage_objects.id"), nullable=True, index=True)
    
    # Metadata
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Capture metadata per media (EXIF/QuickTime, audio waveform, device info, etc.)
    metadata_json = Column(JSON, default=dict)

class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"
    
    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(String, unique=True, index=True)
    
    # Target
    media_file_id = Column(Integer, ForeignKey("media_files.id"))
    analysis_type = Column(String)  # image_analysis, audio_transcription, video_analysis
    
    # Status
    status = Column(String, default="pending")  # pending, processing, completed, failed
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    retry_count = Column(Integer, default=0)
    
    # Error handling
    error_message = Column(Text, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)

class AnalysisResult(Base):
    __tablename__ = "analysis_results"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Target
    waypoint_id = Column(Integer, ForeignKey("waypoints.id"))
    waypoint = relationship("Waypoint", back_populates="analysis_results")
    
    media_file_id = Column(Integer, ForeignKey("media_files.id"))
    analysis_job_id = Column(String)
    
    # Results
    analysis_type = Column(String)
    description = Column(Text)
    categories = Column(JSON, default=list)
    safety_rating = Column(String)  # safe, warning, unsafe
    quality_score = Column(Float)
    confidence = Column(Float)
    
    # Detailed results
    objects_detected = Column(JSON, default=list)
    plant_identification = Column(JSON, default=list)
    technical_metrics = Column(JSON, default=dict)
    
    # Processing info
    model_version = Column(String)
    processing_time_seconds = Column(Float)
    
    created_at = Column(DateTime, default=datetime.utcnow)

# Generic storage objects (user-owned files)
class StorageObject(Base):
    __tablename__ = "storage_objects"

    id = Column(Integer, primary_key=True, index=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), index=True)
    object_key = Column(String, unique=True, index=True)
    original_filename = Column(String)
    file_url = Column(String)
    thumbnail_url = Column(String, nullable=True)
    webview_url = Column(String, nullable=True)  # Web-optimized image URL
    mime_type = Column(String)
    file_size_bytes = Column(BigInteger)
    checksum = Column(String)
    is_public = Column(Boolean, default=False)
    context = Column(String, nullable=True)
    collection_id = Column(String, nullable=True)
    link_id = Column(String, nullable=True, index=True)  # For linking related files together
    title = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    likes = Column(Integer, default=0)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    duration_seconds = Column(Float, nullable=True)
    bit_rate = Column(Integer, nullable=True)
    latitude = Column(Float, nullable=True, index=True)
    longitude = Column(Float, nullable=True, index=True)
    ai_safety_rating = Column(String, nullable=True)
    metadata_json = Column(JSON, default=dict)
    download_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # AI Analysis Fields
    ai_category = Column(String, nullable=True)
    ai_danger_potential = Column(Integer, nullable=True)  # e.g., 1-10 scale

    # Enhanced AI Metadata Fields (v2.0)
    ai_title = Column(String(500), nullable=True)  # AI-generated title
    ai_subtitle = Column(Text, nullable=True)      # Instagram-style subtitle
    ai_tags = Column(JSON, nullable=True)          # Array of tags
    ai_collections = Column(JSON, nullable=True)   # Array of collection suggestions
    safety_info = Column(JSON, nullable=True)      # Safety check results

    # Storage Mode Fields (v3.0)
    storage_mode = Column(String, default="copy")  # "copy", "reference", or "external"
    reference_path = Column(String, nullable=True, index=True)  # Filesystem path when using reference mode
    external_uri = Column(String, nullable=True, index=True)  # External web URI when using external mode
    ai_context_metadata = Column(JSON, nullable=True)  # Context for AI analysis (file_path, semantic hints, etc.)

    # Multi-Tenancy (v4.0)
    tenant_id = Column(String(50), default="arkturian", index=True)  # Tenant identifier for multi-tenancy

# Application logs for client apps (Unity/iOS/etc.)
class AppLog(Base):
    __tablename__ = "app_logs"

    id = Column(Integer, primary_key=True, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    level = Column(String, index=True)  # debug, info, warning, error, critical
    message = Column(Text)
    app_name = Column(String, nullable=True, index=True)
    tags = Column(JSON, default=list)
    metadata_json = Column(JSON, default=dict)
    device_id = Column(String, nullable=True)
    platform = Column(String, nullable=True)  # ios, android, unity, web, etc.
    build = Column(String, nullable=True)  # build number or version
    request_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_app_logs_created_at", "created_at"),
    )

# Pydantic Models for API
class Visibility(str, Enum):
    public = "public"
    followers = "followers"
    private = "private"

class ProcessingState(str, Enum):
    pending = "pending"
    uploading = "uploading"
    uploaded = "uploaded"
    analysing = "analysing"
    moderated = "moderated"
    published = "published"
    quarantined = "quarantined"
    failed = "failed"

class MediaType(str, Enum):
    photo = "photo"
    audio = "audio"
    video = "video"

# Request Models
class UserCreate(BaseModel):
    email: str
    password: str
    display_name: str
    device_id: str

class UserLogin(BaseModel):
    email: str
    password: str
    device_id: str

class TrackCreate(BaseModel):
    name: str
    description: str = ""
    visibility: Visibility = Visibility.private
    track_type: str = "hiking"
    tags: List[str] = []
    client_track_id: str
    storage_object_ids: Optional[List[int]] = None
    storage_collection: Optional[Dict[str, Any]] = None

class WaypointCreate(BaseModel):
    client_waypoint_id: str
    latitude: float
    longitude: float
    altitude: Optional[float] = None
    accuracy: Optional[float] = None
    recorded_at: datetime
    user_description: str = ""
    media_count: int = 0
    waypoint_type: Optional[str] = None
    metadata_json: Optional[dict] = None
    segment_id: Optional[int] = None
    route_id: Optional[int] = None

class WaypointBatch(BaseModel):
    waypoints: List[WaypointCreate]

# Response Models
class UserResponse(BaseModel):
    id: int
    email: str
    display_name: str
    trust_level: str
    created_at: datetime
    
    class Config:
        from_attributes = True

class AuthResponse(BaseModel):
    user_id: int
    access_token: str
    api_key: str
    trust_level: str
    expires_in: int = 86400

class TrackStats(BaseModel):
    total_waypoints: int
    processed_waypoints: int
    pending_analysis: int
    distance_meters: float
    duration_seconds: int

class TrackResponse(BaseModel):
    id: int
    name: str
    description: str
    visibility: str
    track_type: str
    tags: List[str]
    client_track_id: str
    stats: TrackStats
    created_at: datetime
    updated_at: datetime
    metadata_json: Optional[Dict[str, Any]] = None  # Include guide config and other metadata
    storage_object_ids: Optional[List[int]] = None
    storage_collection: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True

class MediaUploadUrl(BaseModel):
    media_slot: int
    upload_url: str
    max_size_bytes: int

class UploadSession(BaseModel):
    session_id: str
    media_upload_urls: List[MediaUploadUrl]
    expires_at: datetime

class WaypointCreateResponse(BaseModel):
    client_waypoint_id: str
    waypoint_id: int
    status: str
    upload_session: Optional[UploadSession] = None

class WaypointBatchResponse(BaseModel):
    results: List[WaypointCreateResponse]

class MediaAnalysis(BaseModel):
    description: str
    categories: List[str]
    safety_rating: str
    quality_score: float
    confidence: float

class MediaFileResponse(BaseModel):
    media_id: int
    type: str
    processing_state: str
    analysis: Optional[MediaAnalysis] = None
    thumbnail_url: Optional[str] = None
    url: Optional[str] = None
    storage_object_id: Optional[int] = None

class WaypointStatusResponse(BaseModel):
    waypoint_id: int
    processing_state: str
    media: List[MediaFileResponse]
    moderation_status: str
    published_at: Optional[datetime] = None
    metadata_json: Optional[Dict[str, Any]] = None

class WaypointLocation(BaseModel):
    latitude: float
    longitude: float

# Lightweight user reference for list views
class SimpleUserRef(BaseModel):
    id: Optional[int] = None
    display_name: Optional[str] = None

# Extended item for list endpoints used by admin dashboard
class WaypointListItem(WaypointStatusResponse):
    track_id: int
    track_name: Optional[str] = None
    creator_id: Optional[int] = None
    creator: Optional[SimpleUserRef] = None
    location: Optional[WaypointLocation] = None
    created_at: Optional[datetime] = None
    media_count: Optional[int] = None
    waypoint_type: Optional[str] = None
    segment_id: Optional[int] = None
    route_id: Optional[int] = None
    metadata_json: Optional[Dict[str, Any]] = None
    priority: Optional[float] = None  # -1.0 to 1.0

class QuotaInfo(BaseModel):
    storage_bytes: int
    storage_limit: int
    uploads_this_month: int
    upload_limit: int

class SyncStatus(BaseModel):
    user_id: int
    last_sync_at: datetime
    pending_uploads: int
    pending_analysis: int
    failed_uploads: int
    quota_used: QuotaInfo

# Detailed Waypoint response for client mapping
class WaypointDetailResponse(BaseModel):
    id: int
    track_id: int
    latitude: float
    longitude: float
    altitude: Optional[float] = None
    accuracy: Optional[float] = None
    recorded_at: datetime
    user_description: Optional[str] = None
    processing_state: Optional[str] = None
    moderation_status: Optional[str] = None
    waypoint_type: Optional[str] = None
    metadata_json: Optional[Dict[str, Any]] = None
    segment_id: Optional[int] = None
    route_id: Optional[int] = None
    priority: Optional[float] = None  # -1.0 to 1.0
    media: List[MediaFileResponse] = []
    storage_object_ids: Optional[List[int]] = None

# Storage Pydantic responses
class StorageObjectResponse(BaseModel):
    id: int
    owner_user_id: Optional[int] = None
    owner_email: Optional[str] = None
    object_key: str
    original_filename: str
    file_url: str
    thumbnail_url: Optional[str] = None
    webview_url: Optional[str] = None
    mime_type: str
    file_size_bytes: int
    checksum: str
    is_public: bool
    context: Optional[str] = None
    collection_id: Optional[str] = None
    link_id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    likes: int = 0
    width: Optional[int] = None
    height: Optional[int] = None
    duration_seconds: Optional[float] = None
    bit_rate: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    ai_safety_rating: Optional[str] = None
    metadata_json: Dict[str, Any] = {}
    download_count: int = 0
    created_at: datetime
    updated_at: datetime

    # AI Analysis Fields
    ai_category: Optional[str] = None
    ai_danger_potential: Optional[int] = None
    hls_url: Optional[str] = None
    transcoding_status: Optional[str] = None
    transcoding_progress: Optional[int] = None
    transcoding_error: Optional[str] = None
    transcoded_file_size_bytes: Optional[int] = None
    ai_safety_status: Optional[str] = None
    ai_safety_error: Optional[str] = None
    
    # Enhanced AI Metadata Fields (v2.0)
    ai_title: Optional[str] = None
    ai_subtitle: Optional[str] = None
    ai_tags: Optional[Union[List[str], Dict[str, Any]]] = None  # Can be list or structured dict
    ai_collections: Optional[List[str]] = None
    ai_context_metadata: Optional[Dict[str, Any]] = None  # Context and debug info (prompt, response)
    safety_info: Optional[Dict[str, Any]] = None

    # Storage Mode Fields (v3.0)
    storage_mode: Optional[str] = "copy"
    reference_path: Optional[str] = None
    external_uri: Optional[str] = None

    # Multi-Tenancy (v4.0)
    tenant_id: Optional[str] = "arkturian"

    class Config:
        from_attributes = True

class StorageListResponse(BaseModel):
    items: List[StorageObjectResponse]
# ============================================================================
# Async Task Models
# ============================================================================

class AsyncTask(Base):
    """Async task tracking for background processing"""
    __tablename__ = "async_tasks"

    task_id = Column(String, primary_key=True)
    object_id = Column(Integer, ForeignKey("storage_objects.id", ondelete="CASCADE"), nullable=False)
    status = Column(String, nullable=False)  # queued, processing, completed, failed
    mode = Column(String, nullable=False)  # fast, quality
    current_phase = Column(String)  # safety_check, ai_analysis, building_knowledge_graph, complete
    progress = Column(Integer, default=0)
    created_at = Column(String, nullable=False)
    started_at = Column(String)
    completed_at = Column(String)
    error = Column(Text)
    result = Column(Text)  # JSON string

    # Relationship
    storage_object = relationship("StorageObject", backref=backref("async_tasks", passive_deletes='all'))
