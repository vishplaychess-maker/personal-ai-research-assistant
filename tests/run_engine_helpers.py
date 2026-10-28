"""Shared hermetic fixtures for the Phase 1B.2R MP-1A RunEngine tests.

In-memory SQLite via ``Base.metadata.create_all`` plus the two partial-unique
indexes the real migration creates that ``create_all`` cannot express.

``make_ctx`` seeds a live ``refresh_sessions`` row for the context's family so
the DB-backed :func:`validate_session_binding` (the authority) passes; the
``revoke_family`` / ``expire_family`` helpers let a test invalidate it after the
context was built.
"""

from __future__ import annotations

import datetime as _dt
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models.models  # noqa: F401  (register tables)
from app.models.models import RefreshSession, User
from app.run_engine.context import AuthContext

_PARTIAL_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_idem_scope ON runs "
    "(COALESCE(tenant_id, ''), user_id, COALESCE(session_id, -1), idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_run_signals_live ON run_signals "
    "(run_id, kind) WHERE consumed_at IS NULL",
    # approval-domain active-proposal dedupe (created by _migrate_approval_domain_1b2
    # in production) — needed by app.approval.service.request_approval in MP-2 tests
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_approvals_active_dedupe "
    "ON pending_approvals (COALESCE(tenant_id, ''), proposal_dedupe_key) "
    "WHERE proposal_dedupe_key IS NOT NULL AND state IN ('requested','approved')",
)


def mk_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        for ddl in _PARTIAL_INDEXES:
            conn.execute(text(ddl))
    return eng


def mk_session(eng):
    # default expire_on_commit=True — raw-SQL mutations in a test must be
    # visible to the next ORM query on the same session.
    return sessionmaker(bind=eng)()


class _FakeUser:
    def __init__(self, uid: int):
        self.id = uid


def ensure_user(db, uid: int) -> None:
    if db.query(User).filter(User.id == uid).first() is None:
        db.add(User(id=uid, username=f"u{uid}", email=f"u{uid}@t.co",
                    hashed_password="x"))
        db.commit()


def seed_family(db, *, user_id: int, family_id: str,
                revoked: bool = False, ttl_days: int = 30) -> None:
    """Insert a live (or revoked / expired) refresh_sessions row for a family."""
    now = _dt.datetime.utcnow()
    db.add(RefreshSession(
        user_id=user_id,
        family_id=family_id,
        token_hash=uuid.uuid4().hex,
        expires_at=now + _dt.timedelta(days=ttl_days),
        revoked_at=now if revoked else None,
        created_at=now,
    ))
    db.commit()


class Clock:
    """Deterministic, advanceable ``now()`` for worker lease / deadline tests.
    Always yields tz-aware UTC so it compares correctly against
    ``DateTime(timezone=True)`` columns."""

    def __init__(self, start=None):
        self.t = start or _dt.datetime.now(_dt.timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds: float):
        self.t = self.t + _dt.timedelta(seconds=seconds)


def mk_factory(eng):
    """A zero-arg session factory for :class:`app.run_engine.worker.RunWorker`."""
    return lambda: mk_session(eng)


def run_snapshot(eng, run_id: str):
    """Fresh-session read of a run's mutable fields as a plain dict (detached)."""
    from app.models.models import Run

    s = mk_session(eng)
    try:
        r = s.query(Run).filter(Run.id == run_id).first()
        if r is None:
            return None
        return {
            "status": r.status,
            "status_reason": r.status_reason,
            "version": r.version,
            "lease_owner": r.lease_owner,
            "lease_expires_at": r.lease_expires_at,
            "run_deadline_at": r.run_deadline_at,
            "last_event_seq": r.last_event_seq,
            "finished_at": r.finished_at,
        }
    finally:
        s.close()


def signal_rows(eng, run_id: str, kind: str = "cancel_requested"):
    from app.models.models import RunSignal

    s = mk_session(eng)
    try:
        return [
            {"id": r.id, "kind": r.kind, "consumed_at": r.consumed_at}
            for r in s.query(RunSignal)
            .filter(RunSignal.run_id == run_id, RunSignal.kind == kind)
            .all()
        ]
    finally:
        s.close()


def event_blob(eng, run_id: str) -> str:
    """All control-event ``data`` for a run concatenated — for leak assertions."""
    from app.models.models import RunEvent

    s = mk_session(eng)
    try:
        return "\n".join(
            e.data or ""
            for e in s.query(RunEvent).filter(RunEvent.run_id == run_id).all()
        )
    finally:
        s.close()


def revoke_family(db, family_id: str) -> None:
    db.execute(
        text("UPDATE refresh_sessions SET revoked_at=:t WHERE family_id=:f"),
        {"t": _dt.datetime.utcnow(), "f": family_id},
    )
    db.commit()


def expire_family(db, family_id: str) -> None:
    db.execute(
        text("UPDATE refresh_sessions SET expires_at=:t WHERE family_id=:f"),
        {"t": _dt.datetime.utcnow() - _dt.timedelta(days=1), "f": family_id},
    )
    db.commit()


_DEFAULT_SID = object()


def make_ctx(
    db,
    *,
    user_id: int = 7,
    tenant_id: str | None = None,
    sid=_DEFAULT_SID,
    binding_valid: bool = True,
    seed: bool = True,
) -> AuthContext:
    """An :class:`AuthContext` plus (by default) a live ``refresh_sessions`` row
    for its family. ``binding_valid=False`` models a legacy token (default
    ``sid=None``, no seeded family, diagnostic flag off). ``seed=False`` builds
    the context without touching the DB (family seeded elsewhere)."""
    if sid is _DEFAULT_SID:
        sid = "fam-alpha" if binding_valid else None
    if seed and binding_valid and sid:
        ensure_user(db, user_id)
        existing = (
            db.query(RefreshSession)
            .filter(RefreshSession.family_id == sid,
                    RefreshSession.user_id == user_id)
            .first()
        )
        if existing is None:
            seed_family(db, user_id=user_id, family_id=sid)
    return AuthContext(
        user=_FakeUser(user_id),
        user_id=user_id,
        tenant_id=tenant_id,
        auth_session_ref=sid,
        durable_session_binding_valid=binding_valid,
    )
