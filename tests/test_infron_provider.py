"""Infron AI provider: routing, Bearer auth, catalog behavior, optionality.

Network is never touched: every httpx sync client is replaced with a recorder
returning canned responses. The final tests prove adding "infron" does not
change how OpenRouter, Ollama, Yeta, or Token Harbor are built or selected.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.services import llm_providers
from app.services.llm_providers import infron_provider as ip
from app.services.llm_providers.infron_provider import (
    DEFAULT_INFRON_MODELS,
    InfronProvider,
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
    monkeypatch.setattr(settings, "infron_api_key", "")
    yield


@pytest.fixture
def recorder(monkeypatch):
    state = {"response": _Response()}
    calls: list = []

    def client_factory(**kwargs):
        return _FakeClient(calls, state["response"])

    monkeypatch.setattr(ip.httpx, "Client", client_factory)
    return calls, state


def test_generate_uses_base_url_and_bearer_auth(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={"choices": [{"message": {"content": "  infron says hi  "}}]}
    )
    provider = InfronProvider(api_key="infron-secret", model="qwen/qwen3.8-27b:free")

    assert provider.generate_response([{"role": "user", "content": "hi"}]) == "infron says hi"

    method, url, kwargs = calls[0]
    assert method == "POST"
    assert url == "https://api.infron.ai/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer infron-secret"
    assert kwargs["json"]["model"] == "qwen/qwen3.8-27b:free"


def test_generate_json_forces_json_only_system(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={"choices": [{"message": {"content": '{"ok": true}'}}]}
    )
    provider = InfronProvider(api_key="infron-secret")

    out = provider.generate_json_response(
        [{"role": "user", "content": "give json"}], system_prompt="be strict"
    )
    assert out == '{"ok": true}'
    _, _, kwargs = calls[0]
    system = kwargs["json"]["messages"][0]
    assert system["role"] == "system" and "valid JSON only" in system["content"]
    assert kwargs["json"]["temperature"] == 0.1


def test_missing_key_fails_with_bounded_message():
    provider = InfronProvider()
    with pytest.raises(RuntimeError) as exc:
        provider.generate_response([{"role": "user", "content": "hi"}])
    assert "INFRON_API_KEY" in str(exc.value)


def test_model_catalog_works_without_a_key_and_orders_free_first(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={
            "data": [
                {"id": "openai/gpt-6-luna"},
                {"id": "apodex/apodex-1.1-mini:free"},
                {"id": "qwen/qwen3.8-27b:free"},
            ]
        }
    )
    provider = InfronProvider()  # /models is public — no key needed

    names = provider.fetch_available_chat_models()
    assert names[:2] == ["apodex/apodex-1.1-mini:free", "qwen/qwen3.8-27b:free"]
    assert "openai/gpt-6-luna" in names
    method, url, kwargs = calls[0]
    assert method == "GET"
    assert url == "https://api.infron.ai/v1/models"
    assert "Authorization" not in kwargs["headers"]


def test_model_catalog_sends_bearer_when_key_present(recorder):
    calls, state = recorder
    state["response"] = _Response(payload={"data": [{"id": "qwen/qwen3.8-27b:free"}]})
    provider = InfronProvider(api_key="infron-secret")

    provider.fetch_available_chat_models()
    _, _, kwargs = calls[0]
    assert kwargs["headers"]["Authorization"] == "Bearer infron-secret"


def test_model_catalog_falls_back_on_provider_error(recorder):
    calls, state = recorder
    state["response"] = _Response(status_code=503, text="upstream down")
    provider = InfronProvider(api_key="infron-secret")
    assert provider.fetch_available_chat_models() == list(DEFAULT_INFRON_MODELS)


def test_health_check_never_raises(recorder):
    _, state = recorder
    state["response"] = _Response(status_code=200)
    assert InfronProvider(api_key="infron-secret").health_check() is True
    assert InfronProvider().health_check() is False  # no key -> no probe


def test_factory_builds_the_infron_provider():
    provider = llm_providers.get_provider(
        config={"provider": "infron", "api_key": "k", "model": "qwen/qwen3.8-27b:free"}
    )
    assert isinstance(provider, InfronProvider)
    assert provider.name == "Infron AI"
    assert provider.default_model == "qwen/qwen3.8-27b:free"


def test_provider_registry_includes_infron():
    from app.routes import models as models_route
    from app.routes import providers as providers_route

    assert "infron" in providers_route.KNOWN_PROVIDERS
    assert providers_route.PROVIDER_LABELS["infron"] == "Infron AI"
    assert providers_route.PREFIXES["infron"] == ""  # never prefix-gated
    assert "infron" in models_route.VALID_PROVIDERS


def test_existing_providers_unaffected():
    """Strict optionality: adding infron must not alter other providers."""
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
        llm_providers.get_provider(
            config={"provider": "token_harbor", "api_key": "t"}
        ),
        TokenHarborProvider,
    )
    # Unknown names still fall back to Ollama, exactly as before.
    assert isinstance(llm_providers.get_provider(config={"provider": "nope"}), OllamaProvider)
