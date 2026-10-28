"""Custom skill marketplace — loader + execution endpoint.

Covers: manifest validation (bad ids, missing fields, non-object JSON),
safe import (syntax errors / missing handler.py are skipped, never raised),
the capabilities listing (kind="custom"), the execution endpoint (404/403/
422/200/500/504), per-user isolation of enablement AND config, and the CSRF
guard. All skills live in a tmp directory injected via ``settings.data_dir``;
nothing here touches the real ``data/custom_skills`` contents.
"""

from __future__ import annotations

import json
import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from tests.auth_helpers import auth_headers, register_and_login

HELLO_HANDLER = '''
async def execute(context: dict) -> dict:
    inputs = dict(context.get("inputs") or {})
    name = inputs.get("name") or "world"
    return {
        "message": "Hello from custom skill, " + str(name) + "!",
        "echoed_inputs": inputs,
        "echoed_config": dict(context.get("config") or {}),
        "context_user_id": context.get("user_id"),
    }
'''

GREETER_HANDLER = '''
async def execute(context: dict) -> dict:
    return {"greeting": "Hi " + str((context.get("inputs") or {}).get("display_name"))}
'''

BOOM_HANDLER = '''
async def execute(context: dict) -> dict:
    raise ValueError("boom")
'''

SLOW_HANDLER = '''
import asyncio

async def execute(context: dict) -> dict:
    await asyncio.sleep(5)
    return {"done": True}
'''

BROKEN_HANDLER = "def execute(context: dict) -> dict:\n    return {oops"


def _write_skill(root, folder: str, manifest: dict | None, handler: str | None):
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (directory / "manifest.json").write_text(
            json.dumps(manifest) if not isinstance(manifest, str) else manifest,
            encoding="utf-8",
        )
    if handler is not None:
        (directory / "handler.py").write_text(handler, encoding="utf-8")
    return directory


def _hello_manifest():
    return {
        "id": "hello_world",
        "name": "Hello World",
        "description": "A friendly test skill that returns a greeting.",
        "icon": "sparkles",
        "inputs_schema": {
            "name": {"type": "string", "label": "Your name", "placeholder": "Ada"}
        },
    }


@pytest.fixture()
def skills_env(tmp_path, monkeypatch):
    """Redirect custom skills to tmp, build a zoo of valid+invalid skills,
    rescan, and restore the real directory state afterwards."""
    from app.services import custom_skill_loader

    root = tmp_path / "custom_skills"

    _write_skill(root, "hello_world", _hello_manifest(), HELLO_HANDLER)
    _write_skill(
        root,
        "greeter",
        {
            "id": "greeter",
            "name": "Greeter",
            "description": "Requires a display name.",
            "icon": "wrench",
            "inputs_schema": {
                "display_name": {"type": "string", "label": "Display name", "required": True}
            },
        },
        GREETER_HANDLER,
    )
    _write_skill(
        root,
        "boom",
        {"id": "boom", "name": "Boom", "description": "Always raises."},
        BOOM_HANDLER,
    )
    _write_skill(
        root,
        "slow",
        {"id": "slow", "name": "Slow", "description": "Sleeps forever."},
        SLOW_HANDLER,
    )
    # Invalid zoo — every one of these must be skipped, never raised.
    _write_skill(root, "broken_syntax", {"id": "broken_syntax", "name": "B", "description": "d"}, BROKEN_HANDLER)
    _write_skill(root, "missing_handler", {"id": "missing_handler", "name": "M", "description": "d"}, None)
    _write_skill(root, "bad_json", "{not json", HELLO_HANDLER)
    _write_skill(root, "bad_id", {"id": "Bad ID!", "name": "B", "description": "d"}, HELLO_HANDLER)
    _write_skill(root, "wrong_folder", {"id": "other_name", "name": "W", "description": "d"}, HELLO_HANDLER)
    _write_skill(root, "missing_fields", {"id": "missing_fields"}, HELLO_HANDLER)
    _write_skill(root, "schema_not_object", {
        "id": "schema_not_object", "name": "S", "description": "d",
        "inputs_schema": ["not", "an", "object"],
    }, HELLO_HANDLER)
    # A plain file (not a folder) among the skills.
    (root / "stray.txt").write_text("not a skill", encoding="utf-8")

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    report = custom_skill_loader.scan_and_load_custom_skills()
    yield {"root": root, "report": report}

    # Restore: undo the patch and rescan the REAL directory so later test
    # modules see the production loader state, not tmp leftovers.
    monkeypatch.undo()
    custom_skill_loader.scan_and_load_custom_skills()


