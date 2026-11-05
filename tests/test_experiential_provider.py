"""Experiential Labs provider: routing, Bearer auth, catalog, optionality.

Network is never touched: every httpx sync client is replaced with a recorder
returning canned responses. The final tests prove adding "experiential" does
not change how existing providers (OpenRouter, Ollama, Yeta, Token Harbor,
Infron, Zro) are built or selected.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.services import llm_providers
from app.services.llm_providers import experiential_provider as ep
from app.services.llm_providers.experiential_provider import (
    DEFAULT_EXPERIENTIAL_MODELS,
    ExperientialProvider,
)

_XPL_KEY = "xpl_" + "0" * 40  # their documented shape: xpl_ + 40 hex chars


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
    monkeypatch.setattr(settings, "experiential_api_key", "")
    yield


@pytest.fixture
def recorder(monkeypatch):
    state = {"response": _Response()}
    calls: list = []

    def client_factory(**kwargs):
        return _FakeClient(calls, state["response"])

    monkeypatch.setattr(ep.httpx, "Client", client_factory)
    return calls, state


def test_generate_uses_base_url_and_bearer_auth(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={"choices": [{"message": {"content": "  exp says hi  "}}]}
    )
    provider = ExperientialProvider(api_key=_XPL_KEY, model="clef-flash")

    assert provider.generate_response([{"role": "user", "content": "hi"}]) == "exp says hi"

    method, url, kwargs = calls[0]
    assert method == "POST"
    assert url == "https://api.experientiallabs.ai/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == f"Bearer {_XPL_KEY}"
    assert kwargs["json"]["model"] == "clef-flash"


def test_generate_json_forces_json_only_system(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={"choices": [{"message": {"content": '{"ok": true}'}}]}
    )
    provider = ExperientialProvider(api_key=_XPL_KEY)

    out = provider.generate_json_response(
        [{"role": "user", "content": "give json"}], system_prompt="be strict"
    )
    assert out == '{"ok": true}'
    _, _, kwargs = calls[0]
    system = kwargs["json"]["messages"][0]
    assert system["role"] == "system" and "valid JSON only" in system["content"]
    assert kwargs["json"]["temperature"] == 0.1


def test_missing_key_fails_with_bounded_message():
    provider = ExperientialProvider()
    with pytest.raises(RuntimeError) as exc:
        provider.generate_response([{"role": "user", "content": "hi"}])
    assert "EXPERIENTIAL_API_KEY" in str(exc.value)


def test_model_catalog_sends_bearer_and_returns_ids(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={"data": [{"id": "clef-flash"}, {"id": "deepseek-v4.1-flash"}]}
    )
    provider = ExperientialProvider(api_key=_XPL_KEY)

    names = provider.fetch_available_chat_models()
    assert names == ["clef-flash", "deepseek-v4.1-flash"]
    method, url, kwargs = calls[0]
    assert method == "GET"
    assert url == "https://api.experientiallabs.ai/v1/models"
    assert kwargs["headers"]["Authorization"] == f"Bearer {_XPL_KEY}"


def test_model_catalog_falls_back_without_a_key(recorder):
    provider = ExperientialProvider()
    assert provider.fetch_available_chat_models() == list(DEFAULT_EXPERIENTIAL_MODELS)
    assert recorder[0] == []  # /models is key-required: no call without a key


def test_model_catalog_falls_back_on_provider_error(recorder):
    calls, state = recorder
    state["response"] = _Response(
        status_code=401, text='{"error":{"code":"invalid_key"}}'
    )
    provider = ExperientialProvider(api_key="xpl_bad")
    assert provider.fetch_available_chat_models() == list(DEFAULT_EXPERIENTIAL_MODELS)


def test_health_check_never_raises(recorder):
    _, state = recorder
    state["response"] = _Response(status_code=200)
    assert ExperientialProvider(api_key=_XPL_KEY).health_check() is True
    assert ExperientialProvider().health_check() is False  # no key -> no probe


def test_factory_builds_the_experiential_provider():
    provider = llm_providers.get_provider(
        config={"provider": "experiential", "api_key": "k", "model": "clef-flash"}
    )
    assert isinstance(provider, ExperientialProvider)
    assert provider.name == "Experiential Labs"
    assert provider.default_model == "clef-flash"


def test_provider_registry_includes_experiential():
    from app.routes import models as models_route
    from app.routes import providers as providers_route

    assert "experiential" in providers_route.KNOWN_PROVIDERS
    assert providers_route.PROVIDER_LABELS["experiential"] == "Experiential Labs"
    assert providers_route.PREFIXES["experiential"] == ""  # live /models judges keys
    assert "experiential" in models_route.VALID_PROVIDERS


def test_existing_providers_unaffected():
    """Strict optionality: adding experiential must not alter other providers."""
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
    # Unknown names still fall back to Ollama, exactly as before.
    assert isinstance(llm_providers.get_provider(config={"provider": "nope"}), OllamaProvider)
