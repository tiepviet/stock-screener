"""Small, dependency-free HTTP security controls for the API.

The application intentionally runs as a single Uvicorn worker because its
state is SQLite/file-backed.  The limiter below is therefore an in-process
safety net, while the durable login attempt counter remains in SQLite.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from threading import RLock

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

_MAX_RATE_LIMIT_KEYS = 10_000


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read a bounded integer environment value without failing startup."""

    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid integer environment value for %s", name)
        return default
    return max(minimum, min(value, maximum))


def _trusted_networks() -> tuple[ipaddress._BaseNetwork, ...]:
    """Parse explicitly trusted reverse-proxy networks.

    There is deliberately no permissive default: a deployment must opt in to
    trusting ``X-Forwarded-For``.  This prevents an internet client from
    bypassing login throttling by supplying a new header value.
    """

    raw = os.getenv("TSE_TRUSTED_PROXY_CIDRS", "")
    networks: list[ipaddress._BaseNetwork] = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            network = ipaddress.ip_network(value, strict=False)
            if network.prefixlen == 0:
                logger.warning("Ignoring overly broad trusted proxy network")
                continue
            networks.append(network)
        except ValueError:
            logger.warning("Ignoring invalid trusted proxy network")
    return tuple(networks)


def _valid_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except (TypeError, ValueError):
        return None


def _is_trusted(value: str, networks: Iterable[ipaddress._BaseNetwork]) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except (TypeError, ValueError):
        return False
    return any(address in network for network in networks)


def resolve_client_ip(request: Request) -> str:
    """Return a bounded client address for auth and rate-limit decisions.

    ``X-Forwarded-For`` is considered only when the direct peer belongs to a
    configured trusted proxy network.  The rightmost untrusted address in the
    chain is selected, which is the standard safe interpretation of a proxy
    chain containing untrusted hops.
    """

    peer = request.client.host if request.client else "unknown"
    peer = str(peer or "unknown")[:128]
    networks = _trusted_networks()
    if not networks or not _is_trusted(peer, networks):
        return peer

    forwarded = request.headers.get("x-forwarded-for", "")
    candidates = [item.strip()[:128] for item in forwarded.split(",") if item.strip()]
    valid = [item for item in candidates if _valid_ip(item) is not None]
    if not valid:
        return peer

    for candidate in reversed(valid):
        if not _is_trusted(candidate, networks):
            return candidate
    return valid[0]


@dataclass
class _RateBucket:
    started: float
    count: int


class _RateLimiter:
    """Bounded fixed-window limiter suitable for a single-worker deployment."""

    def __init__(self, max_keys: int = _MAX_RATE_LIMIT_KEYS) -> None:
        self._buckets: dict[str, _RateBucket] = {}
        self._max_keys = max_keys
        self._lock = RLock()

    def allow(self, key: str, limit: int, window_seconds: float) -> tuple[bool, int, int]:
        """Return ``(allowed, remaining, retry_after_seconds)``."""

        if limit <= 0:
            return True, 0, 0
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or now - bucket.started >= window_seconds:
                if bucket is None and len(self._buckets) >= self._max_keys:
                    # Bound memory before inserting a new key. Expired buckets
                    # are removed first; if all are active, evict the oldest.
                    for old_key, old_bucket in list(self._buckets.items()):
                        if now - old_bucket.started >= window_seconds:
                            self._buckets.pop(old_key, None)
                    while len(self._buckets) >= self._max_keys and self._buckets:
                        oldest_key = next(iter(self._buckets))
                        self._buckets.pop(oldest_key, None)
                bucket = _RateBucket(started=now, count=0)
                self._buckets[key] = bucket

            if bucket.count >= limit:
                retry_after = max(1, int(window_seconds - (now - bucket.started)) + 1)
                return False, 0, retry_after
            bucket.count += 1
            return True, max(0, limit - bucket.count), 0


_RATE_LIMITER = _RateLimiter()


def _max_request_bytes() -> int:
    return _int_env("TSE_MAX_REQUEST_BYTES", 2 * 1024 * 1024, 1024, 50 * 1024 * 1024)


def _max_request_chunks() -> int:
    return _int_env("TSE_MAX_REQUEST_CHUNKS", 4096, 16, 65_536)


