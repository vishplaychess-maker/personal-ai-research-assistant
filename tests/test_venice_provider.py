"""Venice.ai provider: routing, Bearer auth, model catalog fallback.

Network is never touched: every httpx client is replaced with a recorder that
returns a canned response.
"""

from __future__ import annotations

import json
import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.services import llm_providers
from app.services.llm_providers import venice_provider as vp
from app.services.llm_providers.venice_provider import (
    DEFAULT_VENICE_MODELS,
    VeniceProvider,
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
    monkeypatch.setattr(settings, "venice_api_key", "")
    yield


@pytest.fixture
def recorder(monkeypatch):
    """Patch the sync client; caller sets the response via ``respond``."""

    state = {"response": _Response()}
    calls: list = []

    def client_factory(**kwargs):
        return _FakeClient(calls, state["response"])

    monkeypatch.setattr(vp.httpx, "Client", client_factory)
    return calls, state


def test_generate_uses_base_url_and_bearer_auth(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={"choices": [{"message": {"content": "  hello  "}}]}
    )
    provider = VeniceProvider(api_key="venic-secret", model="llama-3.3-70b")

    assert provider.generate_response([{"role": "user", "content": "hi"}]) == "hello"

    method, url, kwargs = calls[0]
    assert method == "POST"
    assert url == "https://api.venice.ai/api/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer venic-secret"
    assert kwargs["json"]["model"] == "llama-3.3-70b"


def test_missing_key_fails_with_bounded_message():
    provider = VeniceProvider()
    with pytest.raises(RuntimeError) as exc:
        provider.generate_response([{"role": "user", "content": "hi"}])
    assert "VENICE_API_KEY" in str(exc.value)


def test_model_catalog_filters_to_text_and_uses_bearer(recorder):
    calls, state = recorder
    state["response"] = _Response(
        payload={
            "data": [
                {"id": "llama-3.3-70b", "type": "text"},
                {"id": "venice-image", "type": "image"},
                {"id": "mistral-small-3-2-24b-instruct", "type": "text"},
                {"id": "no-type-listed"},
            ]
        }
    )
    provider = VeniceProvider(api_key="venic-secret")

    names = provider.fetch_available_chat_models()

    assert names == [
        "llama-3.3-70b",
        "mistral-small-3-2-24b-instruct",
        "no-type-listed",
    ]
    method, url, kwargs = calls[0]
    assert method == "GET"
    assert url == "https://api.venice.ai/api/v1/models"
    assert kwargs["headers"]["Authorization"] == "Bearer venic-secret"


def test_model_catalog_falls_back_without_a_key(recorder):
    calls, _state = recorder
    provider = VeniceProvider()

    assert provider.fetch_available_chat_models() == list(DEFAULT_VENICE_MODELS)
    assert calls == []


def test_model_catalog_falls_back_on_provider_error(recorder):
    _calls, state = recorder
    state["response"] = _Response(status_code=500, payload={}, text="boom")
    provider = VeniceProvider(api_key="venic-secret")

    assert provider.fetch_available_chat_models() == list(DEFAULT_VENICE_MODELS)


def test_factory_builds_the_venice_provider():
    provider = llm_providers.get_provider(
        config={"provider": "venice", "api_key": "k", "model": "llama-3.3-70b"}
    )
    assert isinstance(provider, VeniceProvider)
    assert provider.name == "Venice.ai"
    assert provider.default_model == "llama-3.3-70b"


def test_provider_registry_includes_venice():
    from app.routes import models as models_route
    from app.routes import providers as providers_route

    assert "venice" in providers_route.KNOWN_PROVIDERS
    assert providers_route.PROVIDER_LABELS["venice"] == "Venice.ai"
    assert "venice" in models_route.VALID_PROVIDERS


# ── HTTP: encrypted save + fallback model listing ───────────


@pytest.fixture(autouse=True)
def _http_env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "venice-hmac-key-0001")
    for flag in (
        "run_messages_stream_durable_enabled",
        "run_engine_enabled",
        "run_worker_enabled",
        "run_chat_stream_enabled",
        "run_public_api_enabled",
        "run_deep_research_enabled",
        "run_multi_agent_enabled",
    ):
        monkeypatch.setattr(settings, flag, False)
    from app.services.rate_limiter import reset_rate_limiter

    reset_rate_limiter()
    yield


def _auth(c):
    from tests.auth_helpers import auth_headers, register_and_login

    _uid, token = register_and_login(c, username="venice_" + uuid.uuid4().hex[:8])
    return auth_headers(token)


def test_saving_a_venice_key_encrypts_and_masks_it():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        headers = _auth(c)
        created = c.post(
            "/api/providers",
            headers=headers,
            json={
                "provider_name": "venice",
                "api_key": "venic-secret-key",
                "default_model": "llama-3.3-70b",
                "is_active": True,
            },
        )
        assert created.status_code == 201, created.text[:300]
        body = created.json()
        assert body["provider_name"] == "venice"
        assert "venic-secret-key" not in created.text
        assert "****" in body["api_key"]

        listed = c.get("/api/providers", headers=headers).json()
        assert any(p["provider_name"] == "venice" for p in listed)


def test_models_route_returns_venice_fallback_without_a_key():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        response = c.get("/api/models?provider=venice")
        assert response.status_code == 200, response.text[:300]
        names = [m["name"] for m in response.json()["models"]]
        assert names == list(DEFAULT_VENICE_MODELS)
