"""WusRouter provider: routing, Bearer auth, public catalog, optionality.

Network is never touched: sync httpx clients are replaced with a recorder
returning canned responses, and the streaming test drives a real AsyncClient
over httpx.MockTransport. The final tests prove adding "wusrouter" does not
change how existing providers (OpenRouter, Ollama, Yeta, Token Harbor,
Infron, Zro, Experiential) are built or selected.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.config import settings
from app.services import llm_providers
from app.services.llm_providers import wusrouter_provider as wp
from app.services.llm_providers.wusrouter_provider import (
    DEFAULT_WUSROUTER_MODELS,
    WusRouterProvider,
)

_WR_KEY = "wr-test-key-0123456789"


class _Response:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, recorder, response):
        self._recorder = recorder
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        self._recorder.append(("POST", url, kwargs))
        return self._response

    def get(self, url, **kwargs):
        self._recorder.append(("GET", url, kwargs))
        return self._response


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.setattr(settings, "wusrouter_api_key", "")
    yield


@pytest.fixture
def recorder(monkeypatch):
    state = {"response": _Response()}
    calls: list = []

    def client_factory(**kwargs):
        return _FakeClient(calls, state["response"])

    monkeypatch.setattr(wp.httpx, "Client", client_factory)
    return calls, state


def test_generate_uses_base_url_and_bearer_auth(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={"choices": [{"message": {"content": "  wus says hi  "}}]}
    )
    provider = WusRouterProvider(api_key=_WR_KEY, model="glm-5.3-flash")

    assert (
        provider.generate_response(
            [{"role": "user", "content": "hi"}], system_prompt="be brief"
        )
        == "wus says hi"
    )

    method, url, kwargs = calls[0]
    assert method == "POST"
    assert url == "https://api.wusrouter.com/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == f"Bearer {_WR_KEY}"
    assert kwargs["json"]["model"] == "glm-5.3-flash"
    assert kwargs["json"]["messages"][0] == {"role": "system", "content": "be brief"}
    assert kwargs["json"]["max_tokens"] == settings.wusrouter_max_tokens
    assert kwargs["json"]["temperature"] == settings.wusrouter_temperature


def test_generate_json_forces_json_only_system(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={"choices": [{"message": {"content": '{"ok": true}'}}]}
    )
    provider = WusRouterProvider(api_key=_WR_KEY)

    out = provider.generate_json_response(
        [{"role": "user", "content": "give json"}], system_prompt="be strict"
    )
    assert out == '{"ok": true}'
    _, _, kwargs = calls[0]
    system = kwargs["json"]["messages"][0]
    assert system["role"] == "system" and "valid JSON only" in system["content"]
    assert kwargs["json"]["temperature"] == 0.1


def test_missing_key_fails_with_bounded_message():
    provider = WusRouterProvider()
    with pytest.raises(RuntimeError) as exc:
        provider.generate_response([{"role": "user", "content": "hi"}])
    assert "WUSROUTER_API_KEY" in str(exc.value)


def test_model_catalog_works_without_a_key_and_orders_free_first(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={
            "data": [
                {"id": "glm-5.3-flash"},
                {"id": "wr/gpt-6-luna:free"},
                {"id": "wusrouter/qwen3.8-flash:free"},
            ]
        }
    )
    provider = WusRouterProvider()  # /models is public — no key needed

    names = provider.fetch_available_chat_models()
    assert names[:2] == ["wr/gpt-6-luna:free", "wusrouter/qwen3.8-flash:free"]
    assert "glm-5.3-flash" in names
    method, url, kwargs = calls[0]
    assert method == "GET"
    assert url == "https://api.wusrouter.com/v1/models"
    assert "Authorization" not in kwargs["headers"]


def test_model_catalog_sends_bearer_when_key_present(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={"data": [{"id": "glm-5.3-flash"}, {"id": "kimi-k3"}]}
    )
    provider = WusRouterProvider(api_key=_WR_KEY)

    names = provider.fetch_available_chat_models()
    assert names == ["glm-5.3-flash", "kimi-k3"]
    _, _, kwargs = calls[0]
    assert kwargs["headers"]["Authorization"] == f"Bearer {_WR_KEY}"


def test_model_catalog_falls_back_on_cloudflare_403(recorder):
    calls, state = recorder
    state["response"] = _Response(
        status_code=403, text="<html>Cloudflare challenge</html>"
    )
    provider = WusRouterProvider(api_key=_WR_KEY)
    assert provider.fetch_available_chat_models() == list(DEFAULT_WUSROUTER_MODELS)
    assert len(calls) == 1  # one probe, no retry loop


def test_model_catalog_falls_back_on_empty_catalog(recorder):
    _, state = recorder
    state["response"] = _Response(payload={"data": []})
    provider = WusRouterProvider(api_key=_WR_KEY)
    assert provider.fetch_available_chat_models() == list(DEFAULT_WUSROUTER_MODELS)


def test_health_check_never_raises(recorder):
    _, state = recorder
    state["response"] = _Response(status_code=200)
    assert WusRouterProvider(api_key=_WR_KEY).health_check() is True
    assert WusRouterProvider().health_check() is False  # no key -> no probe
    state["response"] = _Response(status_code=403)
    assert WusRouterProvider(api_key=_WR_KEY).health_check() is False


def test_stream_shapes_request_and_parses_sse(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content.decode())
        events = [
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": " world"}}]},
        ]
        sse = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(200, content=(sse + "data: [DONE]\n\n").encode())

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        wp.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport),
    )
    provider = WusRouterProvider(api_key=_WR_KEY, model="kimi-k3")

    async def drive():
        return [
            event
            async for event in provider.generate_stream_async(
                [{"role": "user", "content": "hi"}]
            )
        ]

    events = asyncio.run(drive())
    assert seen["url"] == "https://api.wusrouter.com/v1/chat/completions"
    assert seen["auth"] == f"Bearer {_WR_KEY}"
    assert seen["body"]["stream"] is True
    assert seen["body"]["model"] == "kimi-k3"
    assert events == [
        {"type": "token", "token": "Hello"},
        {"type": "token", "token": " world"},
        {"type": "done", "response": "Hello world"},
    ]


def test_factory_builds_the_wusrouter_provider():
    provider = llm_providers.get_provider(
        config={"provider": "wusrouter", "api_key": "k", "model": "glm-5.3-flash"}
    )
    assert isinstance(provider, WusRouterProvider)
    assert provider.name == "WusRouter"
    assert provider.default_model == "glm-5.3-flash"


def test_provider_registry_includes_wusrouter():
    from app.routes import models as models_route
    from app.routes import providers as providers_route

    assert "wusrouter" in providers_route.KNOWN_PROVIDERS
    assert providers_route.PROVIDER_LABELS["wusrouter"] == "WusRouter"
    assert providers_route.PREFIXES["wusrouter"] == ""  # live /models judges keys
    assert "wusrouter" in models_route.VALID_PROVIDERS


def test_existing_providers_unaffected():
    """Strict optionality: adding wusrouter must not alter other providers."""
    from app.services.llm_providers.experiential_provider import ExperientialProvider
    from app.services.llm_providers.infron_provider import InfronProvider
    from app.services.llm_providers.ollama_provider import OllamaProvider
    from app.services.llm_providers.openrouter_provider import OpenRouterProvider
    from app.services.llm_providers.token_harbor_provider import TokenHarborProvider
    from app.services.llm_providers.yeta_provider import YetaProvider
    from app.services.llm_providers.zro_provider import ZroProvider

    assert isinstance(
        llm_providers.get_provider(config={"provider": "openrouter", "api_key": "x"}),
        OpenRouterProvider,
    )
    assert isinstance(
        llm_providers.get_provider(config={"provider": "ollama"}), OllamaProvider
    )
    assert isinstance(
        llm_providers.get_provider(config={"provider": "yeta", "api_key": "y"}),
        YetaProvider,
    )
    assert isinstance(
        llm_providers.get_provider(config={"provider": "token_harbor", "api_key": "t"}),
        TokenHarborProvider,
    )
    assert isinstance(
        llm_providers.get_provider(config={"provider": "infron", "api_key": "i"}),
        InfronProvider,
    )
    assert isinstance(
        llm_providers.get_provider(config={"provider": "zro", "api_key": "z"}),
        ZroProvider,
    )
    assert isinstance(
        llm_providers.get_provider(config={"provider": "experiential", "api_key": "e"}),
        ExperientialProvider,
    )
    # Unknown names still fall back to Ollama, exactly as before.
    assert isinstance(llm_providers.get_provider(config={"provider": "nope"}), OllamaProvider)