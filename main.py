"""
Artrack API - Track & Collaboration Service

Handles:
- User Authentication & Authorization
- Track Management (CRUD)
- Waypoints & GPS Routes
- Collaboration (Invitations, Sharing)
- Track Segments & Routes
- Audio Features (TTS, Guides)
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path
from artrack.database import engine, Base

# Import all models BEFORE creating tables to ensure relationships work
from artrack import models
from artrack import collaboration_models

# ---------------------------------------------------------------------------
# Logging — route artrack.* app loggers (incl. artrack.event_bus, used by the
# producer event-bus hook) to stderr so gunicorn/uvicorn captures them in the
# service error-log. Scoped to the "artrack" namespace so third-party loggers
# (httpx, sqlalchemy) keep their own (quieter) levels. Without this the app's
# INFO/WARNING logs are swallowed and the fire-and-forget publish is "blind".
# ---------------------------------------------------------------------------
import logging as _logging
import sys as _sys

_artrack_log = _logging.getLogger("artrack")
if not _artrack_log.handlers:
    _h = _logging.StreamHandler(_sys.stderr)
    _h.setFormatter(_logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    _artrack_log.addHandler(_h)
    _artrack_log.setLevel(_logging.INFO)
    _artrack_log.propagate = False

from artrack.routes import (
    track_routes,
    waypoint_routes,
    collaboration_routes,
    auth_routes,
    gps_routes,
    snap_routes,
    sync_routes,
    segments_routes,
    routes_routes,
    guide_routes,
    categories_routes,
    knowledge_routes,
    osm_routes,
    places_routes,
    admin_routes,
)

# Create database tables
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Artrack API",
    version="1.0.0",
    description="Track management and collaboration service"
)

# CORS
# NOTE: allow_origins=["*"] together with allow_credentials=True is INVALID per the
# Fetch spec — the browser rejects a wildcard Access-Control-Allow-Origin whenever the
# request is credentialed (Bearer/Cookie), surfacing as "No 'Access-Control-Allow-Origin'
# header is present". allow_origin_regex=".*" makes Starlette echo the concrete request
# origin back instead of "*", which is credential-compatible for every origin.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth_routes.router, prefix="/auth", tags=["Authentication"])
# nearby + bbox routes registered FIRST so /tracks/nearby and
# /tracks/bbox/recompute-all aren't shadowed by the
# GET /tracks/{track_id} wildcard from track_routes below.
from artrack.routes import tracks_nearby_routes
app.include_router(tracks_nearby_routes.router, prefix="/tracks", tags=["Tracks"])
app.include_router(track_routes.router, prefix="/tracks", tags=["Tracks"])
app.include_router(waypoint_routes.router, prefix="", tags=["Waypoints"])
app.include_router(collaboration_routes.router, prefix="/collaboration", tags=["Collaboration"])
app.include_router(gps_routes.router, prefix="/tracks", tags=["GPS"])
app.include_router(snap_routes.router, prefix="/snap", tags=["Snap to Road"])
app.include_router(sync_routes.router, prefix="/sync", tags=["Sync"])
app.include_router(segments_routes.router, prefix="/segments", tags=["Segments"])
app.include_router(routes_routes.router, prefix="/tracks", tags=["Routes"])
app.include_router(guide_routes.router, prefix="/guides", tags=["Audio Guides"])
app.include_router(categories_routes.router, prefix="/categories", tags=["Categories"])
app.include_router(knowledge_routes.router, prefix="/tracks", tags=["Route Knowledge"])
app.include_router(osm_routes.router, prefix="/osm", tags=["OpenStreetMap"])
from artrack.routes import weather_routes
app.include_router(weather_routes.router, prefix="/weather", tags=["Weather"])
app.include_router(places_routes.router, prefix="/places", tags=["Google Places"])
# Admin endpoints (system stats, storage cascade-cleanup, moderation) — was
# defined but never mounted; mounting now so the cleanup-refs callback used
# by storage-api becomes reachable.
app.include_router(admin_routes.router, prefix="/admin", tags=["Admin"])

@app.get("/")
def root():
    return {
        "service": "artrack-api",
        "version": "1.0.0",
        "description": "Track management and collaboration service"
    }

@app.get("/health")
def health():
    return {"status": "healthy", "service": "artrack-api"}

# Static files & Debug Console
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/debug")
def debug_console():
    """Serve the API Debug Console"""
    debug_file = STATIC_DIR / "debug.html"
    if debug_file.exists():
        return FileResponse(debug_file)
    return {"error": "debug.html not found"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
