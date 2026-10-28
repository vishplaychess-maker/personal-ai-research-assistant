"""Official MCP registry: registration, connection checks, encrypted config.

The provider checks are mocked at the registry seams (``_http_status`` and
``check_connection``); no real GitHub/Notion/Postgres call is ever made and no
real token appears anywhere.
"""

from __future__ import annotations

import json
import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.services import mcp_registry as reg
from app.services.encryption_service import decrypt_key


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "mcp-registry-hmac-0001")
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


# ── static registry ────────────────────────────────────────


def test_registry_contains_the_three_official_servers():
    ids = {server.id for server in reg.list_servers()}
    assert ids == {"github-mcp", "notion-mcp", "postgres-mcp"}
    assert reg.get_server("github-mcp").required_config_keys == ("GITHUB_TOKEN",)
    assert reg.get_server("notion-mcp").required_config_keys == ("NOTION_TOKEN",)
    assert reg.get_server("postgres-mcp").required_config_keys == ("POSTGRES_URL",)
    assert reg.get_server("unknown") is None


def test_github_check_uses_bearer_auth(monkeypatch):
    seen = {}

    def fake_status(url, headers, timeout=10.0):
        seen["url"] = url
        seen["headers"] = dict(headers)
        return 200

    monkeypatch.setattr(reg, "_http_status", fake_status)

    assert reg.check_connection("github-mcp", {"GITHUB_TOKEN": "test-token"}) is True
    assert seen["url"] == reg.GITHUB_CHECK_URL
    assert seen["headers"]["Authorization"] == "Bearer test-token"


def test_notion_check_rejects_unauthorized(monkeypatch):
    seen = {}

    def fake_status(url, headers, timeout=10.0):
        seen["url"] = url
        seen["headers"] = dict(headers)
        return 401

    monkeypatch.setattr(reg, "_http_status", fake_status)

    assert reg.check_connection("notion-mcp", {"NOTION_TOKEN": "test-token"}) is False
    assert seen["url"] == reg.NOTION_CHECK_URL
    assert seen["headers"]["Authorization"] == "Bearer test-token"
    assert seen["headers"]["Notion-Version"]


def test_postgres_check_is_dispatched(monkeypatch):
    monkeypatch.setattr(reg, "_postgres_check", lambda config: True)
    assert reg.check_connection("postgres-mcp", {"POSTGRES_URL": "postgresql://x"}) is True


def test_missing_keys_and_unknown_servers_fail_closed():
    assert reg.check_connection("github-mcp", {}) is False
    assert reg.check_connection("unknown", {"GITHUB_TOKEN": "x"}) is False


# ── HTTP + encrypted persistence ───────────────────────────


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def signed_in(client):
    from tests.auth_helpers import auth_headers, register_and_login

    _uid, token = register_and_login(client, username="mcp_" + uuid.uuid4().hex[:8])
    return client, auth_headers(token)


@pytest.fixture
def connection_ok(monkeypatch):
    calls = []

    def fake_check(server_id, config):
        calls.append((server_id, dict(config)))
        return True

    monkeypatch.setattr(reg, "check_connection", fake_check)
    return calls


def _patch(client, headers, server_id, enabled, config=None):
    return client.patch(
        f"/api/mcp/registry/{server_id}/config",
        headers=headers,
        json={"enabled": enabled, "config": config or {}},
    )


