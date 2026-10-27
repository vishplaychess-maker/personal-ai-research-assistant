"""Grok provider: two transports, and no credential fallback between them.

The subscription path (Grok CLI chat proxy, OpenAI Responses shape) and the
API-key path (api.x.ai, chat-completions) are different protocols, so each is
pinned by its own test: URL, headers, body keys, streaming events, and the
status codes that must be answered with fixed copy instead of an upstream body.
"""

import asyncio
import json

import httpx
import pytest

from app.config import settings
from app.services.llm_providers.grok_provider import (
    CLIENT_IDENTITY_HEADERS,
    GrokProvider,
    _catalog_ids,
)
from app.services.oauth_token_store import TokenRefreshError

PROXY = "https://cli-chat-proxy.grok.com/v1"
API = "https://api.x.ai/v1"

SUB_TOKEN = "sub-TOKEN-access-material"
API_KEY = "xai-SECRET-api-key"


class _StubStore:
    """Stands in for OAuthTokenStore: hands out a bearer, records refreshes."""

    def __init__(self, token=SUB_TOKEN, *, fail=False):
        self.token = token
        self.fail = fail
        self.sync_calls = 0
        self.async_calls = 0
        self.forced = 0

    def get_valid_token(self, user_id, provider="grok", *, service=None, skew_s=None):
        self.sync_calls += 1
        if self.fail:
            raise TokenRefreshError("Your Grok sign-in is no longer valid.")
        return self.token

    async def get_valid_token_async(self, user_id, provider="grok", *, service=None, skew_s=None):
        self.async_calls += 1
        if self.fail:
            raise TokenRefreshError("Your Grok sign-in is no longer valid.")
        return self.token

    def refresh_now(self, user_id, provider="grok", *, service=None):
        self.forced += 1
        return None

    async def refresh_now_async(self, user_id, provider="grok", *, service=None):
        self.forced += 1
        return None


class _MockedGrok(GrokProvider):
    """GrokProvider with the transport seams swapped for a mock.

    ``_client`` / ``_async_client`` are the provider's only outbound edges, so
    overriding them covers both auth modes without patching httpx globally.
    """

    def __init__(self, handler, **kwargs):
        super().__init__(**kwargs)
        self._mock = httpx.MockTransport(handler)

    def _client(self, *, timeout: float = 0.0) -> httpx.Client:
        return httpx.Client(transport=self._mock)

    def _async_client(self, *, timeout: float = 0.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._mock)


def _oauth_provider(handler, *, store=None) -> _MockedGrok:
    return _MockedGrok(handler, user_id=7, store=store or _StubStore())


def _keyed_provider(handler, **kwargs) -> _MockedGrok:
    return _MockedGrok(handler, api_key=API_KEY, model="grok-4", **kwargs)


def _sse(*events: dict) -> bytes:
    lines = []
    for event in events:
        lines.append(f"data: {json.dumps(event)}\n\n")
    return "".join(lines).encode()


def _collect_stream(stream_coro):
    async def drive():
        return [event async for event in stream_coro]

    return asyncio.run(drive())


# ── subscription transport ──────────────────────────────────


def test_oauth_generation_targets_the_cli_proxy_responses_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "output": [
                    {"content": [{"type": "output_text", "text": "Hello from Grok"}]}
                ]
            },
        )

    provider = _oauth_provider(handler)

    text = provider.generate_response(
        [{"role": "user", "content": "hi"}], system_prompt="Be brief."
    )

    assert text == "Hello from Grok"
    assert seen["url"] == f"{PROXY}/responses"
    assert seen["headers"]["authorization"] == f"Bearer {SUB_TOKEN}"
    for name, value in CLIENT_IDENTITY_HEADERS.items():
        assert seen["headers"][name.lower()] == value
    assert seen["body"]["instructions"] == "Be brief."
    assert seen["body"]["input"][0]["content"][0]["type"] == "input_text"


def test_oauth_body_uses_response_keys_and_never_the_rejected_ones():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"output": []})

    _oauth_provider(handler).generate_response([{"role": "user", "content": "hi"}])

    body = seen["body"]
    assert body["max_output_tokens"] == settings.grok_max_tokens
    assert "max_tokens" not in body
    assert "messages" not in body
    # xAI 400s on service_tier, and reasoning is refused by several models.
    assert "service_tier" not in body
    assert "reasoning" not in body


