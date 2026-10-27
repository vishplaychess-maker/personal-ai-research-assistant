"""Regression: a successful generation must not collapse into INTERNAL_ERROR.

Root cause of "An unexpected error occurred during generation." on a working
Gemini/Google chat: the CAG cache guard referenced a removed `code` variable
(NameError), so the outer except replaced a perfectly good answer with the
generic INTERNAL_ERROR frame. These tests pin the fixed behaviour, the bounded
provider-error path, and the empty-choices stream-chunk guard.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import streaming_service as ss
from app.services.streaming_service import ChatContext


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No network, no cache writes, no evaluator provider call."""
    from app.services import planning_service, semantic_cache
    import app.services.evaluation_service as ev

    calls = {"store": 0}
    monkeypatch.setattr(semantic_cache, "chat_lookup", lambda *a, **k: (None, None))
    monkeypatch.setattr(
        semantic_cache, "chat_store",
        lambda *a, **k: calls.__setitem__("store", calls["store"] + 1),
    )
    monkeypatch.setattr(planning_service, "generate_plan_for_query", lambda **k: [])
    monkeypatch.setattr(ev, "evaluate_response", lambda **k: {"confidence": None, "reason": None})
    monkeypatch.setattr(ss.settings, "enable_mcp_tool", False)
    monkeypatch.setattr(ss.settings, "enable_multi_agent", False)
    return calls


def _ctx(history=None):
    return ChatContext(
        session_id=1,
        user_message=None,
        history=history if history is not None else [{"role": "user", "content": "hello"}],
        system_prompt="sp",
        citations=[],
        sources_used=False,
        memories_used=False,
        user_id=1,
        model_name=None,
        provider_config=None,
        user_input="hello",
        mode="chat",
    )


class _FakeProvider:
    name = "Fake"

    def __init__(self, chunks):
        self._chunks = chunks

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        for chunk in self._chunks:
            yield chunk


def _drain(ctx):
    async def _run():
        out = []
        async for ev in ss.stream_chat_response(ctx):
            out.append(ev)
        return out

    return asyncio.run(_run())


def test_successful_generation_emits_complete_not_internal(monkeypatch):
    monkeypatch.setattr(ss, "get_provider", lambda config=None: _FakeProvider([
        {"type": "token", "token": "hi"},
        {"type": "done", "response": "hi"},
    ]))
    joined = "".join(_drain(_ctx()))
    assert "event: complete" in joined, joined
    assert "INTERNAL_ERROR" not in joined
    assert "An unexpected error occurred during generation." not in joined


def test_cag_store_runs_on_success(monkeypatch, _hermetic):
    monkeypatch.setattr(ss, "get_provider", lambda config=None: _FakeProvider([
        {"type": "token", "token": "hi"},
        {"type": "done", "response": "hi"},
    ]))
    _drain(_ctx())
    assert _hermetic["store"] == 1


def test_provider_stream_error_is_bounded_not_internal(monkeypatch):
    monkeypatch.setattr(ss, "get_provider", lambda config=None: _FakeProvider([
        {"type": "error", "error": "Google AI returned HTTP 404: model retired"},
    ]))
    joined = "".join(_drain(_ctx()))
    assert "PROVIDER_ERROR" in joined
    assert "model retired" in joined
    assert "INTERNAL_ERROR" not in joined


# ── provider stream chunk guard ────────────────────────────────────────


class _FakeHttpResponse:
    def __init__(self, status_code, lines):
        self.status_code = status_code
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStream:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeAsyncClient:
    response = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, *args, **kwargs):
        return _FakeStream(type(self).response)


def test_google_stream_ignores_empty_choices_chunk(monkeypatch):
    """A `choices: []` chunk (keep-alive/usage frame) must not IndexError."""
    import app.services.llm_providers.google_provider as gp

    _FakeAsyncClient.response = _FakeHttpResponse(200, [
        'data: {"choices": []}',
        'data: {"choices": [{"delta": {"content": "ok"}}]}',
        "data: [DONE]",
    ])
    monkeypatch.setattr(gp.httpx, "AsyncClient", _FakeAsyncClient)

    async def _collect():
        out = []
        async for chunk in gp.GoogleProvider(api_key="AQ.test").generate_stream_async(
            messages=[{"role": "user", "content": "hi"}],
        ):
            out.append(chunk)
        return out

    chunks = asyncio.run(_collect())
    assert [c["token"] for c in chunks if c["type"] == "token"] == ["ok"]
    assert chunks[-1]["type"] == "done"
    assert chunks[-1]["response"] == "ok"


# ── a provider that RAISES must not abort the fallback chain ───────────


class _RaisingProvider:
    name = "Ollama"

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        raise RuntimeError("Invalid port: '11434:11434'")
        yield  # pragma: no cover - makes this an async generator


def test_raising_provider_falls_through_to_a_working_one(monkeypatch):
    calls = {"n": 0}

    def _factory(config=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _RaisingProvider()
        return _FakeProvider([
            {"type": "token", "token": "ok"},
            {"type": "done", "response": "ok"},
        ])

    monkeypatch.setattr(ss, "get_provider", _factory)
    joined = "".join(_drain(_ctx()))
    assert "event: complete" in joined, joined
    assert "INTERNAL_ERROR" not in joined


def test_all_providers_raising_is_bounded_not_internal(monkeypatch):
    monkeypatch.setattr(ss, "get_provider", lambda config=None: _RaisingProvider())
    joined = "".join(_drain(_ctx()))
    assert "PROVIDER_ERROR" in joined
    assert "RuntimeError" in joined
    assert "INTERNAL_ERROR" not in joined
