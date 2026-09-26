"""Tamper-evident ActionLog hash chain.

**Security property — stated precisely.**
* This chain DETECTS accidental or unsophisticated: modification, deletion,
  insertion, reordering, duplication, sequence gaps, and chain-link
  corruption. The verifier recomputes every ``row_hash``, checks the
  ``prev_row_hash`` linkage, checks ``seq`` is contiguous from 1, and the DB
  enforces ``UNIQUE(chain_scope_id, seq)`` and ``UNIQUE(row_hash)``.
* It is **NOT** tamper-proof, **NOT** immutable against a privileged database
  administrator, and **NOT** cryptographically impossible to rewrite. An actor
  who can rewrite the complete history AND recompute the entire chain (they
  know the algorithm and ``GENESIS``) can bypass this database-only detection
  model — they are **outside** the current protection boundary.
* FUTURE HARDENING (out of scope for Phase 1B.1, do not implement here): an
  external, independently-stored anchor — periodic signed/HMAC checkpoints of
  ``(chain_scope_id, seq, row_hash)`` written to append-only external storage
  (object-lock bucket, transparency log, or a co-signing service) — would make
  a full-history rewrite detectable. That belongs in a later phase.

Append serialization (spec 1B.1 sec.8):

* **PostgreSQL** — ``SELECT ... FOR UPDATE`` on the scope's ``action_log_heads``
  row serializes concurrent appends.
* **SQLite** — ``BEGIN IMMEDIATE`` takes a reserved lock for the whole
  transaction, serializing writers.
* **Both** — ``UNIQUE(chain_scope_id, seq)`` on ``action_log`` is the
  engine-independent backstop: even if locking is imperfect, two rows can never
  share a ``(scope, seq)`` — the loser gets an ``IntegrityError`` and retries.

A process-local ``asyncio``/``threading`` lock is deliberately NOT relied upon
(it does not help across processes/workers). The storage-specific locking is
kept behind ``_ChainLock`` so it can be swapped.

Application code exposes **no update/delete** for ``action_log`` — only
``append_event``.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.approval.clock import Clock, SystemClock, ensure_utc
from app.approval.errors import ChainIntegrityError
from app.approval.hashing import GENESIS, row_hash
from app.approval.redaction import redact
from app.models.models import ActionLogEntry

_MAX_METADATA_CHARS = 4000
_APPEND_RETRIES = 5


def _norm_ts(value) -> str:
    """Deterministic timestamp string for the chain pre-image. Normalizes to
    tz-aware UTC then formats fixed 6-digit microseconds with NO tz suffix, so
    it round-trips identically whether the DB returns a tz-aware or naive
    datetime (SQLite's DateTime drops tzinfo on read-back)."""
    return ensure_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%f")


# ── scope helpers ──────────────────────────────────────────────────────


def chain_scope_id(*, tenant_id: str | None, user_id: int | None) -> str:
    """`tenant:<id>` when a tenant is known, else `user:<id>`, else `system`.
    Never ambiguous NULL grouping."""
    if tenant_id:
        return f"tenant:{tenant_id}"
    if user_id is not None:
        return f"user:{user_id}"
    return "system"


# ── locking ────────────────────────────────────────────────────────────


class _ChainLock:
    """Serialize appends within one chain scope for the current transaction."""

    def __init__(self, db: Session):
        self.db = db
        self.dialect = db.bind.dialect.name if db.bind is not None else "sqlite"

    def acquire(self, scope: str) -> None:
        if self.dialect == "postgresql":
            # Lock (or create) the head row for this scope.
            self.db.execute(
                text(
                    "INSERT INTO action_log_heads (chain_scope_id, seq, row_hash, updated_at) "
                    "VALUES (:s, 0, :g, now()) ON CONFLICT (chain_scope_id) DO NOTHING"
                ),
                {"s": scope, "g": GENESIS},
            )
            self.db.execute(
                text("SELECT 1 FROM action_log_heads WHERE chain_scope_id = :s FOR UPDATE"),
                {"s": scope},
            )
        elif self.dialect == "sqlite":
            # Promote the transaction to a reserved lock immediately. pysqlite
            # otherwise defers BEGIN until the first write, allowing a reader
            # window between our head read and our insert.
            try:
                self.db.execute(text("BEGIN IMMEDIATE"))
            except Exception:
                # A transaction may already be open; that is fine — the
                # UNIQUE(scope, seq) backstop still guarantees no fork.
                pass
        # other dialects: rely on the UNIQUE backstop + serializable isolation


# ── append ─────────────────────────────────────────────────────────────


def _update_head_advisory(db: Session, dialect: str, scope: str, seq: int,
                          rh: str, updated_at) -> None:
    """Best-effort update of the advisory ``action_log_heads`` row. The chain
    verifier never trusts this table; ``action_log`` is the source of truth.
    Uses portable upsert SQL for sqlite/postgres; a no-op elsewhere."""
    if dialect not in ("sqlite", "postgresql"):
        return
    db.execute(
        text(
            "INSERT INTO action_log_heads (chain_scope_id, seq, row_hash, updated_at) "
            "VALUES (:s, :q, :h, :u) "
            "ON CONFLICT (chain_scope_id) DO UPDATE SET "
            "seq = excluded.seq, row_hash = excluded.row_hash, updated_at = excluded.updated_at"
        ),
        {"s": scope, "q": seq, "h": rh, "u": updated_at},
    )


def _head(db: Session, scope: str) -> tuple[int, str]:
    """Current (seq, row_hash) for a scope, from ``action_log`` (source of
    truth — the heads table is advisory only)."""
    rows = (
        db.query(ActionLogEntry.seq, ActionLogEntry.row_hash)
        .filter(ActionLogEntry.chain_scope_id == scope)
        .order_by(ActionLogEntry.seq.desc())
        .limit(1)
        .all()
    )
    if not rows:
        return 0, GENESIS
    return int(rows[0][0]), rows[0][1]


def append_event(
    db: Session,
    *,
    tenant_id: str | None,
    user_id: int | None,
    event_type: str,
    approval_id: str | None = None,
    grant_id: str | None = None,
    action_hash: str | None = None,
    event_metadata: dict | None = None,
    clock: Clock | None = None,
) -> ActionLogEntry:
    """Append one linked, sanitized event.

    ``chain_scope_id`` is part of the canonical row-hash payload, so two scopes'
    first events (same seq=1, same GENESIS prev, same clock) still produce
    distinct ``row_hash`` values and cannot collide under ``UNIQUE(row_hash)``.

    Concurrency: on an ``IntegrityError`` from ``UNIQUE(chain_scope_id, seq)``
    (a concurrent appender took this seq) the transaction is rolled back and the
    loop retries with a re-read head, **bounded to** ``_APPEND_RETRIES``
    attempts. On exhaustion it fails closed with ``ChainIntegrityError`` — it
    never forks the chain or writes a gap.
    """
    clock = clock or SystemClock()
    scope = chain_scope_id(tenant_id=tenant_id, user_id=user_id)

    meta = redact(event_metadata or {})
    meta_text = json.dumps(meta, sort_keys=True, ensure_ascii=False)
    if len(meta_text) > _MAX_METADATA_CHARS:
        meta = {"_truncated": True, "_bytes": len(meta_text)}
        meta_text = json.dumps(meta, sort_keys=True, ensure_ascii=False)

    last_err: Exception | None = None
    for _ in range(_APPEND_RETRIES):
        lock = _ChainLock(db)
        lock.acquire(scope)
        prev_seq, prev_hash = _head(db, scope)
        seq = prev_seq + 1
        created_at = clock.now()
        payload = {
            "seq": seq,
            "chain_scope_id": scope,
            "event_type": event_type,
            "approval_id": approval_id,
            "grant_id": grant_id,
            "action_hash": action_hash,
            "event_metadata": meta,
            "created_at": _norm_ts(created_at),
        }
        rh = row_hash(prev_hash, payload)
        entry = ActionLogEntry(
            id=str(uuid.uuid4()),
            chain_scope_id=scope,
            seq=seq,
            event_type=event_type,
            approval_id=approval_id,
            grant_id=grant_id,
            action_hash=action_hash,
            event_metadata=meta_text,
            prev_row_hash=prev_hash,
            row_hash=rh,
            created_at=created_at,
        )
        db.add(entry)
        try:
            db.flush()
        except IntegrityError as exc:  # lost the (scope, seq) race — retry
            db.rollback()
            last_err = exc
            continue
        _update_head_advisory(db, lock.dialect, scope, seq, rh, created_at)
        db.commit()
        return entry

    raise ChainIntegrityError(
        f"could not append to chain {scope!r} after {_APPEND_RETRIES} retries: {last_err}"
    )


# ── verification ───────────────────────────────────────────────────────


class ChainReport:
    def __init__(self, ok: bool, *, scope: str, count: int,
                 break_at: int | None = None, detail: str = ""):
        self.ok = ok
        self.scope = scope
        self.count = count
        self.break_at = break_at
        self.detail = detail

    def __repr__(self):
        return (
            f"<ChainReport ok={self.ok} scope={self.scope!r} count={self.count} "
            f"break_at={self.break_at} detail={self.detail!r}>"
        )


def verify_chain(db: Session, *, tenant_id: str | None = None,
                 user_id: int | None = None, scope: str | None = None) -> ChainReport:
    """Recompute every row_hash, check linkage, and check that ``seq`` is
    contiguous from 1. Detects modification, deletion, reordering, duplication
    (also blocked by UNIQUE), and incorrect linking.
    """
    scope = scope or chain_scope_id(tenant_id=tenant_id, user_id=user_id)
    rows = (
        db.query(ActionLogEntry)
        .filter(ActionLogEntry.chain_scope_id == scope)
        .order_by(ActionLogEntry.seq.asc())
        .all()
    )
    if not rows:
        return ChainReport(True, scope=scope, count=0, detail="empty chain")

    prev_hash = GENESIS
    expected_seq = 1
    for r in rows:
        if r.seq != expected_seq:
            return ChainReport(False, scope=scope, count=len(rows), break_at=r.seq,
                               detail=f"seq gap/reorder: expected {expected_seq}, got {r.seq}")
        if r.prev_row_hash != prev_hash:
            return ChainReport(False, scope=scope, count=len(rows), break_at=r.seq,
                               detail=f"broken link at seq {r.seq}")
        try:
            meta = json.loads(r.event_metadata)
        except (ValueError, TypeError):
            return ChainReport(False, scope=scope, count=len(rows), break_at=r.seq,
                               detail=f"unparseable event_metadata at seq {r.seq}")
        payload = {
            "seq": int(r.seq),
            "chain_scope_id": r.chain_scope_id,
            "event_type": r.event_type,
            "approval_id": r.approval_id,
            "grant_id": r.grant_id,
            "action_hash": r.action_hash,
            "event_metadata": meta,
            "created_at": (_norm_ts(r.created_at) if r.created_at is not None else None),
        }
        recomputed = row_hash(r.prev_row_hash, payload)
        if recomputed != r.row_hash:
            return ChainReport(False, scope=scope, count=len(rows), break_at=r.seq,
                               detail=f"row_hash mismatch at seq {r.seq} (row was modified)")
        prev_hash = r.row_hash
        expected_seq += 1

    return ChainReport(True, scope=scope, count=len(rows), detail="verified")
