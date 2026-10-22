import os
from pathlib import Path
from dotenv import load_dotenv
from typing import Optional

# Load .env robustly (try project root and CWD)
_ROOT = Path(__file__).resolve().parent.parent  # /var/www/api.arkturian.com/artrack -> parent is project root
_candidates = [
    _ROOT / ".env",
    Path.cwd() / ".env",
]
for p in _candidates:
    try:
        if p.exists():
            load_dotenv(p, override=False)
    except Exception:
        pass

class Settings:
    # Database
    DATABASE_URL: str = os.getenv("ARTRACK_DATABASE_URL", "sqlite:///./artrack.db")
    
    # File Storage
    UPLOAD_DIR: str = os.getenv("ARTRACK_UPLOAD_DIR", "./uploads/artrack")
    STORAGE_UPLOAD_DIR: str = os.getenv("STORAGE_UPLOAD_DIR", "./uploads/storage")
    BASE_URL: str = os.getenv("ARTRACK_BASE_URL", "https://api.arkturian.com")
    # Default storage host for assets that carry no explicit per-asset
    # storage_host marker (legacy assets). Resolved into file_url/thumbnail_url
    # by artrack.asset_urls.enrich_assets. Per-asset metadata may override this
    # (e.g. assets migrated to arkserver carry storage_host=arkserver).
    STORAGE_DEFAULT_HOST: str = os.getenv("ARTRACK_STORAGE_DEFAULT_HOST", "https://api-storage.arkturian.com")
    
    # API Keys
    GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY")
    OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
    ANTHROPIC_API_KEY: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
    # Eingehender Master-Schluessel von ARTrack. Kein Default mehr: Der alte
    # stand im oeffentlichen Repo (Issue #2385). Fehlt er in der
    # Unit-Umgebung, startet der Dienst nicht (main.py).
    API_KEY: str = os.getenv("API_KEY", "")
    # Signier-Geheimnis der JWTs, getrennt vom API-Schluessel.
    JWT_SECRET: str = os.getenv("JWT_SECRET", "")
    # Ausgehende Schluessel. Sie gehoeren den Zieldiensten, nicht ARTrack, und
    # duerfen deshalb nicht am eigenen Master haengen.
    CONTENT_API_KEY: str = os.getenv("CONTENT_API_KEY", "")
    AI_API_KEY: str = os.getenv("AI_API_KEY", "")
    AI_INTERNAL_API_KEY: str = os.getenv("AI_INTERNAL_API_KEY", "")
    REQUIRED_SECRETS = ("API_KEY", "JWT_SECRET", "CONTENT_API_KEY", "AI_API_KEY", "AI_INTERNAL_API_KEY")

    def missing_secrets(self) -> list:
        return [name for name in self.REQUIRED_SECRETS if not getattr(self, name)]
    # Anmeldung von Menschen ueber auth-api (OAuth 2.1 + PKCE, Post #5579).
    # Ohne AUTH_CLIENT_ID ist der Bearer-Weg aus, X-API-KEY bleibt unberuehrt.
    AUTH_API_BASE: str = os.getenv("AUTH_API_BASE", "https://auth-api.arkturian.com").rstrip("/")
    AUTH_CLIENT_ID: str = os.getenv("AUTH_CLIENT_ID", "")
    # Token-Form ohne https:// (auth-api token_service), nicht die Discovery-Form.
    AUTH_ISSUER: str = os.getenv("AUTH_ISSUER", "auth-api.arkturian.com")
    # Bis das RBAC-System "artrack" steht: auth-api-sub-Werte mit Admin-Recht.
    AUTH_ADMIN_SUBS: list = [s.strip() for s in os.getenv("AUTH_ADMIN_SUBS", "").split(",") if s.strip()]
    AUTH_ADMIN_ROLE: str = os.getenv("AUTH_ADMIN_ROLE", "artrack-admin")
    # Public Firebase project identifier used to verify iOS ID tokens. This is
    # not a credential and can be overridden for another deployment.
    FIREBASE_PROJECT_ID: str = os.getenv(
        "ARTRACK_FIREBASE_PROJECT_ID", "sensepresenter"
    )
    
    # AI Services (using existing endpoints on SAME FastAPI instance)
    AI_BASE_URL: str = os.getenv("AI_BASE_URL", "http://localhost:8001")

    # Content API (for narration storage)
    CONTENT_API_BASE: str = os.getenv("CONTENT_API_BASE", "https://content-api.arkturian.com")

    # Event Bus (IACP Redis PubSub). artrack publishes fire-and-forget events
    # (e.g. waypoint_deleted) to `swfme.webhook.{event}` topics; the swfme-api
    # WebhookWorker subscribes and async-triggers the matching workflow.
    # Decoupled: artrack knows the bus, not swfme's URL/auth. URL carries auth
    # (federation standard IACP_REDIS_URL). Disable with EVENT_BUS_ENABLED=false.
    REDIS_URL: str = os.getenv("ARTRACK_REDIS_URL") or os.getenv("IACP_REDIS_URL") or "redis://127.0.0.1:6379/0"
    EVENT_BUS_ENABLED: bool = os.getenv("ARTRACK_EVENT_BUS_ENABLED", "true").lower() in ("1", "true", "yes")
    
    # Media Settings
    MAX_FILE_SIZE: int = int(os.getenv("ARTRACK_MAX_FILE_SIZE", "52428800"))  # 50MB
    CHUNK_PART_MAX_BYTES: int = int(os.getenv("ARTRACK_CHUNK_PART_MAX_BYTES", str(16 * 1024 * 1024)))  # 16MB default
    CHUNK_UPLOAD_DIR: str = os.getenv("ARTRACK_CHUNK_UPLOAD_DIR", "./uploads/artrack/chunk_uploads")
    ALLOWED_IMAGE_TYPES: list = ["image/jpeg", "image/png", "image/webp"]
    ALLOWED_VIDEO_TYPES: list = ["video/mp4", "video/quicktime", "video/x-msvideo"]
    ALLOWED_AUDIO_TYPES: list = ["audio/mpeg", "audio/wav", "audio/aac", "audio/mp4"]
    
    # Analysis Settings
    ANALYSIS_TIMEOUT: int = int(os.getenv("ARTRACK_ANALYSIS_TIMEOUT", "60"))  # seconds
    
    # Trust System
    NEW_USER_TRUST_LEVEL: str = "new_user"
    TRUSTED_USER_THRESHOLD: int = 10  # number of approved uploads

settings = Settings()
