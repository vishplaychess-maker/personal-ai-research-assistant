"""Client for local (stdio) MCP servers: connect, discover tools, call a tool.

Spawn-per-operation: each discover / call opens a fresh subprocess via
stdio_client (a cancellation-shielded context manager) and closes it on exit.
No persistent connection pool this phase.
"""

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Mapping

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.config import settings
from app.services.async_bridge import run_coro_sync

logger = logging.getLogger(__name__)


class MCPError(RuntimeError):
    """Raised when an MCP server cannot be spawned or the session fails to start."""


@dataclass(frozen=True)
class MCPServerCfg:
    id: int
    name: str
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str] | None
    tool_allowlist: frozenset[str] | None      # None => allow all discovered tools

    @classmethod
    def from_row(cls, row) -> "MCPServerCfg":
        try:
            args = tuple(json.loads(row.args_json or "[]"))
        except (json.JSONDecodeError, TypeError):
            args = ()
        env = None
        if row.env_json:
            try:
                env = dict(json.loads(row.env_json))
            except (json.JSONDecodeError, TypeError):
                env = None
        allow = None
        if row.tool_allowlist_json:
            try:
                allow = frozenset(json.loads(row.tool_allowlist_json))
            except (json.JSONDecodeError, TypeError):
                allow = None
        return cls(
            id=row.id, name=row.name, command=row.command,
            args=args, env=env, tool_allowlist=allow,
        )


def _params(cfg: MCPServerCfg) -> StdioServerParameters:
    return StdioServerParameters(
        command=cfg.command,
        args=list(cfg.args),
        env=dict(cfg.env) if cfg.env else None,
    )


async def _with_session(cfg: MCPServerCfg, fn):
    try:
        async with stdio_client(_params(cfg)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)
    except MCPError:
        raise
    except Exception as exc:  # spawn failure, protocol error, timeout during init
        raise MCPError(f"{type(exc).__name__}: {exc}") from exc


async def _discover_async(cfg: MCPServerCfg) -> list[dict]:
    async def go(session: ClientSession) -> list[dict]:
        res = await session.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": dict(t.inputSchema) if t.inputSchema else {},
            }
            for t in res.tools
        ]

    return await _with_session(cfg, go)


async def _call_async(cfg: MCPServerCfg, tool: str, arguments: dict) -> tuple[str, bool]:
    async def go(session: ClientSession) -> tuple[str, bool]:
        res = await session.call_tool(
            tool, arguments or {},
            read_timeout_seconds=timedelta(seconds=settings.mcp_call_timeout_s),
        )
        parts = []
        for block in getattr(res, "content", []) or []:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
        # error flag attribute name confirmed in Task 1 spike (isError vs is_error)
        is_error = bool(getattr(res, "isError", getattr(res, "is_error", False)))
        return ("\n".join(parts).strip(), is_error)

    return await _with_session(cfg, go)


def discover_tools(cfg: MCPServerCfg) -> list[dict]:
    return run_coro_sync(lambda: _discover_async(cfg))


def call_tool(cfg: MCPServerCfg, tool: str, arguments: dict) -> tuple[str, bool]:
    return run_coro_sync(lambda: _call_async(cfg, tool, arguments))


if __name__ == "__main__":
    import os

    stub = os.path.join(
        os.path.dirname(__file__), "..", "..", "tests", "fixtures", "stub_mcp_server.py"
    )
    cfg = MCPServerCfg(id=0, name="stub", command="python",
                       args=(os.path.abspath(stub),), env=None, tool_allowlist=None)
    tools = discover_tools(cfg)
    assert any(t["name"] == "echo" for t in tools), tools
    text, err = call_tool(cfg, "echo", {"text": "x"})
    assert not err and "echo: x" in text, (text, err)
    print("mcp_service self-check OK")
