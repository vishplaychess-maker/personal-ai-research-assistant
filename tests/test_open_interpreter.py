"""Open Interpreter integration — capability gating, permission levels, safety.

All Open Interpreter interactions are faked (the real package is an OPTIONAL
dependency and is NOT installed here): ``_import_interpreter`` is monkeypatched
with a fake module so tests cover Ask/Auto/Full semantics, the exact-code
approval path, timeouts, user isolation, availability degradation (503), and
the CSFR/auth gates — with zero network and zero real execution.
"""

from __future__ import annotations

import time
import uuid

import pytest
from cryptography.fernet import Fernet

import app.services.open_interpreter_service as oi_service
from app.config import settings
from app.services import capability_registry
from tests.auth_helpers import auth_headers, register_and_login

PROPOSED_CODE = "print('hello from interpreter')"


# ── Fake Open Interpreter package ─────────────────────────────────────────


class FakeComputer:
    def __init__(self, owner: "FakeInterpreter"):
        self._owner = owner

    def run(self, language: str, code: str):
        FakeInterpreter.executions.append((language, code))
        return [f"ran {language}: {code[:40]}"]


class FakeInterpreter:
    instances: list = []
    executions: list = []

    def __init__(self):
        self.auto_run = None
        self.verbose = None
        self.max_output = None
        self.messages: list = []
        self.computer = FakeComputer(self)
        FakeInterpreter.instances.append(self)

    def respond(self, prompt: str):
        self.messages.append({"role": "user", "content": prompt})
        self.messages.append(
            {"role": "assistant", "content": f"```python\n{PROPOSED_CODE}\n```"}
        )
        if self.auto_run:
            self.messages.append({"role": "computer", "content": "hello from interpreter"})
        return self.messages

    def chat(self, prompt: str, display: bool = False):
        return self.respond(prompt)


class FakeModule:
    Interpreter = FakeInterpreter
    __version__ = "0.4.9-fake"


class SlowFakeInterpreter(FakeInterpreter):
    def respond(self, prompt: str):
        time.sleep(1.0)
        return super().respond(prompt)


class SlowFakeModule(FakeModule):
    Interpreter = SlowFakeInterpreter


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.services.rate_limiter import reset_rate_limiter

    reset_rate_limiter()
    yield


@pytest.fixture()
def fake_oi(monkeypatch):
    FakeInterpreter.instances = []
    FakeInterpreter.executions = []
    monkeypatch.setattr(oi_service, "_import_interpreter", lambda: FakeModule())
    return FakeInterpreter


def _auth(c, prefix="oi"):
    _uid, token = register_and_login(c, username=prefix + "_" + uuid.uuid4().hex[:8])
    return auth_headers(token)


def _enable(c, headers, config=None):
    body = {"enabled": True}
    if config is not None:
        body["config"] = config
    return c.patch("/api/capabilities/open_interpreter", json=body, headers=headers)


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


# ── Registry integration ──────────────────────────────────────────────────


def test_registry_includes_open_interpreter_asking_by_default():
    entry = capability_registry.get_capability("open_interpreter")
    assert entry is not None
    assert entry["kind"] == "capability"
    assert entry["enabled"] is False  # fail-closed: opt-in only
    assert entry["config"] == {"permission_level": "ask_every_time"}


def test_normalize_level_fails_safe():
    assert oi_service._normalize_level("auto_approve") == "auto_approve"
    assert oi_service._normalize_level("full_access") == "full_access"
    assert oi_service._normalize_level("ask_every_time") == "ask_every_time"
    assert oi_service._normalize_level("totally_bogus") == "ask_every_time"
    assert oi_service._normalize_level(None) == "ask_every_time"


# ── Status endpoint ───────────────────────────────────────────────────────


def test_status_requires_authentication():
    c = _client()
    with c:
        assert c.get("/api/interpreter/status").status_code in (401, 403)


def test_status_reports_availability_and_ask_default(fake_oi):
    c = _client()
    with c:
        body = c.get("/api/interpreter/status", headers=_auth(c)).json()
        assert body["available"] is True
        assert body["enabled"] is False
        assert body["permission_level"] == "ask_every_time"


def test_status_reports_unavailability_with_hint(monkeypatch):
    monkeypatch.setattr(oi_service, "_import_interpreter", lambda: None)
    c = _client()
    with c:
        body = c.get("/api/interpreter/status", headers=_auth(c)).json()
        assert body["available"] is False
        assert "pip install" in body["install_hint"]


# ── Capability gating ─────────────────────────────────────────────────────


def test_execute_requires_enabled_capability(fake_oi):
    c = _client()
    with c:
        h = _auth(c)
        r = c.post(
            "/api/interpreter/execute", json={"prompt": "list files"}, headers=h
        )
        assert r.status_code == 403
        assert _enable(c, h).status_code == 200
        r = c.post(
            "/api/interpreter/execute", json={"prompt": "list files"}, headers=h
        )
        assert r.status_code == 200


def test_run_requires_enabled_capability(fake_oi):
    c = _client()
    with c:
        r = c.post(
            "/api/interpreter/run",
            json={"code": "print('x')", "language": "python"},
            headers=_auth(c),
        )
        assert r.status_code == 403


