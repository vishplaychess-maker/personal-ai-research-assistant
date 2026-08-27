"""
Phase 6A / 7A / 7B / 7C — Authentication routes.

POST /api/auth/register      — Create a new user account (rate-limited)
POST /api/auth/login         — Authenticate; sets HttpOnly refresh cookie (rate-limited)
POST /api/auth/refresh       — Rotate refresh cookie → new access token + new refresh cookie
POST /api/auth/logout        — Revoke the current refresh session; clears cookies
POST /api/auth/logout-all    — Revoke ALL refresh sessions; clears cookies
GET  /api/auth/me            — Get the currently authenticated user's info
GET  /api/auth/sessions      — List sessions; current-session identified via refresh cookie

Phase 7B additions:
- Access token (15 min) + refresh-token rotation with reuse detection
- Session revocation, current-session detection, last_used_at tracking

Phase 7C additions:
- The raw refresh token is delivered ONLY as an HttpOnly cookie
  (research_assistant_refresh_token, Path=/api/auth). It is never returned
  in JSON responses and never accepted in the JSON body.
- A non-HttpOnly CSRF cookie (research_assistant_csrf_token) implements
  double-submit CSRF protection on state-changing endpoints.
- Login and registration remain exempt from CSRF (pre-authentication).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.models import User
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    LogoutResponse,
    SessionInfo,
    SessionsListResponse,
    UserResponse,
    RefreshResponse,
)
from app.services.auth_service import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.services.rate_limiter import get_rate_limiter, get_lockout_duration
from app.services.cookie_service import (
    set_refresh_cookie,
    clear_refresh_cookie,
    set_csrf_cookie,
    clear_csrf_cookie,
    get_refresh_token,
    require_csrf,
)
from app.services.refresh_token_service import (
    create_refresh_session,
    rotate_refresh_token,
    revoke_refresh_session,
    revoke_all_user_sessions,
    get_user_sessions,
    revoke_session_by_id,
    is_current_session,
    cleanup_expired_sessions,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── Helpers ────────────────────────────────────────────────


def _client_ip(request: Request) -> str:
    """Extract the client IP address from the request, respecting proxies."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _normalize_username(username: str) -> str:
    """Normalize a username for rate-limit tracking (lowercase, strip)."""
    return username.strip().lower()


def _rate_limit_key_ip(client_ip: str) -> str:
    """Rate-limit key for IP-based tracking."""
    return f"rl_ip:{client_ip}"


def _rate_limit_key_user(normalized_username: str) -> str:
    """Rate-limit key for username-based tracking."""
    return f"rl_user:{normalized_username}"


def _rate_limit_key_refresh(client_ip: str) -> str:
    """Rate-limit key for refresh endpoint tracking."""
    return f"rl_refresh:{client_ip}"


# ── POST /api/auth/register ────────────────────────────────


