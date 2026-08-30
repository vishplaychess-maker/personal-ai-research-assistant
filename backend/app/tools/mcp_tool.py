"""Parse and dispatch [MCP_CALL: mcp__<server>__<tool> {json args}] markers.

Mirrors the single-pass [PYTHON_CODE] flow: the LLM emits a marker, we run the
call, and the result is injected back (regenerate in the non-streaming path,
appended in the streaming path).
"""

import json
import logging
import re

from app.services import mcp_service, tool_registry

logger = logging.getLogger(__name__)

MAX_MCP_CALLS_PER_TURN = 3

# name must look like mcp__<server>__<tool...>; args group is greedy to the last
# '}' on the line. resolve_mcp() is the authoritative validator of the name.
_MARKER_RE = re.compile(
    r"\[MCP_CALL:\s*(mcp__[a-z0-9_]+(?:__[A-Za-z0-9_-]+)+)\s*(\{.*\})?\s*\]"
)


def extract_mcp_calls(text: str) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []
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
        if len(calls) >= MAX_MCP_CALLS_PER_TURN:
            break
    return calls


def format_mcp_result(name: str, text: str, is_error: bool) -> str:
    body = f"[error] {text}" if is_error else text
    return (
        f"=== MCP Tool Result ({name}) ===\n"
        f"{body}\n"
        f"=== End of MCP Tool Result ==="
    )


def run_mcp_calls(calls: list[tuple[str, dict]], db, user_id: int) -> str:
    blocks: list[str] = []
    for name, args in calls:
        hit = tool_registry.resolve_mcp(db, user_id, name)
        if hit is None:
            blocks.append(format_mcp_result(
                name, "unknown tool, server disabled, or tool not allow-listed", True))
            continue
        cfg, bare = hit
        try:
            text, is_error = mcp_service.call_tool(cfg, bare, args)
        except Exception as exc:  # MCPError or anything unexpected
            logger.warning("MCP call %s failed: %s", name, exc)
            text, is_error = f"{type(exc).__name__}: {exc}", True
        blocks.append(format_mcp_result(name, text, is_error))
    return "\n\n".join(blocks)


if __name__ == "__main__":
    assert extract_mcp_calls('[MCP_CALL: mcp__s__t {"x": 1}]') == [("mcp__s__t", {"x": 1})]
    assert extract_mcp_calls("[MCP_CALL: mcp__s__t]") == [("mcp__s__t", {})]
    assert extract_mcp_calls("[MCP_CALL: mcp__s__t {bad}]") == []
    assert "[error]" in format_mcp_result("n", "e", True)
    print("mcp_tool self-check OK")