def _auth(c, prefix="cs"):
    _uid, token = register_and_login(c, username=prefix + "_" + uuid.uuid4().hex[:8])
    return auth_headers(token)


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.services.rate_limiter import reset_rate_limiter

    reset_rate_limiter()
    yield


# ── Loader unit tests ──────────────────────────────────────────────────────


def test_scan_loads_valid_skills_and_skips_invalid_ones_gracefully(skills_env):
    from app.services import custom_skill_loader

    loaded = {entry["folder"] for entry in skills_env["report"] if entry["status"] == "loaded"}
    skipped = {entry["folder"] for entry in skills_env["report"] if entry["status"] == "skipped"}
    assert loaded == {"hello_world", "greeter", "boom", "slow"}
    assert {
        "broken_syntax", "missing_handler", "bad_json", "bad_id",
        "wrong_folder", "missing_fields", "schema_not_object",
    } <= skipped
    assert "stray.txt" not in loaded | skipped

    skill = custom_skill_loader.get_custom_skill("hello_world")
    assert skill is not None
    assert skill.name == "Hello World"
    assert skill.icon == "sparkles"
    assert "name" in skill.inputs_schema
    assert custom_skill_loader.get_custom_skill("broken_syntax") is None
    assert custom_skill_loader.get_custom_skill("never_existed") is None


def test_scan_survives_a_missing_directory(monkeypatch):
    from app.services import custom_skill_loader

    monkeypatch.setattr(settings, "data_dir", "/definitely/not/a/real/dir_42")
    report = custom_skill_loader.scan_and_load_custom_skills()
    assert report == []
    assert custom_skill_loader.get_custom_skill("hello_world") is None


def test_registry_includes_custom_entries(skills_env):
    from app.services import capability_registry

    listed = capability_registry.list_capabilities()
    custom = [c for c in listed if c.get("kind") == "custom"]
    assert {c["id"] for c in custom} == {"hello_world", "greeter", "boom", "slow"}
    hello = capability_registry.get_capability("hello_world")
    assert hello is not None and hello["kind"] == "custom"
    assert hello["enabled"] is False
    assert hello["inputs_schema"]["name"]["label"] == "Your name"
    assert capability_registry.get_capability("broken_syntax") is None


# ── HTTP: capabilities listing ────────────────────────────────────────────


def test_capabilities_api_lists_custom_skills(skills_env):
    c = _client()
    with c:
        r = c.get("/api/capabilities", headers=_auth(c))
        assert r.status_code == 200, r.text[:300]
        by_id = {item["id"]: item for item in r.json()}
        hello = by_id["hello_world"]
        assert hello["kind"] == "custom"
        assert hello["enabled"] is False
        assert hello["name"] == "Hello World"
        assert hello["inputs_schema"]["name"]["type"] == "string"
        assert "broken_syntax" not in by_id
        # Built-ins are untouched.
        assert by_id["web_scraper"]["kind"] == "capability"


def test_enable_custom_skill_via_capabilities_patch(skills_env):
    c = _client()
    with c:
        h = _auth(c)
        r = c.patch("/api/capabilities/hello_world", json={"enabled": True}, headers=h)
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body["enabled"] is True and body["kind"] == "custom"
        listed = {i["id"]: i for i in c.get("/api/capabilities", headers=h).json()}
        assert listed["hello_world"]["enabled"] is True
        assert listed["web_scraper"]["enabled"] is True  # built-in default intact


# ── HTTP: execution endpoint ──────────────────────────────────────────────


def test_execution_requires_authentication(skills_env):
    c = _client()
    with c:
        r = c.post(
            "/api/skills/custom/hello_world", json={"inputs": {}}
        )
        assert r.status_code in (401, 403)


