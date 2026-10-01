"""Durable chat -> MCP approval -> same-run resume glue (Phase 1B.2R — MP-3C2).

BACKEND-ONLY. Nothing here touches ``/messages/stream``, SSE, the app lifespan,
the scheduler, session grants, or the frontend. It adds **no** second approval
implementation, **no** second dispatch ledger, and **no** second ``[MCP_CALL]``
grammar — every one of those stays the authority of the module it already lives
in:

* ``app.tools.mcp_tool.extract_mcp_calls`` — the ONE canonical
  ``[MCP_CALL: mcp__server__tool {json args}]`` parser (line-based, JSON args).
* ``app.tools.mcp_tool.format_mcp_result`` — the ONE tool-result envelope.
* ``app.approval.gateway.dispatch`` — trusted-registry revalidation, proposal
  creation + dedupe, one-time approval consumption, ``dispatch_records``
  at-most-once, sanitized audit.
* ``app.approval.identity.open_turn`` / ``open_tool_call`` +
  ``app.approval.hashing.args_hash`` — server-owned execution/tool identity.
* ``app.run_engine.mcp_flow`` — ``open_mcp_args`` (trusted expected context),
  ``mirror_decision_signal`` (approvals-route bridge — already run-kind-agnostic),
  ``default_mcp_transport``, ``MCP_STATUS_REASONS``.
* ``app.run_engine.payloads.seal_payload`` / ``open_payload`` — the context-bound
  Fernet ``run_payloads`` envelope (MP-0). Raw args / results / marker text live
  ONLY here.

The ``RunWorker`` chat driver (``app.run_engine.worker``) calls these helpers;
this module never starts a worker, invokes a provider, or resumes a run itself.
"""

from __future__ import annotations

import json
import re
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.approval.canonical import canonicalize
from app.config import settings
from app.models.models import Run, RunStep
from app.run_engine.payloads import open_payload

# step ``kind`` values on a durable chat run (all already valid RunStep kinds)
CHAT_SEGMENT_KINDS = ("load_context", "llm_generate")  # a generation segment
CHAT_TOOL_STEP_KIND = "tool_dispatch"                   # one MCP cycle
CHAT_CONTINUATION_KIND = "llm_generate"

# payload_type tags reused from MP-2 so ``mcp_flow.open_mcp_args`` /
# ``mcp_flow`` gateway wiring work unchanged on a chat run's tool step.
TOOL_ARGS_PAYLOAD_TYPE = "tool_args"
TOOL_OUTPUT_PAYLOAD_TYPE = "tool_output"
SEGMENT_PAYLOAD_TYPE = "other"  # a generation segment's clean assistant text

# Bounded, machine-readable reasons the chat-tool flow may write to
# ``runs.status_reason`` / ``run_steps.error_code``. Unioned into the worker's
# ``_ALL_STATUS_REASONS`` so ``_apply_transition`` never masks them. Every entry
# is a short slug; none carries marker / arg / result / provider text.
CHAT_TOOL_STATUS_REASONS = frozenset({
    "tool_marker_malformed",     # captured [MCP_CALL] did not parse to exactly one call
    "tool_marker_multiple",      # more than one MCP call in the captured block
    "tool_marker_oversized",     # captured block exceeded run_chat_tool_marker_max_bytes
    "tool_marker_incomplete",    # stream ended before the [MCP_CALL] line closed
    "tool_cycle_limit",          # per-run MCP cycle ceiling reached — no new proposal
    "tool_continuation_failed",  # post-tool continuation could not be resumed
    # MP-4C decision 2: model [SAVE_MEMORY]/[SAVE_DIRECTIVE] proposal outcomes.
    "save_marker_malformed",     # captured save marker did not parse to one action
    "save_blocked_sensitive",    # content refused BEFORE any proposal (never shown)
    "save_content_too_large",    # content exceeded run_engine_memory_write_max_chars
    # MP-4C decision 2 follow-up: duplicate-marker suppression outcomes.
    "save_duplicate_approved",   # an unchanged proposal was already approved+saved
    "save_duplicate_denied",     # an unchanged proposal was denied
    "save_duplicate_expired",    # an unchanged proposal expired
    "save_duplicate_unresolved",  # an unchanged proposal is pending/failed/unknown
    # MP-4C policy: real Python execution refused — no host-isolated runtime.
    "python_host_isolation_unavailable",
    # MP-4C pre-dispatch reservation: a broker client is wired but not ready.
    "broker_not_ready",
    # MP-4C pre-dispatch reservation: the broker had no capacity for this action.
    "executor_quota_exceeded",
    # MP-4C broker refusals that surface as a bounded Run/step reason. Every
    # entry is a slug produced by the executor service or the broker client;
    # anything outside this set still degrades to gateway_error.
    "executor_unavailable",
    "executor_request_rejected",
    "executor_not_ready",
    "executor_reservation_exists",
    "executor_reservation_missing",
    "executor_grant_expired",
    "executor_replay",
    "executor_code_binding_mismatch",
})

