from app.services.system_prompts import build_mcp_tools_block
from app.services.tool_registry import RegisteredTool


def test_empty_when_no_tools():
    assert build_mcp_tools_block([]) == ""


def test_lists_tools_and_marker_format():
    tools = [RegisteredTool("mcp__fs__read", "Read a file", {"type": "object",
             "properties": {"path": {"type": "string"}}}, "mcp", 1)]
    block = build_mcp_tools_block(tools)
    assert "## MCP Tools" in block
    assert "[MCP_CALL:" in block
    assert "mcp__fs__read" in block
    assert "path" in block
