"""
Phase 6A / 7A / 7B — Authentication routes with rate limiting and refresh tokens.

POST /api/auth/register      — Create a new user account (rate-limited)
POST /api/auth/login         — Authenticate, receive access + refresh tokens (rate-limited)
POST /api/auth/refresh       — Exchange a refresh token for a new token pair (rotation)
POST /api/auth/logout        — Revoke the current refresh session
POST /api/auth/logout-all    — Revoke ALL refresh sessions for the current user
GET  /api/auth/me            — Get the currently authenticated user's info

Phase 7B additions:
- Login returns both access_token (15 min) and refresh_token (30 days)
- POST /api/auth/refresh rotates the refresh token with reuse detection
- POST /api/auth/logout and /logout-all revoke sessions server-side
- Backward compatible: existing TokenResponse response model still accepted
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.models import User
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    RefreshRequest,
    RefreshResponse,
    RegisterRequest,
    LogoutResponse,
    SessionInfo,
    SessionsListResponse,
    TokenResponse,
    UserResponse,
)
from app.services.auth_service import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.services.rate_limiter import get_rate_limiter, get_lockout_duration

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
    db: Session = Depends(get_db),
):
    """
    Authenticate a user and return JWT access + refresh tokens.

    Rate-limited by IP and username. Returns generic 'Invalid credentials'
    for all failure modes.

    Phase 7B: Returns both access_token (15 min) and refresh_token (30 days).
    Initializes a refresh session server-side for the refresh token.
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

    logger.info("User logged in: id=%d username=%s from IP=%s",
                user.id, user.username, client_ip)
    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
    )


# ── POST /api/auth/refresh ─────────────────────────────────


@router.post("/refresh", response_model=RefreshResponse)
def refresh_token(
    payload: RefreshRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Exchange a refresh token for a new access + refresh token pair.

    The old refresh token is revoked (rotation). If a revoked token is
    reused (theft detected), the entire token family is revoked.

    Returns generic 'Invalid refresh token' for all failure modes.
    Rate-limited per client IP to prevent abuse.
    """
    # Rate-limit refresh requests per IP
    client_ip = _client_ip(request)
    limiter = get_rate_limiter()
    refresh_key = _rate_limit_key_refresh(client_ip)
    if limiter.is_rate_limited(
        refresh_key,
        settings.refresh_rate_limit_max_attempts,
        settings.refresh_rate_limit_window_seconds,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(settings.refresh_rate_limit_window_seconds)},
        )

    # rotate_refresh_token validates, revokes old, creates new session,
    # and returns (new_raw_token, user_id). Raises HTTPException(401)
    # on any failure (invalid, expired, revoked, or reuse detected).
    new_raw_token, user_id = rotate_refresh_token(
        db, payload.refresh_token, device_info=f"ip:{client_ip}",
    )

    # Issue a new access token for the correct user
    access_token = create_access_token(
        data={"sub": str(user_id)},
        expires_delta=timedelta(minutes=settings.jwt_access_expiry_minutes),
    )

    cleanup_expired_sessions(db)

    logger.debug("Token refreshed: user_id=%d", user_id)
    return RefreshResponse(
        access_token=access_token,
        refresh_token=new_raw_token,
    )


# ── POST /api/auth/logout ──────────────────────────────────


@router.post("/logout", response_model=LogoutResponse)
def logout_user(
    payload: RefreshRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Revoke the current refresh session (logout from this device).

    Requires the refresh token in the request body and a valid
    access token in the Authorization header.
    """
    revoke_refresh_session(db, current_user, payload.refresh_token)
    logger.info("User logged out: id=%d username=%s", current_user.id, current_user.username)
    return LogoutResponse(detail="Logged out successfully")


# ── POST /api/auth/logout-all ──────────────────────────────


@router.post("/logout-all", response_model=LogoutResponse)
def logout_all(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Revoke ALL refresh sessions for the current user
    (logout from all devices).
    """
    count = revoke_all_user_sessions(db, current_user)
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
    """
    # Extract refresh token from Authorization header if present
    # (the access token is used for auth; the refresh token is in the body
    # for logout, but we can optionally accept it as a query param or header)
    raw_token = None

    sessions = get_user_sessions(db, current_user, current_raw_token=raw_token)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    active_count = sum(
        1 for s in sessions
        if s.revoked_at is None and s.expires_at > now
    )

    # Accurate current-session identification requires a safe session
    # identifier design (e.g., a short session ID stored alongside the
    # refresh token). For now, we do not guess which session is current
    # to avoid misleading the client. All sessions get is_current=False.
    session_infos = []
    for s in sessions:
        session_infos.append(SessionInfo(
            id=s.id,
            created_at=s.created_at,
            last_used_at=s.created_at,  # Proxy: uses created_at until per-refresh tracking is added
            expires_at=s.expires_at,
            revoked_at=s.revoked_at,
            device_info=s.device_info,
            is_current=False,
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
):
    """
    Revoke a specific refresh session by ID.

    Only the session owner can revoke their own sessions.
    Returns 404 if the session is not found or does not belong to the user.
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
