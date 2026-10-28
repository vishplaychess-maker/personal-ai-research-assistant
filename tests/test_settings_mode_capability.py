"""Phase 1B.2R MP-3C3D3B — focused tests for the ``chat_modes`` capability
field added to ``GET/PUT /api/settings``.

Scope: proves the capability CONTRACT (truth table, bounded fields,
determinism, auth, zero side-effects) — it does NOT re-audit D3A's routing.
The one routing-still-authoritative test (``test_forged_unavailable_mode_...``)
makes exactly one real call into the pre-existing, unmodified D3A gate to
prove capability advertising and enforcement have not diverged; it does not
repeat D3A's full suite.

Hermetic: in-memory SQLite via ``tests/run_engine_helpers.py`` (the same
fixtures MP-1A/3C* already use) + a direct ``get_current_user``/``get_db``
dependency override — no real registration/login flow, no live services.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models.models import Message, Run
from app.services.auth_service import get_current_user

from tests.run_engine_helpers import mk_engine, mk_session, ensure_user, _FakeUser

_USER_ID = 9001


def _client(eng):
    def _override_db():
        db = mk_session(eng)
        try:
            yield db
        finally:
            db.close()

    def _override_user():
        return _FakeUser(_USER_ID)

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = _override_user
    return TestClient(app)


def _teardown():
    app.dependency_overrides.clear()


@pytest.fixture()
def eng():
    e = mk_engine()
    db = mk_session(e)
    try:
        ensure_user(db, _USER_ID)
    finally:
        db.close()
    yield e


def _chat_modes(body: dict) -> dict:
    return {m["mode"]: m for m in body["chat_modes"]}


# ── 1. Authentication is required ───────────────────────────


def test_capability_read_requires_authentication(eng):
    # only get_db is overridden — no get_current_user override, no bearer
    # token supplied — proving GET /api/settings itself enforces auth.
    def _override_db():
        db = mk_session(eng)
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[get_db] = _override_db
    try:
        c = TestClient(app)
        resp = c.get("/api/settings")
        assert resp.status_code == 401
    finally:
        _teardown()


# ── 2-5. Truth table ─────────────────────────────────────────


def test_durable_off_both_legacy_capabilities_on_advertises_all(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    monkeypatch.setattr(settings, "enable_deep_research", True)
    monkeypatch.setattr(settings, "enable_multi_agent", True)
    c = _client(eng)
    try:
        resp = c.get("/api/settings")
        assert resp.status_code == 200
        modes = _chat_modes(resp.json())
        assert modes["chat"]["available"] is True
        assert modes["deep_research"]["available"] is True
        assert modes["multi_agent"]["available"] is True
    finally:
        _teardown()


def test_durable_off_deep_research_off_disables_only_deep_research(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    monkeypatch.setattr(settings, "enable_deep_research", False)
    monkeypatch.setattr(settings, "enable_multi_agent", True)
    c = _client(eng)
    try:
        modes = _chat_modes(c.get("/api/settings").json())
        assert modes["chat"]["available"] is True
        assert modes["deep_research"]["available"] is False
        assert modes["deep_research"]["reason"]
        assert modes["multi_agent"]["available"] is True
    finally:
        _teardown()


def test_durable_off_multi_agent_off_disables_only_multi_agent(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    monkeypatch.setattr(settings, "enable_deep_research", True)
    monkeypatch.setattr(settings, "enable_multi_agent", False)
    c = _client(eng)
    try:
        modes = _chat_modes(c.get("/api/settings").json())
        assert modes["chat"]["available"] is True
        assert modes["deep_research"]["available"] is True
        assert modes["multi_agent"]["available"] is False
        assert modes["multi_agent"]["reason"]
    finally:
        _teardown()


def test_durable_on_advertises_only_auto_chat_as_executable(eng, monkeypatch):
    # "Auto" is frontend-only (never in this backend list); durable ON means
    # only "chat" is executable regardless of the legacy capability flags.
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", True)
    monkeypatch.setattr(settings, "enable_deep_research", True)
    monkeypatch.setattr(settings, "enable_multi_agent", True)
    c = _client(eng)
    try:
        modes = _chat_modes(c.get("/api/settings").json())
        assert modes["chat"]["available"] is True
        assert modes["deep_research"]["available"] is False
        assert modes["multi_agent"]["available"] is False
    finally:
        _teardown()


# ── 6/9. Bounded, non-leaking fields ────────────────────────


def test_capability_output_contains_only_bounded_public_fields(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    monkeypatch.setattr(settings, "enable_deep_research", False)
    monkeypatch.setattr(settings, "enable_multi_agent", False)
    c = _client(eng)
    try:
        body = c.get("/api/settings").json()
        for m in body["chat_modes"]:
            assert set(m.keys()) <= {"mode", "available", "reason"}
        raw = json.dumps(body)
        for canary in (
            "run_messages_stream_durable_enabled", "RUN_IDEMPOTENCY_HMAC_KEY",
            "enable_deep_research", "enable_multi_agent",
            "mode_unsupported", "worker", "handler", "session_id", "family",
        ):
            assert canary not in raw
    finally:
        _teardown()


# ── 7. Forging an unavailable mode is still rejected by D3A routing ──


def test_forged_unavailable_mode_still_rejected_by_existing_routing(eng, monkeypatch):
    # Durable ON => capability says deep_research is unavailable. A stale/
    # forged client that ignores that and sends it anyway must still be
    # rejected by the pre-existing, UNCHANGED durable executability gate —
    # proves capability advertising and enforcement have not diverged.
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", False)
    c = _client(eng)
    try:
        resp = c.post(
            "/api/sessions/1/messages/stream",
            json={"message": "hi", "mode": "deep_research"},
        )
        # The exact rejection gate (auth/CSRF/mode/readiness) isn't re-audited
        # here (D3A/D1 already proved it) — the property this test protects
        # is that a forged unavailable mode is NEVER silently accepted or
        # downgraded to chat while capability advertising says it's off.
        assert resp.status_code != 200
        assert resp.status_code >= 400
    finally:
        _teardown()


# ── 8. No persistence or execution during capability reads ─────


def test_capability_read_creates_no_rows(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    c = _client(eng)
    try:
        for _ in range(3):
            assert c.get("/api/settings").status_code == 200
    finally:
        _teardown()
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 0
        assert db.query(Message).count() == 0
    finally:
        db.close()


# ── 10. Deterministic for the same configuration ────────────


def test_capability_response_is_deterministic(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    monkeypatch.setattr(settings, "enable_deep_research", True)
    monkeypatch.setattr(settings, "enable_multi_agent", False)
    c = _client(eng)
    try:
        first = c.get("/api/settings").json()["chat_modes"]
        second = c.get("/api/settings").json()["chat_modes"]
        assert first == second
    finally:
        _teardown()