def test_unverified_image_input_is_dropped_loudly_not_silently(caplog):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"output": []})

    with caplog.at_level("WARNING"):
        _oauth_provider(handler).generate_response(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
                    ],
                }
            ]
        )

    parts = seen["body"]["input"][0]["content"]
    assert parts == [{"type": "input_text", "text": "what is this?"}]
    assert "dropped 1 non-text part" in caplog.text


def test_api_key_generation_targets_the_developer_api_chat_completions():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "keyed answer"}}]}
        )

    text = _keyed_provider(handler).generate_response(
        [{"role": "user", "content": "hi"}]
    )

    assert text == "keyed answer"
    assert seen["url"] == f"{API}/chat/completions"
    assert seen["headers"]["authorization"] == f"Bearer {API_KEY}"
    assert seen["body"]["messages"][0]["content"] == "hi"
    assert seen["body"]["model"] == "grok-4"


def test_oauth_mode_never_reads_the_api_key_even_when_one_is_configured():
    """Fail closed: a subscription sign-in must not silently spend API credits."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"output": []})

    provider = _oauth_provider(handler)
    provider._api_key = API_KEY

    provider.generate_response([{"role": "user", "content": "hi"}])

    assert seen["auth"] == f"Bearer {SUB_TOKEN}"
    assert API_KEY not in seen["auth"]


def test_a_dead_grant_raises_instead_of_falling_back(monkeypatch):
    monkeypatch.setattr(settings, "grok_api_key", API_KEY)
    provider = _oauth_provider(
        lambda request: httpx.Response(200, json={"output": []}),
        store=_StubStore(fail=True),
    )

    with pytest.raises(RuntimeError) as caught:
        provider.generate_response([{"role": "user", "content": "hi"}])

    assert "sign-in" in str(caught.value).lower()


def test_no_credential_at_all_is_a_bounded_error(monkeypatch):
    monkeypatch.setattr(settings, "grok_api_key", "")
    provider = GrokProvider()

    with pytest.raises(RuntimeError) as caught:
        provider.generate_response([{"role": "user", "content": "hi"}])

    assert "Sign in with Grok" in str(caught.value)


# ── auth and billing statuses ───────────────────────────────


def test_a_401_refreshes_once_and_retries(monkeypatch):
    calls = []
    store = _StubStore()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(401, json={"error": {"message": "expired token"}})
        return httpx.Response(200, json={"output": [{"content": [{"text": "after refresh"}]}]})

    provider = _oauth_provider(handler, store=store)

    assert provider.generate_response([{"role": "user", "content": "hi"}]) == "after refresh"
    assert len(calls) == 2
    assert store.forced == 1


def test_a_second_401_stops_instead_of_looping():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, json={"error": {"message": "nope"}})

    provider = _oauth_provider(handler)

    with pytest.raises(RuntimeError) as caught:
        provider.generate_response([{"role": "user", "content": "hi"}])

    assert len(calls) == 2
    assert "HTTP 401" in str(caught.value)
    # The upstream message is replaced by fixed copy for auth statuses.
    assert "nope" not in str(caught.value)


def test_402_spending_limit_becomes_subscription_copy_not_the_upstream_body():
    provider = _oauth_provider(
        lambda request: httpx.Response(
            402, json={"error": {"message": "personal-team-blocked:spending-limit"}}
        )
    )

    with pytest.raises(RuntimeError) as caught:
        provider.generate_response([{"role": "user", "content": "hi"}])

    assert "subscription" in str(caught.value).lower()
    assert "spending-limit" not in str(caught.value)


def test_429_is_reported_as_a_rate_limit():
    provider = _oauth_provider(lambda request: httpx.Response(429, json={}))

    with pytest.raises(RuntimeError) as caught:
        provider.generate_response([{"role": "user", "content": "hi"}])

    assert "rate limit" in str(caught.value).lower()


def test_server_errors_keep_a_bounded_detail_snippet():
    provider = _oauth_provider(
        lambda request: httpx.Response(500, text="upstream exploded")
    )

    with pytest.raises(RuntimeError) as caught:
        provider.generate_response([{"role": "user", "content": "hi"}])

    assert "HTTP 500" in str(caught.value)
    assert "upstream exploded" in str(caught.value)


# ── streaming ───────────────────────────────────────────────


def test_oauth_stream_maps_response_deltas_to_token_events():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("accept") == "text/event-stream"
        return httpx.Response(
            200,
            content=_sse(
                {"type": "response.output_text.delta", "delta": "Mol"},
                {"type": "response.output_text.delta", "delta": "ten"},
                {"type": "response.completed",
                 "response": {"output": [{"content": [{"text": "Molten"}]}]}},
            ),
        )

    events = _collect_stream(
        _oauth_provider(handler).generate_stream_async([{"role": "user", "content": "hi"}])
    )

    assert [e["type"] for e in events] == ["token", "token", "done"]
    assert events[0]["token"] == "Mol"
    assert events[-1]["response"] == "Molten"


def test_oauth_stream_completes_from_deltas_when_no_completed_event_arrives():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse({"type": "response.output_text.delta", "delta": "partial"}),
        )

    events = _collect_stream(
        _oauth_provider(handler).generate_stream_async([{"role": "user", "content": "hi"}])
    )

    assert events[-1] == {"type": "done", "response": "partial"}


def test_oauth_stream_reports_an_early_stop_as_an_error_event():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_sse({"type": "response.failed", "response": {"status": "failed"}})
        )

    events = _collect_stream(
        _oauth_provider(handler).generate_stream_async([{"role": "user", "content": "hi"}])
    )

    assert events[0]["type"] == "error"


def test_oauth_stream_refused_by_the_tier_surfaces_copy_not_a_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="subscription required: SUPERGROK_HEAVY")

    events = _collect_stream(
        _oauth_provider(handler).generate_stream_async([{"role": "user", "content": "hi"}])
    )

    assert events[0]["type"] == "error"
    assert "subscription" in events[0]["error"].lower()
    assert "SUPERGROK_HEAVY" not in events[0]["error"]


def test_oauth_stream_refreshes_once_on_a_401():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(
            200,
            content=_sse(
                {"type": "response.output_text.delta", "delta": "second try"},
                {"type": "response.completed", "response": {"output": []}},
            ),
        )

    store = _StubStore()
    events = _collect_stream(
        _oauth_provider(handler, store=store).generate_stream_async(
            [{"role": "user", "content": "hi"}]
        )
    )

    assert len(calls) == 2
    assert store.async_calls == 2
    assert events[-1] == {"type": "done", "response": "second try"}


def test_api_key_stream_parses_chat_completion_deltas():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"content": "chunk"}}]},
                {"choices": [{"delta": {"content": " two"}}]},
            ) + b"data: [DONE]\n\n",
        )

    events = _collect_stream(
        _keyed_provider(handler).generate_stream_async([{"role": "user", "content": "hi"}])
    )

    assert events[-1] == {"type": "done", "response": "chunk two"}


def test_stream_without_any_credential_is_an_error_not_an_exception(monkeypatch):
    monkeypatch.setattr(settings, "grok_api_key", "")
    provider = GrokProvider()

    events = _collect_stream(
        provider.generate_stream_async([{"role": "user", "content": "hi"}])
    )

    assert events[0]["type"] == "error"


# ── model discovery and health ──────────────────────────────


def test_oauth_catalog_comes_from_models_v2_with_cli_identity():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={"data": [{"id": "grok-4.6"}, {"id": "grok-4"}, {"id": "grok-imagine-video"}]},
        )

    models = _oauth_provider(handler).fetch_available_chat_models()

    assert seen["url"] == f"{PROXY}/models-v2"
    assert seen["headers"]["x-grok-client-identifier"] == "grok-shell"
    assert models == ["grok-4", "grok-4.6"]


def test_catalog_filters_api_key_only_and_non_chat_models():
    payload = {
        "models": [
            {"id": "grok-4"},
            {"id": "grok-build-0.1"},
            {"id": "grok-4-fast-embeddings"},
            {"id": "grok-imagine-image"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    assert _oauth_provider(handler).fetch_available_chat_models() == ["grok-4"]


def test_catalog_fetch_shapes_are_all_understood():
    assert _catalog_ids(["grok-4", "grok-4.6"]) == ["grok-4", "grok-4.6"]
    assert _catalog_ids({"items": [{"name": "grok-4"}]}) == ["grok-4"]
    assert _catalog_ids({"unexpected": True}) == []
    assert _catalog_ids(None) == []


def test_no_sign_in_means_no_catalog_claim(monkeypatch):
    """Fail closed: callers treat None as "cannot verify availability"."""
    monkeypatch.setattr(settings, "grok_api_key", "")

    assert GrokProvider().fetch_available_chat_models() is None
    assert GrokProvider().health_check() is False


def test_oauth_health_check_is_a_catalog_probe():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "grok-4"}]})

    assert _oauth_provider(handler).health_check() is True


def test_a_broken_catalog_probe_is_false_not_an_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    assert _oauth_provider(handler).health_check() is False
    assert _oauth_provider(handler).fetch_available_chat_models() == []
