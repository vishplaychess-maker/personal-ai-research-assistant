"""Yeta AI provider: routing, Bearer auth, catalog fallback, strict optionality.

Network is never touched: every httpx sync client is replaced with a recorder
returning canned responses. The last tests prove adding "yeta" does not
change how OpenRouter or Ollama are built or selected.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.services import llm_providers
from app.services.llm_providers import yeta_provider as yp
from app.services.llm_providers.yeta_provider import (
    DEFAULT_YETA_MODELS,
    YetaProvider,
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
    monkeypatch.setattr(settings, "yeta_api_key", "")
    yield


@pytest.fixture
def recorder(monkeypatch):
    state = {"response": _Response()}
    calls: list = []

    def client_factory(**kwargs):
        return _FakeClient(calls, state["response"])

    monkeypatch.setattr(yp.httpx, "Client", client_factory)
    return calls, state


def test_generate_uses_base_url_and_bearer_auth(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={"choices": [{"message": {"content": "  yeta says hi  "}}]}
    )
    provider = YetaProvider(api_key="yeta-secret", model="glm-5.3-flash")

    assert provider.generate_response([{"role": "user", "content": "hi"}]) == "yeta says hi"

    method, url, kwargs = calls[0]
    assert method == "POST"
    assert url == "https://platform.yeta.ai/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer yeta-secret"
    assert kwargs["json"]["model"] == "glm-5.3-flash"


def test_missing_key_fails_with_bounded_message():
    provider = YetaProvider()
    with pytest.raises(RuntimeError) as exc:
        provider.generate_response([{"role": "user", "content": "hi"}])
    assert "YETA_API_KEY" in str(exc.value)


def test_model_catalog_uses_bearer_and_returns_ids(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={"data": [{"id": "deepseek-v4.1-flash"}, {"id": "glm-5.3-flash"}]}
    )
    provider = YetaProvider(api_key="yeta-secret")

    names = provider.fetch_available_chat_models()
    assert names == ["deepseek-v4.1-flash", "glm-5.3-flash"]
    method, url, kwargs = calls[0]
    assert method == "GET"
    assert url == "https://platform.yeta.ai/v1/models"
    assert kwargs["headers"]["Authorization"] == "Bearer yeta-secret"


def test_model_catalog_falls_back_without_a_key(recorder):
    provider = YetaProvider()
    assert provider.fetch_available_chat_models() == list(DEFAULT_YETA_MODELS)
    assert recorder[0] == []  # no network call happened


def test_model_catalog_falls_back_on_provider_error(recorder):
    calls, state = recorder
    state["response"] = _Response(status_code=401, text="Invalid token")
    provider = YetaProvider(api_key="bad-key")
    assert provider.fetch_available_chat_models() == list(DEFAULT_YETA_MODELS)


def test_health_check_never_raises(recorder):
    _, state = recorder
    state["response"] = _Response(status_code=200)
    assert YetaProvider(api_key="yeta-secret").health_check() is True
    assert YetaProvider().health_check() is False  # no key -> no probe


def test_factory_builds_the_yeta_provider():
    provider = llm_providers.get_provider(
        config={"provider": "yeta", "api_key": "k", "model": "glm-5.3-flash"}
    )
    assert isinstance(provider, YetaProvider)
    assert provider.name == "Yeta AI"
    assert provider.default_model == "glm-5.3-flash"


def test_provider_registry_includes_yeta():
    from app.routes import models as models_route
    from app.routes import providers as providers_route

    assert "yeta" in providers_route.KNOWN_PROVIDERS
    assert providers_route.PROVIDER_LABELS["yeta"] == "Yeta AI"
    assert providers_route.PREFIXES["yeta"] == ""  # never prefix-gated
    assert "yeta" in models_route.VALID_PROVIDERS


def test_existing_providers_unaffected():
    """Strict optionality: adding yeta must not alter other providers."""
    from app.services.llm_providers.ollama_provider import OllamaProvider
    from app.services.llm_providers.openrouter_provider import OpenRouterProvider

    assert isinstance(
        llm_providers.get_provider(config={"provider": "openrouter", "api_key": "x"}),
        OpenRouterProvider,
    )
    assert isinstance(
        llm_providers.get_provider(config={"provider": "ollama"}), OllamaProvider
    )
    # Unknown names still fall back to Ollama, exactly as before.
    assert isinstance(llm_providers.get_provider(config={"provider": "nope"}), OllamaProvider)
