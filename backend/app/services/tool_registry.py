"""Unified catalog of tools the agent could call: native + per-user MCP.

Phase 1: only MCP tools are dispatched (via the [MCP_CALL] marker). Native
tools keep their existing triggers; they are listed here so a future
bind_tools loop has one authoritative source (the "seam").
"""

import json
import logging
from dataclasses import dataclass

from app.config import settings
from app.models.models import MCPServer
from app.services.mcp_service import MCPServerCfg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    input_schema: dict
    source: str            # "native" | "mcp"
    server_id: int | None


_NATIVE_DESCRIPTORS: list[RegisteredTool] = [
    RegisteredTool("web_scraper", "Fetch and read the visible text of a web page.", {}, "native", None),
    RegisteredTool("youtube_summarizer", "Fetch and summarize a YouTube transcript.", {}, "native", None),
    RegisteredTool("python_sandbox", "Execute a short Python snippet and return its output.", {}, "native", None),
    RegisteredTool("terminal", "Propose a shell command for human approval.", {}, "native", None),
]


def _enabled_mcp_servers(db, user_id: int) -> list[MCPServer]:
    return (
        db.query(MCPServer)
        .filter(MCPServer.user_id == user_id, MCPServer.enabled.is_(True))
        .all()
    )


def _allowed(row: MCPServer, tool_name: str) -> bool:
    if not row.tool_allowlist_json:
        return True
    try:
        allow = set(json.loads(row.tool_allowlist_json))
    except (json.JSONDecodeError, TypeError):
        return True
    return not allow or tool_name in allow


def list_tools(db, user_id: int) -> list[RegisteredTool]:
    tools = list(_NATIVE_DESCRIPTORS)
    if not settings.enable_mcp_tool:
        return tools
    for row in _enabled_mcp_servers(db, user_id):
        try:
            discovered = json.loads(row.tools_json or "[]")
        except (json.JSONDecodeError, TypeError):
            discovered = []
        for t in discovered:
            tname = t.get("name")
            if not tname or "__" in tname:
                if tname and "__" in tname:
                    logger.warning("MCP tool %r on server %r skipped: name contains '__'", tname, row.name)
                continue
            if not _allowed(row, tname):
                continue
            tools.append(RegisteredTool(
                name=f"mcp__{row.name}__{tname}",
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}) or {},
                source="mcp",
                server_id=row.id,
            ))
    return tools


def resolve_mcp(db, user_id: int, namespaced: str) -> tuple[MCPServerCfg, str] | None:
    parts = namespaced.split("__")
    if len(parts) != 3 or parts[0] != "mcp":
        return None
    _, server_name, tool = parts
    for row in _enabled_mcp_servers(db, user_id):
        if row.name != server_name:
            continue
        try:
            discovered = {t.get("name") for t in json.loads(row.tools_json or "[]")}
        except (json.JSONDecodeError, TypeError):
            discovered = set()
        if tool not in discovered or not _allowed(row, tool):
            return None
        return MCPServerCfg.from_row(row), tool
    return None


if __name__ == "__main__":
    # Pure structural self-check (no DB): namespacing + split rules.
    assert ("mcp__a__b".split("__")) == ["mcp", "a", "b"]
    assert len("mcp__a__b__c".split("__")) == 4
    print("tool_registry self-check OK")
