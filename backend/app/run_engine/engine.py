"""RunEngine core transactional services (Phase 1B.2R — MP-1A).

The *only* sanctioned way to create a durable run, move it through the state
machine, or ask for its cancellation. This layer is deliberately NOT in a
route, in ``streaming_service``, or on a model method — a request handler (MP-2+)
calls these functions; nothing here starts a worker, executes a tool, touches
streaming, or resumes an approval.

Guarantees:

* **Scoped idempotent submission** — scope is the normalized
  ``(tenant_id, user_id, session_id, idempotency_key)`` tuple. The server
  generates ``run_id``. The partial-UNIQUE index ``uq_runs_idem_scope`` is the
  final authority; a pre-``SELECT`` and a bounded retry only smooth the race.
  A replay returns the existing run with no new ``run.created`` event and no
  new step. A key reused for a different request identity is a deterministic
  conflict.
* **Optimistic concurrency** — a state transition is a single conditional
  ``UPDATE`` guarded by ``runs.version`` (compare-and-swap). The version bumps
  exactly once per successful transition. A stale writer gets a typed conflict.
  Terminal states are protected. Cancellation wins races (D4).
* **Fail-closed session binding** — every read AND mutation calls
  :func:`app.run_engine.session_binding.validate_session_binding` against the
  live database, inside the operation's own session, owner-constrained and
  run-bound (D5). The cached ``AuthContext`` boolean is diagnostic only.

INTERIM identity note: ``idempotency_identity_hash`` is currently a *structural*
tag over the identity tuple (tenant/user/session/key/kind/thread) — it does NOT
yet cover semantic request content (prompt / task / model options), because
MP-1A has no such input parameter. A server-keyed HMAC over a canonical
validated submission is a pending decision (see the MP-1A completion report §5).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.approval.canonical import canonicalize
from app.config import settings
from app.models.models import Run, RunSignal, _utcnow_aware
from app.run_engine._dberr import constraint_violation
from app.run_engine.context import AuthContext
from app.run_engine.errors import (
    CancellationInProgress,
    IdempotencyKeyConflict,
    IdempotencyKeyTooLong,
    InvalidRunTransition,
    RunAlreadyTerminal,
    RunNotFound,
    RunStateConflict,
    RunSubmitContention,
    SessionBindingMismatch,
    SessionBindingRequired,
)
from app.run_engine.events import (
    _CANCEL_REASON_CODES,
    _try_append_event,
    append_control_event,
    validate_event_data,
)
from app.run_engine.session_binding import validate_session_binding
from app.run_engine.signals import build_signal_payload
from app.run_engine.state_machine import (
    CANCELLING,
    CANCELLED,
    CREATED,
    RUNNING,
    RUN_TERMINAL,
    assert_run_transition,
)

_SUBMIT_MAX_ATTEMPTS = 3
_CANCEL_MAX_ATTEMPTS = 3
_RUN_KINDS = frozenset({"chat_stream", "chat_sync", "scheduled", "sub_run", "mcp_call",
                        "deep_research", "multi_agent"})  # Phase 1B.2R MP-3C3D4C/D5B
_CANCEL_STATES = frozenset({CANCELLING, CANCELLED})


# ── helpers ──────────────────────────────────────────────────────────


def _normalize_key(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("idempotency_key is required and must be a non-empty string")
    key = raw.strip()
    limit = int(settings.run_idempotency_key_max_bytes)
    byte_len = len(key.encode("utf-8"))
    if byte_len > limit:
        raise IdempotencyKeyTooLong(byte_len, limit)
    return key


def _identity_hash(
    *,
    tenant_id: Optional[str],
    user_id: int,
    session_id: Optional[int],
    idempotency_key: str,
    kind: str,
    thread_id: Optional[str],
) -> str:
    """INTERIM **structural** identity tag over the request's identity tuple.

    Domain-separated (``"structural-1"``) so a future server-keyed *semantic*
    HMAC (over a canonical validated submission: prompt / task / model options)
    cannot collide with it. This tag does NOT prove two submissions carry the
    same semantic request — MP-1A has no semantic input parameter yet, and a
    client-supplied fingerprint is explicitly NOT trusted as identity. See the
    MP-1A completion report §5 (pending decision).
    """
    material = {
        "v": "structural-1",
        "tenant_id": tenant_id,
        "user_id": int(user_id),
        "session_id": session_id if session_id is None else int(session_id),
        "idempotency_key": idempotency_key,
        "kind": kind,
        "thread_id": thread_id,
    }
    return hashlib.sha256(canonicalize(material)).hexdigest()


def _select_by_idem_scope(
    db: Session,
    *,
    tenant_id: Optional[str],
    user_id: int,
    session_id: Optional[int],
    idempotency_key: str,
) -> Optional[Run]:
    """Mirror the ``uq_runs_idem_scope`` partial index exactly:
    ``COALESCE(tenant_id,''), user_id, COALESCE(session_id,-1), idempotency_key``.
    """
    return (
        db.query(Run)
        .filter(
            func.coalesce(Run.tenant_id, "") == (tenant_id or ""),
            Run.user_id == user_id,
            func.coalesce(Run.session_id, -1) == (
                -1 if session_id is None else int(session_id)
            ),
            Run.idempotency_key == idempotency_key,
        )
        .first()
    )


def _load_run_or_404(db: Session, run_id: str) -> Run:
    """Load a run by id (no auth). Raises :class:`RunNotFound` if absent.
    Ownership / tenant / session-family checks are ``validate_session_binding``'s
    job — never authorize on ``run_id`` alone."""
    run = db.query(Run).filter(Run.id == run_id).first()
    if run is None:
        raise RunNotFound(run_id)
    return run


# ── public API ───────────────────────────────────────────────────────


def submit_run(
    db: Session,
    ctx: AuthContext,
    *,
    idempotency_key: str,
    session_id: Optional[int] = None,
    thread_id: Optional[str] = None,
    kind: str = "chat_stream",
    parent_run_id: Optional[str] = None,
    identity_hash: Optional[str] = None,
    commit: bool = True,
) -> Run:
    """Create — or idempotently return — the durable run for this request.

    Fresh DB session-family validation on entry, on the replay/race-recovery
    return, and again immediately before commit. Raises
    :class:`SessionBindingRequired`, :class:`SessionBindingMismatch`,
    :class:`IdempotencyKeyTooLong`, :class:`IdempotencyKeyConflict`,
    :class:`RunSubmitContention`, or ``ValueError`` for a bad ``kind`` / key.

    ``identity_hash`` — when given (Phase 1B.2R MP-3A public route), it is the
    authoritative run identity for the ``(scope, key)``: a **server-computed
    HMAC** over the canonical validated submission. It fully replaces the
    interim structural tag; "same key + a different semantic submission" then
    conflicts. When ``None`` the structural tag is used (MP-1A/1B/2 callers).

    ``commit=False`` leaves the created run + its ``run.created`` event
    uncommitted so the caller can persist the Message / initial RunStep /
    input payload in the SAME transaction (MP-3A submission atomicity).
    """
    validate_session_binding(db, ctx)
    if kind not in _RUN_KINDS:
        raise ValueError(f"unknown run kind {kind!r}")
    key = _normalize_key(idempotency_key)
    tenant_id = ctx.tenant_id
    user_id = ctx.user_id

    # Structural identity tag — client-supplied thread_id only (a server-derived
    # `run:<id>` is not an identity input, or a replay could never match). This
    # is NOT a semantic tag (see _identity_hash / report §5).
    client_thread_id = (
        thread_id.strip()
        if isinstance(thread_id, str) and thread_id.strip()
        else None
    )
    if identity_hash is not None:
        s = identity_hash.strip().lower()
        if len(s) != 64 or any(c not in "0123456789abcdef" for c in s):
            raise ValueError("identity_hash must be a 64-char hex digest")
        incoming_identity = s
    else:
        incoming_identity = _identity_hash(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            idempotency_key=key,
            kind=kind,
            thread_id=client_thread_id,
        )

    for _ in range(_SUBMIT_MAX_ATTEMPTS):
        existing = _select_by_idem_scope(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            idempotency_key=key,
        )
        if existing is not None:
            # replay must clear the SAME authorization as a fresh submit
            validate_session_binding(db, ctx, run=existing)
            if existing.idempotency_identity_hash != incoming_identity:
                raise IdempotencyKeyConflict(existing.id)
            return existing  # replay — no new run, no new run.created event

        run_id = str(uuid.uuid4())
        run = Run(
            id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            thread_id=client_thread_id or f"run:{run_id}",
            kind=kind,
            parent_run_id=parent_run_id,
            status=CREATED,
            idempotency_key=key,
            idempotency_identity_hash=incoming_identity,
            auth_session_ref=ctx.auth_session_ref,
            version=0,
            last_event_seq=0,
        )
        db.add(run)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            if not constraint_violation(exc, "uq_runs_idem_scope"):
                raise
            continue  # a concurrent submit won — loop, re-select, return theirs

        # initial control event — same transaction as the run insert, so a
        # rollback removes both; the row is txn-local so no CAS race here.
        event = _try_append_event(
            db,
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            event_type="run.created",
            data_text=validate_event_data("run.created", {"kind": kind}),
        )
        if event is None:  # pragma: no cover - impossible for a fresh row
            db.rollback()
            continue
        validate_session_binding(db, ctx, run=run)  # final pre-commit re-check
        if commit:
            db.commit()
        return run

    raise RunSubmitContention()


def get_run(db: Session, ctx: AuthContext, run_id: str) -> Run:
    """Session-bound read. Fresh DB session-family validation. Raises
    :class:`RunNotFound` (non-disclosing) for an absent / other-owner /
    other-tenant run, or :class:`SessionBindingMismatch` for another family."""
    run = _load_run_or_404(db, run_id)
    validate_session_binding(db, ctx, run=run)
    return run


def transition_run(
    db: Session,
    ctx: AuthContext,
    run_id: str,
    *,
    to: str,
    expected_version: int,
    status_reason: Optional[str] = None,
    allow_reconcile: bool = False,
) -> Run:
    """Move ``run_id`` to state ``to`` iff its ``version`` still equals
    ``expected_version`` and ``current -> to`` is legal.

    Raises :class:`SessionBindingRequired`, :class:`SessionBindingMismatch`,
    :class:`RunNotFound`, :class:`RunAlreadyTerminal`,
    :class:`InvalidRunTransition`, :class:`CancellationInProgress`, or
    :class:`RunStateConflict`.
    """
    run = _load_run_or_404(db, run_id)
    validate_session_binding(db, ctx, run=run)
    frm = run.status

    if frm in RUN_TERMINAL:
        if to == frm:
            return run  # idempotent re-assertion
        raise RunAlreadyTerminal(frm)

    # Cancellation wins races (D4): once the run is cancelling, any non-cancel
    # transition the caller was attempting is moot.
    if frm in _CANCEL_STATES and to not in _CANCEL_STATES:
        raise CancellationInProgress()

    assert_run_transition(frm, to, allow_reconcile=allow_reconcile)

    now = _utcnow_aware()
    values = {
        "status": to,
        "status_reason": status_reason,
        "version": Run.version + 1,
        "updated_at": now,
    }
    if to == RUNNING and run.started_at is None:
        values["started_at"] = now
    if to in RUN_TERMINAL:
        values["finished_at"] = now

    res = db.execute(
        update(Run)
        .where(
            Run.id == run_id,
            Run.version == expected_version,
            Run.status == frm,
        )
        .values(**values)
    )
    if res.rowcount != 1:
        db.rollback()
        fresh = db.query(Run).filter(Run.id == run_id).first()
        if (
            fresh is not None
            and fresh.status in _CANCEL_STATES
            and to not in _CANCEL_STATES
        ):
            raise CancellationInProgress()
        raise RunStateConflict(
            expected_version,
            None if fresh is None else fresh.version,
        )

    validate_session_binding(db, ctx, run=run)  # final pre-commit re-check
    db.commit()
    fresh = _load_run_or_404(db, run_id)
    validate_session_binding(db, ctx, run=fresh)
    return fresh


@dataclass(frozen=True)
class CancellationResult:
    """Outcome of :func:`request_cancellation` — deterministic and idempotent."""

    run_id: str
    status: str
    signal_created: bool
    already_requested: bool = False
    already_terminal: bool = False


def request_cancellation(
    db: Session,
    ctx: AuthContext,
    run_id: str,
    *,
    reason_code: Optional[str] = None,
) -> CancellationResult:
    """Record a cancellation *request* for ``run_id`` — a live
    ``run_signals(cancel_requested)`` row plus one ``run.cancel.requested``
    control event. Does NOT mark the run cancelled, does NOT consume the
    signal (the worker does both, MP-1B+).

    Idempotent: a second call while a live signal exists is a no-op. A terminal
    run yields a deterministic result with no signal and no mutation. Raises
    :class:`SessionBindingRequired`, :class:`SessionBindingMismatch`,
    :class:`RunNotFound`, or ``ValueError`` for a bad ``reason_code``.
    """
    if reason_code is not None and reason_code not in _CANCEL_REASON_CODES:
        raise ValueError(f"reason_code {reason_code!r} is not an allowed cancel code")

    for _ in range(_CANCEL_MAX_ATTEMPTS):
        run = _load_run_or_404(db, run_id)
        validate_session_binding(db, ctx, run=run)

        if run.status in RUN_TERMINAL:
            return CancellationResult(
                run_id=run_id,
                status=run.status,
                signal_created=False,
                already_terminal=True,
            )

        live = (
            db.query(RunSignal)
            .filter(
                RunSignal.run_id == run_id,
                RunSignal.kind == "cancel_requested",
                RunSignal.consumed_at.is_(None),
            )
            .first()
        )
        if live is not None:
            return CancellationResult(
                run_id=run_id,
                status=run.status,
                signal_created=False,
                already_requested=True,
            )

        payload = build_signal_payload(
            "cancel_requested",
            reason_code=reason_code,
            server_ts=_utcnow_aware(),
        )
        signal = RunSignal(
            id=str(uuid.uuid4()),
            run_id=run_id,
            kind="cancel_requested",
            payload=payload,
            created_by_user_id=ctx.user_id,
            created_by_tenant_id=ctx.tenant_id,
        )
        db.add(signal)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            if not constraint_violation(exc, "uq_run_signals_live"):
                raise
            # a concurrent request created the live signal — idempotent result
            fresh = _load_run_or_404(db, run_id)
            validate_session_binding(db, ctx, run=fresh)
            return CancellationResult(
                run_id=run_id,
                status=fresh.status,
                signal_created=False,
                already_requested=True,
            )

        event = _try_append_event(
            db,
            run_id=run_id,
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            event_type="run.cancel.requested",
            data_text=validate_event_data(
                "run.cancel.requested",
                {"reason_code": reason_code} if reason_code else None,
            ),
        )
        if event is None:
            db.rollback()
            continue  # run_seq contention — redo the whole request

        validate_session_binding(db, ctx, run=run)  # final pre-commit re-check
        db.commit()
        return CancellationResult(
            run_id=run_id,
            status=run.status,
            signal_created=True,
        )

    # exhausted retries on event-seq contention
    raise CancellationInProgress()


# re-export so callers can `from app.run_engine.engine import append_control_event`
__all__ = [
    "CancellationResult",
    "append_control_event",
    "get_run",
    "request_cancellation",
    "submit_run",
    "transition_run",
]
