"""
Phase 6A — Authentication service.

Provides JWT creation/verification and password hashing/verification
using python-jose[cryptography] and passlib[bcrypt].

FastAPI dependency `get_current_user` extracts the authenticated user
from the `Authorization: Bearer <token>` header.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.models import User

logger = logging.getLogger(__name__)

# ── Password hashing ───────────────────────────────────────

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Hash a password using bcrypt (12 rounds)."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain-text password against a bcrypt hash."""
    return pwd_context.verify(plain_password, hashed_password)


# ── JWT creation / verification ────────────────────────────


def create_access_token(
    data: dict,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """
    Create a JWT access token.

    Args:
        data: Claims to embed in the token (must include 'sub' for user ID).
        expires_delta: Token lifetime (defaults to config jwt_expiry_hours).

    Returns:
        Encoded JWT string.
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(hours=settings.jwt_expiry_hours)
    )
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """
    Decode and verify a JWT access token.

    Args:
        token: The JWT string to decode.

    Returns:
        The decoded claims dict.

    Raises:
        HTTPException(401): If the token is invalid, expired, or malformed.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        return payload
    except JWTError as exc:
        logger.debug("JWT decode failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── Bearer token scheme ────────────────────────────────────

bearer_scheme = HTTPBearer(auto_error=False)


def _resolve_user_id(
    credentials: Optional[HTTPAuthorizationCredentials],
    db: Session,
) -> tuple[User, int]:
    """
    Resolve the authenticated user from a token, or fall back to default user.

    Phase 6B backward compatibility:
      - If a valid token is provided → return the authenticated user
      - If no token is provided → return the default user (id=1)
      - If an invalid/malformed token is provided → raise 401

    Returns:
        Tuple of (User, user_id).

    Raises:
        HTTPException(401): If the token is invalid or user not found.
    """
    if credentials is None:
        # No token: fall back to default user (backward compat until Phase 6C)
        user = db.query(User).filter(User.id == 1).first()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Default user not found",
            )
        return user, user.id

    # Token provided: verify it
    payload = decode_access_token(credentials.credentials)
    user_id_str: Optional[str] = payload.get("sub")
    if user_id_str is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: missing subject",
        )

    try:
        user_id = int(user_id_str)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: malformed subject",
        )

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    return user, user_id


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    FastAPI dependency that extracts the authenticated User from the JWT.

    Unlike get_optional_user, this ALWAYS requires a valid token.
    Used for endpoints that must have authenticated access (e.g. /api/auth/me).

    Raises:
        HTTPException(401): If no token, or token is invalid/expired.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(credentials.credentials)
    user_id_str: Optional[str] = payload.get("sub")
    if user_id_str is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: missing subject",
        )

    try:
        user_id = int(user_id_str)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: malformed subject",
        )

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    return user


def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    FastAPI dependency for backward-compatible authentication.

    - If a valid token is provided → returns the authenticated User
    - If no token is provided → returns the default user (id=1)
    - If an invalid/malformed token is provided → raises 401

    This allows Phase 6B to be deployed without updating the frontend.
    Once Phase 6C adds frontend auth, users will automatically get
    proper user isolation.
    """
    user, _ = _resolve_user_id(credentials, db)
    return user
