"""Parse and dispatch [MCP_CALL: mcp__<server>__<tool> {json args}] markers.

Phase 1B.2: MCP execution is GATED by the trusted approval gateway
(``app.approval.gateway``). The legacy zero-approval auto-execution is gone.

* ``run_mcp_calls`` is the LIVE chat-path entry. It NEVER auto-executes. It
  returns a structured `blocked` / `approval-required` block:
    - gateway not required            -> risky MCP execution disabled (blocked)
    - gateway unavailable             -> blocked (fail closed)
    - gateway required + available     -> approval-required guidance; the live
      chat generator cannot resume an approved one-time call (no durable turn
      identity — deferred to the durable-run phase), so it does not execute and
      does not create proposals here (avoids unbounded pending rows on retry).
* ``dispatch_mcp_call_via_gateway`` is the TRUSTED INTERNAL reference path used
  by tests / the T45 eval / a future durable dispatcher. It opens a server-owned
  ExecutionTurn + ToolCall and runs ``gateway.dispatch`` with the real
  ``mcp_service.call_tool`` as the ``execute_mcp`` callback.
"""

import json
import logging
import re

from app.config import settings
from app.services import mcp_service, tool_registry

logger = logging.getLogger(__name__)

# Hard fallback if settings are somehow unavailable. The effective cap is
# ``settings.max_mcp_calls_per_response`` (validated as >= 1 at startup).
MAX_MCP_CALLS_PER_TURN = 3


def _max_calls_per_response() -> int:
    try:
        return max(1, int(settings.max_mcp_calls_per_response))
    except Exception:
        return MAX_MCP_CALLS_PER_TURN

# name must look like mcp__<server>__<tool...>; args group is greedy to the last
# '}' on the line. resolve_mcp() is the authoritative validator of the name.
_MARKER_RE = re.compile(
    r"\[MCP_CALL:\s*(mcp__[a-z0-9_]+(?:__[A-Za-z0-9_-]+)+)\s*(\{.*\})?\s*\]"
)


def extract_mcp_calls(text: str) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []
    cap = _max_calls_per_response()
    for line in (text or "").splitlines():
        m = _MARKER_RE.search(line)
        if not m:
            continue
        name, raw_args = m.group(1), (m.group(2) or "").strip()
        if raw_args:
            try:
                args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                logger.warning("MCP marker had unparseable args: %s", raw_args)
                continue
            if not isinstance(args, dict):
                continue
        else:
            args = {}
        calls.append((name, args))
        if len(calls) >= cap:
            break
    return calls


def format_mcp_result(name: str, text: str, is_error: bool) -> str:
    body = f"[error] {text}" if is_error else text
    return (
        f"=== MCP Tool Result ({name}) ===\n"
        f"{body}\n"
        f"=== End of MCP Tool Result ==="
    )


def _blocks(calls: list[tuple[str, dict]], message: str) -> str:
    return "\n\n".join(format_mcp_result(name, message, True) for name, _ in calls)


def run_mcp_calls(calls: list[tuple[str, dict]], db, user_id: int, **_ignored) -> str:
    """LIVE chat-path entry. Never auto-executes an MCP tool.

    ``**_ignored`` keeps the call sites in ``streaming_service`` /
    ``langgraph_workflow`` working unchanged. Any extra kwargs are accepted and
    dropped — the live path has no durable turn identity to use.
    """
    if not calls:
        return ""
    if not settings.enable_mcp_tool:
        return _blocks(calls, "MCP tools are disabled")
    if not settings.approval_gateway_required:
        return _blocks(
            calls,
            "MCP execution is disabled: the approval gateway is not enabled, so "
            "risky MCP tool execution is off. (This never restores legacy "
            "auto-execution.) No tool was run.",
        )
    if not settings.approval_gateway_available:
        return _blocks(calls, "MCP execution blocked: the approval gateway is "
                              "unavailable (fail closed). No tool was run.")
    return _blocks(
        calls,
        "MCP tool execution requires approval via the /api/approvals workflow. "
        "The live chat path cannot resume an approved call yet (durable-run "
        "phase); no tool was executed and no proposal was created here.",
    )


def dispatch_mcp_call_via_gateway(
    db,
    *,
    user_id: int,
    mcp_name: str,
    args: dict,
    call_index: int = 0,
    tenant_id: str | None = None,
    session_id: int | None = None,
    run_id: str | None = None,
    execution_turn=None,
    clock=None,
):
    """TRUSTED INTERNAL reference path (tests / T45 / future durable dispatcher).

    Opens a server-owned ExecutionTurn + ToolCall and routes one MCP call
    through ``app.approval.gateway.dispatch`` with the real
    ``mcp_service.call_tool`` as the execute callback. Returns the
    ``GatewayResult``. Never used by the live streaming/langgraph path.

    ``call_index`` is a **server-allocated slot** within the (server-owned)
    turn — the trusted caller assigns it as it iterates its own dispatch list.
    It is NOT, and must never become, a value trusted merely because it matches
    an LLM marker's ordinal position: ``open_tool_call`` binds the slot to the
    trusted ``(tool_id, args_hash)`` and refuses to rebind it, so a reordered
    or regenerated marker list cannot inherit another call's authorization.
    """
    from app.approval import gateway
    from app.approval.hashing import args_hash
    from app.approval.identity import open_tool_call, open_turn

    turn = execution_turn or open_turn(
        db, user_id=user_id, tenant_id=tenant_id, session_id=session_id,
        run_id=run_id, kind="reference", clock=clock,
    )
    call = open_tool_call(
        db, turn=turn, call_index=call_index, tool_id=mcp_name,
        args_hash=args_hash(args), clock=clock,
    )
    ctx = gateway.GatewayContext(
        db=db, user_id=user_id, tenant_id=tenant_id, session_id=session_id,
        run_id=run_id, execution_turn_id=turn.id, tool_call_id=call.id,
    )

    def _execute() -> tuple[str, bool]:
        hit = tool_registry.resolve_mcp(db, user_id, mcp_name)
        if hit is None:
            return ("unknown tool, server disabled, or tool not allow-listed", True)
        cfg, bare = hit
        try:
            return mcp_service.call_tool(cfg, bare, args)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP call %s failed: %s", mcp_name, exc)
            return (f"{type(exc).__name__}: {exc}", True)

    return gateway.dispatch(ctx, mcp_name=mcp_name, args=args,
                            execute_mcp=_execute, clock=clock)


if __name__ == "__main__":
    assert extract_mcp_calls('[MCP_CALL: mcp__s__t {"x": 1}]') == [("mcp__s__t", {"x": 1})]
    assert extract_mcp_calls("[MCP_CALL: mcp__s__t]") == [("mcp__s__t", {})]
    assert extract_mcp_calls("[MCP_CALL: mcp__s__t {bad}]") == []
    assert "[error]" in format_mcp_result("n", "e", True)
    print("mcp_tool self-check OK")
