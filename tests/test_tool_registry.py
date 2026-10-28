import json

import pytest

from app.services import tool_registry
from app.services.tool_registry import RegisteredTool, list_tools, resolve_mcp


class _Row:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.name = kw.get("name", "stub")
        self.command = kw.get("command", "python")
        self.args_json = kw.get("args_json", '["-m","x"]')
        self.env_json = kw.get("env_json")
        self.enabled = kw.get("enabled", True)
        self.tool_allowlist_json = kw.get("tool_allowlist_json")
        self.tools_json = kw.get("tools_json", json.dumps([
            {"name": "echo", "description": "e", "inputSchema": {"type": "object"}},
            {"name": "danger", "description": "d", "inputSchema": {}},
        ]))


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    monkeypatch.setattr(tool_registry, "_enabled_mcp_servers", lambda db, uid: [_Row()])


def test_flag_off_returns_native_only(monkeypatch):
    monkeypatch.setattr(tool_registry.settings, "enable_mcp_tool", False)
    tools = list_tools(db=None, user_id=1)
    assert tools and all(t.source == "native" for t in tools)


def test_flag_on_adds_namespaced_mcp_tools(monkeypatch):
    monkeypatch.setattr(tool_registry.settings, "enable_mcp_tool", True)
    names = {t.name for t in list_tools(db=None, user_id=1)}
    assert "mcp__stub__echo" in names
    assert "mcp__stub__danger" in names


def test_allowlist_filters(monkeypatch):
    monkeypatch.setattr(tool_registry.settings, "enable_mcp_tool", True)
    monkeypatch.setattr(
        tool_registry, "_enabled_mcp_servers",
        lambda db, uid: [_Row(tool_allowlist_json='["echo"]')],
    )
    names = {t.name for t in list_tools(db=None, user_id=1)}
    assert "mcp__stub__echo" in names
    assert "mcp__stub__danger" not in names


def test_resolve_mcp_round_trip(monkeypatch):
    monkeypatch.setattr(tool_registry.settings, "enable_mcp_tool", True)
    hit = resolve_mcp(db=None, user_id=1, namespaced="mcp__stub__echo")
    assert hit is not None
    cfg, bare = hit
    assert bare == "echo" and cfg.name == "stub"
    assert resolve_mcp(db=None, user_id=1, namespaced="mcp__stub__danger__x") is None
    assert resolve_mcp(db=None, user_id=1, namespaced="not_mcp") is None
