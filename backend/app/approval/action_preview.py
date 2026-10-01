"""MP-4C - exact Python action preview for the authenticated APPROVER.

Policy (chosen): the authenticated owner of a REQUESTED `python_sandbox`
approval must be able to read the EXACT code bound to that approval before
Approve becomes available. This module is the ONE server-side read path that
may disclose it; it never returns code to any other tool, state, user or scope.

Safety contract:

* Server-owned identity only. `Run` / `RunStep` / `ToolCall` are resolved from
  the PendingApproval row; no client-supplied code, ids or binding is accepted.
* Fail closed, disclose nothing, on ANY of: non-owner, non-python tool,
  non-requested / expired state, sandbox flag off, missing or inconsistent
  run/step/call binding, wrong `python_execution` origin, wrong args/action
  hash, or a payload that cannot be opened with its trusted context.
* The FULL UTF-8 body is returned, or nothing. It is never truncated (a partial
  excerpt could be mistaken for the approved action).
* Read-only: nothing is persisted, logged, emitted as an event, or written to
  the ActionLog. The sealed `tool_args` payload stays the only store.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.approval import state_machine as _sm
from app.approval.clock import SystemClock, ensure_utc
from app.approval.hashing import args_hash
from app.approval.service import reconstruct_envelope
from app.config import settings
from app.models.models import PendingApproval, Run, RunStep, ToolCall


class ActionPreviewError(Exception):
    """Bounded refusal. The reason is a short slug; it NEVER carries code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def build_python_action_preview(db: Session, row: PendingApproval,
                                *, actor_user_id: int) -> dict:
    """Return the exact bound Python action, or raise `ActionPreviewError`.

    Callers must have already authorised `actor_user_id` for `row` (the route
    uses `_owned_approval`); the owner check is repeated here as defence in
    depth so the helper is safe on its own.
    """
    from app.run_engine import chat_tools, mcp_flow, python_approval_identity as pai
    from app.run_engine import sandbox_policy
    from app.run_engine.errors import RunPayloadError

    # 1 - owner
    if row is None or int(row.user_id) != int(actor_user_id):
        raise ActionPreviewError("not_found")

    # 2 - python_sandbox ONLY (generic MCP previews are refused)
    if (row.tool_id or "") != pai.PYTHON_TOOL_ID:
        raise ActionPreviewError("unsupported_tool")

    # 3 - must still be an actionable REQUESTED approval
    if row.state != _sm.REQUESTED:
        raise ActionPreviewError("not_requested")
    if row.expires_at is None or ensure_utc(row.expires_at) <= SystemClock().now():
        raise ActionPreviewError("expired")

    # 4 - the feature flag that governs the whole Python path
    if not getattr(settings, "run_engine_sandbox", False):
        raise ActionPreviewError("preview_disabled")

    # 5 - the server-owned run, owned and tenant-matched
    if not row.run_id or not row.tool_call_id:
        raise ActionPreviewError("binding_mismatch")
    run = db.query(Run).filter(Run.id == row.run_id).first()
    if run is None or int(run.user_id) != int(actor_user_id):
        raise ActionPreviewError("binding_mismatch")
    if (run.tenant_id or None) != (row.tenant_id or None):
        raise ActionPreviewError("binding_mismatch")

    # 6 - the ONE tool_dispatch step bound to this call
    step = (db.query(RunStep)
            .filter(RunStep.run_id == run.id,
                    RunStep.tool_call_id == row.tool_call_id,
                    RunStep.kind == chat_tools.CHAT_TOOL_STEP_KIND)
            .order_by(RunStep.seq.asc()).first())
    if step is None or not step.input_payload_id:
        raise ActionPreviewError("binding_mismatch")

    # 7 - the server-owned Python origin marker
    try:
        ref = json.loads(step.input_ref or "{}")
    except (ValueError, TypeError):
        raise ActionPreviewError("binding_mismatch")
    if (ref.get("tool_id") != pai.PYTHON_TOOL_ID
            or ref.get("purpose") != "python_execution"):
        raise ActionPreviewError("binding_mismatch")

    # 8 - open the sealed args with TRUSTED context only (fails closed)
    try:
        args = mcp_flow.open_mcp_args(db, run, step)
    except RunPayloadError:
        raise ActionPreviewError("payload_unavailable")
    except Exception:  # noqa: BLE001 - any other failure is still a refusal
        raise ActionPreviewError("payload_unavailable")

    # 9 - exact {"code": <non-empty str>} shape
    if (not isinstance(args, dict) or set(args) != {"code"}
            or not isinstance(args.get("code"), str) or not args["code"].strip()):
        raise ActionPreviewError("bad_action_shape")

    # 10 - args/action hash binding across approval, step and tool call
    a = args_hash(args)
    call = db.query(ToolCall).filter(ToolCall.id == step.tool_call_id).first()
    if (a != (row.args_hash or "")
            or a != (ref.get("args_hash") or "")
            or call is None or a != (call.args_hash or "")):
        raise ActionPreviewError("binding_mismatch")
    try:
        env = reconstruct_envelope(row)
    except Exception:  # noqa: BLE001
        raise ActionPreviewError("binding_mismatch")
    if env is None or env.action_hash() != row.action_hash:
        raise ActionPreviewError("binding_mismatch")

    # 11 - the COMPLETE body must fit the configured ceiling, else refuse
    code = args["code"]
    code_bytes = len(code.encode("utf-8"))
    if code_bytes > sandbox_policy.max_code_bytes():
        raise ActionPreviewError("preview_too_large")

    return {
        "approval_id": row.id,
        "tool_id": row.tool_id,
        "code": code,
        "code_bytes": code_bytes,
        "args_hash": a,
        "action_hash": row.action_hash,
        "purpose": row.purpose_redacted or "",
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }




