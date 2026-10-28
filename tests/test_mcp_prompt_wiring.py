from app.services import tool_registry
from app.services.tool_registry import RegisteredTool


def test_langgraph_prompt_includes_mcp_block(monkeypatch):
    from app.services import langgraph_workflow as lw
    monkeypatch.setattr(lw.settings, "enable_mcp_tool", True)
    monkeypatch.setattr(
        tool_registry, "list_tools",
        lambda db, uid: [RegisteredTool("mcp__s__echo", "Echo", {}, "mcp", 1)],
    )
    state = {
        "system_prompt": None, "memory_context": "", "retrieved_context": "",
        "web_context": "", "command_result": "", "code_result": "",
        "mcp_result": "", "db": object(), "user_id": 1,
    }
    prompt = lw._build_system_prompt(state)
    assert "## MCP Tools" in prompt
    assert "mcp__s__echo" in prompt


def test_mcp_result_block_rendered(monkeypatch):
    from app.services import langgraph_workflow as lw
    monkeypatch.setattr(lw.settings, "enable_mcp_tool", True)
    monkeypatch.setattr(tool_registry, "list_tools", lambda db, uid: [])
    state = {
        "system_prompt": None, "memory_context": "", "retrieved_context": "",
        "web_context": "", "command_result": "", "code_result": "",
        "mcp_result": "=== MCP Tool Result (mcp__s__t) ===\nX\n=== End of MCP Tool Result ===",
        "db": object(), "user_id": 1,
    }
    assert "MCP Tool Result" in lw._build_system_prompt(state)


def test_chat_context_carries_user_id():
    from app.services.streaming_service import ChatContext
    from app.models.models import Message
    ctx = ChatContext(
        session_id=1, user_message=Message(), history=[], system_prompt="x",
        citations=[], sources_used=False, memories_used=False, user_id=7,
    )
    assert ctx.user_id == 7