def test_execute_requires_csrf_for_browser_origin(fake_oi):
    c = _client()
    with c:
        h = _auth(c)
        _enable(c, h)
        headers = {**h, "Origin": "http://localhost:5173"}
        r = c.post(
            "/api/interpreter/execute", json={"prompt": "hi"}, headers=headers
        )
        assert r.status_code == 403


# ── Ask mode — the code-proposal approval flow ────────────────────────────


def test_ask_mode_proposes_code_and_never_executes(fake_oi):
    c = _client()
    with c:
        h = _auth(c)
        _enable(c, h)  # default level = ask_every_time
        r = c.post(
            "/api/interpreter/execute",
            json={"prompt": "list files in the workspace"},
            headers=h,
        )
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body["status"] == "needs_approval"
        assert PROPOSED_CODE in body["code"]
        assert body["language"] == "python"
        # NOTHING was executed, and the interpreter was configured fail-safe.
        assert FakeInterpreter.executions == []
        assert all(inst.auto_run is False for inst in FakeInterpreter.instances)


def test_approved_code_run_executes_exactly_the_reviewed_code(fake_oi):
    c = _client()
    with c:
        h = _auth(c)
        _enable(c, h)  # ask mode
        code = "print('user reviewed and approved this')"
        r = c.post(
            "/api/interpreter/run",
            json={"code": code, "language": "python"},
            headers=h,
        )
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body["status"] == "executed"
        assert "ran python" in body["output"]
        assert FakeInterpreter.executions == [("python", code)]


def test_run_rejects_unknown_language(fake_oi):
    c = _client()
    with c:
        h = _auth(c)
        _enable(c, h)
        r = c.post(
            "/api/interpreter/run",
            json={"code": "print('x')", "language": "cobol"},
            headers=h,
        )
        assert r.status_code == 422


# ── Auto / Full mode ──────────────────────────────────────────────────────


@pytest.mark.parametrize("level", ["auto_approve", "full_access"])
def test_auto_and_full_modes_execute_automatically(fake_oi, level):
    c = _client()
    with c:
        h = _auth(c)
        _enable(c, h, config={"permission_level": level})
        r = c.post(
            "/api/interpreter/execute",
            json={"prompt": "list files in the workspace"},
            headers=h,
        )
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body["status"] == "executed"
        assert body["permission_level"] == level
        assert "hello from interpreter" in body["output"]
        assert any(inst.auto_run is True for inst in FakeInterpreter.instances)


def test_stored_level_respected_per_user(fake_oi):
    c = _client()
    with c:
        user_a = _auth(c)
        user_b = _auth(c, prefix="oiB")
        _enable(c, user_a, config={"permission_level": "auto_approve"})
        # User B enabled too but keeps the default ask level.
        _enable(c, user_b)
        ra = c.post(
            "/api/interpreter/execute", json={"prompt": "task"}, headers=user_a
        )
        rb = c.post(
            "/api/interpreter/execute", json={"prompt": "task"}, headers=user_b
        )
        assert ra.json()["status"] == "executed"
        assert rb.json()["status"] == "needs_approval"


def test_each_request_gets_a_fresh_interpreter(fake_oi):
    c = _client()
    with c:
        h = _auth(c)
        _enable(c, h, config={"permission_level": "auto_approve"})
        c.post("/api/interpreter/execute", json={"prompt": "one"}, headers=h)
        c.post("/api/interpreter/execute", json={"prompt": "two"}, headers=h)
        # No shared conversation state: a fresh instance per request.
        assert len(FakeInterpreter.instances) == 2
        assert FakeInterpreter.instances[0].messages is not FakeInterpreter.instances[1].messages


# ── Stability: timeout + availability degradation ─────────────────────────


def test_hung_interpreter_times_out_and_app_survives(monkeypatch):
    FakeInterpreter.instances = []
    FakeInterpreter.executions = []
    monkeypatch.setattr(oi_service, "_import_interpreter", lambda: SlowFakeModule())
    monkeypatch.setattr(oi_service, "EXECUTION_TIMEOUT_S", 0.1)
    monkeypatch.setattr(oi_service, "GENERATION_TIMEOUT_S", 0.1)
    c = _client()
    with c:
        h = _auth(c)
        _enable(c, h, config={"permission_level": "auto_approve"})
        r = c.post(
            "/api/interpreter/execute", json={"prompt": "task"}, headers=h
        )
        assert r.status_code == 504
        # The app itself is unharmed.
        assert c.get("/api/capabilities", headers=h).status_code == 200


def test_missing_package_answers_503_with_install_hint(monkeypatch):
    monkeypatch.setattr(oi_service, "_import_interpreter", lambda: None)
    c = _client()
    with c:
        h = _auth(c)
        _enable(c, h)
        r = c.post(
            "/api/interpreter/execute", json={"prompt": "task"}, headers=h
        )
        assert r.status_code == 503
        assert "pip install" in r.json()["detail"]
        r = c.post(
            "/api/interpreter/run",
            json={"code": "print('x')", "language": "python"},
            headers=h,
        )
        assert r.status_code == 503