def _rate_limit_for(path: str) -> tuple[str, int, float]:
    """Choose a stable policy based on endpoint cost, not the concrete path."""

    if path in {"/health", "/api/v1/health", "/api/v1/auth/setup-status"}:
        return "public", _int_env("TSE_PUBLIC_RATE_LIMIT", 120, 1, 10_000), 60.0
    if path.endswith("/auth/login"):
        return "login", _int_env("TSE_LOGIN_RATE_LIMIT", 30, 1, 10_000), 60.0
    if path.startswith("/api/v1/alerts/channels/") or path.endswith("/portfolio/check"):
        return "alert_delivery", _int_env("TSE_ALERT_RATE_LIMIT", 10, 1, 10_000), 60.0
    portfolio_provider_paths = (
        "/api/v1/portfolio/positions",
        "/api/v1/portfolio/refresh-prices",
    )
    if path.startswith(portfolio_provider_paths):
        return "provider", _int_env("TSE_PROVIDER_RATE_LIMIT", 30, 1, 10_000), 60.0
    provider_prefixes = (
        "/api/v1/markets/",
        "/api/v1/screeners/",
        "/api/v1/signals/",
        "/api/v1/earnings/",
        "/api/v1/backtests",
        "/api/v1/price-targets/",
        "/api/v1/profit-targets/",
        "/api/v1/alerts/scans",
    )
    if path.startswith(provider_prefixes):
        return "provider", _int_env("TSE_PROVIDER_RATE_LIMIT", 30, 1, 10_000), 60.0
    return "api", _int_env("TSE_API_RATE_LIMIT", 120, 1, 10_000), 60.0


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add baseline headers, cache policy, and bounded request throttling."""

    def __init__(
        self,
        app,
        *,
        cors_origins: Iterable[str] = (),
        cors_allow_all: bool = False,
    ) -> None:
        super().__init__(app)
        self.cors_origins = frozenset(cors_origins)
        self.cors_allow_all = cors_allow_all

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        policy, limit, window = _rate_limit_for(path)
        key = resolve_client_ip(request)
        allowed, remaining, retry_after = _RATE_LIMITER.allow(
            f"{policy}:{key}", limit, window
        )
        if not allowed:
            response = self._error_response(
                request,
                429,
                "Too many requests; try again later",
                limit,
                0,
                retry_after=retry_after,
            )
            return response

        def body_error(status_code: int, detail: str) -> JSONResponse:
            return self._error_response(request, status_code, detail, limit, remaining)

        body_limit = _max_request_bytes()
        chunk_limit = _max_request_chunks()
        content_length_values = request.headers.getlist("content-length")
        transfer_encoding = request.headers.get("transfer-encoding")
        declared_size: int | None = None
        if content_length_values:
            raw_length = content_length_values[0].strip()
            if (
                len(content_length_values) != 1
                or transfer_encoding
                or not re.fullmatch(r"[0-9]+", raw_length)
            ):
                return body_error(400, "Invalid Content-Length")
            # Avoid converting an attacker-controlled, arbitrarily long integer
            # before the bounded body check can reject it.
            if len(raw_length) > 20:
                return body_error(413, "Request body is too large")
            declared_size = int(raw_length)
            if declared_size > body_limit:
                return body_error(413, "Request body is too large")

        # Buffer every ASGI request, including an unframed GET/HEAD body. The
        # server normally supplies an empty http.request message for bodyless
        # methods, and always buffering keeps the limit authoritative across
        # ASGI servers and HTTP/2 transports.
        messages = []
        total = 0
        while True:
            message = await request.receive()
            message_type = message.get("type")
            if message_type == "http.disconnect":
                # Never hand a partial/disconnected request to a state-changing
                # route. There is no useful response to send to a gone client,
                # but returning a deterministic response keeps ASGI callers
                # from observing a misleading parser error.
                return body_error(400, "Client disconnected before request completed")
            if message_type != "http.request":
                return body_error(400, "Invalid request body framing")
            body = message.get("body", b"") or b""
            if not isinstance(body, (bytes, bytearray, memoryview)):
                return body_error(400, "Invalid request body framing")
            if len(messages) >= chunk_limit:
                return body_error(413, "Request body has too many chunks")
            total += len(body)
            if total > body_limit:
                return body_error(413, "Request body is too large")
            messages.append(message)
            if not message.get("more_body", False):
                break

        if declared_size is not None and total != declared_size:
            return body_error(400, "Request body length does not match Content-Length")

        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        request._receive = replay_receive

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        self._apply_headers(response, request)
        return response

    def _error_response(
        self,
        request: Request,
        status_code: int,
        detail: str,
        limit: int,
        remaining: int,
        *,
        retry_after: int | None = None,
    ) -> JSONResponse:
        response = JSONResponse(status_code=status_code, content={"detail": detail})
        if retry_after is not None:
            response.headers["Retry-After"] = str(retry_after)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        self._apply_headers(response, request)
        return response

    def _apply_cors_headers(self, response, request: Request) -> None:
        origin = request.headers.get("origin")
        if not origin:
            return
        if self.cors_allow_all:
            response.headers.setdefault("Access-Control-Allow-Origin", "*")
        elif origin in self.cors_origins:
            response.headers.setdefault("Access-Control-Allow-Origin", origin)
            response.headers.setdefault("Access-Control-Allow-Credentials", "true")
            response.headers.setdefault("Vary", "Origin")
        else:
            return
        if request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
            response.headers.setdefault(
                "Access-Control-Allow-Methods",
                "GET, POST, PUT, PATCH, DELETE, OPTIONS",
            )
            response.headers.setdefault(
                "Access-Control-Allow-Headers", "Authorization, Content-Type, Accept"
            )
            response.headers.setdefault("Access-Control-Max-Age", "600")

    def _apply_headers(self, response, request: Request) -> None:
        # These are policy headers, not hints: downstream handlers must not be
        # able to weaken the baseline by supplying a permissive value.
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        if request.url.path in {"/docs", "/redoc"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self' https://cdn.jsdelivr.net; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src 'self' data: https://fastapi.tiangolo.com; "
                "frame-ancestors 'none'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; base-uri 'self'; object-src 'none'; "
                "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; "
                "form-action 'self'"
            )
        if request.url.scheme == "https" or os.getenv("TSE_FORCE_HTTPS_HEADERS", "").lower() in {
            "1",
            "true",
            "yes",
        }:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )

        path = request.url.path
        if path.startswith("/assets/") and response.status_code < 400:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.startswith("/api/") or path in {"/health", "/docs", "/redoc", "/openapi.json"}:
            response.headers["Cache-Control"] = "no-store"
        elif response.status_code < 400:
            response.headers["Cache-Control"] = "no-cache"
        self._apply_cors_headers(response, request)


__all__ = [
    "SecurityHeadersMiddleware",
    "resolve_client_ip",
]
