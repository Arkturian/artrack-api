"""Anmeldung von Menschen ueber auth-api (OAuth 2.1 + PKCE).

Der Browser macht PKCE und den Redirect zu auth-api, artrack-api tauscht den
Code und den Refresh-Token gegen auth-api /oauth/token (Backend-to-Backend,
keine CORS-Aenderung in auth-api). Eingehende Bearer-Tokens werden per JWKS
geprueft: RS256, iss = AUTH_ISSUER, aud = AUTH_CLIENT_ID. Damit gelten nur
Tokens, die auth-api fuer genau diesen Client ausgestellt hat.

Rechte: Admin ist, wer in AUTH_ADMIN_SUBS steht oder die Rolle AUTH_ADMIN_ROLE
im System "artrack" hat. Admins arbeiten als system@api, damit sie dieselben
Tracks sehen wie bisher mit dem Master-Schluessel. Alle anderen werden
abgewiesen, solange es keine eigenen Rollen gibt.
"""
import logging
from datetime import datetime
from typing import Optional

import httpx
import jwt
from fastapi import APIRouter, Form, HTTPException
from sqlalchemy.orm import Session

from .config import settings
from .models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/oauth", tags=["auth"])

_jwks_client: Optional[jwt.PyJWKClient] = None


def oauth_enabled() -> bool:
    return bool(settings.AUTH_CLIENT_ID)


def _jwks() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        # PyJWKClient caches the keys and refetches on an unknown kid
        _jwks_client = jwt.PyJWKClient(
            f"{settings.AUTH_API_BASE}/api/v1/auth/.well-known/jwks.json",
            cache_keys=True,
            lifespan=3600,
        )
    return _jwks_client


def verify_bearer(token: str) -> dict:
    """Return the verified claims or raise HTTPException(401)."""
    if not oauth_enabled():
        raise HTTPException(status_code=401, detail="Bearer login is not configured")
    try:
        key = _jwks().get_signing_key_from_jwt(token).key
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=settings.AUTH_ISSUER,
            audience=settings.AUTH_CLIENT_ID,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWKClientError as e:
        logger.warning("JWKS lookup failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid token", headers={"WWW-Authenticate": "Bearer"})
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}", headers={"WWW-Authenticate": "Bearer"})


def is_admin(claims: dict) -> bool:
    if claims.get("sub") in settings.AUTH_ADMIN_SUBS:
        return True
    for r in claims.get("roles") or []:
        if isinstance(r, dict) and r.get("sys") == "artrack" and r.get("role") == settings.AUTH_ADMIN_ROLE:
            return True
    return False


def user_for_claims(claims: dict, db: Session) -> User:
    """Map a verified person to the ArTrack user they act as."""
    if not is_admin(claims):
        raise HTTPException(status_code=403, detail="Kein Zugang zu ARTrack fuer dieses Konto")
    user = db.query(User).filter(User.email == "system@api").first()
    if not user:
        raise HTTPException(status_code=500, detail="system@api fehlt")
    user.last_active_at = datetime.utcnow()
    db.commit()
    user._oauth_sub = claims.get("sub")
    user._oauth_email = claims.get("email")
    return user


async def _token_request(data: dict) -> dict:
    data = {**data, "client_id": settings.AUTH_CLIENT_ID}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{settings.AUTH_API_BASE}/api/v1/auth/oauth/token", data=data)
    try:
        body = r.json()
    except ValueError:
        body = {"error": "invalid_response"}
    if r.status_code != 200:
        # Pass the OAuth error through (invalid_grant etc.), never the request
        raise HTTPException(status_code=400 if r.status_code < 500 else 502, detail=body)
    return body


@router.get("/config")
def oauth_config():
    """What the dashboard needs to start the PKCE redirect. All values are public."""
    return {
        "enabled": oauth_enabled(),
        "client_id": settings.AUTH_CLIENT_ID or None,
        "authorize_url": f"{settings.AUTH_API_BASE}/api/v1/auth/oauth/authorize",
    }


@router.post("/token")
async def oauth_token(
    grant_type: str = Form(...),
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
    refresh_token: Optional[str] = Form(None),
):
    """Exchange an authorization code or refresh token at auth-api.

    Only the two grants a browser login needs are relayed. The access token is
    checked here as well, so a person without ArTrack access gets 403 right at
    login instead of an empty dashboard.
    """
    if not oauth_enabled():
        raise HTTPException(status_code=503, detail="Bearer login is not configured")
    if grant_type == "authorization_code":
        if not (code and redirect_uri and code_verifier):
            raise HTTPException(status_code=400, detail="code, redirect_uri und code_verifier sind noetig")
        body = await _token_request({"grant_type": grant_type, "code": code, "redirect_uri": redirect_uri, "code_verifier": code_verifier})
    elif grant_type == "refresh_token":
        if not refresh_token:
            raise HTTPException(status_code=400, detail="refresh_token fehlt")
        body = await _token_request({"grant_type": grant_type, "refresh_token": refresh_token})
    else:
        raise HTTPException(status_code=400, detail="grant_type nicht unterstuetzt")
    claims = verify_bearer(body.get("access_token", ""))
    if not is_admin(claims):
        raise HTTPException(status_code=403, detail="Kein Zugang zu ARTrack fuer dieses Konto")
    return {
        "access_token": body.get("access_token"),
        "refresh_token": body.get("refresh_token"),
        "expires_in": body.get("expires_in"),
        "token_type": body.get("token_type", "Bearer"),
        "user": {"sub": claims.get("sub"), "email": claims.get("email"), "name": claims.get("name"), "picture": claims.get("picture")},
    }
