"""Phase 1B.2R MP-3C3D4C — focused tests for the ONE authoritative deep-research
executability predicate and its reuse by mode routing + settings capability.
No network, no worker, no DB writes beyond hermetic fixtures.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.models.models import Run
from app.run_engine.deep_research_readiness import (
    deep_research_executable,
    deep_research_unavailable_reason,
)
from app.run_engine import messages_stream as ms
from tests.run_engine_helpers import ensure_user, make_ctx, mk_engine, mk_session

_HMAC = "test-hmac-key-deep-research-readiness-0"


def _all_on(monkeypatch):
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "a" * 32)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_worker_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_deep_research_enabled", True)


# ── A1. Default flag false ───────────────────────────────────


def test_flag_defaults_to_false():
    assert settings.run_deep_research_enabled is False


# ── A2/A3. Not executable / capability unavailable unless every prerequisite ──


def test_not_executable_by_default():
    assert deep_research_executable() is False
    assert deep_research_unavailable_reason() == "deep_research_not_executable"


@pytest.mark.parametrize("missing", [
    "run_engine_enabled", "run_worker_enabled", "run_chat_stream_enabled",
    "run_public_api_enabled", "run_deep_research_enabled",
])
def test_missing_any_single_prerequisite_keeps_it_unavailable(monkeypatch, missing):
    _all_on(monkeypatch)
    monkeypatch.setattr(settings, missing, False)
    assert deep_research_executable() is False


def test_missing_hmac_key_keeps_it_unavailable(monkeypatch):
    _all_on(monkeypatch)
    monkeypatch.delenv("RUN_IDEMPOTENCY_HMAC_KEY", raising=False)
    assert deep_research_executable() is False


# ── A5. Available when all prerequisites true ────────────────


def test_executable_when_everything_is_on(monkeypatch):
    _all_on(monkeypatch)
    assert deep_research_executable() is True
    assert deep_research_unavailable_reason() is None


# ── mode-routing reuse: /messages/stream + /api/runs share the SAME gate ──


def test_unsupported_mode_reason_delegates_to_the_predicate_for_deep_research(monkeypatch):
    assert ms.unsupported_mode_reason("deep_research") == "deep_research_not_executable"
    _all_on(monkeypatch)
    assert ms.unsupported_mode_reason("deep_research") is None


def test_chat_mode_reason_is_unaffected_by_deep_research_flag(monkeypatch):
    # "chat" must remain always-supported regardless of deep-research config —
    # proves the two are not accidentally coupled through a shared static set.
    assert ms.unsupported_mode_reason("chat") is None
    _all_on(monkeypatch)
    monkeypatch.setattr(settings, "run_deep_research_enabled", False)
    assert ms.unsupported_mode_reason("chat") is None


def test_unrecognized_mode_still_gets_the_generic_bounded_slug():
    code = ms.unsupported_mode_reason("banana")
    assert code is not None and code.startswith("mode_unsupported")


# ── real authenticated /api/runs route proof (not source-inspection) ──
#
# test_run_engine_mp3c3d3a.py already proves, through a REAL TestClient
# request, that /api/runs rejects mode="deep_research" with 422 and zero
# persisted rows when the deep-research prerequisites are NOT met (its
# `test_direct_run_api_rejects_deep_research_exact_422_before_persistence`,
# using `_configure_run_api_ready` which deliberately leaves
# run_deep_research_enabled/run_worker_enabled off). What was still missing
# anywhere in the suite was the POSITIVE-path proof at the same real-route
# level: that when every deep-research prerequisite genuinely is met, the
# same route actually accepts and queues the run. Reusing that file's exact
# fixtures (ladder rung 2) rather than re-deriving a second copy.

from tests.test_run_engine_mp3c3d3a import (
    _run_api_client,
    _run_api_teardown,
    _seed_run_api_session,
    run_api_eng,
)


def test_direct_run_api_accepts_deep_research_when_fully_ready(run_api_eng, monkeypatch):
    _all_on(monkeypatch)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    _seed_run_api_session(run_api_eng)
    c, app, db = _run_api_client(run_api_eng, monkeypatch)
    try:
        r = c.post("/api/runs", json={"session_id": 1, "message": "hi", "mode": "deep_research"},
                   headers={"Idempotency-Key": "runapi-dr-ready-key"})
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "queued"
        run_id = r.json()["run_id"]
    finally:
        _run_api_teardown(app)
        db.close()
    db2 = mk_session(run_api_eng)
    try:
        run = db2.query(Run).filter(Run.id == run_id).first()
        assert run is not None
        assert run.kind == "deep_research"
    finally:
        db2.close()
