"""
Phase 6A / 7A — Authentication routes with rate limiting.

POST /api/auth/register   — Create a new user account (rate-limited)
POST /api/auth/login      — Authenticate and receive a JWT token (rate-limited)
GET  /api/auth/me         — Get the currently authenticated user's info

All routes return structured JSON responses with clear error messages.
Login errors are intentionally generic to prevent username enumeration.

Phase 7A additions:
- IP-based rate limiting on login and register
- Account lockout after repeated failed login attempts
- Failed-attempt counter reset on successful login
- Expired lockout automatically cleared
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
    RegisterRequest,
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


@router.post("/login", response_model=TokenResponse)
def login_user(
    payload: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Authenticate a user and return a JWT access token.

    Rate-limited by:
      - IP address (configurable max attempts per window)
      - Normalized username (tracks failed attempts for lockout)

    Account lockout:
      - After 'rate_limit_lockout_threshold' consecutive failures, the account
        is locked for an exponentially increasing duration.
      - Lockout is automatically cleared after the duration expires.
      - Successful login resets the failure counter.

    Returns a generic 'Invalid credentials' error for all failure modes
    to prevent username enumeration attacks.

    The returned token should be sent as:
      Authorization: Bearer <token>
    """
    client_ip = _client_ip(request)
    normalized_username = _normalize_username(payload.username)
    limiter = get_rate_limiter()
    ip_key = _rate_limit_key_ip(client_ip)
    user_key = _rate_limit_key_user(normalized_username)

    # 1. Check IP-based rate limit (peek only — don't record yet)
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

    # 3. Check account lockout for existing users
    now = datetime.now(timezone.utc)
    if user is not None and user.locked_until is not None:
        # Use naive UTC for SQLite compatibility
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
            # Lockout has expired — reset the counter
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
        # Record the failed attempt
        limiter.record_attempt(ip_key)

        # Increment account lockout counter
        user.failed_login_attempts += 1

        # Check if we should lock the account
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

    # 6. Successful login — reset counters
    limiter.reset_attempts(ip_key)
    limiter.reset_attempts(user_key)
    user.failed_login_attempts = 0
    user.locked_until = None
    db.commit()

    # Create JWT with user ID as subject (must be string per JWT spec)
    access_token = create_access_token(data={"sub": str(user.id)})

    logger.info("User logged in: id=%d username=%s from IP=%s",
                user.id, user.username, client_ip)
    return TokenResponse(access_token=access_token)


# ── GET /api/auth/me ───────────────────────────────────────


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    """
    Get the currently authenticated user's information.

    Requires a valid Bearer token in the Authorization header.
    Returns the user's public profile (id, username, email, created_at).
    """
    return current_user
