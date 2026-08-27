"""
Phase 7C — HttpOnly refresh cookie & double-submit CSRF protection.

Responsibilities:
- Set/clear the HttpOnly refresh cookie on auth responses.
- Set/clear the non-HttpOnly CSRF cookie used for double-submit CSRF.
- FastAPI dependency ``require_csrf`` that enforces CSRF on state-changing
  requests using constant-time comparison between the X-CSRF-Token header
  and the CSRF cookie.

Design notes (CSRF enforcement scope):
- CSRF protection is meaningful for browser-originated requests. Requests
  that carry an ``Origin`` header (browsers) are enforced. Requests without
  an ``Origin`` header (curl, httpx test clients, server-to-server) are
  skipped, which keeps the existing API-client integration suites working
  while still protecting the browser frontend.
- The refresh cookie is HttpOnly and scoped to ``Path=/api/auth`` so it is
  never visible to JavaScript and is only sent to auth endpoints.
- SameSite=Lax (configurable) provides an additional browser-level layer.
"""

import hmac
import logging
import secrets

from fastapi import HTTPException, Request, Response, status

from app.config import settings

logger = logging.getLogger(__name__)


# ── Refresh cookie (HttpOnly) ──────────────────────────────


def set_refresh_cookie(response: Response, token: str) -> None:
    """Set the HttpOnly refresh-token cookie on a response.

    The cookie is scoped to Path=/api/auth so it is only sent to auth
    endpoints. Max-Age matches JWT_REFRESH_EXPIRY_DAYS.
    """
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=token,
        max_age=settings.jwt_refresh_expiry_days * 24 * 60 * 60,
        path="/api/auth",
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
    )


def clear_refresh_cookie(response: Response) -> None:
    """Expire the HttpOnly refresh-token cookie."""
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path="/api/auth",
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
    )


def get_refresh_token(request: Request) -> str | None:
    """Return the raw refresh token from the HttpOnly cookie (or None)."""
    return request.cookies.get(settings.refresh_cookie_name)


# ── CSRF cookie (non-HttpOnly, double-submit) ──────────────


def set_csrf_cookie(response: Response) -> str:
    """Set a fresh CSRF cookie on a response and return its value.

    The cookie is intentionally NOT HttpOnly so the frontend can read it
    and echo it back in the X-CSRF-Token header (double-submit pattern).
    """
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=token,
        max_age=settings.jwt_refresh_expiry_days * 24 * 60 * 60,
        path="/",
        httponly=False,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
    )
    return token


def clear_csrf_cookie(response: Response) -> None:
    """Expire the CSRF cookie."""
    response.delete_cookie(
        key=settings.csrf_cookie_name,
        path="/",
        secure=settings.cookie_secure,
        samesite="lax",
    )


def get_csrf_token(request: Request) -> str | None:
    """Return the CSRF token from the cookie (or None)."""
    return request.cookies.get(settings.csrf_cookie_name)


# ── CSRF dependency ────────────────────────────────────────


def require_csrf(request: Request) -> None:
    """FastAPI dependency enforcing double-submit CSRF.

    Only enforces when the request carries an ``Origin`` header (i.e. it is
    a browser request). Compares the ``X-CSRF-Token`` header to the CSRF
    cookie with a constant-time comparison. Raises 403 on missing/mismatch.

    GET/HEAD/OPTIONS requests are not subject to this dependency (callers
    only attach it to state-changing routes), so no method check is needed
    here; the dependency is simply only used on POST/PATCH/PUT/DELETE routes.
    """
    if "Origin" not in request.headers:
        # Not a browser request (curl / API client) — CSRF does not apply.
        return

    header_token = request.headers.get("X-CSRF-Token")
    cookie_token = get_csrf_token(request)

    if not header_token or not cookie_token:
        logger.debug("CSRF check failed: missing token (header=%s cookie=%s)",
                     bool(header_token), bool(cookie_token))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing",
        )

    if not hmac.compare_digest(header_token, cookie_token):
        logger.debug("CSRF check failed: token mismatch")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token mismatch",
        )
