"""Natural-language routing in the Telegram service (Option B).

The provider and the chat pipeline are patched at the service's seams, so no
Telegram traffic, Tavily call, or LLM call happens in these tests.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import telegram_service as ts
from app.services.skill_executors import (
    WebSearchNotConfigured,
    WebSearchRateLimited,
)


class _Identity:
    def __init__(self, identifier):
        self.id = identifier


class _Message:
    def __init__(self, text, user_id=7, chat_id=55):
        self.text = text
        self.chat = _Identity(chat_id)
        self.from_user = _Identity(user_id)


class _NullDb:
    def close(self):
        pass


class _Recorder(ts.TelegramService):
    def __init__(self, **kwargs):
        kwargs.setdefault("bot_token", "")
        kwargs.setdefault("stream_edit", False)
        kwargs.setdefault("session_factory", _NullDb)
        super().__init__(**kwargs)
        self.sent = []
        self.actions = []
        self.edited = []

    async def _send(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text, parse_mode))
        return 1

    async def _edit(self, chat_id, message_id, text):
        self.edited.append((chat_id, message_id, text))

    async def _chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _session(monkeypatch):
    monkeypatch.setattr(ts, "resolve_telegram_session", lambda db, uid: (1, 42))
    monkeypatch.setattr(ts, "web_search_preference", lambda db, uid: None)
    yield


# ── intent detection ─────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "search for python 3.12 features",
        "What's the latest news on space exploration?",
        "Any news about the launch",
        "What is the weather today?",
        "current price of gold",
        "find the OpenAI docs",
    ],
)
def test_search_intent_keywords(text):
    assert ts.detect_intent(text) == ts.SEARCH_INTENT


@pytest.mark.parametrize(
    "text",
    [
        "hello there",
        "What is a monad?",
        "explain index funds",
        "/search news",
        "",
    ],
)
def test_chat_intent(text):
    assert ts.detect_intent(text) == ts.CHAT_INTENT


# ── formatting ───────────────────────────────────────────────


def test_format_renders_three_escaped_markdown_links():
    results = [
        {"title": "Title_One", "url": "https://example.com/1", "content": "Snippet one"},
        {"title": "Two", "url": "https://example.com/2", "content": "Snippet two"},
        {"title": "Three", "url": "https://example.com/3", "content": "Snippet three"},
        {"title": "Four", "url": "https://example.com/4", "content": "Snippet four"},
    ]
    text = ts.format_search_results("python 3.12", results)

    assert text.startswith("🔍 *Search Results for: python 3.12*")
    assert "*1.* [Title\\_One](https://example.com/1)" in text
    assert "https://example.com/3" in text
    assert "https://example.com/4" not in text  # top 3 only
    assert "_Snippet one_" in text
    assert "💡 _I can provide more details if needed!_" in text


def test_format_handles_no_results():
    text = ts.format_search_results("obscure query", [])
    assert "No results found" in text


# ── routing ──────────────────────────────────────────────────


def test_search_intent_calls_executor_and_sends_markdown(monkeypatch):
    calls = []

    def fake_search(query, *, limit=3):
        calls.append((query, limit))
        return [
            {"title": "Result One", "url": "https://example.com/1", "content": "one"},
            {"title": "Result Two", "url": "https://example.com/2", "content": "two"},
        ]

    service = _Recorder(search_executor=fake_search)
    _run(service.handle_message(_Message("What is the latest news on space exploration?")))

    assert calls == [("What is the latest news on space exploration?", 3)]
    assert service.actions == [(55, "typing")]
    assert len(service.sent) == 1
    chat_id, text, parse_mode = service.sent[0]
    assert chat_id == 55 and parse_mode == "Markdown"
    assert "🔍" in text and "[Result One](https://example.com/1)" in text


def test_disabled_capability_falls_back_to_chat(monkeypatch):
    responded = []

    async def fake_respond(chat_id, telegram_user_id, text):
        responded.append((chat_id, telegram_user_id, text))

    service = _Recorder(search_executor=lambda *a, **k: pytest.fail("search must not run"))
    service._respond = fake_respond
    monkeypatch.setattr(ts, "web_search_preference", lambda db, uid: False)

    _run(service.handle_message(_Message("latest news on space")))

    assert responded == [(55, 7, "latest news on space")]
    assert service.sent == []


def test_non_search_message_uses_chat_flow():
    responded = []

    async def fake_respond(chat_id, telegram_user_id, text):
        responded.append(text)

    service = _Recorder(search_executor=lambda *a, **k: pytest.fail("search must not run"))
    service._respond = fake_respond

    _run(service.handle_message(_Message("What is a monad?")))

    assert responded == ["What is a monad?"]


def test_rate_limited_error_message():
    def boom(*args, **kwargs):
        raise WebSearchRateLimited("quota")

    service = _Recorder(search_executor=boom)
    _run(service.handle_message(_Message("latest news")))

    assert service.sent[-1][1] == ts.SEARCH_RATE_LIMITED_MESSAGE
    assert service.actions == [(55, "typing")]


def test_not_configured_error_message():
    def boom(*args, **kwargs):
        raise WebSearchNotConfigured("no key")

    service = _Recorder(search_executor=boom)
    _run(service.handle_message(_Message("latest news")))

    assert service.sent[-1][1] == ts.SEARCH_UNAVAILABLE_MESSAGE


def test_unexpected_error_message():
    def boom(*args, **kwargs):
        raise RuntimeError("provider down")

    service = _Recorder(search_executor=boom)
    _run(service.handle_message(_Message("latest news")))

    assert service.sent[-1][1] == ts.SEARCH_FAILED_MESSAGE


def test_help_mentions_natural_language_search():
    service = _Recorder()
    _run(service.handle_message(_Message("/help")))

    text = service.sent[-1][1]
    assert "/help" in text
    assert "search the web" in text
