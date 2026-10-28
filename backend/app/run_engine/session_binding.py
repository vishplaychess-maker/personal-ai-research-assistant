"""Run <-> authenticated-session binding (Phase 1B.2R — MP-0, D5).

A run binds to a *revocable session* via ``runs.auth_session_ref``, which holds
a ``refresh_sessions.family_id`` — a UUIDv4 that survives access/refresh token
rotation within one login and is cleared (``revoked_at`` set on the whole
family) on logout or reuse/theft detection. **A token or token hash is NEVER
stored on a run.**

Repository evidence:
  * ``app.models.models.RefreshSession`` — ``family_id`` (indexed),
    ``token_hash`` (SHA-256), ``expires_at``, ``revoked_at`` (NULL == active).
  * ``app.services.refresh_token_service`` — ``_revoke_family()`` sets
    ``revoked_at`` for every row of a family (logout / reuse detection).

Known gap (MP-1 auth seam, documented — NOT worked around here): the access
token JWT currently carries only ``{"sub": user_id, "exp", "iat"}``
(``app.services.auth_service.create_access_token``). It does not carry the
``family_id``, so a request handler cannot yet populate ``auth_session_ref``.
Until that seam lands, ``auth_session_ref`` stays NULL and
:func:`session_ref_is_active` returns ``False`` — a future resume / dispatch
MUST fail closed.

MP-1A adds :func:`validate_session_binding` — the **authoritative**, database-backed
check every RunEngine operation runs inside its own transaction/session. The
cached ``AuthContext.durable_session_binding_valid`` boolean is diagnostic only
(it reflects the state at token-parse time and goes stale after logout, family
revocation, expiry, or reuse detection); it is **not** authorization.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.models import RefreshSession
from app.run_engine.errors import RunNotFound, SessionBindingMismatch, SessionBindingRequired

if TYPE_CHECKING:  # avoid an import cycle (context imports this module)
    from app.models.models import Run
    from app.run_engine.context import AuthContext

# --- 5C (product decision, pending) -----------------------------------------
# When a run already exists, must the caller's signed family EQUAL the run's
# ``auth_session_ref``? STRICT (True) = a user's other active login (phone,
# second tab) can NOT read / replay / mutate a run started elsewhere. This is
# the safe default; loosening it to "same user, any active family" is a product
# decision (see the MP-1A completion report §5).
_SESSION_BOUND_RUN_ACCESS = True


def session_ref_is_active(db: Session, auth_session_ref: str | None) -> bool:
    """True IFF ``auth_session_ref`` is a non-NULL ``family_id`` with at least
    one active (``revoked_at IS NULL``, not expired) ``refresh_sessions`` row.

    Fails CLOSED: ``None`` / unknown / revoked / expired -> ``False``.

    NOTE: this does **not** constrain the owner — use
    :func:`validate_session_binding` for authorization.
    """
    if not auth_session_ref:
        return False
    now = _dt.datetime.utcnow()  # refresh_sessions timestamps are naive UTC
    stmt = (
        select(func.count())
        .select_from(RefreshSession)
        .where(RefreshSession.family_id == auth_session_ref)
        .where(RefreshSession.revoked_at.is_(None))
        .where(RefreshSession.expires_at > now)
    )
    return (db.execute(stmt).scalar() or 0) > 0


def validate_session_binding(
    db: Session,
    ctx: "AuthContext",
    *,
    run: "Optional[Run]" = None,
) -> None:
    """Authoritative, fail-closed session-family check for a RunEngine op.

    MUST be called inside the operation's own transaction/session, as close as
    practical to the protected read/write. Proves, against the live database:

    * ``ctx.auth_session_ref`` is present (a legacy token without ``sid`` can
      never touch a durable run);
    * a ``refresh_sessions`` row exists for that ``family_id`` **owned by
      ``ctx.user_id``**, not revoked, not expired
      (query is always owner-constrained — never by family id alone);
    * when ``run`` is given: the run is owned by ``ctx.user_id`` and the
      tenant matches (else :class:`RunNotFound`, non-disclosing), and — under
      the strict default — the run's ``auth_session_ref`` equals
      ``ctx.auth_session_ref`` (else :class:`SessionBindingMismatch`).

    Raises :class:`SessionBindingRequired` / :class:`SessionBindingMismatch` /
    :class:`RunNotFound`. Never trusts a family id supplied by a request body
    or a service caller — only ``ctx`` (built from the signed token).
    """
    fam = ctx.auth_session_ref
    if not fam:
        raise SessionBindingRequired()

    now = _dt.datetime.utcnow()
    active = db.execute(
        select(func.count())
        .select_from(RefreshSession)
        .where(RefreshSession.family_id == fam)
        .where(RefreshSession.user_id == ctx.user_id)  # owner-constrained
        .where(RefreshSession.revoked_at.is_(None))
        .where(RefreshSession.expires_at > now)
    ).scalar() or 0
    if active < 1:
        raise SessionBindingRequired()

    if run is None:
        return

    row_tenant = (getattr(run, "tenant_id", None) or None)
    ctx_tenant = (ctx.tenant_id or None)
    if run.user_id != ctx.user_id or row_tenant != ctx_tenant:
        raise RunNotFound(run.id)

    if _SESSION_BOUND_RUN_ACCESS:
        if not run.auth_session_ref or run.auth_session_ref != fam:
            raise SessionBindingMismatch()
