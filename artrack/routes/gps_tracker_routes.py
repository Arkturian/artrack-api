"""
gps_tracker_routes — serves the Tokyo-Night audio-guide frontend.

This router replaces what used to live in the standalone gps-tracker-api
repository. The frontend code now lives under artrack-api/frontend/
and is built via Vite to artrack-api/frontend/dist/. This module:

  1. Caches the built index.html template at startup
  2. Injects runtime config as a JSON script tag (`__GPS_CONFIG__`)
  3. Serves the HTML at /api/gps-tracker with a ?session= query param
  4. Mounts the Vite-hashed asset bundle at /gps-tracker-assets/

Runtime config fields (matches AudioGuideConfig in the library):
  - session             : Guide bot session name (default: 'Guide')
  - server              : optional IACP federation override (default: '')
  - mapboxToken         : from MAPBOX_ACCESS_TOKEN env
  - cloudApiBootstrap   : from CLOUD_API_BOOTSTRAP env

The actual audio-guide runtime is served via the @arkturian/audio-guide
NPM package that the frontend depends on — this backend just serves the
built HTML shell. All dynamic behavior (GPS, LLM, TTS, map, music) happens
client-side against the cloud-api IACP queue, independent of artrack-api.

Originally written for gps-tracker-api (standalone), merged into
artrack-api on 2026-04-09 as part of the "audio-guide library + artrack
fusion" big-bang refactor.
"""

import json
import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

# Layout assumption: this file lives at
#   artrack-api/artrack/routes/gps_tracker_routes.py
# Frontend dist is at
#   artrack-api/frontend/dist/
# So REPO_ROOT is two parents above this file.
_REPO_ROOT = Path(__file__).parent.parent.parent
_FRONTEND_DIST = _REPO_ROOT / "frontend" / "dist"
_INDEX_HTML_PATH = _FRONTEND_DIST / "index.html"

_MAPBOX_TOKEN = os.getenv("MAPBOX_ACCESS_TOKEN", "")
_CLOUD_API_BOOTSTRAP = os.getenv(
    "CLOUD_API_BOOTSTRAP",
    "https://cloud.arkserver.arkturian.com",
)

# Cache the index.html template once at startup. systemd restart picks
# up new builds (GH Actions rsyncs dist/ before restarting the service).
_index_template: str = ""
if _INDEX_HTML_PATH.exists():
    _index_template = _INDEX_HTML_PATH.read_text()


def _render_index(session: str, server: str) -> str:
    """Inject runtime config as JSON into the __GPS_CONFIG__ placeholder.

    Uses json.dumps() with </ -> <\\/ escaping so the payload cannot
    break out of the <script> element even with pathological input.
    The four fields (session, server, mapboxToken, cloudApiBootstrap)
    match the AudioGuideConfig interface in @arkturian/audio-guide.
    """
    if not _index_template:
        return (
            "<h1>artrack-api frontend error</h1>"
            "<p>frontend/dist/index.html not found. "
            "Run <code>cd frontend && npm install && npm run build</code> "
            "from the repo root.</p>"
        )

    config = {
        "session": session,
        "server": server,
        "mapboxToken": _MAPBOX_TOKEN,
        "cloudApiBootstrap": _CLOUD_API_BOOTSTRAP,
    }
    # JSON encode, then escape `</` to `<\/` so the payload cannot
    # break out of the <script> element even under pathological input.
    config_json = json.dumps(config, ensure_ascii=False).replace("</", "<\\/")
    return _index_template.replace("__GPS_CONFIG_JSON__", config_json)


@router.get("/gps-tracker", response_class=HTMLResponse)
def gps_tracker(session: str = "Guide", server: str = ""):
    """Serve the Tokyo-Night audio-guide frontend with injected config.

    Query params:
      session : Guide bot session name (default: 'Guide')
      server  : optional IACP federation override (default: auto-discover)
    """
    return HTMLResponse(_render_index(session, server))
