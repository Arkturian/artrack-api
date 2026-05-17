from sqlalchemy import create_engine, MetaData, text, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from databases import Database
import os
from .config import settings
from .models import Base

# Database setup
# Tune pool to reduce connection exhaustion under load and add pre-ping
_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "20"))
_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "40"))
_POOL_TIMEOUT = int(os.getenv("DB_POOL_TIMEOUT", "10"))
_POOL_RECYCLE = int(os.getenv("DB_POOL_RECYCLE", "1800"))
_ECHO_POOL = os.getenv("DB_ECHO_POOL", "false").lower() == "true"

engine = create_engine(
    settings.DATABASE_URL,
    pool_size=_POOL_SIZE,
    max_overflow=_MAX_OVERFLOW,
    pool_timeout=_POOL_TIMEOUT,
    pool_recycle=_POOL_RECYCLE,
    pool_pre_ping=True,
    echo_pool='debug' if _ECHO_POOL else False,
    connect_args={"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}
)

# Enable WAL and reasonable SQLite pragmas to improve concurrent access
if settings.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        try:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA busy_timeout=5000;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.close()
        except Exception:
            # Best-effort; do not crash if pragmas fail
            pass

# Create async database instance (size its internal pool)
_ASYNC_MIN_SIZE = int(os.getenv("DB_ASYNC_MIN_SIZE", "1"))
_ASYNC_MAX_SIZE = int(os.getenv("DB_ASYNC_MAX_SIZE", "10"))
database = Database(settings.DATABASE_URL, min_size=_ASYNC_MIN_SIZE, max_size=_ASYNC_MAX_SIZE)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def create_tables():
    """Create all database tables"""
    Base.metadata.create_all(bind=engine)

def get_db():
    """Database dependency for FastAPI"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

async def connect_db():
    """Connect to database (for startup)"""
    await database.connect()
    create_tables()
    # Lightweight, idempotent migrations for SQLite deployments
    try:
        if settings.DATABASE_URL.startswith("sqlite"):
            with engine.connect() as conn:
                # Ensure waypoints.is_public exists
                cols = {row[1] for row in conn.execute(text("PRAGMA table_info(waypoints)"))}
                if "is_public" not in cols:
                    conn.execute(text("ALTER TABLE waypoints ADD COLUMN is_public BOOLEAN DEFAULT 0"))
                    # Create index if not present
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_waypoints_public ON waypoints (is_public)"))

                # Ensure media_files.storage_object_id exists
                mcols = {row[1] for row in conn.execute(text("PRAGMA table_info(media_files)"))}
                if "storage_object_id" not in mcols:
                    conn.execute(text("ALTER TABLE media_files ADD COLUMN storage_object_id INTEGER"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_media_storage_obj ON media_files (storage_object_id)"))

                # Ensure media_files.content_hash exists
                if "content_hash" not in mcols:
                    conn.execute(text("ALTER TABLE media_files ADD COLUMN content_hash TEXT"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_media_content_hash ON media_files (content_hash)"))

                # Ensure media_files.metadata_json exists
                if "metadata_json" not in mcols:
                    conn.execute(text("ALTER TABLE media_files ADD COLUMN metadata_json JSON"))

                # Ensure waypoints.segment_id exists
                if "segment_id" not in cols:
                    conn.execute(text("ALTER TABLE waypoints ADD COLUMN segment_id INTEGER"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_waypoint_segment ON waypoints (segment_id)"))

                # Ensure waypoints.route_id exists
                if "route_id" not in cols:
                    conn.execute(text("ALTER TABLE waypoints ADD COLUMN route_id INTEGER"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_waypoint_route ON waypoints (route_id)"))

                # Ensure track_segments table exists
                existing_tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
                if "track_segments" not in existing_tables:
                    conn.execute(text(
                        """
                        CREATE TABLE track_segments (
                            id INTEGER PRIMARY KEY,
                            track_id INTEGER NOT NULL,
                            created_by INTEGER NOT NULL,
                            started_at DATETIME NOT NULL,
                            ended_at DATETIME NULL,
                            name TEXT NULL,
                            route_id INTEGER NULL,
                            metadata_json JSON
                        )
                        """
                    ))
                else:
                    # Add route_id and metadata_json if missing
                    tcols = {row[1] for row in conn.execute(text("PRAGMA table_info(track_segments)"))}
                    if "route_id" not in tcols:
                        conn.execute(text("ALTER TABLE track_segments ADD COLUMN route_id INTEGER"))
                        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_track_segments_route ON track_segments (route_id)"))
                    if "metadata_json" not in tcols:
                        conn.execute(text("ALTER TABLE track_segments ADD COLUMN metadata_json JSON"))
                # Ensure indexes on track_segments
                try:
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_track_segments_track ON track_segments (track_id)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_track_segments_started ON track_segments (started_at)"))
                except Exception:
                    pass

                # Ensure track_routes table exists
                if "track_routes" not in existing_tables:
                    conn.execute(text(
                        """
                        CREATE TABLE track_routes (
                            id INTEGER PRIMARY KEY,
                            track_id INTEGER NOT NULL,
                            created_by INTEGER NOT NULL,
                            name TEXT NOT NULL,
                            color TEXT NULL,
                            description TEXT NULL,
                            storage_object_ids JSON,
                            created_at DATETIME NOT NULL
                        )
                        """
                    ))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_track_routes_track ON track_routes (track_id)"))
                else:
                    # Add description if missing
                    trcols = {row[1] for row in conn.execute(text("PRAGMA table_info(track_routes)"))}
                    if "description" not in trcols:
                        conn.execute(text("ALTER TABLE track_routes ADD COLUMN description TEXT"))
                    if "storage_object_ids" not in trcols:
                        conn.execute(text("ALTER TABLE track_routes ADD COLUMN storage_object_ids JSON"))
                    if "storage_collection" not in trcols:
                        conn.execute(text("ALTER TABLE track_routes ADD COLUMN storage_collection JSON"))

                # Ensure storage_objects has AI analysis columns
                scols = {row[1] for row in conn.execute(text("PRAGMA table_info(storage_objects)"))}
                # Ensure tracks has storage asset fields
                tcols = {row[1] for row in conn.execute(text("PRAGMA table_info(tracks)"))}
                if "storage_object_ids" not in tcols:
                    conn.execute(text("ALTER TABLE tracks ADD COLUMN storage_object_ids JSON"))
                if "storage_collection" not in tcols:
                    conn.execute(text("ALTER TABLE tracks ADD COLUMN storage_collection JSON"))
                # Bounding-circle derived attributes (see services/track_bbox.py).
                # Used by GET /tracks/nearby as a cheap broad-phase spatial filter
                # before the more expensive snap-to-polyline. NULL until first
                # waypoint write or admin recompute.
                if "bbox_center_lat" not in tcols:
                    conn.execute(text("ALTER TABLE tracks ADD COLUMN bbox_center_lat FLOAT"))
                if "bbox_center_lon" not in tcols:
                    conn.execute(text("ALTER TABLE tracks ADD COLUMN bbox_center_lon FLOAT"))
                if "bbox_radius_m" not in tcols:
                    conn.execute(text("ALTER TABLE tracks ADD COLUMN bbox_radius_m FLOAT"))
                if "ai_category" not in scols:
                    conn.execute(text("ALTER TABLE storage_objects ADD COLUMN ai_category VARCHAR"))
                if "ai_danger_potential" not in scols:
                    conn.execute(text("ALTER TABLE storage_objects ADD COLUMN ai_danger_potential INTEGER"))
                
                # Ensure storage_objects has link_id column for linking related files
                if "link_id" not in scols:
                    conn.execute(text("ALTER TABLE storage_objects ADD COLUMN link_id VARCHAR"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_storage_link_id ON storage_objects (link_id)"))
                
                # Ensure storage_objects has webview_url column for web-optimized images
                if "webview_url" not in scols:
                    conn.execute(text("ALTER TABLE storage_objects ADD COLUMN webview_url VARCHAR"))
                
                # Ensure storage_objects has HLS transcoding columns
                if "hls_url" not in scols:
                    conn.execute(text("ALTER TABLE storage_objects ADD COLUMN hls_url VARCHAR"))
                if "transcoding_status" not in scols:
                    conn.execute(text("ALTER TABLE storage_objects ADD COLUMN transcoding_status VARCHAR"))
                if "transcoding_progress" not in scols:
                    conn.execute(text("ALTER TABLE storage_objects ADD COLUMN transcoding_progress INTEGER"))
                if "transcoding_error" not in scols:
                    conn.execute(text("ALTER TABLE storage_objects ADD COLUMN transcoding_error TEXT"))
                if "transcoded_file_size_bytes" not in scols:
                    conn.execute(text("ALTER TABLE storage_objects ADD COLUMN transcoded_file_size_bytes INTEGER"))
                if "ai_safety_status" not in scols:
                    conn.execute(text("ALTER TABLE storage_objects ADD COLUMN ai_safety_status VARCHAR"))
                if "ai_safety_error" not in scols:
                    conn.execute(text("ALTER TABLE storage_objects ADD COLUMN ai_safety_error TEXT"))

    except Exception:
        # Do not fail startup on migration errors; logs will show details
        pass

async def disconnect_db():
    """Disconnect from database (for shutdown)"""
    await database.disconnect()