import pytest

from app.tools import mcp_tool
from app.tools.mcp_tool import extract_mcp_calls, format_mcp_result, run_mcp_calls


def test_extract_basic():
    calls = extract_mcp_calls('before\n[MCP_CALL: mcp__srv__echo {"text": "hi"}]\nafter')
    assert calls == [("mcp__srv__echo", {"text": "hi"})]


def test_extract_no_args():
    assert extract_mcp_calls("[MCP_CALL: mcp__srv__list]") == [("mcp__srv__list", {})]


def test_extract_multiple_capped_at_3():
    text = "\n".join(f'[MCP_CALL: mcp__s__t{i} {{}}]' for i in range(5))
    assert len(extract_mcp_calls(text)) == 3


def test_extract_cap_is_config_driven(monkeypatch):
    # Decision 5: MCP calls parsed per generated response are bounded and the
    # bound comes from validated settings, not a hard-coded constant.
    text = "\n".join(f'[MCP_CALL: mcp__s__t{i} {{}}]' for i in range(10))
    monkeypatch.setattr(mcp_tool.settings, "max_mcp_calls_per_response", 1)
    assert len(extract_mcp_calls(text)) == 1
    monkeypatch.setattr(mcp_tool.settings, "max_mcp_calls_per_response", 5)
    assert len(extract_mcp_calls(text)) == 5


def test_extract_skips_malformed_json():
    assert extract_mcp_calls('[MCP_CALL: mcp__s__t {not json}]') == []


def test_format_result_error_marker():
    out = format_mcp_result("mcp__s__t", "boom", True)
    assert "mcp__s__t" in out and "[error]" in out


# Phase 1B.2: run_mcp_calls is the LIVE chat-path entry and NEVER auto-executes.
# It always returns a structured blocked/approval-required block and never
# invokes mcp_service.call_tool. (The trusted internal path
# dispatch_mcp_call_via_gateway is tested in tests/test_approval_gateway.py.)


@pytest.fixture
def _no_calltool(monkeypatch):
    seen = {"n": 0}
    monkeypatch.setattr(mcp_tool.mcp_service, "call_tool",
                        lambda *a, **k: seen.__setitem__("n", seen["n"] + 1) or ("X", False))
    monkeypatch.setattr(mcp_tool.tool_registry, "resolve_mcp", lambda db, uid, n: None)
    return seen


def test_run_mcp_calls_blocked_when_tools_disabled(monkeypatch, _no_calltool):
    monkeypatch.setattr(mcp_tool.settings, "enable_mcp_tool", False)
    out = run_mcp_calls([("mcp__x__y", {})], db=None, user_id=1)
    assert _no_calltool["n"] == 0
    assert "[error]" in out and "disabled" in out.lower()


def test_run_mcp_calls_blocked_when_gateway_not_required(monkeypatch, _no_calltool):
    monkeypatch.setattr(mcp_tool.settings, "enable_mcp_tool", True)
    monkeypatch.setattr(mcp_tool.settings, "approval_gateway_required", False)
    out = run_mcp_calls([("mcp__x__y", {})], db=None, user_id=1)
    assert _no_calltool["n"] == 0
    assert "[error]" in out and "disabled" in out.lower()
    assert "auto-execution" in out.lower()  # never restores legacy behavior


def test_run_mcp_calls_blocked_when_gateway_unavailable(monkeypatch, _no_calltool):
    monkeypatch.setattr(mcp_tool.settings, "enable_mcp_tool", True)
    monkeypatch.setattr(mcp_tool.settings, "approval_gateway_required", True)
    monkeypatch.setattr(mcp_tool.settings, "approval_gateway_available", False)
    out = run_mcp_calls([("mcp__x__y", {})], db=None, user_id=1)
    assert _no_calltool["n"] == 0
    assert "[error]" in out and "unavailable" in out.lower()


def test_run_mcp_calls_approval_required_when_gateway_on(monkeypatch, _no_calltool):
    monkeypatch.setattr(mcp_tool.settings, "enable_mcp_tool", True)
    monkeypatch.setattr(mcp_tool.settings, "approval_gateway_required", True)
    monkeypatch.setattr(mcp_tool.settings, "approval_gateway_available", True)
    out = run_mcp_calls([("mcp__s__t1", {}), ("mcp__s__t2", {})], db=None, user_id=1)
    assert _no_calltool["n"] == 0                      # never executes
    assert out.count("=== MCP Tool Result") == 2       # one block per call
    assert "approval" in out.lower() and "no tool was executed" in out.lower()
    assert "durable-run" in out.lower()                # documents the limitation
