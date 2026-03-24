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
    
    # API Keys
    GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY")
    OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
    ANTHROPIC_API_KEY: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
    API_KEY: str = os.getenv("API_KEY", "Inetpass1")
    
    # AI Services (using existing endpoints on SAME FastAPI instance)
    AI_BASE_URL: str = os.getenv("AI_BASE_URL", "http://localhost:8001")

    # Content API (for narration storage)
    CONTENT_API_BASE: str = os.getenv("CONTENT_API_BASE", "https://content-api.arkturian.com")
    
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