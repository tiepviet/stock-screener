"""FastAPI dependencies for bearer authentication."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.stock_screener import db, jwt_auth

from .security import resolve_client_ip

logger = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated identity used by request handlers."""

    id: int
    username: str
    token_version: int
    can_send_alerts: bool = False


def _unauthorized(detail: str = "Invalid or expired access token") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _decode_user(credentials: HTTPAuthorizationCredentials | None) -> CurrentUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("Authentication required")
    payload = jwt_auth.verify_token(credentials.credentials)
    if not isinstance(payload, dict):
        raise _unauthorized()

    try:
        user_id = int(payload.get("sub", ""))
        username = payload.get("username")
        token_version = int(payload.get("tv"))
    except (TypeError, ValueError):
        raise _unauthorized() from None
    if user_id <= 0 or not isinstance(username, str) or not username.strip():
        raise _unauthorized()

    # Do not trust a token merely because its signature is valid: the user
    # must still exist and the per-user token version must match.  The
    # version is bumped on logout and password changes.
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, username, token_version, can_send_alerts FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if row is None or str(row["username"]) != username:
        raise _unauthorized()
    try:
        current_version = int(row["token_version"])
    except (TypeError, ValueError):
        raise _unauthorized() from None
    if current_version != token_version:
        raise _unauthorized("Access token has been revoked")

    return CurrentUser(
        id=user_id,
        username=username,
        token_version=current_version,
        can_send_alerts=bool(row["can_send_alerts"]),
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser:
    """Return the current user or raise a standards-compliant 401."""

    return _decode_user(credentials)


def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser | None:
    """Optional variant used only by endpoints that can be public."""

    if credentials is None:
        return None
    return _decode_user(credentials)


def client_ip(request: Request) -> str:
    """Get a bounded client address using the configured proxy trust policy."""

    return resolve_client_ip(request)


__all__ = ["CurrentUser", "client_ip", "get_current_user", "get_optional_user"]