def build_save_action_preview(db: Session, row: PendingApproval,
                              *, actor_user_id: int) -> dict:
    """MP-4C decision 2 - the EXACT memory/directive content bound to a REQUESTED
    save approval, for the owning user only.

    Same safety contract as the Python builder: server-owned identity only, fail
    closed with no content on ANY mismatch, and the WHOLE content or nothing (a
    partial excerpt is never shown, so an approver always sees exactly what they
    approve). Read-only: nothing is persisted, logged or emitted.
    """
    from app.run_engine import chat_tools, mcp_flow
    from app.run_engine import save_approval_identity as sai
    from app.run_engine.errors import RunPayloadError

    # 1 - owner
    if row is None or int(row.user_id) != int(actor_user_id):
        raise ActionPreviewError("not_found")

    # 2 - save tools ONLY (generic MCP / python previews are refused here)
    kind = sai.kind_for_tool_id((row.tool_id or ""))
    if kind is None:
        raise ActionPreviewError("unsupported_tool")

    # 3 - must still be an actionable REQUESTED approval
    if row.state != _sm.REQUESTED:
        raise ActionPreviewError("not_requested")
    if row.expires_at is None or ensure_utc(row.expires_at) <= SystemClock().now():
        raise ActionPreviewError("expired")

    # 4 - the server-owned flag that governs the whole save path
    if not getattr(settings, "run_engine_memory_write_enabled", False):
        raise ActionPreviewError("preview_disabled")

    # 5 - the server-owned run, owned and tenant-matched
    if not row.run_id or not row.tool_call_id:
        raise ActionPreviewError("binding_mismatch")
    run = db.query(Run).filter(Run.id == row.run_id).first()
    if run is None or int(run.user_id) != int(actor_user_id):
        raise ActionPreviewError("binding_mismatch")
    if (run.tenant_id or None) != (row.tenant_id or None):
        raise ActionPreviewError("binding_mismatch")

    # 6 - the ONE tool_dispatch step bound to this call
    step = (db.query(RunStep)
            .filter(RunStep.run_id == run.id,
                    RunStep.tool_call_id == row.tool_call_id,
                    RunStep.kind == chat_tools.CHAT_TOOL_STEP_KIND)
            .order_by(RunStep.seq.asc()).first())
    if step is None or not step.input_payload_id:
        raise ActionPreviewError("binding_mismatch")

    # 7 - the server-owned save origin marker
    try:
        ref = json.loads(step.input_ref or "{}")
    except (ValueError, TypeError):
        raise ActionPreviewError("binding_mismatch")
    if (ref.get("tool_id") != row.tool_id
            or ref.get("purpose") != (kind + "_write")):
        raise ActionPreviewError("binding_mismatch")

    # 8 - open the sealed args with TRUSTED context only (fails closed)
    try:
        args = mcp_flow.open_mcp_args(db, run, step)
    except RunPayloadError:
        raise ActionPreviewError("payload_unavailable")
    except Exception:  # noqa: BLE001 - any other failure is still a refusal
        raise ActionPreviewError("payload_unavailable")

    # 9 - exact args shape per kind
    if not isinstance(args, dict):
        raise ActionPreviewError("bad_action_shape")
    if kind == "memory":
        if set(args) != {"content", "category"}:
            raise ActionPreviewError("bad_action_shape")
    elif set(args) != {"content"}:
        raise ActionPreviewError("bad_action_shape")
    content = args.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ActionPreviewError("bad_action_shape")

    # 10 - args/action hash binding across approval, step and tool call
    a = args_hash(args)
    call = db.query(ToolCall).filter(ToolCall.id == step.tool_call_id).first()
    if (a != (row.args_hash or "")
            or a != (ref.get("args_hash") or "")
            or call is None or a != (call.args_hash or "")):
        raise ActionPreviewError("binding_mismatch")
    try:
        env = reconstruct_envelope(row)
    except Exception:  # noqa: BLE001
        raise ActionPreviewError("binding_mismatch")
    if env is None or env.action_hash() != row.action_hash:
        raise ActionPreviewError("binding_mismatch")

    # 11 - the COMPLETE content must fit the configured ceiling, else refuse
    content_chars = len(content)
    if content_chars > chat_tools.save_content_max_chars():
        raise ActionPreviewError("preview_too_large")

    return {
        "approval_id": row.id,
        "tool_id": row.tool_id,
        "kind": kind,
        "content": content,
        "content_chars": content_chars,
        "args_hash": a,
        "action_hash": row.action_hash,
        "purpose": row.purpose_redacted or "",
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }

__all__ = ["ActionPreviewError", "build_python_action_preview",
           "build_save_action_preview"]
