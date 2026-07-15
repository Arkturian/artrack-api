from fastapi import HTTPException, Header, Depends, Request
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
import secrets
import string
from typing import Optional

from .database import get_db
from .models import User, UserCreate, UserLogin, AuthResponse
from .config import settings

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT settings
SECRET_KEY = settings.API_KEY  # Using existing API key as JWT secret
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Hash a password"""
    return pwd_context.hash(password)

def generate_api_key() -> str:
    """Generate a random API key"""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(32))

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """Create a JWT access token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def verify_api_key(api_key: str, db: Session) -> Optional[User]:
    """Verify API key and return user"""
    if not api_key:
        return None
    
    # Check if it's the master API key (existing system)
    if api_key == settings.API_KEY:
        # Ensure a persistent 'system' admin user exists to satisfy FK constraints
        system_email = "system@api"
        user = db.query(User).filter(User.email == system_email).first()
        if not user:
            # Create a real DB user entry with admin trust level
            user = User(
                email=system_email,
                display_name="System",
                password_hash="",  # not used
                api_key=generate_api_key(),  # distinct from master key
                trust_level="admin",
                device_ids=[],
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        # Update last active and return
        user.last_active_at = datetime.utcnow()
        db.commit()
        return user
    
    # Check user API keys
    user = db.query(User).filter(User.api_key == api_key).first()
    if user:
        # Update last active time
        user.last_active_at = datetime.utcnow()
        db.commit()
        return user

    # Check read-only keys: same identity, but mutating requests are
    # rejected in get_current_user via the transient marker below
    user = db.query(User).filter(User.api_key_readonly == api_key).first()
    if user:
        user.last_active_at = datetime.utcnow()
        db.commit()
        user._readonly_key = True
        return user

    return None

def get_current_user(
    request: Request,
    api_key: str = Header(None, alias="X-API-KEY"),
    db: Session = Depends(get_db)
) -> User:
    """Get current user from API key"""
    user = verify_api_key(api_key, db)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if getattr(user, "_readonly_key", False) and request.method not in ("GET", "HEAD", "OPTIONS"):
        raise HTTPException(status_code=403, detail="Read-only API key: write operations are not allowed")
    return user

def get_current_user_optional(
    api_key: str = Header(None, alias="X-API-KEY"),
    db: Session = Depends(get_db)
) -> Optional[User]:
    """Get current user from API key (optional - returns None if not authenticated)"""
    return verify_api_key(api_key, db)

def get_user_by_email(db: Session, email: str) -> Optional[User]:
    """Get user by email"""
    return db.query(User).filter(User.email == email).first()

def create_user(db: Session, user_create: UserCreate) -> User:
    """Create a new user"""
    # Check if user already exists
    existing_user = get_user_by_email(db, user_create.email)
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    # Create new user
    hashed_password = get_password_hash(user_create.password)
    api_key = generate_api_key()
    
    db_user = User(
        email=user_create.email,
        display_name=user_create.display_name,
        password_hash=hashed_password,
        api_key=api_key,
        device_ids=[user_create.device_id] if user_create.device_id else [],
        trust_level=settings.NEW_USER_TRUST_LEVEL
    )
    
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    
    return db_user

def authenticate_user(db: Session, email: str, password: str) -> Optional[User]:
    """Authenticate user with email and password"""
    user = get_user_by_email(db, email)
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user

def login_user(db: Session, user_login: UserLogin) -> AuthResponse:
    """Login user and return auth response"""
    user = authenticate_user(db, user_login.email, user_login.password)
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    
    # Add device ID if not already present
    if user_login.device_id and user_login.device_id not in user.device_ids:
        user.device_ids = user.device_ids + [user_login.device_id]
        user.last_active_at = datetime.utcnow()
        db.commit()
    
    # Create access token
    access_token_expires = timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    access_token = create_access_token(
        data={"sub": user.email, "user_id": user.id},
        expires_delta=access_token_expires
    )
    
    return AuthResponse(
        user_id=user.id,
        access_token=access_token,
        api_key=user.api_key,
        trust_level=user.trust_level,
        expires_in=int(access_token_expires.total_seconds())
    )

def register_user(db: Session, user_create: UserCreate) -> AuthResponse:
    """Register new user and return auth response"""
    user = create_user(db, user_create)
    
    # Create access token
    access_token_expires = timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    access_token = create_access_token(
        data={"sub": user.email, "user_id": user.id},
        expires_delta=access_token_expires
    )
    
    return AuthResponse(
        user_id=user.id,
        access_token=access_token,
        api_key=user.api_key,
        trust_level=user.trust_level,
        expires_in=int(access_token_expires.total_seconds())
    )

# --- Google / Firebase Sign-In ---
def _ensure_firebase_admin_initialized():
    try:
        import firebase_admin
        from firebase_admin import credentials
        try:
            firebase_admin.get_app()
        except ValueError:
            # Try application default credentials; requires GOOGLE_APPLICATION_CREDENTIALS env var
            try:
                cred = credentials.ApplicationDefault()
            except Exception:
                cred = None
            if cred is not None:
                firebase_admin.initialize_app(cred)
            else:
                # Initialize without explicit credentials (may work in some environments)
                firebase_admin.initialize_app()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Firebase admin init failed: {e}")

def login_with_google(db: Session, id_token: str, device_id: Optional[str], display_name: Optional[str]) -> AuthResponse:
    """Verify Firebase ID token, upsert user, return AuthResponse with api_key."""
    try:
        _ensure_firebase_admin_initialized()
        import firebase_admin
        from firebase_admin import auth as fb_auth

        decoded = fb_auth.verify_id_token(id_token)
        uid = decoded.get("uid")
        email = decoded.get("email")
        name = decoded.get("name") or display_name or "Firebase User"
        if not uid:
            raise HTTPException(status_code=400, detail="Invalid Google ID token")
        if not email:
            # Fallback synthetic email to fit existing schema uniqueness
            email = f"{uid}@firebase.local"

        # Find or create user
        user = get_user_by_email(db, email)
        if not user:
            api_key = generate_api_key()
            user = User(
                email=email,
                display_name=name,
                password_hash="",  # not used for federated users
                api_key=api_key,
                device_ids=[device_id] if device_id else [],
                trust_level=settings.NEW_USER_TRUST_LEVEL,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            # Update display name and device if needed
            updated = False
            if name and user.display_name != name:
                user.display_name = name
                updated = True
            if device_id and device_id not in (user.device_ids or []):
                user.device_ids = (user.device_ids or []) + [device_id]
                updated = True
            if updated:
                user.last_active_at = datetime.utcnow()
                db.commit()

        # Issue access token and return
        access_token_expires = timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
        access_token = create_access_token(
            data={"sub": email, "user_id": user.id},
            expires_delta=access_token_expires
        )
        return AuthResponse(
            user_id=user.id,
            access_token=access_token,
            api_key=user.api_key,
            trust_level=user.trust_level,
            expires_in=int(access_token_expires.total_seconds())
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Google login failed: {e}")