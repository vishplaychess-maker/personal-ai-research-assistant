"""Authenticated request context for the durable run engine (Phase 1B.2R — MP-1A).

Every RunEngine service call takes an :class:`AuthContext` instead of a bare
``User``. It carries exactly what the engine needs to enforce ownership and the
D5 session-revocation rule:

* the authenticated ``User`` and its ``user_id`` (ownership root),
* a normalized ``tenant_id`` (multi-tenant isolation seam — always ``None``
  today; the repo has no tenant source yet),
* ``auth_session_ref`` — the ``sid`` claim from the access token, i.e. a
  ``refresh_sessions.family_id`` (never a token / hash / cookie / header),
* ``durable_session_binding_valid`` — **DIAGNOSTIC ONLY**, not authorization.
  It records whether the family looked live at token-parse time; it goes stale
  after logout, family revocation, expiry, or reuse detection. Every RunEngine
  read and mutation re-validates the family against the live database inside its
  own transaction via
  :func:`app.run_engine.session_binding.validate_session_binding`.

A legacy access token with no ``sid`` claim keeps its existing non-RunEngine
compatibility, but every durable-run operation fails closed.

No route consumes :func:`get_auth_context` in MP-1A — it is the seam MP-2 wires
into ``/messages/stream`` and the approvals route. Nothing here runs a worker,
touches streaming, or executes a tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import User
from app.run_engine.session_binding import session_ref_is_active
from app.services.auth_service import (
    bearer_scheme,
    decode_access_token,
    resolve_user_from_payload,
)


def _normalize_tenant(raw: object) -> Optional[str]:
    """Trim + lowercase a tenant id; empty / non-str -> ``None``."""
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().lower()
    return cleaned or None


def _normalize_sid(raw: object) -> Optional[str]:
    """The ``sid`` claim must be a non-empty string (a ``family_id``)."""
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    return cleaned or None


@dataclass(frozen=True)
class AuthContext:
    """Immutable per-request identity + session-binding snapshot."""

    user: User
    user_id: int
    tenant_id: Optional[str]
    auth_session_ref: Optional[str]
    durable_session_binding_valid: bool


def build_auth_context(
    db: Session,
    *,
    user: User,
    token_payload: dict,
) -> AuthContext:
    """Assemble an :class:`AuthContext` from an already-verified access-token
    payload and its resolved ``User``. Performs the fail-closed family check.
    """
    sid = _normalize_sid(token_payload.get("sid"))
    tenant_id = _normalize_tenant(token_payload.get("tenant"))
    binding_valid = bool(sid) and session_ref_is_active(db, sid)
    return AuthContext(
        user=user,
        user_id=user.id,
        tenant_id=tenant_id,
        auth_session_ref=sid,
        durable_session_binding_valid=binding_valid,
    )


def get_auth_context(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthContext:
    """FastAPI dependency: require a valid access token and return its
    :class:`AuthContext`. (No MP-1A route uses this yet — MP-2 does.)
    """
    from fastapi import HTTPException, status

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(credentials.credentials)
    user = resolve_user_from_payload(payload, db)
    return build_auth_context(db, user=user, token_payload=payload)
