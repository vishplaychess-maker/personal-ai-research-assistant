"""Zro (MoonMath.ai) provider: routing, Bearer auth, catalog, optionality.

Network is never touched: every httpx sync client is replaced with a recorder
returning canned responses. The final tests prove adding "zro" does not change
how OpenRouter, Ollama, Yeta, Token Harbor, or Infron are built or selected.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.services import llm_providers
from app.services.llm_providers import zro_provider as zp
from app.services.llm_providers.zro_provider import (
    DEFAULT_ZRO_MODELS,
    ZroProvider,
)


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
    monkeypatch.setattr(settings, "zro_api_key", "")
    yield


@pytest.fixture
def recorder(monkeypatch):
    state = {"response": _Response()}
    calls: list = []

    def client_factory(**kwargs):
        return _FakeClient(calls, state["response"])

    monkeypatch.setattr(zp.httpx, "Client", client_factory)
    return calls, state


def test_generate_uses_base_url_and_bearer_auth(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={"choices": [{"message": {"content": "  zro says hi  "}}]}
    )
    provider = ZroProvider(api_key="zro-secret", model="deepseek-v4.1-flash")

    assert provider.generate_response([{"role": "user", "content": "hi"}]) == "zro says hi"

    method, url, kwargs = calls[0]
    assert method == "POST"
    assert url == "https://zro.moonmath.ai/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer zro-secret"
    assert kwargs["json"]["model"] == "deepseek-v4.1-flash"


def test_generate_json_forces_json_only_system(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={"choices": [{"message": {"content": '{"ok": true}'}}]}
    )
    provider = ZroProvider(api_key="zro-secret")

    out = provider.generate_json_response(
        [{"role": "user", "content": "give json"}], system_prompt="be strict"
    )
    assert out == '{"ok": true}'
    _, _, kwargs = calls[0]
    system = kwargs["json"]["messages"][0]
    assert system["role"] == "system" and "valid JSON only" in system["content"]
    assert kwargs["json"]["temperature"] == 0.1


def test_missing_key_fails_with_bounded_message():
    provider = ZroProvider()
    with pytest.raises(RuntimeError) as exc:
        provider.generate_response([{"role": "user", "content": "hi"}])
    assert "ZRO_API_KEY" in str(exc.value)


def test_model_catalog_sends_bearer_and_returns_ids(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={"data": [{"id": "glm-5.3"}, {"id": "deepseek-v4.1-flash"}]}
    )
    provider = ZroProvider(api_key="zro-secret")

    names = provider.fetch_available_chat_models()
    assert names == ["deepseek-v4.1-flash", "glm-5.3"]
    method, url, kwargs = calls[0]
    assert method == "GET"
    assert url == "https://zro.moonmath.ai/v1/models"
    assert kwargs["headers"]["Authorization"] == "Bearer zro-secret"


def test_model_catalog_falls_back_without_a_key(recorder):
    provider = ZroProvider()
    assert provider.fetch_available_chat_models() == list(DEFAULT_ZRO_MODELS)
    assert recorder[0] == []  # /models is key-required: no call, no leak attempt


def test_model_catalog_falls_back_on_provider_error(recorder):
    calls, state = recorder
    state["response"] = _Response(status_code=401, text="Authentication Error")
    provider = ZroProvider(api_key="bad-key")
    assert provider.fetch_available_chat_models() == list(DEFAULT_ZRO_MODELS)


def test_health_check_never_raises(recorder):
    _, state = recorder
    state["response"] = _Response(status_code=200)
    assert ZroProvider(api_key="zro-secret").health_check() is True
    assert ZroProvider().health_check() is False  # no key -> no probe


def test_factory_builds_the_zro_provider():
    provider = llm_providers.get_provider(
        config={"provider": "zro", "api_key": "k", "model": "deepseek-v4.1-flash"}
    )
    assert isinstance(provider, ZroProvider)
    assert provider.name == "Zro"
    assert provider.default_model == "deepseek-v4.1-flash"


def test_provider_registry_includes_zro():
    from app.routes import models as models_route
    from app.routes import providers as providers_route

    assert "zro" in providers_route.KNOWN_PROVIDERS
    assert providers_route.PROVIDER_LABELS["zro"] == "Zro (MoonMath)"
    assert providers_route.PREFIXES["zro"] == ""  # never prefix-gated
    assert "zro" in models_route.VALID_PROVIDERS


def test_existing_providers_unaffected():
    """Strict optionality: adding zro must not alter other providers."""
    from app.services.llm_providers.infron_provider import InfronProvider
    from app.services.llm_providers.ollama_provider import OllamaProvider
    from app.services.llm_providers.openrouter_provider import OpenRouterProvider
    from app.services.llm_providers.token_harbor_provider import TokenHarborProvider
    from app.services.llm_providers.yeta_provider import YetaProvider

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
    # Unknown names still fall back to Ollama, exactly as before.
    assert isinstance(llm_providers.get_provider(config={"provider": "nope"}), OllamaProvider)