# only ``[MCP_CALL]`` is resumable; every other execution marker stays blocked
# with MP-3B/3C1's bounded ``tool_resume_required`` (handled in the worker).


def marker_max_bytes() -> int:
    return max(1, int(settings.run_chat_tool_marker_max_bytes))


def max_tool_cycles() -> int:
    return max(1, int(settings.run_chat_max_tool_cycles))


def save_content_max_chars() -> int:
    """Configured character ceiling for ONE model save body. Enforced BEFORE a
    proposal exists, so a proposal can never bind content the approver preview
    would have to truncate."""
    return max(1, int(settings.run_engine_memory_write_max_chars))


def tool_result_max_bytes() -> int:
    return max(1, int(settings.run_chat_tool_result_max_bytes))


class ChatToolMarkerError(Exception):
    """A captured ``[MCP_CALL]`` block is unusable. ``reason`` is a bounded slug
    from :data:`CHAT_TOOL_STATUS_REASONS` — never marker or argument text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason if reason in CHAT_TOOL_STATUS_REASONS else "tool_marker_malformed"


def parse_one_mcp_call(captured_marker: str) -> Tuple[str, dict]:
    """Parse a confidentially-captured ``[MCP_CALL: … ]`` slice with the ONE
    canonical parser and require **exactly one** call.

    Raises :class:`ChatToolMarkerError` (``tool_marker_malformed`` for zero /
    unparseable, ``tool_marker_multiple`` for more than one). Never logs or
    echoes the marker text.
    """
    from app.tools.mcp_tool import extract_mcp_calls  # canonical grammar (lazy import)

    if not isinstance(captured_marker, str) or "[MCP_CALL" not in captured_marker.upper():
        raise ChatToolMarkerError("tool_marker_malformed")
    calls = extract_mcp_calls(captured_marker)
    if len(calls) == 0:
        raise ChatToolMarkerError("tool_marker_malformed")
    if len(calls) > 1:
        raise ChatToolMarkerError("tool_marker_multiple")
    name, args = calls[0]
    if not isinstance(name, str) or not name.startswith("mcp__") or not isinstance(args, dict):
        raise ChatToolMarkerError("tool_marker_malformed")
    return name, args


# The two PERSISTENT-WRITE openers (MP-4C decision 2). Anchored + DOTALL: the
# captured slice is ONE line, exactly as MarkerScanner guarantees. Neither
# pattern accepts a nested '[' so a second marker can never be smuggled in.
_SAVE_MEMORY_RE = re.compile(r"^\s*\[save_memory:\s*(?P<content>.*?)\]\s*$",
                             re.IGNORECASE | re.DOTALL)
_SAVE_DIRECTIVE_RE = re.compile(r"^\s*\[save_directive:\s*(?P<content>.*?)\]\s*$",
                                re.IGNORECASE | re.DOTALL)


def parse_one_save_marker(captured_marker: str) -> Tuple[str, dict]:
    """Parse a confidentially-captured [SAVE_MEMORY: ...] / [SAVE_DIRECTIVE: ...]
    slice into ("memory"|"directive", args). Raises ChatToolMarkerError with the
    bounded reason save_marker_malformed. Never logs or echoes the content."""
    if not isinstance(captured_marker, str):
        raise ChatToolMarkerError("save_marker_malformed")
    text = captured_marker.strip()
    for kind, rx in (("memory", _SAVE_MEMORY_RE),
                     ("directive", _SAVE_DIRECTIVE_RE)):
        m = rx.match(text)
        if m is None:
            continue
        content = (m.group("content") or "").strip()
        if not content or "[" in content:
            raise ChatToolMarkerError("save_marker_malformed")
        if kind == "memory":
            # "preference" is the same default category the legacy explicit
            # memory path used; the write handler re-validates the enum.
            return kind, {"content": content, "category": "preference"}
        return kind, {"content": content}
    raise ChatToolMarkerError("save_marker_malformed")


def normalized_args_json(args: dict) -> str:
    """Canonical JSON for the normalized args — sealed once in an encrypted
    ``RunPayload`` (``tool_args``), never stored raw elsewhere."""
    return canonicalize(args).decode("utf-8")


def open_segment_text(db: Session, run: Run, step: RunStep) -> str:
    """Decrypt one generation segment's clean assistant text from its sealed
    ``RunPayload`` using TRUSTED expected context from the server-owned rows."""
    if not step.output_payload_id:
        return ""
    return open_payload(
        db, step.output_payload_id,
        expected_tenant_id=run.tenant_id, expected_user_id=run.user_id,
        expected_run_id=run.id, expected_step_id=step.id,
        expected_payload_type=SEGMENT_PAYLOAD_TYPE, expected_action_hash=None,
    )


def open_tool_output(db: Session, run: Run, step: RunStep) -> Tuple[str, bool]:
    """Decrypt one tool-dispatch step's sealed result. Returns
    ``(text, is_error)`` — ``is_error`` is derived from the step's own bounded
    ``error_code`` (``tool_errored``), never from the payload bytes."""
    if not step.output_payload_id:
        return ("", True)
    text = open_payload(
        db, step.output_payload_id,
        expected_tenant_id=run.tenant_id, expected_user_id=run.user_id,
        expected_run_id=run.id, expected_step_id=step.id,
        expected_payload_type=TOOL_OUTPUT_PAYLOAD_TYPE, expected_action_hash=None,
    )
    return (text, step.error_code == "tool_errored")


def tool_result_envelope(tool_id: str, text: str, is_error: bool) -> str:
    """Wrap an MCP result as the repo's canonical, clearly-delimited data
    envelope for feeding back to the provider as an **untrusted user-role
    message** (never the system prompt). Size-bounded — a larger result is
    middle-truncated so a hostile tool cannot blow the context budget."""
    from app.tools.mcp_tool import format_mcp_result  # canonical envelope

    cap = tool_result_max_bytes()
    body = text or ""
    if len(body.encode("utf-8")) > cap:
        head = body[: cap // 2]
        tail = body[-(cap // 4):]
        body = f"{head}\n…[tool result truncated: exceeded {cap} bytes]…\n{tail}"
    return format_mcp_result(tool_id, body, is_error)


def tool_step_tool_id(step: RunStep) -> Optional[str]:
    """The server-owned tool id recorded on a tool-dispatch step's non-secret
    ``input_ref`` (mirrors ``mcp_flow.step_tool_id``)."""
    try:
        return json.loads(step.input_ref or "{}").get("tool_id")
    except (ValueError, TypeError):
        return None


__all__ = [
    "CHAT_SEGMENT_KINDS", "CHAT_TOOL_STEP_KIND", "CHAT_CONTINUATION_KIND",
    "TOOL_ARGS_PAYLOAD_TYPE", "TOOL_OUTPUT_PAYLOAD_TYPE", "SEGMENT_PAYLOAD_TYPE",
    "CHAT_TOOL_STATUS_REASONS", "ChatToolMarkerError", "parse_one_mcp_call",
    "normalized_args_json", "open_segment_text", "open_tool_output",
    "tool_result_envelope", "tool_step_tool_id", "marker_max_bytes",
    "max_tool_cycles", "tool_result_max_bytes",
]
