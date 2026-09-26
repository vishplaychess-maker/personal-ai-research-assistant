"""Durable one-time MCP approval resume — glue between RunEngine and the
Phase 1B.2 approval gateway / trusted registry / dispatch ledger (Phase 1B.2R MP-2).

INTERNAL / IN-PROCESS ONLY. Nothing here touches ``/messages/stream``, SSE, the
frontend, the scheduler, session grants, or a public Run route.

Pieces
------
* :func:`create_mcp_run` — the internal service. Creates a durable ``mcp_call``
  run + one ``tool_dispatch`` step, seals the normalized tool arguments in a
  context-bound encrypted :class:`RunPayload`, opens the server-owned
  ``ExecutionTurn`` / ``ToolCall``, and enqueues the run. It does NOT propose,
  approve, or dispatch anything.
* :func:`mirror_decision_signal` — called by the approvals route inside the SAME
  transaction as ``record_decision``: if the approval is bound to a still-waiting
  durable run, append ONE idempotent ``run_signals(approval_decided)`` row. The
  approve/deny endpoints never execute a tool; this is the only bridge.
* :func:`open_mcp_args` / :func:`finalize_gateway_result` — helpers the
  :class:`app.run_engine.worker.RunWorker` MCP driver uses to open the sealed
  args and to seal the gateway's result before the run's terminal transition.

The at-most-once external dispatch, one-time approval consumption, trusted-tool
revalidation, envelope re-hash + identity binding, and the ``dispatch_records``
crash ledger all remain the **gateway's** authority (``app.approval.gateway``),
unchanged — the worker just calls ``gateway.dispatch`` with a
``GatewayContext`` built entirely from server-owned run/step rows.
"""

from __future__ import annotations

import json
import uuid
from typing import Callable, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.approval import state_machine as _asm
from app.approval.canonical import canonicalize
from app.approval.hashing import args_hash
from app.approval.identity import open_tool_call, open_turn
from app.models.models import (
    PendingApproval,
    Run,
    RunSignal,
    RunStep,
    _utcnow_aware,
)
from app.run_engine.context import AuthContext
from app.run_engine.engine import submit_run, transition_run
from app.run_engine.payloads import open_payload, seal_payload
from app.run_engine.session_binding import validate_session_binding
from app.run_engine.signals import build_signal_payload
from app.run_engine.state_machine import (
    CREATED,
    QUEUED,
    RUNNING,
    WAITING_FOR_APPROVAL,
)

MCP_RUN_KIND = "mcp_call"
MCP_STEP_KIND = "tool_dispatch"

# Bounded status/error codes the MCP driver may write to ``runs.status_reason`` /
# ``run_steps.error_code``. A superset of the gateway's BLOCK_REASONS plus the
# durable-flow-specific terminals. Every entry is a short slug (<= 24 chars).
try:  # pragma: no cover - import guard only
    from app.approval.gateway import BLOCK_REASONS as _GW_BLOCK_REASONS
except Exception:  # pragma: no cover
    _GW_BLOCK_REASONS = frozenset()

MCP_STATUS_REASONS = frozenset(_GW_BLOCK_REASONS) | frozenset({
    "approval_denied",
    "session_revoked",
    "cancelled",
    "outcome_unknown",
    "output_persist_failed",
    "tool_errored",
    "no_step",
    "gateway_error",
})


# ── the internal service ─────────────────────────────────────────────


def create_mcp_run(
    db: Session,
    ctx: AuthContext,
    *,
    mcp_name: str,
    args: dict,
    session_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    clock=None,
) -> Run:
    """Create + enqueue a durable ``mcp_call`` run for exactly one MCP call.

    Fresh session-family validation. Server-owned turn/call identity. Normalized
    args sealed in an encrypted :class:`RunPayload` (never stored raw anywhere
    else). Returns the run in ``queued``. Raises the RunEngine session-binding
    errors, or ``ValueError`` for a bad ``mcp_name`` / ``args``.
    """
    validate_session_binding(db, ctx)
    if not isinstance(mcp_name, str) or not mcp_name.strip():
        raise ValueError("mcp_name must be a non-empty string")
    if not isinstance(args, dict):
        raise ValueError("args must be a dict")

    run = submit_run(
        db, ctx,
        idempotency_key=idempotency_key or uuid.uuid4().hex,
        session_id=session_id,
        kind=MCP_RUN_KIND,
    )

    turn = open_turn(
        db, user_id=ctx.user_id, tenant_id=ctx.tenant_id, session_id=session_id,
        run_id=run.id, kind="reference", clock=clock,
    )
    a_hash = args_hash(args)
    call = open_tool_call(
        db, turn=turn, call_index=0, tool_id=mcp_name, args_hash=a_hash, clock=clock,
    )

    step_id = str(uuid.uuid4())
    # normalized args -> canonical JSON -> sealed once in an encrypted RunPayload
    normalized = canonicalize(args).decode("utf-8")
    payload = seal_payload(
        db,
        tenant_id=ctx.tenant_id, user_id=ctx.user_id, run_id=run.id, step_id=step_id,
        payload_type="tool_args", plaintext=normalized, commit=False,
    )

    step = RunStep(
        id=step_id, run_id=run.id, tenant_id=ctx.tenant_id, user_id=ctx.user_id,
        session_id=session_id, seq=1, kind=MCP_STEP_KIND, engine="native",
        status="pending", execution_turn_id=turn.id, tool_call_id=call.id,
        attempt=0, input_payload_id=payload.id,
        input_ref=json.dumps({"tool_id": mcp_name, "args_hash": a_hash,
                              "args_payload_id": payload.id}, sort_keys=True),
    )
    db.add(step)
    db.flush()

    # commits the step + the sealed-args payload + the created->queued transition
    transition_run(db, ctx, run.id, to=QUEUED, expected_version=0)
    return db.query(Run).filter(Run.id == run.id).first()


