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


def test_extract_skips_malformed_json():
    assert extract_mcp_calls('[MCP_CALL: mcp__s__t {not json}]') == []


def test_format_result_error_marker():
    out = format_mcp_result("mcp__s__t", "boom", True)
    assert "mcp__s__t" in out and "[error]" in out


def test_run_rejects_unknown_without_spawn(monkeypatch):
    monkeypatch.setattr(mcp_tool.tool_registry, "resolve_mcp", lambda db, uid, n: None)
    called = {"n": 0}
    monkeypatch.setattr(mcp_tool.mcp_service, "call_tool",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or ("", False))
    out = run_mcp_calls([("mcp__x__y", {})], db=None, user_id=1)
    assert called["n"] == 0
    assert "unknown" in out.lower() or "not available" in out.lower()


def test_run_dispatches_known(monkeypatch):
    from app.services.mcp_service import MCPServerCfg
    cfg = MCPServerCfg(id=1, name="s", command="python", args=(), env=None, tool_allowlist=None)
    monkeypatch.setattr(mcp_tool.tool_registry, "resolve_mcp", lambda db, uid, n: (cfg, "t"))
    monkeypatch.setattr(mcp_tool.mcp_service, "call_tool", lambda c, t, a: ("RESULT", False))
    out = run_mcp_calls([("mcp__s__t", {"a": 1})], db=None, user_id=1)
    assert "RESULT" in out
