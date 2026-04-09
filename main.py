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
)

# Create database tables
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Artrack API",
    version="1.0.0",
    description="Track management and collaboration service"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth_routes.router, prefix="/auth", tags=["Authentication"])
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
