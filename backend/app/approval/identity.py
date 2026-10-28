"""Server-owned execution identity seam (Phase 1B.2).

Two identities, both **generated and owned by the server** — never accepted as
authoritative from the client or the LLM, and **not derived from, or authorized
by, an ordinal position alone**:

* ``execution_turn_id`` — one generation turn (parent of >=1 logical tool call).
* ``tool_call_id``      — the persistent server-owned identity of one logical
  tool call within a turn (the row's UUID). Distinct calls in one turn get
  distinct ids. A reference-domain retry of the SAME persisted logical call
  reuses its stored ``tool_call_id``; a genuinely new call gets a new one.

``call_index`` is a **server-assigned allocation slot** within a persisted turn.
It carries no authority by itself: it is trusted only because the trusted server
dispatch loop assigned it, NOT because it happens to match an LLM marker's
position. A slot may be reused **only** when the stored trusted
``(tool_id, args_hash)`` for that slot match; a different tool identity or a
different ``args_hash`` on an existing slot fails closed (``ValueError``) so a
reordered or regenerated marker list can never inherit a different call's
authorization. A raw client/LLM-supplied index must be mapped to a server slot
by the trusted caller — it must never be passed through and trusted directly.

Lifecycle / retry / retention (documented):

* creation point   : the trusted server dispatch path (``open_turn`` /
  ``open_tool_call``), never a public API, never the streaming request handler
  in this phase.
* persistence      : ``execution_turns`` / ``tool_calls`` tables, owned by
  ``(tenant_id, user_id, session_id)``.
* retry behaviour  : ``open_tool_call(turn_id, call_index=…)`` is idempotent on
  ``UNIQUE(execution_turn_id, call_index)`` — a repeat returns the existing row.
* regenerated vs transport retry : the LIVE ``/messages/stream`` path is NOT
  modified in Phase 1B.2 and CANNOT distinguish a transport retry from a
  regenerated response. Real cross-request turn preservation is deferred to the
  durable-run phase. Only where the same persisted turn/call ids are supplied
  through a trusted internal path does the retry guarantee apply.
* expiry/retention : turns/calls are audit records; prune with the ActionLog
  retention policy (a later operational concern). No TTL enforced here.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.approval.clock import Clock, SystemClock
from app.models.models import ExecutionTurn, ToolCall


def _uuid() -> str:
    return str(uuid.uuid4())


def open_turn(
    db: Session,
    *,
    user_id: int,
    tenant_id: str | None = None,
    session_id: int | None = None,
    run_id: str | None = None,
    assistant_message_id: int | None = None,
    kind: str = "reference",
    clock: Clock | None = None,
) -> ExecutionTurn:
    """Create a new server-owned generation turn."""
    clock = clock or SystemClock()
    turn = ExecutionTurn(
        id=_uuid(), tenant_id=tenant_id, user_id=user_id, session_id=session_id,
        run_id=run_id, assistant_message_id=assistant_message_id, kind=kind,
        created_at=clock.now(),
    )
    db.add(turn)
    db.commit()
    return turn


def open_tool_call(
    db: Session,
    *,
    turn: ExecutionTurn,
    call_index: int,
    tool_id: str,
    args_hash: str,
    clock: Clock | None = None,
) -> ToolCall:
    """Create (or, on a retry of the same persisted logical call, return) the
    ToolCall at ``call_index`` within ``turn``.

    Idempotent via ``UNIQUE(execution_turn_id, call_index)``: a repeat with the
    same ``(turn, call_index)`` returns the stored row (and its id) so a
    reference-domain retry reuses the identity. A mismatch of ``tool_id`` /
    ``args_hash`` on an existing index is a caller bug and raises ValueError —
    it must not silently reuse a different call's identity.
    """
    clock = clock or SystemClock()
    existing = (
        db.query(ToolCall)
        .filter(ToolCall.execution_turn_id == turn.id, ToolCall.call_index == call_index)
        .one_or_none()
    )
    if existing is not None:
        if existing.tool_id != tool_id or existing.args_hash != args_hash:
            raise ValueError(
                f"tool_call slot ({turn.id!r}, {call_index}) already bound to a "
                "different (tool_id, args_hash); refusing to reuse its identity"
            )
        return existing

    call = ToolCall(
        id=_uuid(), execution_turn_id=turn.id, tenant_id=turn.tenant_id,
        user_id=turn.user_id, session_id=turn.session_id, call_index=call_index,
        tool_id=tool_id, args_hash=args_hash, created_at=clock.now(),
    )
    db.add(call)
    try:
        db.commit()
    except IntegrityError:
        # lost a race to create the same slot — return the winner
        db.rollback()
        won = (
            db.query(ToolCall)
            .filter(ToolCall.execution_turn_id == turn.id, ToolCall.call_index == call_index)
            .one()
        )
        if won.tool_id != tool_id or won.args_hash != args_hash:
            raise ValueError("concurrent tool_call slot bound to a different call")
        return won
    return call
