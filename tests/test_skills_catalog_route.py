"""GET /api/skills/catalog - the built-in (filesystem) skill catalog.

Regression covered end to end: the Settings > Skills tab called only
GET /api/skills (user-created skills), so the bundled SKILL.md playbooks were
invisible in the UI - a fresh user saw a tab with zero skills. This endpoint is
the read-only surface the tab now renders, clearly separated from user skills.

Scope note: the backend was not *broken* - it simply never exposed the
filesystem catalog. These tests cover the new route, its ordering relative to
the dynamic /{skill_id} route, its auth gate, and that it leaks neither bodies
nor filesystem paths.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from tests.auth_helpers import auth_headers, register_and_login

# All seven bundled SKILL.md directories. image-generation and
# video-generation were dropped by the parser until they gained frontmatter.
_EXPECTED = {"web-research", "code-review", "browser-automation",
             "commit-message", "knowledge-graph",
             "image-generation", "video-generation"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "skills-catalog-hmac-key-0001")
    for flag in ("run_messages_stream_durable_enabled", "run_engine_enabled",
                 "run_worker_enabled", "run_chat_stream_enabled",
                 "run_public_api_enabled", "run_deep_research_enabled",
                 "run_multi_agent_enabled"):
        monkeypatch.setattr(settings, flag, False)
    from app.services.rate_limiter import reset_rate_limiter
    reset_rate_limiter()
    yield


def _auth(c):
    _uid, token = register_and_login(c, username="skillcat_" + uuid.uuid4().hex[:8])
    return auth_headers(token)


def test_catalog_lists_builtin_skills_for_an_authenticated_user():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        h = _auth(c)
        r = c.get("/api/skills/catalog", headers=h)
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body["enabled"] is True
        names = {s["name"] for s in body["skills"]}
        assert _EXPECTED <= names, names
        for s in body["skills"]:
            assert s["description"], s
            assert set(s) == {"name", "description", "pinned"}


def test_catalog_never_leaks_bodies_or_filesystem_paths():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        h = _auth(c)
        text = c.get("/api/skills/catalog", headers=h).text
        assert "SKILL.md" not in text
        assert "When to use" not in text          # L2 body must not be exposed
        assert ":\\" not in text and "/app" not in text and "/backend" not in text


def test_catalog_reports_disabled_when_the_feature_is_off(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        h = _auth(c)
        monkeypatch.setattr(settings, "thunder_skills_enabled", False)
        r = c.get("/api/skills/catalog", headers=h)
        assert r.status_code == 200, r.text[:300]
        assert r.json() == {"enabled": False, "skills": []}


def test_dynamic_skill_route_still_resolves_after_adding_catalog():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        h = _auth(c)
        # 404 (owned-skill lookup) not 422 - proves /catalog did not shadow it
        assert c.get("/api/skills/999999", headers=h).status_code == 404
        assert c.get("/api/skills", headers=h).status_code == 200


def test_catalog_requires_authentication():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        assert c.get("/api/skills/catalog").status_code in (401, 403)
