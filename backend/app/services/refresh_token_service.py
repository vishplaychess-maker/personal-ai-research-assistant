"""
Phase 7B — Refresh Token Service.

Provides secure refresh token management:
- SHA-256 hashed storage (never store raw tokens)
- Token family IDs for reuse/theft detection
- Rotation: each refresh produces a new token, old one revoked
- Reuse detection: if a revoked token is used, the entire family is revoked
- Periodic cleanup of expired sessions
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import User, RefreshSession

logger = logging.getLogger(__name__)


# ── Token generation / hashing ─────────────────────────────


def generate_refresh_token() -> tuple[str, str, str]:
    """Generate a cryptographically secure refresh token.

    Returns:
        Tuple of (raw_token, sha256_hash, family_id).
        The raw token is returned to the client; only the hash is stored.
        family_id is a UUIDv4 identifying the token family.
    """
    raw_token = secrets.token_urlsafe(48)  # 48 bytes → 64 chars base64url
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    family_id = str(uuid4())
    return raw_token, token_hash, family_id


def hash_refresh_token(raw_token: str) -> str:
    """Return the SHA-256 hex digest of a raw refresh token."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


# ── Session CRUD ───────────────────────────────────────────


def create_refresh_session(
    db: Session,
    user: User,
    device_info: Optional[str] = None,
) -> str:
    """Create a new refresh session and return the raw token string.

    The raw token is returned to the caller (to give to the client).
    Only the SHA-256 hash is stored in the database.

    Automatically enforces the max active sessions limit:
    if the user has more active sessions than the configured maximum,
    the oldest sessions are revoked.
    """
    raw_token, token_hash, family_id = generate_refresh_token()
    expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
        days=settings.jwt_refresh_expiry_days
    )

    session = RefreshSession(
        user_id=user.id,
        family_id=family_id,
        token_hash=token_hash,
        expires_at=expires_at,
        device_info=device_info,
    )
    db.add(session)
    db.flush()

    # Enforce max active sessions after creating the new one
    enforce_max_active_sessions(db, user, settings.max_active_sessions)

    db.commit()

    logger.debug(
        "Created refresh session: user_id=%d session_id=%d",
        user.id, session.id,
    )
    return raw_token


