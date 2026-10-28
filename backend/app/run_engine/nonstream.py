"""MP-5 prerequisite - the non-streaming chat execution adapter.

NOT wired to any route (see the test that pins this). It exists because the
/messages cutover cannot be shipped while the legacy terminal-approval encoding
still lives in the messages route: an approval marker stored in an assistant
message metadata field, decided by the next chat message text, and resumed
through the legacy workflow entry point with its own resume parameter.
Switching only the fresh-submission path would leave TWO approval paths live in
one session.

This adapter is the independent half the cutover needs and creates NO second
authority: it SUBMITS through the canonical RunEngine, OBSERVES the Run to a
terminal state, and reports the canonical approval identity when the Run parks.
It never decides an approval, never writes that legacy marker field, never calls
the legacy workflow, and never treats a wait timeout as an answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Bounded outcome vocabulary.
SUCCEEDED = "succeeded"
PENDING_APPROVAL = "pending_approval"
PENDING = "pending"
FAILED = "failed"
CANCELLED = "cancelled"
DENIED = "denied"
TIMED_OUT = "timed_out"

# Bounded failure reasons.
REASON_RUN_NOT_FOUND = "run_not_found"
REASON_SESSION_MISSING = "session_missing"
REASON_INVALID_SUBMISSION = "invalid_submission"
REASON_BINDING_INVALID = "session_binding_invalid"
REASON_IDEMPOTENCY_CONFLICT = "idempotency_conflict"
REASON_SUBMIT_FAILED = "submit_failed"

_TERMINAL_OK = frozenset({SUCCEEDED, FAILED, TIMED_OUT, CANCELLED, DENIED})


@dataclass(frozen=True)
class SyncChatOutcome:
    """The truthful result of one non-streaming turn."""
    status: str
    run_id: Optional[str] = None
    created: bool = False
    approval_id: Optional[str] = None
    assistant_message_id: Optional[int] = None
    run_status: Optional[str] = None
    reason: Optional[str] = None


def execute_chat_sync(
    db: Session,
    ctx,
    *,
    session,
    message: str,
    idempotency_key: str,
    wait_deadline_s: float,
    mode: str = "chat",
) -> SyncChatOutcome:
    """Submit one chat turn through the canonical RunEngine and observe it.

    The caller owns the request identity (idempotency key) and the AuthContext
    (user + live refresh-session family): both go through the canonical
    validate_session_binding, so there is no bypass here.

    Retrieval enrichment is deliberately not performed in this slice - the run is
    sealed with the canonical empty context snapshot.
    """
    from app.config import settings
    from app.run_engine import submission as _sub
    from app.run_engine.errors import (IdempotencyKeyConflict,
                                       SessionBindingMismatch,
                                       SessionBindingRequired)
    from app.run_engine.state_machine import WAITING_FOR_APPROVAL
    from app.run_engine.sync_wait import SyncWaitError, wait_for_terminal
    from app.models.models import Message, MessageRole, Run

    if session is None or session.user_id != ctx.user_id:
        return SyncChatOutcome(FAILED, reason=REASON_SESSION_MISSING)

    provider = None
    try:
        from app.services.settings_service import get_user_llm_config

        provider = (get_user_llm_config(db, ctx.user_id) or {}).get("provider")
    except Exception:  # bounded: fall back to the deployment provider
        provider = None
    provider = provider or settings.llm_provider

    try:
        submission = _sub.build_chat_submission(
            session_id=session.id, message=message, image_url=None,
            provider=provider, model=session.model, mode=mode,
            system_prompt=session.system_prompt, options={},
        )
    except ValueError:
        return SyncChatOutcome(FAILED, reason=REASON_INVALID_SUBMISSION)

    try:
        run, created = _sub.create_chat_run(
            db, ctx, submission=submission, idempotency_key=idempotency_key)
    except IdempotencyKeyConflict:
        return SyncChatOutcome(FAILED, reason=REASON_IDEMPOTENCY_CONFLICT)
    except (SessionBindingRequired, SessionBindingMismatch):
        return SyncChatOutcome(FAILED, reason=REASON_BINDING_INVALID)
    except Exception as exc:  # bounded: class name only
        logger.warning("non-streaming submission failed (%s)", type(exc).__name__)
        return SyncChatOutcome(FAILED, reason=REASON_SUBMIT_FAILED)

    try:
        terminal = wait_for_terminal(db, run.id, deadline_s=wait_deadline_s)
    except SyncWaitError as exc:
        if exc.status is None:
            return SyncChatOutcome(FAILED, run_id=run.id, created=created,
                                   reason=REASON_RUN_NOT_FOUND)
        if exc.status == WAITING_FOR_APPROVAL:
            # Parked for a REAL user decision. Report the canonical approval
            # identity; deciding it belongs to the MP-2 approvals API.
            db.expire_all()
            parked = db.query(Run).filter(Run.id == run.id).one_or_none()
            return SyncChatOutcome(
                PENDING_APPROVAL, run_id=run.id, created=created,
                approval_id=(parked.pending_approval_id if parked else None),
                run_status=exc.status)
        # Still running (or queued) at the deadline: NEVER an answer.
        return SyncChatOutcome(PENDING, run_id=run.id, created=created,
                              run_status=exc.status)

    status = terminal.status if terminal.status in _TERMINAL_OK else FAILED
    message_id = None
    if status == SUCCEEDED:
        row = (db.query(Message)
               .filter(Message.session_id == session.id,
                       Message.role == MessageRole.assistant)
               .order_by(Message.id.desc()).first())
        message_id = row.id if row is not None else None
    return SyncChatOutcome(status, run_id=terminal.id, created=created,
                           assistant_message_id=message_id,
                           run_status=terminal.status,
                           reason=terminal.status_reason)


__all__ = [
    "CANCELLED", "DENIED", "FAILED", "PENDING", "PENDING_APPROVAL",
    "REASON_BINDING_INVALID", "REASON_IDEMPOTENCY_CONFLICT",
    "REASON_INVALID_SUBMISSION", "REASON_RUN_NOT_FOUND", "REASON_SESSION_MISSING",
    "REASON_SUBMIT_FAILED", "SUCCEEDED", "TIMED_OUT", "SyncChatOutcome",
    "execute_chat_sync",
]