# ── approvals-route bridge (same transaction as the decision) ────────


_MIRROR_STATES = frozenset({CREATED, QUEUED, RUNNING, WAITING_FOR_APPROVAL})


def mirror_decision_signal(db: Session, approval: PendingApproval, *,
                           decision: str) -> bool:
    """Flush ONE idempotent ``run_signals(approval_decided)`` for a durable run
    bound to ``approval``, iff the run can still act on it. Called by the
    approvals route BEFORE ``record_decision(commit=False)`` and never committed
    here — the decision's own ActionLog append issues the single commit, so the
    decision, its audit event, and this signal persist (or roll back) together.

    ``decision`` is the route's intended outcome (``_asm.APPROVED`` /
    ``_asm.DENIED``) — the approval row is still ``requested`` at this point.

    Idempotent: ``uq_run_signals_live`` means a second decision cannot create a
    duplicate live signal (a nested savepoint absorbs the ``IntegrityError``
    without disturbing the outer transaction). Returns True iff a row was added.
    """
    run_id = getattr(approval, "run_id", None)
    if not run_id:
        return False
    if decision == _asm.APPROVED:
        decision = "approved"
    elif decision == _asm.DENIED:
        decision = "denied"
    else:
        return False
    run = db.query(Run).filter(Run.id == run_id).first()
    if run is None or run.pending_approval_id != approval.id:
        return False
    if run.status not in _MIRROR_STATES:
        return False

    payload = build_signal_payload(
        "approval_decided", approval_id=approval.id, decision=decision,
        server_ts=_utcnow_aware(),
    )
    sig = RunSignal(
        id=str(uuid.uuid4()), run_id=run.id, kind="approval_decided", payload=payload,
        created_by_user_id=run.user_id, created_by_tenant_id=run.tenant_id,
    )
    try:
        with db.begin_nested():
            db.add(sig)
            db.flush()
    except IntegrityError:
        return False  # a live approval_decided signal already exists — idempotent
    return True


# ── worker-side helpers (called by RunWorker's MCP driver) ──────────


def open_mcp_args(db: Session, run: Run, step: RunStep) -> dict:
    """Decrypt the sealed tool arguments with TRUSTED expected context taken
    only from the server-owned run/step rows. Raises the RunPayload errors."""
    raw = open_payload(
        db, step.input_payload_id,
        expected_tenant_id=run.tenant_id, expected_user_id=run.user_id,
        expected_run_id=run.id, expected_step_id=step.id,
        expected_payload_type="tool_args", expected_action_hash=None,
    )
    return json.loads(raw)


def step_tool_id(step: RunStep) -> Optional[str]:
    try:
        return json.loads(step.input_ref or "{}").get("tool_id")
    except (ValueError, TypeError):
        return None


def default_mcp_transport(db: Session, user_id: int, tool_id: str,
                          args: dict) -> tuple[str, bool]:
    """Real MCP transport for a live deployment. Tests inject a fake instead.
    Returns ``(text, is_error)`` — an ordinary tool failure is ``is_error=True``,
    NOT an exception, so the gateway records a *known* ``failed`` outcome. Only a
    process kill leaves an ambiguous ``started`` record."""
    from app.services import mcp_service
    from app.tools import tool_registry

    hit = tool_registry.resolve_mcp(db, user_id, tool_id)
    if hit is None:
        return ("unknown tool, server disabled, or tool not allow-listed", True)
    cfg, bare = hit
    try:
        return mcp_service.call_tool(cfg, bare, args)
    except Exception as exc:  # noqa: BLE001 — bounded, no message/stack persisted
        return (f"{type(exc).__name__}", True)


MCPTransport = Callable[[Session, int, str, dict], tuple[str, bool]]
