"""MP-4C - derive a signed executor GRANT from DURABLE server-owned rows.

UNWIRED in this slice: nothing calls this from _mcp_dispatch, the worker, or the
approval gateway. The application-side DispatchRecord remains the execution
authority; this module only ASSERTS that authority into a signed claim set.

Identity is never taken from a request or a model. Every field comes from a row:
  Run      -> user_id, tenant_id, run_id
  RunStep  -> step_id (must be a tool_dispatch step of THAT run and owner)
  ToolCall -> tool_call_id + args_hash (the action identity)
  DispatchRecord -> outcome must already be 'started' for that tool_call_id

A grant is issued ONLY when those durable identities agree. Otherwise a bounded
GrantDerivationError is raised and no credential exists.
"""
from __future__ import annotations

import time
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.models.models import DispatchRecord, Run, RunStep, ToolCall
from mp4c_contract import grant as g

RUN_NOT_FOUND = "executor_grant_run_not_found"
STEP_NOT_FOUND = "executor_grant_step_not_found"
TOOL_CALL_NOT_FOUND = "executor_grant_tool_call_not_found"
DISPATCH_NOT_STARTED = "executor_grant_dispatch_not_started"
OWNER_MISMATCH = "executor_grant_owner_mismatch"
BINDING_MISMATCH = "executor_grant_binding_mismatch"
APPROVAL_MISMATCH = "executor_grant_approval_mismatch"

#: The one tool whose execution this contract authorizes.
PYTHON_TOOL_ID = "python_sandbox"
TOOL_DISPATCH_KIND = "tool_dispatch"
DISPATCH_STARTED = "started"


