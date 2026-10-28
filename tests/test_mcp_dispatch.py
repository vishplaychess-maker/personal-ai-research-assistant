# -*- coding: utf-8 -*-
"""Task 11 — [MCP_CALL] dispatch in the non-streaming and streaming chat paths.

Covers (goal brief):
  - non-streaming: generate_answer parses [MCP_CALL], executes via
    run_mcp_calls, stores state["mcp_result"], sets regenerate = True
  - streaming: the done-branch appends the formatted MCP result
  - regression safety: no markers / flag off => no dispatch
"""

import asyncio

import pytest

from app.services import cache_service


@pytest.fixture(autouse=True)
def _clean_cag_cache():
    """Tests share one process; the CAG cache must never leak between them."""
    cache_service.clear()
    yield
    cache_service.clear()


def test_generate_answer_dispatches_mcp(monkeypatch):
    from app.services import langgraph_workflow as lw

    monkeypatch.setattr(lw.settings, "enable_mcp_tool", True)
    monkeypatch.setattr(
        lw,
        "generate_response",
        lambda **kw: '[MCP_CALL: mcp__s__echo {"text": "hi"}]',
    )
    # Patch the binding langgraph uses (direct import), so the real
    # subprocess/DB path never runs in a unit test.
    monkeypatch.setattr(
        lw,
        "_run_mcp_calls",
        lambda calls, db, user_id: (
            "=== MCP Tool Result (mcp__s__echo) ===\n"
            "echo: hi\n"
            "=== End of MCP Tool Result ==="
        ),
    )

    state = _minimal_state()
    out = lw.generate_answer(state)
    assert out.get("regenerate") is True
    assert "echo: hi" in out.get("mcp_result", "")


def test_generate_answer_ignores_marker_when_flag_off(monkeypatch):
    from app.services import langgraph_workflow as lw

    monkeypatch.setattr(lw.settings, "enable_mcp_tool", False)
    monkeypatch.setattr(
        lw, "generate_response", lambda **kw: "[MCP_CALL: mcp__s__echo {}]"
    )

    state = _minimal_state()
    out = lw.generate_answer(state)
    assert not out.get("mcp_result")


def test_generate_answer_no_marker_no_mcp(monkeypatch):
    """Regression safety: a plain answer must never trigger MCP dispatch."""
    from app.services import langgraph_workflow as lw

    monkeypatch.setattr(lw.settings, "enable_mcp_tool", True)
    monkeypatch.setattr(lw, "generate_response", lambda **kw: "plain answer")

    def _boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("run_mcp_calls must not run without markers")

    monkeypatch.setattr(lw, "_run_mcp_calls", _boom)

    state = _minimal_state()
    out = lw.generate_answer(state)
    assert out.get("response") == "plain answer"
    assert not out.get("mcp_result")


def test_stream_done_appends_mcp_result(monkeypatch):
    """Streaming done-branch appends the formatted MCP result to the answer."""
    from app.services import streaming_service as ss
    from app.services.streaming_service import ChatContext

    class _FakeProvider:
        async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
            yield {"type": "done", "response": 'text [MCP_CALL: mcp__s__echo {"x": 1}]'}

    class _DummyDB:
        def close(self):
            pass

    import app.database

    monkeypatch.setattr(ss.settings, "enable_mcp_tool", True)
    monkeypatch.setattr(ss, "get_provider", lambda config=None: _FakeProvider())
    monkeypatch.setattr(
        ss,
        "run_mcp_calls",
        lambda calls, db, user_id: (
            "=== MCP Tool Result (mcp__s__echo) ===\n"
            "echo ok\n"
            "=== End of MCP Tool Result ==="
        ),
    )
    monkeypatch.setattr(app.database, "SessionLocal", lambda: _DummyDB())

    ctx = ChatContext(
        session_id=1,
        user_message=None,
        history=[{"role": "user", "content": "q"}],
        system_prompt="sp",
        citations=[],
        sources_used=False,
        memories_used=False,
        user_id=1,
        model_name=None,
        provider_config=None,
    )

    events = asyncio.run(_collect(ss, ctx))
    joined = "".join(events)
    assert "echo ok" in joined
    assert "=== MCP Tool Result (mcp__s__echo) ===" in joined


async def _collect(ss, ctx):
    out = []
    async for ev in ss.stream_chat_response(ctx):
        out.append(ev)
    return out


def _minimal_state():
    return {
        "error": None,
        "messages": [],
        "user_input": "q",
        "image_url": None,
        "model_name": None,
        "db": None,
        "user_id": 1,
        "session_id": 1,
        "regenerate": False,
        "pending_command": None,
        "system_prompt": None,
        "memory_context": "",
        "retrieved_context": "",
        "web_context": "",
        "command_result": "",
        "code_result": "",
        "mcp_result": "",
    }
