"""Token Harbor provider: routing, Bearer auth, catalog fallback, optionality.

Network is never touched: every httpx sync client is replaced with a recorder
returning canned responses. The final tests prove adding "token_harbor" does
not change how OpenRouter, Ollama, or Yeta are built or selected.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.services import llm_providers
from app.services.llm_providers import token_harbor_provider as thp
from app.services.llm_providers.token_harbor_provider import (
    DEFAULT_TOKEN_HARBOR_MODELS,
    TokenHarborProvider,
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
    monkeypatch.setattr(settings, "token_harbor_api_key", "")
    yield


@pytest.fixture
def recorder(monkeypatch):
    state = {"response": _Response()}
    calls: list = []

    def client_factory(**kwargs):
        return _FakeClient(calls, state["response"])

    monkeypatch.setattr(thp.httpx, "Client", client_factory)
    return calls, state


def test_generate_uses_base_url_and_bearer_auth(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={"choices": [{"message": {"content": "  harbor says hi  "}}]}
    )
    provider = TokenHarborProvider(api_key="thk_live_secret", model="deepseek-v4.1-flash:free")

    assert provider.generate_response([{"role": "user", "content": "hi"}]) == "harbor says hi"

    method, url, kwargs = calls[0]
    assert method == "POST"
    assert url == "https://tokenharbor.ai/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer thk_live_secret"
    assert kwargs["json"]["model"] == "deepseek-v4.1-flash:free"


def test_generate_json_forces_json_only_system(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={"choices": [{"message": {"content": '{"ok": true}'}}]}
    )
    provider = TokenHarborProvider(api_key="thk_live_secret")

    out = provider.generate_json_response(
        [{"role": "user", "content": "give json"}], system_prompt="be strict"
    )
    assert out == '{"ok": true}'
    _, _, kwargs = calls[0]
    system = kwargs["json"]["messages"][0]
    assert system["role"] == "system" and "valid JSON only" in system["content"]
    assert kwargs["json"]["temperature"] == 0.1


def test_missing_key_fails_with_bounded_message():
    provider = TokenHarborProvider()
    with pytest.raises(RuntimeError) as exc:
        provider.generate_response([{"role": "user", "content": "hi"}])
    assert "TOKEN_HARBOR_API_KEY" in str(exc.value)


def test_model_catalog_uses_bearer_and_puts_free_first(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={
            "data": [
                {"id": "tokenharbor/deepseek-v4.1-flash"},
                {"id": "openai/gpt-mini"},
                {"id": "deepseek-v4.1-flash:free"},
            ]
        }
    )
    provider = TokenHarborProvider(api_key="thk_live_secret")

    names = provider.fetch_available_chat_models()
    assert names[0] == "deepseek-v4.1-flash:free"
    method, url, kwargs = calls[0]
    assert method == "GET"
    assert url == "https://tokenharbor.ai/v1/models"
    assert kwargs["headers"]["Authorization"] == "Bearer thk_live_secret"


def test_model_catalog_falls_back_without_a_key(recorder):
    provider = TokenHarborProvider()
    assert provider.fetch_available_chat_models() == list(DEFAULT_TOKEN_HARBOR_MODELS)
    assert recorder[0] == []  # no network call happened


def test_model_catalog_falls_back_on_provider_error(recorder):
    calls, state = recorder
    state["response"] = _Response(status_code=401, text="No valid API key found")
    provider = TokenHarborProvider(api_key="bad-key")
    assert provider.fetch_available_chat_models() == list(DEFAULT_TOKEN_HARBOR_MODELS)


def test_health_check_never_raises(recorder):
    _, state = recorder
    state["response"] = _Response(status_code=200)
    assert TokenHarborProvider(api_key="thk_live_secret").health_check() is True
    assert TokenHarborProvider().health_check() is False  # no key -> no probe


def test_factory_builds_the_token_harbor_provider():
    provider = llm_providers.get_provider(
        config={
            "provider": "token_harbor",
            "api_key": "k",
            "model": "deepseek-v4.1-flash:free",
        }
    )
    assert isinstance(provider, TokenHarborProvider)
    assert provider.name == "Token Harbor"
    assert provider.default_model == "deepseek-v4.1-flash:free"


def test_provider_registry_includes_token_harbor():
    from app.routes import models as models_route
    from app.routes import providers as providers_route

    assert "token_harbor" in providers_route.KNOWN_PROVIDERS
    assert providers_route.PROVIDER_LABELS["token_harbor"] == "Token Harbor"
    assert providers_route.PREFIXES["token_harbor"] == ""  # never prefix-gated
    assert "token_harbor" in models_route.VALID_PROVIDERS


def test_existing_providers_unaffected():
    """Strict optionality: adding token_harbor must not alter other providers."""
    from app.services.llm_providers.ollama_provider import OllamaProvider
    from app.services.llm_providers.openrouter_provider import OpenRouterProvider
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
    # Unknown names still fall back to Ollama, exactly as before.
    assert isinstance(llm_providers.get_provider(config={"provider": "nope"}), OllamaProvider)
