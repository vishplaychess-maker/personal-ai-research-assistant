"""Durable control-event append / read primitives (Phase 1B.2R — MP-1A).

``run_events`` rows in the ``control`` category are the run's durable,
replayable lifecycle log. Per run, ``run_seq`` is strictly increasing with no
gaps; ``UNIQUE(run_id, run_seq)`` is the final authority and ``runs.last_event_seq``
is the allocator's high-water mark. Seq allocation and the ``last_event_seq``
bump happen in ONE transaction with the row insert, so a failed insert never
permanently advances the mark.

**Per-event closed schemas.** ``data`` is NOT "any non-secret scalar" — each
allowed event type has an exact schema (required keys, optional keys, per-field
type / enum / length). Everything else — unknown type, unknown/extra key,
missing required key, nested value, wrong type, free-form note, prompt, model
i/o, tool name/args, request/response body, credential, token, cookie, header,
ciphertext, exception text, arbitrary URL, client timestamp, client-supplied
id/seq — is rejected. Only server-generated ids, approved state enums and
bounded reason/error codes are storable.

MP-1A ships only the primitives + these five schemas. There is NO SSE / polling
route, NO text-stream (``category == "text"``) handling, and NO worker.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import Run, RunEvent, _utcnow_aware
from app.run_engine._dberr import constraint_violation
from app.run_engine.context import AuthContext
from app.run_engine.errors import (
    ControlEventContention,
    ControlEventFieldError,
    ControlEventTooLarge,
    DisallowedControlEventType,
    RunNotFound,
)
from app.run_engine.multi_agent_orchestration import (
    AGENT_PROGRESS_CODES as _AGENT_PROGRESS_CODES,
)
from app.run_engine.session_binding import validate_session_binding
from app.run_engine.state_machine import RUN_STATES

_RUN_KINDS = frozenset({"chat_stream", "chat_sync", "scheduled", "sub_run", "mcp_call",
                        "deep_research", "multi_agent"})  # Phase 1B.2R MP-3C3D4C/D5B
# Bounded, machine-readable codes only — never free text.
_CANCEL_REASON_CODES = frozenset({
    "user_abort", "client_request", "superseded", "policy", "timeout_guard",
})
# MP-1A error events come only from the engine's own failure branches.
_ERROR_CODES = frozenset({
    "submission_failed", "binding_lost", "state_conflict",
    "seq_contention", "internal",
})

_SUBMIT_MAX_ATTEMPTS = 5


# ── per-event schema registry ────────────────────────────────────────


@dataclass(frozen=True)
class _FieldRule:
    """One field's contract. Every MP-1A field is a closed enum of short
    server-side codes — no free-form strings, numbers, or client values."""

    enum: frozenset
    nullable: bool = False

    def check(self, event_type: str, name: str, value: Any) -> None:
        if value is None:
            if self.nullable:
                return
            raise ControlEventFieldError(event_type, name, "null not allowed")
        if isinstance(value, (dict, list, tuple, set)):
            raise ControlEventFieldError(event_type, name, "nested value not allowed")
        if isinstance(value, bool) or value not in self.enum:
            raise ControlEventFieldError(event_type, name, "value not in allowed set")


@dataclass(frozen=True)
class _EventSchema:
    required: frozenset = field(default_factory=frozenset)
    optional: frozenset = field(default_factory=frozenset)
    fields: dict = field(default_factory=dict)


CONTROL_EVENT_SCHEMAS: dict[str, _EventSchema] = {
    # server-issued on run creation
    "run.created": _EventSchema(
        required=frozenset({"kind"}),
        fields={"kind": _FieldRule(enum=_RUN_KINDS)},
    ),
    # a run entered the queue — carries nothing
    "run.queued": _EventSchema(),
    # a validated state transition; from/to MUST be real run states and the
    # engine only writes this alongside the committed transition
    "run.status": _EventSchema(
        required=frozenset({"from_status", "to_status"}),
        fields={
            "from_status": _FieldRule(enum=frozenset(RUN_STATES)),
            "to_status": _FieldRule(enum=frozenset(RUN_STATES)),
        },
    ),
    # a cancellation was requested (not performed)
    "run.cancel.requested": _EventSchema(
        optional=frozenset({"reason_code"}),
        fields={"reason_code": _FieldRule(enum=_CANCEL_REASON_CODES)},
    ),
    # an engine-side failure — bounded code only, never raw exception text
    "run.error": _EventSchema(
        required=frozenset({"error_code"}),
        fields={"error_code": _FieldRule(enum=_ERROR_CODES)},
    ),
    # Phase 1B.2R MP-3C3D4C — bounded deep-research progress signal. `code`
    # is the ONE closed set from app.run_engine.research_sources
    # .RESEARCH_PROGRESS_CODES (no "research_planning" — D4-DECISION-1) plus
    # the new terminal-adjacent "research_synthesizing" stage. Deliberately
    # NO count/query/URL/title/passage/exception field — `_FieldRule` here
    # only supports closed enums (no bounded-integer-range rule exists in
    # this schema mechanism today), so a count field is intentionally
    # deferred rather than adding new, unproven range-validation logic to
    # this shared, already-tested module for one caller.
    "research.progress": _EventSchema(
        required=frozenset({"code"}),
        fields={"code": _FieldRule(enum=frozenset({
            "research_searching", "research_fetching",
            "research_sources_ready", "research_synthesizing",
        }))},
    ),
    # Phase 1B.2R MP-3C3D5B — bounded durable multi-agent progress signal.
    # Schema defined/validated NOW so `validate_event_data` already rejects
    # anything outside this closed set; NO worker exists yet to emit one
    # (foundation-only phase). `code`'s enum is the imported
    # `multi_agent_orchestration.AGENT_PROGRESS_CODES` frozenset — the ONE
    # canonical definition, not a second literal copy (unlike
    # "research.progress" above, which predates this phase and is out of its
    # scope to change). Deliberately no role/content/citation/exception
    # field — same closed-enum-only limitation `research.progress` carries.
    "agent.progress": _EventSchema(
        required=frozenset({"code"}),
        fields={"code": _FieldRule(enum=_AGENT_PROGRESS_CODES)},
    ),
}

CONTROL_EVENT_TYPES: frozenset[str] = frozenset(CONTROL_EVENT_SCHEMAS)


def validate_event_data(event_type: str, data: Optional[dict[str, Any]]) -> str:
    """Validate ``data`` against the closed schema for ``event_type`` and
    return the canonical compact JSON string to store.

    Raises :class:`DisallowedControlEventType` for an unknown type,
    :class:`ControlEventFieldError` for any schema violation, and
    :class:`ControlEventTooLarge` if the serialized form exceeds
    ``settings.run_control_event_max_bytes``.
    """
    schema = CONTROL_EVENT_SCHEMAS.get(event_type)
    if schema is None:
        raise DisallowedControlEventType(event_type)

    payload: dict[str, Any] = {}
    if data is not None:
        if not isinstance(data, dict):
            raise ControlEventFieldError(event_type, "<data>", "data must be a dict or None")
        allowed = schema.required | schema.optional
        for key, value in data.items():
            if not isinstance(key, str):
                raise ControlEventFieldError(event_type, str(key), "key must be a string")
            if key not in allowed:
                raise ControlEventFieldError(event_type, key, "unknown key")
            schema.fields[key].check(event_type, key, value)
            payload[key] = value

    missing = schema.required - set(payload)
    if missing:
        raise ControlEventFieldError(event_type, sorted(missing)[0], "required key missing")

    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    byte_len = len(text.encode("utf-8"))
    limit = int(settings.run_control_event_max_bytes)
    if byte_len > limit:
        raise ControlEventTooLarge(byte_len, limit)
    return text


def _try_append_event(
    db: Session,
    *,
    run_id: str,
    tenant_id: Optional[str],
    user_id: int,
    event_type: str,
    data_text: str,
    category: str = "control",
) -> Optional[RunEvent]:
    """One attempt to allocate the next ``run_seq`` and insert the row.

    Returns the flushed :class:`RunEvent` on success, or ``None`` on a lost
    race (the ``last_event_seq`` CAS matched no row, or ``UNIQUE(run_id,
    run_seq)`` rejected the insert). On ``None`` the session is left usable —
    the CAS-miss path is a clean no-op; the unique-violation path rolls back
    first. The caller re-reads and retries, or gives up.
    """
    run = db.query(Run).filter(Run.id == run_id).first()
    if run is None:
        raise RunNotFound(run_id)

    current = int(run.last_event_seq or 0)
    next_seq = current + 1

    res = db.execute(
        update(Run)
        .where(Run.id == run_id, Run.last_event_seq == current)
        .values(last_event_seq=next_seq, updated_at=_utcnow_aware())
    )
    if res.rowcount != 1:
        return None

    event = RunEvent(
        id=str(uuid.uuid4()),
        run_id=run_id,
        run_seq=next_seq,
        tenant_id=tenant_id,
        user_id=user_id,
        category=category,
        type=event_type,
        data=data_text,
    )
    db.add(event)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        if not constraint_violation(exc, "uq_run_events_run_seq"):
            raise
        return None
    return event


def append_control_event(
    db: Session,
    ctx: AuthContext,
    run_id: str,
    *,
    type: str,
    data: Optional[dict[str, Any]] = None,
) -> RunEvent:
    """Append one schema-valid control event to ``run_id`` and commit.

    Fresh DB session-family validation (owner-constrained, run-bound). Bounded
    retry on ``run_seq`` contention. Raises :class:`DisallowedControlEventType`,
    :class:`ControlEventFieldError`, :class:`ControlEventTooLarge`,
    :class:`RunNotFound`, :class:`SessionBindingRequired`,
    :class:`SessionBindingMismatch`, or :class:`ControlEventContention`.
    """
    if type not in CONTROL_EVENT_TYPES:
        raise DisallowedControlEventType(type)
    data_text = validate_event_data(type, data)

    run = db.query(Run).filter(Run.id == run_id).first()
    validate_session_binding(db, ctx, run=run)  # raises RunNotFound if run is None

    for _ in range(_SUBMIT_MAX_ATTEMPTS):
        event = _try_append_event(
            db,
            run_id=run_id,
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            event_type=type,
            data_text=data_text,
        )
        if event is not None:
            validate_session_binding(db, ctx, run=run)  # final pre-commit re-check
            db.commit()
            return event
        db.rollback()

    raise ControlEventContention()


def read_control_events(
    db: Session,
    ctx: AuthContext,
    run_id: str,
    *,
    after_seq: int = 0,
    limit: Optional[int] = None,
) -> list[RunEvent]:
    """Session-bound ascending read of ``control`` events with ``run_seq >
    after_seq``. Fresh DB session-family validation. ``limit`` is clamped to
    (and defaults to) ``settings.run_event_read_max_page``.
    """
    run = db.query(Run).filter(Run.id == run_id).first()
    validate_session_binding(db, ctx, run=run)

    max_page = int(settings.run_event_read_max_page)
    if limit is None or limit > max_page or limit < 1:
        limit = max_page

    return (
        db.query(RunEvent)
        .filter(
            RunEvent.run_id == run_id,
            RunEvent.category == "control",
            RunEvent.run_seq > int(after_seq),
        )
        .order_by(RunEvent.run_seq.asc())
        .limit(limit)
        .all()
    )


def read_run_events(
    db: Session,
    ctx: AuthContext,
    run_id: str,
    *,
    after_seq: int = 0,
    limit: Optional[int] = None,
) -> list[RunEvent]:
    """Session-bound ascending read of **all** events (``control`` + ``text``)
    with ``run_seq > after_seq``, strictly ordered by ``run_seq``. Fresh DB
    session-family validation. ``limit`` is clamped to (and defaults to)
    ``settings.run_event_read_max_page`` — the SSE tail's bounded page.
    """
    run = db.query(Run).filter(Run.id == run_id).first()
    validate_session_binding(db, ctx, run=run)

    max_page = int(settings.run_event_read_max_page)
    if limit is None or limit > max_page or limit < 1:
        limit = max_page

    return (
        db.query(RunEvent)
        .filter(RunEvent.run_id == run_id, RunEvent.run_seq > int(after_seq))
        .order_by(RunEvent.run_seq.asc())
        .limit(limit)
        .all()
    )
