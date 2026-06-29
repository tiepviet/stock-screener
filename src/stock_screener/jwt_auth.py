"""
JWT auth — 30-day tokens for "stay logged in" UX.

The token is signed with a server-side secret (HS256) and stored in the
browser's localStorage. On every page load the dashboard reads the
token, verifies signature + expiry, and silently logs the user in.

Secret management:
  - Auto-generated on first use, stored at data/jwt_secret.key
  - 32 bytes, URL-safe base64
  - File is in .gitignore — losing it forces all users to re-login
  - Rotate by deleting the file; existing tokens become invalid

Security notes:
  - HTTPS required in production (localStorage is readable by any JS on
    the same origin). The Streamlit deployment must terminate TLS.
  - 30-day window is a UX trade-off; tighten by setting `days_valid`
    in `create_token`.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt

from . import db

logger = logging.getLogger(__name__)

_ALGO = "HS256"
_SECRET_PATH = db.DATA_DIR / "jwt_secret.key"
_ISSUER = "tse-stock-screener"
_TOKEN_DAYS = 30


def _load_or_create_secret(path: Path | None = None) -> str:
    """Return the JWT signing secret, creating it on first use."""
    env_secret = os.environ.get("TSE_JWT_SECRET")
    if env_secret:
        return env_secret
    p = path or _SECRET_PATH
    if p.exists():
        return p.read_text().strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(32)
    p.write_text(secret)
    try:
        p.chmod(0o600)  # owner read/write only
    except OSError:
        pass  # Windows / non-POSIX FS
    logger.info("Generated new JWT secret at %s", p)
    return secret


def create_token(
    user_id: int,
    username: str,
    days_valid: int = _TOKEN_DAYS,
    secret: str | None = None,
) -> str:
    """Create a signed JWT for the given user. Default lifetime: 30 days."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "iss": _ISSUER,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=days_valid)).timestamp()),
    }
    key = secret or _load_or_create_secret()
    return jwt.encode(payload, key, algorithm=_ALGO)


def verify_token(token: str, secret: str | None = None) -> dict | None:
    """Verify signature + expiry. Returns decoded payload, or None on any failure.

    Does NOT verify the user still exists in the DB — call sites should
    cross-check `sub` against `auth.get_by_username` if they care.
    """
    if not token or not isinstance(token, str):
        return None
    key = secret or _load_or_create_secret()
    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=[_ALGO],
            issuer=_ISSUER,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        logger.info("JWT expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.info("JWT invalid: %s", e)
        return None
    return payload


def rotate_secret(path: Path | None = None) -> Path:
    """Force a new secret. Invalidates all existing tokens (next page
    load will redirect to the login form)."""
    p = path or _SECRET_PATH
    if p.exists():
        p.unlink()
    _load_or_create_secret(p)
    return p
