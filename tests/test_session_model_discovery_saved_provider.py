"""PATCH /api/sessions/{id}/model discovery must use the SAVED user credentials.

Regression under test: a user who saved an OpenRouter key through the Settings
UI (PUT /api/settings, which writes the legacy user_settings row) could not
select a model at all - the route answered 503 "no provider model discovery is
available" - unless the same key was ALSO placed in a global env file.
Discovery read only user_providers rows and then the global provider, so the
saved key was invisible to it.

The fix routes saved credentials through the SAME secure resolver the run path
uses (get_user_llm_config: active user_providers row -> legacy user_settings
row, scoped to the authenticated user, decrypted, with the intentional env-key
fallback), and keeps that global fallback working.

Two guarantees are asserted, not assumed: the decrypted key must reach the
provider factory, and the raw key must never appear in a response or a log.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.routes import sessions as sessions_route
from app.services import settings_service
from tests.auth_helpers import auth_headers, register_and_login
from tests.run_engine_helpers import ensure_user, mk_engine, mk_session

CANARY = "sk-or-v1-CANARYKEYMATERIAL0123456789abcdef"
MODEL = "nvidia/nemotron-3-super-120b-a12b:free"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """No global/env OpenRouter key anywhere: only saved credentials may work."""
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "session-discovery-hmac-key-0001")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(settings, "openrouter_api_key", "", raising=False)
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    monkeypatch.setattr(settings, "run_engine_enabled", False)
    monkeypatch.setattr(settings, "run_worker_enabled", False)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", False)
    monkeypatch.setattr(settings, "run_public_api_enabled", False)
    monkeypatch.setattr(settings, "run_deep_research_enabled", False)
    monkeypatch.setattr(settings, "run_multi_agent_enabled", False)
    from app.services.rate_limiter import reset_rate_limiter
    reset_rate_limiter()
    yield


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


class _FakeProvider:
    def __init__(self, models, boom=False):
        self._models = list(models)
        self._boom = boom

    def fetch_available_chat_models(self):
        if self._boom:
            raise RuntimeError("discovery exploded")
        return list(self._models)


def _install_provider(monkeypatch, *, models=(), boom=False, recorder=None,
                      models_for_key=None):
    """Replace the provider factory, recording every config it receives.

    A config-bearing call represents "the user's own saved credentials"; a
    None config represents the global/env fallback and yields NO models, so a
    test can only pass if the saved credential path itself worked.
    """
    def _get_provider(config=None):
        if recorder is not None:
            recorder.append(config)
        if config is None:
            return _FakeProvider([])
        key = (config or {}).get("api_key")
        chosen = models_for_key(key) if models_for_key else list(models)
        return _FakeProvider(chosen, boom=boom)

    import app.services.llm_providers as providers_pkg
    monkeypatch.setattr(providers_pkg, "get_provider", _get_provider)


def _headers(c, token):
    h = auth_headers(token)
    csrf = c.cookies.get("research_assistant_csrf_token")
    if csrf:
        h["X-CSRF-Token"] = csrf
    return h


# -- the resolver itself ------------------------------------------------


def test_saved_settings_key_drives_discovery_for_its_owner(eng, monkeypatch):
    db = mk_session(eng)
    try:
        ensure_user(db, 7)
        settings_service.save_user_settings(db, 7, "openrouter",
                                            api_key=CANARY, model=MODEL)
        rec: list = []
        _install_provider(monkeypatch, recorder=rec,
                          models_for_key=lambda key: [MODEL] if key == CANARY else [])
        assert sessions_route.fetch_available_chat_models(db, 7) == [MODEL]
        # the DECRYPTED saved key reached the factory, and the env/global
        # fallback was never needed (no config=None call).
        assert any((c or {}).get("api_key") == CANARY for c in rec)
        assert all(c is not None for c in rec)
    finally:
        db.close()


def test_saved_key_of_one_user_is_invisible_to_another(eng, monkeypatch):
    db = mk_session(eng)
    try:
        ensure_user(db, 7)
        ensure_user(db, 8)
        settings_service.save_user_settings(db, 7, "openrouter",
                                            api_key=CANARY, model=MODEL)
        rec: list = []
        _install_provider(monkeypatch, recorder=rec,
                          models_for_key=lambda key: [MODEL])
        assert sessions_route.fetch_available_chat_models(db, 8) == []
        assert all((c or {}).get("api_key") != CANARY for c in rec)
    finally:
        db.close()


def test_no_saved_credentials_and_no_env_key_fails_closed(eng, monkeypatch):
    db = mk_session(eng)
    try:
        ensure_user(db, 9)
        _install_provider(monkeypatch, models=[MODEL])
        assert sessions_route.fetch_available_chat_models(db, 9) == []
    finally:
        db.close()


def test_discovery_failure_is_contained_and_leaks_nothing(eng, monkeypatch, caplog):
    db = mk_session(eng)
    try:
        ensure_user(db, 10)
        settings_service.save_user_settings(db, 10, "openrouter",
                                            api_key=CANARY, model=MODEL)
        _install_provider(monkeypatch, boom=True)
        with caplog.at_level("DEBUG"):
            assert sessions_route.fetch_available_chat_models(db, 10) == []
        assert CANARY not in caplog.text
    finally:
        db.close()


def test_global_env_provider_path_still_works(eng, monkeypatch):
    """The intentional server-side env path must keep working unchanged."""
    db = mk_session(eng)
    try:
        ensure_user(db, 11)
        monkeypatch.setattr(settings, "openrouter_api_key",
                            "sk-or-v1-ENVKEY", raising=False)
        rec: list = []

        def _get_provider(config=None):
            rec.append(config)
            return _FakeProvider([MODEL] if config is None else [])

        import app.services.llm_providers as providers_pkg
        monkeypatch.setattr(providers_pkg, "get_provider", _get_provider)
        assert sessions_route.fetch_available_chat_models(db, 11) == [MODEL]
        assert rec == [None]          # no saved rows -> the global provider was used
    finally:
        db.close()


# -- the HTTP route the user actually presses ---------------------------


def test_route_model_patch_succeeds_with_only_the_saved_settings_key(monkeypatch, caplog):
    from fastapi.testclient import TestClient
    from app.main import app

    rec: list = []
    _install_provider(monkeypatch, recorder=rec,
                      models_for_key=lambda key: [MODEL] if key == CANARY else [])
    with TestClient(app) as c:
        _uid, token = register_and_login(c, username="disco_" + uuid.uuid4().hex[:8])
        h = _headers(c, token)
        sid = c.post("/api/sessions", json={"title": "disco"}, headers=h).json()["id"]
        with caplog.at_level("DEBUG"):
            put = c.put("/api/settings", headers=h, json={
                "llm_provider": "openrouter", "api_key": CANARY, "model": MODEL})
            assert put.status_code == 200, put.text[:300]
            r = c.patch("/api/sessions/" + str(sid) + "/model",
                        headers=h, json={"model": MODEL})
            assert r.status_code == 200, r.text[:300]
            assert r.json()["model"] == MODEL
        # the saved key was used, and it appears in neither response nor log
        assert any((cfg or {}).get("api_key") == CANARY for cfg in rec)
        assert CANARY not in r.text and CANARY not in put.text
        assert CANARY not in caplog.text


def test_route_model_patch_503_when_nothing_is_saved(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    _install_provider(monkeypatch, models=[MODEL])
    with TestClient(app) as c:
        _uid, token = register_and_login(c, username="discon_" + uuid.uuid4().hex[:8])
        h = _headers(c, token)
        sid = c.post("/api/sessions", json={"title": "disco"}, headers=h).json()["id"]
        r = c.patch("/api/sessions/" + str(sid) + "/model",
                    headers=h, json={"model": MODEL})
        assert r.status_code == 503, r.text[:300]
        assert "no provider model discovery is available" in r.text


def test_route_model_patch_is_isolated_between_users(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    rec: list = []
    _install_provider(monkeypatch, recorder=rec, models_for_key=lambda key: [MODEL])
    with TestClient(app) as a:
        _uid, token = register_and_login(a, username="isoA_" + uuid.uuid4().hex[:8])
        ha = _headers(a, token)
        assert a.put("/api/settings", headers=ha, json={
            "llm_provider": "openrouter", "api_key": CANARY,
            "model": MODEL}).status_code == 200
        with TestClient(app) as b:
            _uid2, token2 = register_and_login(b, username="isoB_" + uuid.uuid4().hex[:8])
            hb = _headers(b, token2)
            sid = b.post("/api/sessions", json={"title": "iso"}, headers=hb).json()["id"]
            rec.clear()
            r = b.patch("/api/sessions/" + str(sid) + "/model",
                        headers=hb, json={"model": MODEL})
            assert r.status_code == 503, r.text[:300]
            assert all((cfg or {}).get("api_key") != CANARY for cfg in rec)
