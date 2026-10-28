"""GET/PATCH /api/capabilities — executable capability toggles.

The catalog is static code; only per-user overrides are stored in
``skill_preferences``. These tests cover auth, the catalog/override merge,
upsert behaviour (including config round-trip), 404 for unknown ids, per-user
isolation, and the CSRF guard on the mutating route.

The sibling ``/api/skills`` playbook routes are intentionally untouched and
covered by tests/test_skills_catalog_route.py.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from tests.auth_helpers import auth_headers, register_and_login

_CAPABILITY_IDS = {
    "web_search", "web_scraper", "code_executor", "file_reader",
    "desktop_automation", "openbot_research", "github-mcp", "notion-mcp",
    "postgres-mcp",
}
_CONFIGURED_IDS = {"code_executor", "desktop_automation"}
_MCP_IDS = {"github-mcp", "notion-mcp", "postgres-mcp"}
# web_scraper ships enabled (the Deep Dive scraper needs no API key); every
# other capability stays opt-in.
_ENABLED_DEFAULT_IDS = {"web_scraper"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "capabilities-hmac-key-0001")
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
    _uid, token = register_and_login(c, username="cap_" + uuid.uuid4().hex[:8])
    return auth_headers(token)


def test_capabilities_require_authentication():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        assert c.get("/api/capabilities").status_code in (401, 403)


def test_lists_static_catalog_with_disabled_defaults():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.get("/api/capabilities", headers=_auth(c))
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert {item["id"] for item in body} == _CAPABILITY_IDS
        for item in body:
            assert set(item) == {
                "id", "name", "description", "icon", "enabled", "config",
                "kind", "required_config_keys",
            }
            assert item["name"]
            assert item["description"]
            assert item["enabled"] is (item["id"] in _ENABLED_DEFAULT_IDS)
            if item["id"] in _CONFIGURED_IDS:
                assert item["config"] == {"permission_level": "auto_approve"}
            elif item["id"] in _MCP_IDS:
                assert item["kind"] == "mcp_server"
                assert item["config"] == {"configured": False}
                assert item["required_config_keys"]
            else:
                assert item["config"] == {}


def test_patch_upserts_and_get_reflects_the_override():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        h = _auth(c)
        r = c.patch("/api/capabilities/web_search", json={"enabled": True}, headers=h)
        assert r.status_code == 200, r.text[:300]
        assert r.json()["enabled"] is True
        assert r.json()["id"] == "web_search"

        listed = {item["id"]: item for item in c.get("/api/capabilities", headers=h).json()}
        assert listed["web_search"]["enabled"] is True
        assert listed["code_executor"]["enabled"] is False


def test_patch_config_round_trips_and_survives_flag_only_updates():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        h = _auth(c)
        r = c.patch(
            "/api/capabilities/web_search",
            json={"enabled": True, "config": {"max_results": 5}},
            headers=h,
        )
        assert r.status_code == 200, r.text[:300]
        assert r.json()["config"] == {"max_results": 5}

        # A later flag-only PATCH must not erase the stored config.
        r = c.patch("/api/capabilities/web_search", json={"enabled": False}, headers=h)
        assert r.status_code == 200
        assert r.json()["config"] == {"max_results": 5}
        assert r.json()["enabled"] is False


def test_unknown_capability_returns_404():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.patch(
            "/api/capabilities/not_a_real_skill",
            json={"enabled": True},
            headers=_auth(c),
        )
        assert r.status_code == 404


def test_overrides_are_isolated_per_user():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        user_a = _auth(c)
        user_b = _auth(c)
        assert (
            c.patch(
                "/api/capabilities/code_executor", json={"enabled": True}, headers=user_a
            ).status_code
            == 200
        )
        a_items = {i["id"]: i for i in c.get("/api/capabilities", headers=user_a).json()}
        b_items = {i["id"]: i for i in c.get("/api/capabilities", headers=user_b).json()}
        assert a_items["code_executor"]["enabled"] is True
        assert b_items["code_executor"]["enabled"] is False


def test_patch_requires_csrf_for_browser_origin():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        headers = {
            **_auth(c),
            "Origin": "http://localhost:5173",
        }
        r = c.patch("/api/capabilities/web_search", json={"enabled": True}, headers=headers)
        assert r.status_code == 403
