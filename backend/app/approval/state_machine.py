"""Approval state machine — atomic compare-and-swap transitions.

States:  requested -> approved | denied | expired
         approved  -> consumed | expired | revoked
Terminal: denied, expired, consumed, revoked.

Every transition is a single conditional UPDATE:

    UPDATE pending_approvals
       SET state = :to, <ts col> = :now [, ...]
     WHERE id = :id AND state = :from [AND action_hash = :h]

``rowcount == 1`` -> this caller won. ``0`` -> the row was not in the required
precondition state (already decided / consumed / expired), or a concurrent
writer won the race -> raise ``InvalidTransition`` (or a more specific error).

The **server** decides expiry: ``expire_if_due`` runs the same CAS before any
resolve/consume, using an injectable clock. Client time is never trusted.
"""

from __future__ import annotations

import datetime as _dt

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.approval.clock import Clock, SystemClock, ensure_utc
from app.approval.errors import (
    ApprovalAlreadyConsumed,
    ApprovalExpired,
    EnvelopeMismatch,
    InvalidTransition,
)
from app.models.models import PendingApproval

REQUESTED = "requested"
APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"
CONSUMED = "consumed"
REVOKED = "revoked"

STATES = frozenset({REQUESTED, APPROVED, DENIED, EXPIRED, CONSUMED, REVOKED})
TERMINAL = frozenset({DENIED, EXPIRED, CONSUMED, REVOKED})

# from -> allowed set of to
ALLOWED: dict[str, frozenset[str]] = {
    REQUESTED: frozenset({APPROVED, DENIED, EXPIRED}),
    APPROVED: frozenset({CONSUMED, EXPIRED, REVOKED}),
    DENIED: frozenset(),
    EXPIRED: frozenset(),
    CONSUMED: frozenset(),
    REVOKED: frozenset(),
}


def _cas(db: Session, approval_id: str, frm: str, to: str, *,
         now: _dt.datetime, extra: dict | None = None,
         action_hash: str | None = None) -> int:
    """Run one conditional UPDATE. Returns rowcount (0 or 1). Caller commits."""
    values = {"state": to, **(extra or {})}
    stmt = (
        update(PendingApproval)
        .where(PendingApproval.id == approval_id)
        .where(PendingApproval.state == frm)
    )
    if action_hash is not None:
        stmt = stmt.where(PendingApproval.action_hash == action_hash)
    stmt = stmt.values(**values).execution_options(synchronize_session=False)
    return db.execute(stmt).rowcount


def expire_if_due(db: Session, approval_id: str, *, clock: Clock | None = None) -> bool:
    """CAS ``requested|approved -> expired`` when ``expires_at <= now``.
    Returns True iff this call performed the expiry. Idempotent & race-safe.
    Does not touch ``decided_at``.
    """
    clock = clock or SystemClock()
    now = clock.now()
    row = db.get(PendingApproval, approval_id)
    if row is None or row.state in TERMINAL:
        return False
    if ensure_utc(row.expires_at) > now:
        return False
    for frm in (REQUESTED, APPROVED):
        if _cas(db, approval_id, frm, EXPIRED, now=now):
            db.commit()
            return True
    return False


def approve(db: Session, approval_id: str, *, clock: Clock | None = None,
            commit: bool = True) -> None:
    _decide(db, approval_id, APPROVED, clock=clock, commit=commit)


def deny(db: Session, approval_id: str, *, clock: Clock | None = None,
         commit: bool = True) -> None:
    _decide(db, approval_id, DENIED, clock=clock, commit=commit)


def _decide(db: Session, approval_id: str, to: str, *, clock: Clock | None,
            commit: bool = True) -> None:
    """CAS ``requested -> to``. When ``commit`` is False the caller owns the
    transaction boundary (used by the approvals route to commit the decision and
    a durable-run RunSignal together, atomically); on a lost CAS it then raises
    without rolling back so the caller can roll the whole unit back."""
    clock = clock or SystemClock()
    now = clock.now()
    row = db.get(PendingApproval, approval_id)
    if row is None:
        raise InvalidTransition(f"approval {approval_id!r} not found")
    if ensure_utc(row.expires_at) <= now:
        expire_if_due(db, approval_id, clock=clock)
        raise ApprovalExpired(f"approval {approval_id!r} expired before decision")
    n = _cas(db, approval_id, REQUESTED, to, now=now, extra={"decided_at": now})
    if n != 1:
        if commit:
            db.rollback()
        cur = db.get(PendingApproval, approval_id)
        raise InvalidTransition(
            f"cannot move approval {approval_id!r} requested -> {to}; "
            f"current state is {cur.state if cur else 'missing'!r}"
        )
    if commit:
        db.commit()


def consume(db: Session, approval_id: str, *, expected_action_hash: str,
            clock: Clock | None = None) -> PendingApproval:
    """CAS ``approved -> consumed`` iff ``action_hash == expected_action_hash``
    and not expired. Exactly one caller can ever succeed.

    Raises ``EnvelopeMismatch`` (hash differs), ``ApprovalExpired``,
    ``ApprovalAlreadyConsumed``, or ``InvalidTransition`` (not approved).
    """
    clock = clock or SystemClock()
    now = clock.now()
    row = db.get(PendingApproval, approval_id)
    if row is None:
        raise InvalidTransition(f"approval {approval_id!r} not found")
    if row.action_hash != expected_action_hash:
        raise EnvelopeMismatch(
            f"approval {approval_id!r} is bound to a different action_hash"
        )
    if ensure_utc(row.expires_at) <= now:
        expire_if_due(db, approval_id, clock=clock)
        raise ApprovalExpired(f"approval {approval_id!r} expired before consumption")

    n = _cas(db, approval_id, APPROVED, CONSUMED, now=now,
             extra={"consumed_at": now}, action_hash=expected_action_hash)
    if n == 1:
        db.commit()
        return db.get(PendingApproval, approval_id)

    db.rollback()
    cur = db.get(PendingApproval, approval_id)
    state = cur.state if cur else "missing"
    if state == CONSUMED:
        raise ApprovalAlreadyConsumed(f"approval {approval_id!r} already consumed")
    if state == EXPIRED:
        raise ApprovalExpired(f"approval {approval_id!r} expired")
    if state == DENIED:
        raise InvalidTransition(f"approval {approval_id!r} was denied")
    raise InvalidTransition(
        f"cannot consume approval {approval_id!r}; current state is {state!r}"
    )
