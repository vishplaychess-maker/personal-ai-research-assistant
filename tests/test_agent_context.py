"""Agent context collection: workspace filtering and MCP-provider context."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import agent_context, mcp_config_service, mcp_registry


def test_workspace_context_excludes_secrets(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "main.py").write_text("print('hi')", encoding="utf-8")
    (workspace / "README.md").write_text("docs", encoding="utf-8")
    (workspace / ".env").write_text("SECRET=1", encoding="utf-8")
    (workspace / "secrets").mkdir()
    (workspace / "deploy.key").write_text("key", encoding="utf-8")

    monkeypatch.setattr(settings, "desktop_automation_enabled", True)
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))

    events = []
    bundle = agent_context.collect(None, 1, lambda *a, **k: events.append((a, k)))

    text = bundle.text()
    assert "main.py" in text
    assert "README.md" in text
    assert ".env" not in text
    assert "secrets" not in text
    assert "deploy.key" not in text
    assert "workspace" in bundle.tools


def test_mcp_context_uses_registry_credentials(monkeypatch):
    monkeypatch.setattr(settings, "desktop_automation_enabled", False)
    server = mcp_registry.OfficialMCPServer(
        id="github-mcp",
        name="GitHub",
        description="test",
        required_config_keys=("GITHUB_TOKEN",),
    )
    pref = SimpleNamespace(enabled=True, config_json='{"config": {"GITHUB_TOKEN": "x"}}')

    monkeypatch.setattr(mcp_registry, "list_servers", lambda: [server])
    monkeypatch.setattr(
        mcp_config_service, "get_preference", lambda db, user_id, sid: pref
    )
    monkeypatch.setattr(
        mcp_config_service,
        "decrypt_stored_config",
        lambda _raw: {"GITHUB_TOKEN": "test-token"},
    )
    monkeypatch.setattr(
        agent_context, "_github_context", lambda _token: "GitHub user: tester"
    )

    events = []
    bundle = agent_context.collect(
        object(), 1, lambda *a, **k: events.append((a, k))
    )

    assert "GitHub context" in bundle.text()
    assert "github-mcp" in bundle.tools
    assert bundle.warnings == []
    assert any(event[1].get("tool") == "github-mcp" for event in events)
