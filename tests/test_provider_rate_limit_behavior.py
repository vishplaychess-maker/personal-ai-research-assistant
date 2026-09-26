"""B-2 behavioral: a mocked 429 through the REAL provider and its boundaries."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.run_engine import chat_stream, messages_stream, worker
from app.services.llm_providers import openrouter_provider as orp
from app.services.llm_providers.errors import ProviderRateLimited

#: Sensitive-looking upstream text that must NEVER reach a reason or a UI detail.
CANARY = "sk-live-CANARY-9f2b X-Request-Id: 1234 upstream says rate limit exceeded"


class _Resp:
    def __init__(self, status):
        self.status_code = status
    async def aread(self):
        return CANARY.encode("utf-8")
    async def aiter_lines(self):
        for line in ():
            yield line


class _Stream:
    def __init__(self, status):
        self._status = status
    async def __aenter__(self):
        return _Resp(self._status)
    async def __aexit__(self, *exc):
        return False


class _Client:
    def __init__(self, status):
        self._status = status
    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
    async def __aenter__(self):
        return self
    async def __aexit__(self, *exc):
        return False
    def stream(self, *a, **kw):
        return _Stream(self._status)


def _client_factory(status):
    def _factory(*a, **kw):
        return _Client(status)
    return _factory


def _chunks(status, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(status))
    provider = orp.OpenRouterProvider(api_key="k", model="m")
    async def _collect():
        return [c async for c in provider.generate_stream_async([], None, None)]
    return asyncio.run(_collect())


def test_a_mocked_429_is_classified_and_never_leaks_the_canary(monkeypatch):
    chunks = _chunks(429, monkeypatch)
    assert len(chunks) == 1 and chunks[0]["type"] == "error"
    assert chunks[0].get("code") == "provider_rate_limited"
    assert CANARY not in repr(chunks)
    err = chat_stream.ProviderStreamError(chunks[0]["code"])
    assert worker._provider_reason(err) == "provider_rate_limited"
    detail = messages_stream._FAIL_DETAIL["provider_rate_limited"]
    assert CANARY not in detail and "429" not in detail


def test_the_rate_limit_classification_survives_stream_normalization(monkeypatch):
    class _P:
        async def generate_stream_async(self, *a, **kw):
            raise ProviderRateLimited()
            yield {}
    # A REAL ChatInput: a synthetic namespace fails _adrive BEFORE the provider is
    # reached (AttributeError on `system_prompt`), which would misattribute the
    # classification failure to the product.
    chat_input = chat_stream.ChatInput(messages=[], system_prompt=None, model="m",
                                       provider="openrouter", mode="chat")
    # REAL datetimes: a fake clock makes _adrive raise TypeError before it ever
    # reaches the provider, which would misattribute the classification failure.
    def _now():
        return datetime.now(timezone.utc)
    with pytest.raises(chat_stream.ProviderStreamError) as ei:
        chat_stream.run_stream(_P(), chat_input, on_token=lambda _t: None,
                               on_idle=lambda: None,
                               deadline=_now() + timedelta(seconds=10),
                               now_fn=_now, poll_interval=0.01)
    assert ei.value.code == "provider_rate_limited"
    assert worker._provider_reason(ei.value) == "provider_rate_limited"
    assert CANARY not in (str(ei.value) + repr(ei.value))


def test_a_non_429_response_stays_a_generic_provider_error(monkeypatch):
    chunks = _chunks(500, monkeypatch)
    assert len(chunks) == 1 and chunks[0]["type"] == "error"
    assert "code" not in chunks[0], "only a 429 may carry a classification"
    err = chat_stream.ProviderStreamError()
    assert worker._provider_reason(err) == "provider_error"
    assert worker._provider_reason(RuntimeError(CANARY)) == "provider_error"
    assert CANARY not in messages_stream._FAIL_DETAIL["provider_error"]