def test_execution_unknown_skill_returns_404(skills_env):
    c = _client()
    with c:
        r = c.post(
            "/api/skills/custom/does_not_exist", json={"inputs": {}}, headers=_auth(c)
        )
        assert r.status_code == 404


def test_execution_requires_enabled_preference(skills_env):
    c = _client()
    with c:
        h = _auth(c)
        # Never enabled → 403.
        r = c.post("/api/skills/custom/hello_world", json={"inputs": {}}, headers=h)
        assert r.status_code == 403
        # Enabled for user A; user B still gets 403 (enablement is per user).
        assert (
            c.patch(
                "/api/capabilities/hello_world", json={"enabled": True}, headers=h
            ).status_code
            == 200
        )
        other = _auth(c, prefix="csB")
        r = c.post("/api/skills/custom/hello_world", json={"inputs": {}}, headers=other)
        assert r.status_code == 403


def test_execution_returns_handler_result(skills_env):
    c = _client()
    with c:
        h = _auth(c)
        c.patch("/api/capabilities/hello_world", json={"enabled": True}, headers=h)
        r = c.post(
            "/api/skills/custom/hello_world",
            json={"inputs": {"name": "Ada"}},
            headers=h,
        )
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body["skill_id"] == "hello_world"
        assert body["result"]["message"] == "Hello from custom skill, Ada!"
        assert body["result"]["echoed_inputs"] == {"name": "Ada"}


def test_execution_validates_required_inputs(skills_env):
    c = _client()
    with c:
        h = _auth(c)
        c.patch("/api/capabilities/greeter", json={"enabled": True}, headers=h)
        r = c.post("/api/skills/custom/greeter", json={"inputs": {}}, headers=h)
        assert r.status_code == 422
        assert "Display name" in r.json()["detail"]
        r = c.post(
            "/api/skills/custom/greeter",
            json={"inputs": {"display_name": "Ada"}},
            headers=h,
        )
        assert r.status_code == 200
        assert r.json()["result"] == {"greeting": "Hi Ada"}


def test_execution_config_is_isolated_per_user(skills_env):
    """User A's stored config must reach only A's executions — never B's."""
    c = _client()
    with c:
        user_a = _auth(c)
        user_b = _auth(c, prefix="csB")
        c.patch(
            "/api/capabilities/hello_world",
            json={"enabled": True, "config": {"api_key": "A-SECRET"}},
            headers=user_a,
        )
        c.patch(
            "/api/capabilities/hello_world",
            json={"enabled": True, "config": {"api_key": "B-SECRET"}},
            headers=user_b,
        )
        ra = c.post("/api/skills/custom/hello_world", json={"inputs": {}}, headers=user_a)
        rb = c.post("/api/skills/custom/hello_world", json={"inputs": {}}, headers=user_b)
        assert ra.status_code == 200 and rb.status_code == 200
        assert ra.json()["result"]["echoed_config"] == {"api_key": "A-SECRET"}
        assert rb.json()["result"]["echoed_config"] == {"api_key": "B-SECRET"}
        assert "A-SECRET" not in json.dumps(rb.json())


def test_execution_handler_exception_is_contained(skills_env):
    c = _client()
    with c:
        h = _auth(c)
        c.patch("/api/capabilities/boom", json={"enabled": True}, headers=h)
        r = c.post("/api/skills/custom/boom", json={"inputs": {}}, headers=h)
        assert r.status_code == 500
        # The app itself is unharmed.
        assert c.get("/api/capabilities", headers=h).status_code == 200


def test_execution_hung_handler_times_out(skills_env, monkeypatch):
    import app.services.custom_skill_loader as loader

    monkeypatch.setattr(loader, "EXECUTION_TIMEOUT_S", 0.1)
    c = _client()
    with c:
        h = _auth(c)
        c.patch("/api/capabilities/slow", json={"enabled": True}, headers=h)
        r = c.post("/api/skills/custom/slow", json={"inputs": {}}, headers=h)
        assert r.status_code == 504


def test_execution_requires_csrf_for_browser_origin(skills_env):
    c = _client()
    with c:
        headers = {**_auth(c), "Origin": "http://localhost:5173"}
        r = c.post("/api/skills/custom/hello_world", json={"inputs": {}}, headers=headers)
        assert r.status_code == 403
