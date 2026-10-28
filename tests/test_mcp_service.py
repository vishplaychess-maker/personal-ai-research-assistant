"""Run on the ra-test stack:
    docker compose -p ra-test -f docker-compose.test.yml exec -T backend python -m pytest tests/test_mcp_service.py -v
"""
import os

import pytest

from app.services.mcp_service import MCPServerCfg, discover_tools, call_tool, MCPError

STUB = os.path.join(os.path.dirname(__file__), "fixtures", "stub_mcp_server.py")


def _cfg(**kw):
    base = dict(id=1, name="stub", command="python", args=(STUB,), env=None, tool_allowlist=None)
    base.update(kw)
    return MCPServerCfg(**base)


def test_discover_lists_stub_tools():
    tools = discover_tools(_cfg())
    names = {t["name"] for t in tools}
    assert "echo" in names
    echo = next(t for t in tools if t["name"] == "echo")
    assert "text" in echo["inputSchema"].get("properties", {})


def test_call_tool_round_trips():
    text, is_error = call_tool(_cfg(), "echo", {"text": "hi"})
    assert is_error is False
    assert "echo: hi" in text


def test_call_tool_reports_tool_error():
    text, is_error = call_tool(_cfg(), "always_fails", {})
    assert is_error is True
    assert text


def test_bad_command_raises_mcperror():
    with pytest.raises(MCPError):
        discover_tools(_cfg(command="definitely-not-a-real-binary-xyz"))
