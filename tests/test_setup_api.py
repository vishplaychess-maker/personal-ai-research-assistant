"""First-run setup endpoints (Phase 1 - .exe packaging)."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import paths
from app.config import settings
from app.routes import setup as setup_routes
from app.services import setup_service

SECRET = "AIzaSyTEST-only-key-never-echoed-000111"


@pytest.fixture
def client(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(paths, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "get_config_path", lambda: config_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    app = FastAPI()
    app.include_router(setup_routes.router)
    return TestClient(app), config_path


def test_status_before_configure(client):
    test_client, _ = client
    resp = test_client.get("/api/setup/status")
    assert resp.status_code == 200
    assert resp.json() == {"configured": False}


def test_configure_writes_config_and_never_echoes(client):
    test_client, config_path = client
    resp = test_client.post("/api/setup/configure", json={"gemini_api_key": SECRET})
    assert resp.status_code == 200
    assert resp.json() == {"configured": True}
    assert SECRET not in resp.text
    assert set(resp.json().keys()) == {"configured"}
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored == {"gemini_api_key": SECRET}


def test_status_after_configure(client):
    test_client, config_path = client
    test_client.post("/api/setup/configure", json={"gemini_api_key": SECRET})
    assert test_client.get("/api/setup/status").json() == {"configured": True}
    assert config_path.exists()


@pytest.mark.parametrize("body", [
    {"gemini_api_key": ""},
    {"gemini_api_key": "   "},
    {},
])
def test_configure_rejects_empty_key(client, body):
    test_client, config_path = client
    resp = test_client.post("/api/setup/configure", json=body)
    assert resp.status_code == 400
    assert "must be a non-empty string" in resp.json()["detail"]
    assert not config_path.exists()


def test_configure_rejects_oversize_key_without_echoing(client):
    test_client, config_path = client
    oversized = "K" * (setup_routes.MAX_API_KEY_CHARS + 1)
    resp = test_client.post("/api/setup/configure", json={"gemini_api_key": oversized})
    assert resp.status_code == 400
    assert oversized not in resp.text
    assert resp.json()["detail"] == "gemini_api_key is too long"
    assert not config_path.exists()


def test_configure_save_failure_is_bounded_500(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(setup_service, "save_gemini_api_key", lambda key: False)
    resp = test_client.post("/api/setup/configure", json={"gemini_api_key": SECRET})
    assert resp.status_code == 500
    assert SECRET not in resp.text


def test_status_true_when_env_key_present(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    assert test_client.get("/api/setup/status").json() == {"configured": True}


def test_configure_never_logs_the_key(client, caplog):
    test_client, _ = client
    with caplog.at_level("INFO"):
        test_client.post("/api/setup/configure", json={"gemini_api_key": SECRET})
    assert SECRET not in caplog.text


def test_resolver_env_wins_over_config(client, monkeypatch):
    _test_client, config_path = client
    config_path.write_text(json.dumps({"gemini_api_key": "from-config"}), encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    assert setup_service.resolve_gemini_api_key() == "from-env"


def test_resolver_uses_config_for_gemini_provider(client):
    _test_client, config_path = client
    config_path.write_text(json.dumps({"gemini_api_key": "from-config"}), encoding="utf-8")
    assert setup_service.resolve_gemini_api_key() == "from-config"
    assert setup_service.is_configured() is True


def test_resolver_ignores_config_for_other_provider(client, monkeypatch):
    _test_client, config_path = client
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    config_path.write_text(json.dumps({"gemini_api_key": "from-config"}), encoding="utf-8")
    assert setup_service.resolve_gemini_api_key() == ""
