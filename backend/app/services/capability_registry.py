"""Static catalog of executable capabilities offered to the agent.

This is deliberately code, not a database table: the catalog ships with the
application, while only per-user overrides live in ``skill_preferences``. It is
separate from ``app.routes.skills`` (user-authored instruction playbooks) and
from filesystem ``SKILL.md`` discovery.
"""

from __future__ import annotations

from typing import Any

from app.services.mcp_registry import OFFICIAL_SERVERS

CAPABILITIES: tuple[dict[str, Any], ...] = (
    {
        "id": "web_search",
        "name": "Web Search",
        "description": "Search the web and read pages the agent cites in its answers.",
        "icon": "globe",
        "enabled": False,
    },
    {
        "id": "web_scraper",
        "name": "Web Scraper",
        "description": "Read and summarize full web pages as clean Markdown.",
        "icon": "globe",
        "enabled": True,
        "kind": "capability",
    },
    {
        "id": "openbot_research",
        "kind": "capability",
        "name": "Autonomous Research (OpenBot)",
        "description": "Delegates complex, multi-step research to an isolated AI agent",
        "icon": "brain",
        "enabled": False,
    },
    {
        "id": "code_executor",
        "name": "Code Executor",
        "description": "Run snippets in an isolated workspace and return the output.",
        "icon": "terminal",
        "enabled": False,
        "config": {"permission_level": "auto_approve"},
    },
    {
        "id": "open_interpreter",
        "kind": "capability",
        "name": "Open Interpreter (Code Execution)",
        "description": (
            "LLM-powered code execution on this machine via Open Interpreter. "
            "Optional dependency — the app degrades gracefully when it is not "
            "installed. Defaults to Ask: proposed code is shown for approval."
        ),
        "icon": "terminal",
        "enabled": False,
        "config": {"permission_level": "ask_every_time"},
    },
    {
        "id": "file_reader",
        "name": "File Reader",
        "description": "Open files from the workspace to summarize or extract text.",
        "icon": "file-text",
        "enabled": False,
    },
    {
        "id": "desktop_automation",
        "name": "Desktop Automation",
        "description": "Read and write workspace files and run sandboxed commands on this computer.",
        "icon": "monitor",
        "enabled": False,
        "config": {"permission_level": "auto_approve"},
    },
    *(
        {
            "id": server.id,
            "name": f"{server.name} MCP",
            "description": server.description,
            "icon": server.icon,
            "enabled": False,
            "kind": "mcp_server",
            "required_config_keys": list(server.required_config_keys),
        }
        for server in OFFICIAL_SERVERS
    ),
)


def _custom_capabilities() -> list[dict[str, Any]]:
    """Loaded user-defined skills (kind="custom"), or [] when the loader is
    unavailable. Lazily imported: the loader must never be a hard dependency
    of the static catalog, and any failure degrades to builtin-only."""
    try:
        from app.services.custom_skill_loader import list_custom_capabilities

        return list_custom_capabilities()
    except Exception:  # noqa: BLE001 — catalog must never crash on custom skills
        return []


def list_capabilities() -> list[dict[str, Any]]:
    """Return a mutable copy of the catalog, so callers cannot mutate defaults.

    Custom skills scanned from ``data/custom_skills/`` are appended with
    ``kind="custom"`` alongside the built-in entries.
    """
    return [dict(capability) for capability in CAPABILITIES] + _custom_capabilities()


def get_capability(skill_id: str) -> dict[str, Any] | None:
    """Return a copy of one catalog entry, or ``None`` when it is unknown."""
    for capability in CAPABILITIES:
        if capability["id"] == skill_id:
            return dict(capability)
    for capability in _custom_capabilities():
        if capability["id"] == skill_id:
            return dict(capability)
    return None
