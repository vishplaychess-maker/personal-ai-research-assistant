"""Minimal stdio MCP server for tests. Tools: echo(text)->"echo: <text>", always_fails()->raises.
Spawned by mcp_service as:  python tests/fixtures/stub_mcp_server.py
"""
from mcp.server.fastmcp import FastMCP as MCPServer   # mcp 1.12.4 (v1.x)

mcp = MCPServer("stub")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the input back with a prefix."""
    return f"echo: {text}"


@mcp.tool()
def always_fails() -> str:
    """Raises, to exercise the error path."""
    raise RuntimeError("intentional failure")


if __name__ == "__main__":
    mcp.run()   # stdio transport by default
