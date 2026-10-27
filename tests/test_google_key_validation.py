"""Google AI API key validation (real live check, no AIza prefix).

Google keys come in several formats (AI Studio "AIza...", Firebase/Cloud
"AQ...."). The old prefix check wrongly rejected valid "AQ." keys, so the
validation now calls the Generative Language models endpoint and maps the
outcome to a bounded message. These tests mock the HTTP layer: 200, 400, 401,
403, 500, timeout and transport error.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes import providers as providers_route
from app.routes import settings as settings_route
from app.services import google_key_validation as gkv
from tests.auth_helpers import auth_headers, register_and_login

CANARY = "AQ.SUPER-SECRET-CANARY-0001"


class _Resp:
    def __init__(self, status_code, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


# ── verify_google_api_key: status mapping ──────────────────────────────


@pytest.mark.parametrize("status,payload,expected", [
    (200, {"models": [{"name": "models/gemini-1.5-flash"}]}, (True, None)),
    (200, {"models": []}, (False, gkv.INVALID_KEY_MESSAGE)),
    (200, {}, (False, gkv.INVALID_KEY_MESSAGE)),
    (400, {"error": {"code": 400}}, (False, gkv.INVALID_KEY_MESSAGE)),
    (401, {}, (False, gkv.INVALID_KEY_MESSAGE)),
    (403, {}, (False, gkv.INVALID_KEY_MESSAGE)),
    (500, {}, (False, gkv.UNVERIFIED_KEY_MESSAGE)),
])
def test_verify_maps_status_and_body(monkeypatch, status, payload, expected):
    monkeypatch.setattr(gkv, "_request_models", lambda key, s=status, p=payload: _Resp(s, p))
    assert gkv.verify_google_api_key(CANARY) == expected


def test_verify_rejects_unparseable_200_body(monkeypatch):
    monkeypatch.setattr(gkv, "_request_models", lambda key: _Resp(200, raise_json=True))
    assert gkv.verify_google_api_key(CANARY) == (False, gkv.INVALID_KEY_MESSAGE)


def test_verify_timeout_is_bounded(monkeypatch):
    def boom(key):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(gkv, "_request_models", boom)
    assert gkv.verify_google_api_key(CANARY) == (False, gkv.UNVERIFIED_KEY_MESSAGE)


def test_verify_transport_error_is_bounded(monkeypatch):
    def boom(key):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(gkv, "_request_models", boom)
    assert gkv.verify_google_api_key(CANARY) == (False, gkv.UNVERIFIED_KEY_MESSAGE)


def test_verify_unexpected_error_fails_closed(monkeypatch):
    def boom(key):
        raise RuntimeError("boom")

    monkeypatch.setattr(gkv, "_request_models", boom)
    assert gkv.verify_google_api_key(CANARY) == (False, gkv.UNVERIFIED_KEY_MESSAGE)


def test_verify_empty_key_passes_without_a_request(monkeypatch):
    def boom(key):
        raise AssertionError("no request expected for an empty key")

    monkeypatch.setattr(gkv, "_request_models", boom)
    assert gkv.verify_google_api_key("") == (True, None)


def test_request_sends_key_as_header_never_in_url(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _Resp(200, {"models": [{"name": "models/gemini-1.5-flash"}]})

    monkeypatch.setattr(gkv.httpx, "get", fake_get)
    assert gkv.verify_google_api_key(CANARY) == (True, None)
    assert captured["url"] == gkv.GOOGLE_MODELS_URL
    assert CANARY not in captured["url"]
    assert "key=" not in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == CANARY
    assert captured["timeout"] == gkv.VERIFY_TIMEOUT_S


def test_key_is_never_logged(monkeypatch, caplog):
    monkeypatch.setattr(gkv, "_request_models", lambda key: _Resp(400, {}))
    with caplog.at_level("DEBUG"):
        gkv.verify_google_api_key(CANARY)

    def boom(key):
        raise httpx.TimeoutException("x")

    monkeypatch.setattr(gkv, "_request_models", boom)
    with caplog.at_level("DEBUG"):
        gkv.verify_google_api_key(CANARY)
    assert CANARY not in caplog.text


# ── route level: /api/providers (the Add Provider dialog) ──────────────


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _auth(test_client):
    _uid, token = register_and_login(
        test_client, username="gkey_" + uuid.uuid4().hex[:8]
    )
    return auth_headers(token)


def test_google_is_no_longer_prefix_checked():
    assert "google" not in providers_route.PREFIXES


def test_aq_prefixed_key_is_accepted(monkeypatch, client):
    monkeypatch.setattr(providers_route, "verify_google_api_key", lambda key: (True, None))
    headers = _auth(client)
    resp = client.post(
        "/api/providers",
        headers=headers,
        json={"provider_name": "google", "api_key": "AQ.valid-looking-key",
              "default_model": "gemini-1.5-flash"},
    )
    assert resp.status_code == 201, resp.text[:300]
    assert resp.json()["provider_name"] == "google"
    assert "AQ.valid-looking-key" not in resp.text


def test_live_validator_receives_the_exact_key(monkeypatch, client):
    seen = {}

    def fake(key):
        seen["key"] = key
        return True, None

    monkeypatch.setattr(providers_route, "verify_google_api_key", fake)
    headers = _auth(client)
    resp = client.post(
        "/api/providers",
        headers=headers,
        json={"provider_name": "google", "api_key": "AQ.observed-key"},
    )
    assert resp.status_code == 201, resp.text[:300]
    assert seen["key"] == "AQ.observed-key"


@pytest.mark.parametrize("message", [gkv.INVALID_KEY_MESSAGE, gkv.UNVERIFIED_KEY_MESSAGE])
def test_google_invalid_key_returns_bounded_400(monkeypatch, client, message):
    monkeypatch.setattr(
        providers_route, "verify_google_api_key", lambda key, m=message: (False, m)
    )
    headers = _auth(client)
    resp = client.post(
        "/api/providers",
        headers=headers,
        json={"provider_name": "google", "api_key": "AQ.rejected-key"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == message
    assert "AQ.rejected-key" not in resp.text


def test_other_providers_still_prefix_checked(client):
    headers = _auth(client)
    resp = client.post(
        "/api/providers",
        headers=headers,
        json={"provider_name": "openrouter", "api_key": "definitely-not-a-key"},
    )
    assert resp.status_code == 400
    assert "sk-or-" in resp.json()["detail"]


def test_google_models_dropdown_is_populated(monkeypatch, client):
    monkeypatch.setattr(providers_route, "verify_google_api_key", lambda key: (True, None))

    class _FakeProvider:
        def fetch_available_chat_models(self):
            return ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash"]

    monkeypatch.setattr(
        providers_route, "get_provider", lambda config=None: _FakeProvider()
    )
    headers = _auth(client)
    add = client.post(
        "/api/providers",
        headers=headers,
        json={"provider_name": "google", "api_key": "AQ.valid-key",
              "default_model": "gemini-1.5-flash"},
    )
    assert add.status_code == 201, add.text[:300]
    resp = client.get("/api/providers/models", headers=headers)
    assert resp.status_code == 200, resp.text[:300]
    groups = [g for g in resp.json() if g["provider"] == "google"]
    assert groups, resp.json()
    names = [m["name"] for m in groups[0]["models"]]
    assert "gemini-1.5-flash" in names


# ── route level: PUT /api/settings (single-provider path) ──────────────


def test_settings_google_key_is_live_validated(monkeypatch, client):
    monkeypatch.setattr(
        settings_route, "verify_google_api_key",
        lambda key: (False, gkv.INVALID_KEY_MESSAGE),
    )
    headers = _auth(client)
    resp = client.put(
        "/api/settings",
        headers=headers,
        json={"llm_provider": "google", "api_key": "AQ.bad", "model": "gemini-1.5-flash"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == gkv.INVALID_KEY_MESSAGE


def test_settings_google_key_accepted_when_live_check_passes(monkeypatch, client):
    monkeypatch.setattr(
        settings_route, "verify_google_api_key", lambda key: (True, None)
    )
    headers = _auth(client)
    resp = client.put(
        "/api/settings",
        headers=headers,
        json={"llm_provider": "google", "api_key": "AQ.good", "model": "gemini-1.5-flash"},
    )
    assert resp.status_code == 200, resp.text[:300]
    assert "AQ.good" not in resp.text


# ── provider save persists the encrypted key; dropdown is never empty ──


def test_google_provider_save_persists_encrypted_key(monkeypatch, client):
    from cryptography.fernet import Fernet

    from app.database import SessionLocal
    from app.models.models import UserProvider
    from app.services import encryption

    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(providers_route, "verify_google_api_key", lambda key: (True, None))
    uid, token = register_and_login(client, username="gkv_" + uuid.uuid4().hex[:8])
    headers = auth_headers(token)
    plaintext = "AQ.plain-provider-secret"
    resp = client.post(
        "/api/providers",
        headers=headers,
        json={"provider_name": "google", "api_key": plaintext,
              "default_model": "gemini-1.5-flash"},
    )
    assert resp.status_code == 201, resp.text[:300]
    assert plaintext not in resp.text

    db = SessionLocal()
    try:
        row = (db.query(UserProvider).filter(UserProvider.user_id == uid)
               .order_by(UserProvider.id.desc()).first())
    finally:
        db.close()
    assert row is not None
    assert row.encrypted_api_key.startswith(encryption.FERNET_PREFIX)
    assert row.api_key == ""
    assert plaintext not in row.encrypted_api_key


def test_google_models_endpoint_not_empty_without_key(monkeypatch):
    """The Add Provider dropdown must never be empty for Google."""
    from app.config import settings

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(settings, "google_api_key", "")
    with TestClient(app) as c:
        resp = c.get("/api/models?provider=google")
    assert resp.status_code == 200, resp.text[:300]
    names = [m["name"] for m in resp.json()["models"]]
    assert names, resp.json()
    assert "gemini-1.5-flash" in names


def test_google_provider_falls_back_to_curated_without_key(monkeypatch):
    from app.config import settings
    from app.services.llm_providers.google_provider import (
        GoogleProvider,
        _POPULAR_GEMINI_MODELS,
    )

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(settings, "google_api_key", "")
    models = GoogleProvider().fetch_available_chat_models()
    assert set(models) == set(_POPULAR_GEMINI_MODELS)
