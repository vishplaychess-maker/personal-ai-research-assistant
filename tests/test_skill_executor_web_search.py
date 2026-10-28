"""Web Search executor — provider mocked, no network, no real key."""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.skill_executors import web_search as ws


class _FakeClient:
    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.payload


class UsageLimitExceededError(Exception):
    """Same class name as tavily.errors.UsageLimitExceededError."""


class InvalidAPIKeyError(Exception):
    """Same class name as tavily.errors.InvalidAPIKeyError."""


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "test-key")
    yield


def test_missing_key_raises_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "")
    with pytest.raises(ws.WebSearchNotConfigured):
        ws.execute_web_search("latest news")


def test_empty_query_returns_empty_without_a_client():
    assert ws.execute_web_search("   ") == []


def test_normalises_and_caps_results():
    client = _FakeClient(
        {
            "results": [
                {"title": "One", "url": "https://example.com/1", "content": "first"},
                {"title": "No URL", "content": "dropped"},
                {"title": "Two", "url": "https://example.com/2", "content": "second"},
                {"title": "Three", "url": "https://example.com/3", "content": "third"},
                {"title": "Four", "url": "https://example.com/4", "content": "fourth"},
            ]
        }
    )

    results = ws.execute_web_search("anything", limit=3, client=client)

    assert results == [
        {"title": "One", "url": "https://example.com/1", "content": "first"},
        {"title": "Two", "url": "https://example.com/2", "content": "second"},
        {"title": "Three", "url": "https://example.com/3", "content": "third"},
    ]
    assert client.calls == [
        {"query": "anything", "max_results": 3, "search_depth": "basic"}
    ]


def test_empty_title_falls_back_to_url():
    client = _FakeClient({"results": [{"url": "https://example.com/a"}]})
    assert ws.execute_web_search("q", client=client)[0]["title"] == "https://example.com/a"


def test_quota_error_becomes_rate_limited():
    client = _FakeClient(exc=UsageLimitExceededError("quota"))
    with pytest.raises(ws.WebSearchRateLimited):
        ws.execute_web_search("q", client=client)


def test_invalid_key_becomes_not_configured():
    client = _FakeClient(exc=InvalidAPIKeyError("bad key"))
    with pytest.raises(ws.WebSearchNotConfigured):
        ws.execute_web_search("q", client=client)


def test_unknown_provider_error_becomes_failed():
    client = _FakeClient(exc=RuntimeError("boom"))
    with pytest.raises(ws.WebSearchFailed):
        ws.execute_web_search("q", client=client)
