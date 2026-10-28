"""Proxy-aware access checks for setup and remote (tunnel) API access.

Tunnel products (Tailscale Serve, Cloudflare Tunnel, nginx) connect to the
app from loopback, so ``request.client.host`` is ``127.0.0.1`` even for a
REMOTE caller. Client address alone therefore cannot decide whether a
request is local. Any forwarding header is treated as a proxy signal and
the request is classified as remote (fail closed).

Two guards live here:

* :func:`is_remote_request` -- true when a forwarding header is present OR
  the peer address is not loopback.
* :func:`has_valid_session` -- a valid Bearer access token or a live
  HttpOnly refresh session cookie.

Nothing in this module logs header values, tokens, or cookie values.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models.models import RefreshSession, User
from app.services.auth_service import decode_access_token
from app.services.refresh_token_service import hash_refresh_token

logger = logging.getLogger(__name__)

#: Peer addresses that mean "this machine". ``testclient`` is Starlette's
#: in-process TestClient default; it never represents a network peer.
LOOPBACK_HOSTS = frozenset(
    {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1", "testclient"}
)

#: Header presence (any value) marks the request as arriving through a proxy.
#: X-Forwarded-For / Forwarded are the required signals; the rest are common
#: markers set by Cloudflare Tunnel and reverse proxies.
FORWARDING_HEADERS = (
    "x-forwarded-for",
    "forwarded",
    "x-real-ip",
    "cf-connecting-ip",
)

#: Paths reachable without a session from a remote caller. Everything else
#: under /api requires one. Refresh/logout are NOT listed: they carry the
#: HttpOnly session cookie, so the guard validates it before the route runs.
PUBLIC_API_PATHS = frozenset({
    "/health",
    "/api/setup/status",
    "/api/auth/login",
    "/api/auth/register",
})


def is_loopback_host(host: Optional[str]) -> bool:
    """Return True only for a known loopback peer address."""
    return bool(host) and host in LOOPBACK_HOSTS


def is_loopback_client(request: Request) -> bool:
    """Return True when the direct peer of ``request`` is loopback."""
    client = getattr(request, "client", None)
    return is_loopback_host(getattr(client, "host", None))


def is_forwarded(request: Request) -> bool:
    """Return True when a proxy/tunnel forwarding header is present."""
    headers = request.headers
    return any(name in headers for name in FORWARDING_HEADERS)


def is_remote_request(request: Request) -> bool:
    """A request is remote when it is proxied or the peer is not loopback."""
    return is_forwarded(request) or not is_loopback_client(request)


def is_public_api_path(path: str) -> bool:
    """True for the small allow-list that stays open to remote callers."""
    clean = "/" + (path or "").lstrip("/")
    return clean in PUBLIC_API_PATHS


def _bearer_token(request: Request) -> Optional[str]:
    """Return the Bearer token, or None when no Authorization header exists.

    An Authorization header with any other scheme is treated as invalid.
    """
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return ""
    return token


def _valid_bearer(token: str, db: Session) -> bool:
    """Verify signature/expiry and that the subject still exists."""
    try:
        payload = decode_access_token(token)
        user_id = int(payload.get("sub"))
    except (HTTPException, TypeError, ValueError):
        return False
    return db.query(User.id).filter(User.id == user_id).first() is not None


def _valid_refresh_cookie(raw_token: str, db: Session) -> bool:
    """Verify a live (unrevoked, unexpired) refresh session cookie."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = (
        db.query(RefreshSession.id)
        .join(User, User.id == RefreshSession.user_id)
        .filter(
            RefreshSession.token_hash == hash_refresh_token(raw_token),
            RefreshSession.revoked_at.is_(None),
            RefreshSession.expires_at > now,
        )
        .first()
    )
    return row is not None


def has_valid_session(request: Request, db: Optional[Session] = None) -> bool:
    """True when the request carries a valid access token or session cookie.

    Fail closed: an invalid Bearer header never falls back to the cookie, and
    any storage error is treated as "not authenticated". Never logs secrets.
    """
    own_session = db is None
    if own_session:
        try:
            db = SessionLocal()
        except Exception as exc:  # noqa: BLE001 - auth check must never raise
            logger.warning("session check unavailable (%s)", type(exc).__name__)
            return False
    try:
        token = _bearer_token(request)
        if token is not None:
            return bool(token) and _valid_bearer(token, db)

        raw_cookie = request.cookies.get(settings.refresh_cookie_name)
        if not raw_cookie:
            return False
        return _valid_refresh_cookie(raw_cookie, db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("session check failed (%s)", type(exc).__name__)
        return False
    finally:
        if own_session and db is not None:
            db.close()


__all__ = [
    "LOOPBACK_HOSTS",
    "FORWARDING_HEADERS",
    "PUBLIC_API_PATHS",
    "is_loopback_host",
    "is_loopback_client",
    "is_forwarded",
    "is_remote_request",
    "is_public_api_path",
    "has_valid_session",
]