@router.post("/register", response_model=UserResponse, status_code=201)
def register_user(
    payload: RegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Register a new user account.

    Validates that the username and email are unique, hashes the password
    with bcrypt, and returns the new user's public info (no password).

    The user must log in separately to receive a JWT token.
    Registration is rate-limited by IP address.
    """
    # Rate-limit registration by IP
    limiter = get_rate_limiter()
    ip_key = _rate_limit_key_ip(_client_ip(request))
    if limiter.is_rate_limited(ip_key, settings.rate_limit_max_attempts, settings.rate_limit_window_seconds):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(settings.rate_limit_window_seconds)},
        )

    # Check username uniqueness
    existing_username = (
        db.query(User).filter(User.username == payload.username).first()
    )
    if existing_username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already taken",
        )

    # Check email uniqueness
    if payload.email:
        existing_email = (
            db.query(User).filter(User.email == payload.email).first()
        )
        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )

    # Hash the password
    hashed = hash_password(payload.password)

    # Create user
    user = User(
        username=payload.username,
        email=payload.email,
        hashed_password=hashed,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    logger.info("New user registered: id=%d username=%s from IP=%s",
                user.id, user.username, _client_ip(request))
    return user


# ── POST /api/auth/login ───────────────────────────────────


@router.post("/login", response_model=LoginResponse)
def login_user(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """
    Authenticate a user and return a JWT access token.

    Rate-limited by IP and username. Returns generic 'Invalid credentials'
    for all failure modes.

    Phase 7C: The refresh token is delivered exclusively as an HttpOnly
    cookie (`research_assistant_refresh_token`), never in the JSON response.
    A CSRF cookie is also set so the browser can echo it in state-changing
    requests (double-submit CSRF).
    """
    client_ip = _client_ip(request)
    normalized_username = _normalize_username(payload.username)
    limiter = get_rate_limiter()
    ip_key = _rate_limit_key_ip(client_ip)
    user_key = _rate_limit_key_user(normalized_username)

    # 1. Check IP-based rate limit
    is_ip_limited, _ = limiter.peek_rate_limit(
        ip_key, settings.rate_limit_max_attempts, settings.rate_limit_window_seconds
    )
    if is_ip_limited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(settings.rate_limit_window_seconds)},
        )

    # 2. Find user
    user: Optional[User] = (
        db.query(User).filter(User.username == payload.username).first()
    )

    # 3. Check account lockout
    now = datetime.now(timezone.utc)
    if user is not None and user.locked_until is not None:
        lockout_naive = user.locked_until.replace(tzinfo=timezone.utc)
        if lockout_naive > now:
            remaining = int((lockout_naive - now).total_seconds())
            limiter.record_attempt(ip_key)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please try again later.",
                headers={"Retry-After": str(max(remaining, 1))},
            )
        else:
            user.failed_login_attempts = 0
            user.locked_until = None
            db.commit()

    # 4. Generic error for unknown user or missing password
    if user is None or not user.hashed_password:
        limiter.record_attempt(ip_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 5. Verify password
    if not verify_password(payload.password, user.hashed_password):
        limiter.record_attempt(ip_key)
        user.failed_login_attempts += 1

        if user.failed_login_attempts >= settings.rate_limit_lockout_threshold:
            lockout_duration = get_lockout_duration(
                user.failed_login_attempts,
                settings.rate_limit_lockout_base_seconds,
                settings.rate_limit_lockout_max_seconds,
            )
            user.locked_until = (
                datetime.now(timezone.utc).replace(tzinfo=None)
                + timedelta(seconds=lockout_duration)
            )
            logger.info(
                "Account locked: user_id=%d username=%s failures=%d duration=%ds from IP=%s",
                user.id, user.username, user.failed_login_attempts,
                lockout_duration, client_ip,
            )

        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 6. Successful login — reset lockout counters
    limiter.reset_attempts(ip_key)
    limiter.reset_attempts(user_key)
    user.failed_login_attempts = 0
    user.locked_until = None
    db.commit()

    # 7. Create access token (short-lived)
    access_token = create_access_token(
        data={"sub": str(user.id)},
        expires_delta=timedelta(minutes=settings.jwt_access_expiry_minutes),
    )

    # 8. Create refresh session
    refresh_token = create_refresh_session(
        db, user, device_info=f"ip:{client_ip}",
    )

    # 9. Periodic cleanup (every login, lightweight)
    cleanup_expired_sessions(db)

    # 10. Deliver the refresh token as an HttpOnly cookie and set a CSRF
    #     cookie for double-submit protection on subsequent requests.
    set_refresh_cookie(response, refresh_token)
    set_csrf_cookie(response)

    logger.info("User logged in: id=%d username=%s from IP=%s",
                user.id, user.username, client_ip)
    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
    )


# ── POST /api/auth/refresh ─────────────────────────────────


@router.post("/refresh", response_model=RefreshResponse)
def refresh_token(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    """
    Exchange the HttpOnly refresh cookie for a new access token + new cookie.

    The refresh token is read ONLY from the HttpOnly cookie. A JSON body
    token is ignored after Phase 7C. The old token is revoked (rotation);
    if a revoked token is reused (theft detected), the entire token family
    is revoked and the cookies are cleared.

    Returns generic 'Invalid refresh token' for all failure modes.
    Rate-limited per client IP to prevent abuse.
    CSRF-protected via double-submit (require_csrf).
    """
    # Rate-limit refresh requests per IP
    client_ip = _client_ip(request)
    limiter = get_rate_limiter()
    refresh_key = _rate_limit_key_refresh(client_ip)
    if limiter.is_rate_limited(
        refresh_key,
        settings.refresh_rate_limit_requests,
        settings.refresh_rate_limit_window_seconds,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(settings.refresh_rate_limit_window_seconds)},
        )

    # Read the refresh token from the HttpOnly cookie only.
    raw_token = get_refresh_token(request)
    if not raw_token:
        # No refresh cookie present → return a JSONResponse with both cookies
        # cleared (consistent with the error path below). Clearing on the
        # injected `response` param would be silently discarded by FastAPI
        # when an exception is raised, so we return the response explicitly.
        error_response = JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Invalid refresh token"},
        )
        clear_refresh_cookie(error_response)
        clear_csrf_cookie(error_response)
        return error_response

    try:
        # rotate_refresh_token validates, revokes old, creates new session,
        # and returns (new_raw_token, user_id). Raises HTTPException(401)
        # on any failure (invalid, expired, revoked, or reuse detected).
        new_raw_token, user_id = rotate_refresh_token(
            db, raw_token, device_info=f"ip:{client_ip}",
        )
    except HTTPException as exc:
        # Invalid / expired / revoked / reuse-detected → clear the cookies
        # so the browser does not keep sending a dead token.
        #
        # NOTE: FastAPI discards cookies set on the injected `response` param
        # when an exception is raised (the exception handler builds a fresh
        # response object). We therefore return the error response explicitly
        # with the cleared cookies attached to it.
        error_response = JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )
        clear_refresh_cookie(error_response)
        clear_csrf_cookie(error_response)
        return error_response

    # Issue a new access token for the correct user
    access_token = create_access_token(
        data={"sub": str(user_id)},
        expires_delta=timedelta(minutes=settings.jwt_access_expiry_minutes),
    )

    cleanup_expired_sessions(db)

    # Rotate both cookies: new refresh token + fresh CSRF token.
    set_refresh_cookie(response, new_raw_token)
    set_csrf_cookie(response)

    logger.debug("Token refreshed: user_id=%d", user_id)
    return RefreshResponse(
        access_token=access_token,
        token_type="bearer",
    )


# ── POST /api/auth/logout ──────────────────────────────────


@router.post("/logout", response_model=LogoutResponse)
def logout_user(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    """
    Revoke the current refresh session (logout from this device).

    The refresh token is read from the HttpOnly cookie (never the body).
    Clears both the refresh and CSRF cookies.
    Requires a valid access token in the Authorization header.
    CSRF-protected via double-submit (require_csrf).
    """
    raw_token = get_refresh_token(request)
    if raw_token:
        revoke_refresh_session(db, current_user, raw_token)
    clear_refresh_cookie(response)
    clear_csrf_cookie(response)
    logger.info("User logged out: id=%d username=%s", current_user.id, current_user.username)
    return LogoutResponse(detail="Logged out successfully")


# ── POST /api/auth/logout-all ──────────────────────────────


@router.post("/logout-all", response_model=LogoutResponse)
def logout_all(
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    """
    Revoke ALL refresh sessions for the current user
    (logout from all devices). Clears both cookies.
    CSRF-protected via double-submit (require_csrf).
    """
    count = revoke_all_user_sessions(db, current_user)
    clear_refresh_cookie(response)
    clear_csrf_cookie(response)
    logger.info("User logged out from all devices: id=%d username=%s sessions=%d",
                current_user.id, current_user.username, count)
    return LogoutResponse(detail=f"Logged out from {count} device(s)")


# ── GET /api/auth/sessions ────────────────────────────────


@router.get("/sessions", response_model=SessionsListResponse)
def list_sessions(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    List all refresh sessions for the authenticated user.

    Returns safe metadata only (no token hashes, no raw tokens).
    The current session is identified by `is_current` flag.

    Phase 7C: the current session is identified using the HttpOnly refresh
    cookie on the backend (no X-Refresh-Token header required). The raw
    token is hashed internally and matched against stored token_hash values
    with a constant-time comparison. If the cookie is missing, invalid,
    expired, or revoked, all sessions return is_current=False. Refresh
    tokens are never accepted via query params or headers.
    """
    raw_token = get_refresh_token(request)

    sessions = get_user_sessions(db, current_user)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    active_count = sum(
        1 for s in sessions
        if s.revoked_at is None and s.expires_at > now
    )

    session_infos = []
    for s in sessions:
        # Mark current only for the exact matching token AND an active
        # (non-revoked, non-expired) session. Constant-time hash comparison
        # is performed inside is_current_session().
        is_current = (
            is_current_session(s, raw_token)
            and s.revoked_at is None
            and s.expires_at > now
        )
        session_infos.append(SessionInfo(
            id=s.id,
            created_at=s.created_at,
            last_used_at=s.last_used_at or s.created_at,
            expires_at=s.expires_at,
            revoked_at=s.revoked_at,
            device_info=s.device_info,
            is_current=is_current,
        ))

    return SessionsListResponse(
        sessions=session_infos,
        total=len(session_infos),
        active_count=active_count,
    )


# ── POST /api/auth/sessions/{session_id}/revoke ────────────


@router.post("/sessions/{session_id}/revoke", response_model=LogoutResponse)
def revoke_session(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    """
    Revoke a specific refresh session by ID.

    Only the session owner can revoke their own sessions.
    Returns 404 if the session is not found or does not belong to the user.
    CSRF-protected via double-submit (require_csrf).
    """
    revoked = revoke_session_by_id(db, current_user, session_id)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )

    logger.info(
        "User revoked session: user_id=%d session_id=%d",
        current_user.id, session_id,
    )
    return LogoutResponse(detail="Session revoked")


# ── GET /api/auth/me ───────────────────────────────────────


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    """
    Get the currently authenticated user's information.

    Requires a valid Bearer token in the Authorization header.
    Returns the user's public profile (id, username, email, created_at).
    """
    return current_user