def rotate_refresh_token(
    db: Session,
    old_raw_token: str,
    device_info: Optional[str] = None,
) -> tuple[str, int]:
    """Rotate a refresh token: revoke the old one and issue a new one.

    Returns a tuple (new_raw_token, user_id) to avoid a separate lookup
    after rotation by the caller.

    Raises:
        HTTPException(401): If the token is invalid, expired, or revoked.
        HTTPException(401): If reuse is detected (revoked token reused).
    """
    token_hash = hash_refresh_token(old_raw_token)

    session = db.query(RefreshSession).filter(
        RefreshSession.token_hash == token_hash
    ).first()

    if session is None:
        logger.warning("Refresh token not found (possible ghost token)")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    user_id = session.user_id

    # Check expiry
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if session.expires_at <= now:
        logger.debug("Refresh token expired: session_id=%d user_id=%d", session.id, user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token expired",
        )

    # Check if already revoked → reuse detection
    if session.revoked_at is not None:
        # Token theft detected! Revoke the entire family.
        _revoke_family(db, session.family_id)
        logger.warning(
            "Refresh token reuse detected! Revoked family %s "
            "(user_id=%d session_id=%d)",
            session.family_id, user_id, session.id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    # Revoke the current session
    session.revoked_at = now
    db.flush()

    # Create a new session in the same family with a new token
    raw_token, new_token_hash, _ = generate_refresh_token()
    expires_at = now + timedelta(days=settings.jwt_refresh_expiry_days)

    new_session = RefreshSession(
        user_id=user_id,
        family_id=session.family_id,
        token_hash=new_token_hash,
        expires_at=expires_at,
        device_info=device_info,
    )
    db.add(new_session)
    db.commit()

    logger.debug(
        "Rotated refresh token: user_id=%d old_session=%d new_session=%d",
        user_id, session.id, new_session.id,
    )
    return raw_token, user_id


def revoke_refresh_session(
    db: Session,
    user: User,
    raw_token: str,
) -> None:
    """Revoke a specific refresh session for the given user.

    The token must belong to the authenticated user to prevent
    cross-user revocation.
    """
    token_hash = hash_refresh_token(raw_token)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    session = db.query(RefreshSession).filter(
        RefreshSession.token_hash == token_hash,
        RefreshSession.user_id == user.id,
        RefreshSession.revoked_at.is_(None),
    ).first()

    if session is not None:
        session.revoked_at = now
        db.commit()
        logger.debug(
            "Revoked refresh session: user_id=%d session_id=%d",
            user.id, session.id,
        )


def revoke_all_user_sessions(db: Session, user: User) -> int:
    """Revoke ALL active refresh sessions for the given user.

    Returns the number of sessions revoked.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    count = (
        db.query(RefreshSession)
        .filter(
            RefreshSession.user_id == user.id,
            RefreshSession.revoked_at.is_(None),
        )
        .update({"revoked_at": now}, synchronize_session=False)
    )
    db.commit()
    logger.info(
        "Revoked all refresh sessions: user_id=%d count=%d",
        user.id, count,
    )
    return count


def get_user_sessions(
    db: Session,
    user: User,
    current_raw_token: Optional[str] = None,
) -> list[RefreshSession]:
    """Return all non-expired refresh sessions for the given user.

    If current_raw_token is provided, marks that session as current.
    Sorted by created_at descending (most recent first).
    Does NOT include token_hash or any sensitive data in the result.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    current_hash = hash_refresh_token(current_raw_token) if current_raw_token else None

    sessions = (
        db.query(RefreshSession)
        .filter(
            RefreshSession.user_id == user.id,
        )
        .order_by(RefreshSession.created_at.desc())
        .all()
    )
    return sessions


def revoke_session_by_id(
    db: Session,
    user: User,
    session_id: int,
) -> bool:
    """Revoke a specific refresh session by ID for the given user.

    Returns True if a session was revoked, False if not found.
    Prevents cross-user revocation by filtering on user_id.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session = db.query(RefreshSession).filter(
        RefreshSession.id == session_id,
        RefreshSession.user_id == user.id,
        RefreshSession.revoked_at.is_(None),
    ).first()

    if session is None:
        return False

    session.revoked_at = now
    db.commit()
    logger.debug(
        "Revoked session by ID: user_id=%d session_id=%d",
        user.id, session_id,
    )
    return True


def is_current_session(
    session: RefreshSession,
    current_raw_token: Optional[str],
) -> bool:
    """Check if this session matches the current raw refresh token."""
    if current_raw_token is None:
        return False
    return session.token_hash == hash_refresh_token(current_raw_token)


def enforce_max_active_sessions(
    db: Session,
    user: User,
    max_sessions: int,
) -> int:
    """Enforce the maximum active session limit for a user.

    If the user has more than max_sessions active (non-revoked, non-expired)
    sessions, revoke the oldest ones until the limit is satisfied.

    Returns the number of sessions revoked.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Count active sessions
    active_sessions = (
        db.query(RefreshSession)
        .filter(
            RefreshSession.user_id == user.id,
            RefreshSession.revoked_at.is_(None),
            RefreshSession.expires_at > now,
        )
        .order_by(RefreshSession.created_at.asc())
        .all()
    )

    if len(active_sessions) <= max_sessions:
        return 0

    # Revoke oldest sessions beyond the limit
    to_revoke = active_sessions[:-max_sessions]
    for session in to_revoke:
        session.revoked_at = now

    db.commit()
    logger.info(
        "Enforced max sessions for user_id=%d: revoked %d oldest session(s) (%d active)",
        user.id, len(to_revoke), len(active_sessions),
    )
    return len(to_revoke)


# ── Reuse detection ────────────────────────────────────────


def _revoke_family(db: Session, family_id: str) -> int:
    """Revoke ALL sessions in a token family (theft response).

    Returns the number of sessions revoked.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    count = (
        db.query(RefreshSession)
        .filter(
            RefreshSession.family_id == family_id,
            RefreshSession.revoked_at.is_(None),
        )
        .update({"revoked_at": now}, synchronize_session=False)
    )
    db.commit()
    if count:
        logger.warning(
            "Revoked %d session(s) in family %s (token theft detected)",
            count, family_id,
        )
    return count


# ── Cleanup ────────────────────────────────────────────────


# ── Probabilistic cleanup ─────────────────────────────────

_CLEANUP_INTERVAL = 100  # Run full cleanup every ~N calls
_cleanup_counter = 0


def _should_cleanup() -> bool:
    """Return True probabilistically to trigger cleanup."""
    global _cleanup_counter
    _cleanup_counter += 1
    if _cleanup_counter >= _CLEANUP_INTERVAL:
        _cleanup_counter = 0
        return True
    return False


def reset_cleanup_counter() -> None:
    """Reset the probabilistic cleanup counter (for testing)."""
    global _cleanup_counter
    _cleanup_counter = 0


def cleanup_expired_sessions(db: Session) -> int:
    """Remove expired and long-revoked sessions from the database.

    Only runs periodically (every 100 calls) to avoid unnecessary DB I/O
    on every auth operation.

    Deletes:
    - Expired sessions (expires_at passed)
    - Sessions revoked more than 7 days ago

    Returns the number of deleted rows.
    """
    if not _should_cleanup():
        return 0

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=7)  # Keep revoked sessions for 7 days

    expired = (
        db.query(RefreshSession)
        .filter(RefreshSession.expires_at <= now)
        .delete(synchronize_session=False)
    )
    old_revoked = (
        db.query(RefreshSession)
        .filter(
            RefreshSession.revoked_at.isnot(None),
            RefreshSession.revoked_at <= cutoff,
        )
        .delete(synchronize_session=False)
    )
    db.commit()

    total = expired + old_revoked
    if total:
        logger.debug("Cleaned up %d refresh sessions (%d expired, %d old revoked)",
                      total, expired, old_revoked)
    return total