class GrantDerivationError(Exception):
    """Bounded refusal; the code is a slug, never a row value or a secret."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _load_bound_rows(db: Session, *, run_id: str, step_id: str,
                     tool_call_id: str, tenant_id, tool_id):
    """The durable Run / step / ToolCall identity chain, or a bounded refusal."""
    run = db.query(Run).filter(Run.id == run_id).one_or_none()
    if run is None:
        raise GrantDerivationError(RUN_NOT_FOUND)
    step = db.query(RunStep).filter(RunStep.id == step_id).one_or_none()
    if step is None:
        raise GrantDerivationError(STEP_NOT_FOUND)
    call = db.query(ToolCall).filter(ToolCall.id == tool_call_id).one_or_none()
    if call is None:
        raise GrantDerivationError(TOOL_CALL_NOT_FOUND)
    if tenant_id is not None and tenant_id != run.tenant_id:
        raise GrantDerivationError(OWNER_MISMATCH)
    if not (step.run_id == run.id
            and step.user_id == run.user_id
            and step.tenant_id == run.tenant_id
            and call.user_id == run.user_id
            and call.tenant_id == run.tenant_id):
        raise GrantDerivationError(OWNER_MISMATCH)
    if step.kind != TOOL_DISPATCH_KIND:
        raise GrantDerivationError(BINDING_MISMATCH)
    if step.tool_call_id and step.tool_call_id != call.id:
        raise GrantDerivationError(OWNER_MISMATCH)
    if step.execution_turn_id and step.execution_turn_id != call.execution_turn_id:
        raise GrantDerivationError(OWNER_MISMATCH)
    if call.tool_id != tool_id:
        raise GrantDerivationError(BINDING_MISMATCH)
    return run, step, call


def derive_reservation_grant(db: Session, *, run_id: str, step_id: str,
                             tool_call_id: str, code: str,
                             tenant_id: Optional[str] = None,
                             tool_id: str = PYTHON_TOOL_ID,
                             ttl_s: int = g.MAX_TTL_S,
                             now: Optional[int] = None,
                             key: Optional[bytes] = None) -> str:
    """PRE-DISPATCH capacity credential for the approved, bound action.

    Deliberately does NOT require DispatchRecord.started: the gateway has not been
    asked to dispatch yet, and the whole point is to discover "busy" BEFORE that
    row is committed. Purpose-separated, so this token can never execute code.
    """
    run, step, call = _load_bound_rows(db, run_id=run_id, step_id=step_id,
                                       tool_call_id=tool_call_id,
                                       tenant_id=tenant_id, tool_id=tool_id)
    issued = int(time.time()) if now is None else int(now)
    ttl = max(1, min(int(ttl_s), g.MAX_TTL_S))
    sha, length = g.code_sha256(code)
    claims = g.GrantClaims(
        v=g.GRANT_VERSION, jti=uuid.uuid4().hex, iat=issued, exp=issued + ttl,
        user_id=run.user_id, tenant_id=run.tenant_id, run_id=run.id,
        step_id=step.id, tool_call_id=call.id,
        approval_id=(step.approval_id or run.pending_approval_id),
        action_hash=call.args_hash, code_sha256=sha, code_len=length)
    return g.issue(claims, key if key is not None else g.key_from_env(),
                   purpose=g.PURPOSE_RESERVE)


def derive_executor_grant(db: Session, *, run_id: str, step_id: str,
                          tool_call_id: str, code: str,
                          tenant_id: Optional[str] = None,
                          tool_id: str = PYTHON_TOOL_ID,
                          ttl_s: int = g.MAX_TTL_S,
                          now: Optional[int] = None,
                          key: Optional[bytes] = None) -> str:
    """Signed grant for EXACTLY this code, or GrantDerivationError. Never partial."""
    run = db.query(Run).filter(Run.id == run_id).one_or_none()
    if run is None:
        raise GrantDerivationError(RUN_NOT_FOUND)
    step = db.query(RunStep).filter(RunStep.id == step_id).one_or_none()
    if step is None:
        raise GrantDerivationError(STEP_NOT_FOUND)
    call = db.query(ToolCall).filter(ToolCall.id == tool_call_id).one_or_none()
    if call is None:
        raise GrantDerivationError(TOOL_CALL_NOT_FOUND)

    # The caller's tenant claim must agree with the durable row.
    if tenant_id is not None and tenant_id != run.tenant_id:
        raise GrantDerivationError(OWNER_MISMATCH)
    # Ownership chain: every row must name the SAME owner.
    if not (step.run_id == run.id
            and step.user_id == run.user_id
            and step.tenant_id == run.tenant_id
            and call.user_id == run.user_id
            and call.tenant_id == run.tenant_id):
        raise GrantDerivationError(OWNER_MISMATCH)
    if step.kind != TOOL_DISPATCH_KIND:
        raise GrantDerivationError(BINDING_MISMATCH)
    if step.tool_call_id and step.tool_call_id != call.id:
        raise GrantDerivationError(OWNER_MISMATCH)
    if step.execution_turn_id and step.execution_turn_id != call.execution_turn_id:
        raise GrantDerivationError(OWNER_MISMATCH)
    if call.tool_id != tool_id:
        raise GrantDerivationError(BINDING_MISMATCH)

    # The gateway ledger must ALREADY have committed 'started' for this call.
    record = (db.query(DispatchRecord)
              .filter(DispatchRecord.tool_call_id == call.id).one_or_none())
    if record is None or record.outcome != DISPATCH_STARTED:
        raise GrantDerivationError(DISPATCH_NOT_STARTED)
    if record.user_id != run.user_id or record.tenant_id != run.tenant_id:
        raise GrantDerivationError(OWNER_MISMATCH)
    # The committed DispatchRecord IS the execution authority: bind ITS action
    # hash. It is NOT required to equal ToolCall.args_hash — the gateway hashes
    # the executor-facing action, the proposal hashed its own args, and they are
    # different computations (observed live). ToolCall existence + tool_id +
    # ownership are still enforced above.

    references = {ref for ref in (record.approval_id, step.approval_id,
                                  run.pending_approval_id) if ref}
    if len(references) > 1:
        raise GrantDerivationError(APPROVAL_MISMATCH)
    approval_id = references.pop() if references else None

    issued = int(time.time()) if now is None else int(now)
    ttl = max(1, min(int(ttl_s), g.MAX_TTL_S))
    sha, length = g.code_sha256(code)
    claims = g.GrantClaims(
        v=g.GRANT_VERSION, jti=uuid.uuid4().hex, iat=issued, exp=issued + ttl,
        user_id=run.user_id, tenant_id=run.tenant_id, run_id=run.id,
        step_id=step.id, tool_call_id=call.id, approval_id=approval_id,
        action_hash=record.action_hash, code_sha256=sha, code_len=length)
    return g.issue(claims, key if key is not None else g.key_from_env())


__all__ = ["GrantDerivationError", "derive_executor_grant",
           "derive_reservation_grant", "PYTHON_TOOL_ID",
           "TOOL_DISPATCH_KIND", "DISPATCH_STARTED", "RUN_NOT_FOUND",
           "STEP_NOT_FOUND", "TOOL_CALL_NOT_FOUND", "DISPATCH_NOT_STARTED",
           "OWNER_MISMATCH", "BINDING_MISMATCH", "APPROVAL_MISMATCH"]