def test_config_is_encrypted_and_status_is_connected(signed_in, connection_ok):
    from app.database import SessionLocal
    from app.models.models import SkillPreference

    client, headers = signed_in
    token = "test-token-" + uuid.uuid4().hex

    response = _patch(client, headers, "github-mcp", True, {"GITHUB_TOKEN": token})
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body["status"] == "connected"
    assert body["enabled"] is True
    assert body["configured_keys"] == ["GITHUB_TOKEN"]
    assert token not in response.text

    db = SessionLocal()
    try:
        rows = (
            db.query(SkillPreference)
            .filter(SkillPreference.skill_id == "github-mcp")
            .all()
        )
        assert rows
        decrypted = []
        for row in rows:
            assert token not in (row.config_json or "")
            stored = json.loads(row.config_json)
            ciphertext = stored["config"]["GITHUB_TOKEN"]
            assert ciphertext.startswith("gAAAA")
            decrypted.append(decrypt_key(ciphertext))
        assert token in decrypted
    finally:
        db.close()

    listed = client.get("/api/mcp/registry", headers=headers)
    assert token not in listed.text
    github = next(s for s in listed.json() if s["id"] == "github-mcp")
    assert github["status"] == "connected"


def test_failed_connection_check_is_rejected(signed_in, monkeypatch):
    monkeypatch.setattr(reg, "check_connection", lambda server_id, config: False)
    client, headers = signed_in

    response = _patch(
        client, headers, "notion-mcp", True, {"NOTION_TOKEN": "bad-token"}
    )
    assert response.status_code == 400
    assert "Connection check failed" in response.json()["detail"]

    listed = client.get("/api/mcp/registry", headers=headers).json()
    notion = next(s for s in listed if s["id"] == "notion-mcp")
    assert notion["status"] == "not_configured"


def test_enabling_without_required_keys_is_422(signed_in, connection_ok):
    client, headers = signed_in
    response = _patch(client, headers, "postgres-mcp", True, {})
    assert response.status_code == 422
    assert "POSTGRES_URL" in response.json()["detail"]


def test_disabling_does_not_run_a_connection_check(signed_in, monkeypatch):
    def explode(server_id, config):
        raise AssertionError("connection check must not run when disabling")

    monkeypatch.setattr(reg, "check_connection", explode)
    client, headers = signed_in

    response = _patch(client, headers, "github-mcp", False)
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["status"] == "not_configured"


def test_reconfigure_uses_the_decrypted_existing_token(signed_in, connection_ok):
    client, headers = signed_in
    token = "test-token-" + uuid.uuid4().hex
    assert _patch(client, headers, "github-mcp", True, {"GITHUB_TOKEN": token}).status_code == 200

    response = _patch(client, headers, "github-mcp", True)  # no new token
    assert response.status_code == 200
    assert connection_ok[-1] == ("github-mcp", {"GITHUB_TOKEN": token})


def test_unknown_server_is_404(signed_in):
    client, headers = signed_in
    assert _patch(client, headers, "does-not-exist", True, {}).status_code == 404


def test_capabilities_masks_mcp_credentials(signed_in, connection_ok):
    client, headers = signed_in
    token = "test-token-" + uuid.uuid4().hex
    assert _patch(client, headers, "github-mcp", True, {"GITHUB_TOKEN": token}).status_code == 200

    listed = client.get("/api/capabilities", headers=headers)
    assert token not in listed.text
    github = next(i for i in listed.json() if i["id"] == "github-mcp")
    assert github["kind"] == "mcp_server"
    assert github["config"] == {"configured": True}
    assert github["required_config_keys"] == ["GITHUB_TOKEN"]

    # Credentials may not be written through the generic capabilities route.
    rejected = client.patch(
        "/api/capabilities/github-mcp",
        headers=headers,
        json={"enabled": True, "config": {"GITHUB_TOKEN": "other"}},
    )
    assert rejected.status_code == 422


def test_enabling_via_capabilities_requires_configured_credentials(signed_in):
    client, headers = signed_in
    response = client.patch(
        "/api/capabilities/notion-mcp",
        headers=headers,
        json={"enabled": True},
    )
    assert response.status_code == 422


def test_existing_stdio_mcp_routes_are_unchanged(signed_in):
    client, headers = signed_in
    response = client.get("/api/mcp/servers", headers=headers)
    assert response.status_code == 200
    assert isinstance(response.json(), list)
