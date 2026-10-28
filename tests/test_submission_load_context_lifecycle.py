"""D6C-J9 — seq-1 `load_context` RunStep lifecycle contract (RED first).

Authorized contract:

    mode == "chat"           -> seq-1 load_context status = pending  (chat worker OWNS it)
    mode == "deep_research"  -> seq-1 load_context status = skipped  (compatibility placeholder)
    mode == "multi_agent"    -> seq-1 load_context status = skipped  (compatibility placeholder)

Both public submission routes (/api/runs and /messages/stream) funnel through
`submission.create_chat_run`, which is the single place this row is created, so the
assertions here exercise the exact common submission path the correction touches.

Run: PYTHONPATH=backend python -m pytest tests/test_submission_load_context_lifecycle.py -q
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import ResearchSession, RunStep
from app.run_engine import submission as sub
from app.run_engine.state_machine import (
    STEP_PENDING,
    STEP_SKIPPED,
    STEP_TERMINAL,
)
from tests.run_engine_helpers import ensure_user, make_ctx, mk_engine, mk_session

_HMAC = "unit-test-hmac-key-0123456789abcdef"

NON_CHAT_MODES = ("deep_research", "multi_agent")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_deep_research_enabled", True)
    monkeypatch.setattr(settings, "run_multi_agent_enabled", True)


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


def _seed(db, *, uid=7, sid_int=1):
    ensure_user(db, uid)
    if db.query(ResearchSession).filter(ResearchSession.id == sid_int).first() is None:
        db.add(ResearchSession(id=sid_int, user_id=uid, title="t"))
        db.commit()


def _create(db, *, mode, key):
    return sub.create_chat_run(
        db, make_ctx(db, sid="fam-j9"),
        submission=sub.build_chat_submission(
            session_id=1, message="hello", image_url=None, model="m1",
            mode=mode, system_prompt="sp", options={}),
        idempotency_key=key,
    )


def _seq1(db, run_id):
    return (db.query(RunStep)
            .filter(RunStep.run_id == run_id, RunStep.seq == 1).one())


# ── contract ──────────────────────────────────────────────────────────────

def test_skipped_is_a_documented_terminal_step_state():
    assert STEP_SKIPPED == "skipped"
    assert STEP_SKIPPED in STEP_TERMINAL
    assert STEP_PENDING not in STEP_TERMINAL


def test_chat_seq1_load_context_stays_pending_and_is_worker_owned(eng):
    db = mk_session(eng)
    try:
        _seed(db)
        run, created = _create(db, mode="chat", key="J9-CHAT")
        assert created is True
        step = _seq1(db, run.id)
        assert step.kind == "load_context"
        assert step.status == STEP_PENDING, step.status
        assert step.finished_at is None
        assert STEP_PENDING not in STEP_TERMINAL
        db.close()
    finally:
        eng.dispose()


def test_default_and_omitted_mode_behave_as_chat(eng):
    db = mk_session(eng)
    try:
        _seed(db)
        run, _ = sub.create_chat_run(
            db, make_ctx(db, sid="fam-j9b"),
            submission=sub.build_chat_submission(
                session_id=1, message="hi", image_url=None, model="m1",
                mode=None, system_prompt="sp", options={}),
            idempotency_key="J9-DEFAULT")
        step = _seq1(db, run.id)
        assert run.kind == "chat_stream"
        assert step.status == STEP_PENDING, step.status
        db.close()
    finally:
        eng.dispose()


@pytest.mark.parametrize("mode", NON_CHAT_MODES)
def test_non_chat_seq1_load_context_is_skipped(eng, mode):
    db = mk_session(eng)
    try:
        _seed(db)
        run, created = _create(db, mode=mode, key="J9-" + mode)
        assert created is True
        assert run.kind == mode
        step = _seq1(db, run.id)
        assert step.kind == "load_context"
        assert step.status == STEP_SKIPPED, (
            "non-chat seq-1 load_context is %r (no component owns it)" % step.status)
        assert step.status in STEP_TERMINAL
        assert step.finished_at is not None
        db.close()
    finally:
        eng.dispose()


@pytest.mark.parametrize("mode", NON_CHAT_MODES)
def test_exactly_one_seq1_step_and_no_provider_work_for_it(eng, mode):
    db = mk_session(eng)
    try:
        _seed(db)
        run, _ = _create(db, mode=mode, key="J9-ONE-" + mode)
        rows = db.query(RunStep).filter(RunStep.run_id == run.id,
                                        RunStep.seq == 1).all()
        assert len(rows) == 1
        assert rows[0].kind == "load_context"
        # the placeholder carries the sealed input payload but no output of its own
        assert rows[0].input_payload_id is not None
        assert rows[0].output_payload_id is None
        assert rows[0].execution_turn_id is None
        db.close()
    finally:
        eng.dispose()


@pytest.mark.parametrize("mode", NON_CHAT_MODES)
def test_idempotent_replay_does_not_mutate_or_duplicate_the_placeholder(eng, mode):
    db = mk_session(eng)
    try:
        _seed(db)
        run1, created1 = _create(db, mode=mode, key="J9-REPLAY-" + mode)
        before = _seq1(db, run1.id)
        snapshot = (before.status, before.id, before.finished_at)
        run2, created2 = _create(db, mode=mode, key="J9-REPLAY-" + mode)
        assert created1 is True and created2 is False
        assert run2.id == run1.id
        after = _seq1(db, run2.id)
        assert (after.status, after.id, after.finished_at) == snapshot
        assert db.query(RunStep).filter(RunStep.run_id == run1.id,
                                        RunStep.seq == 1).count() == 1
        db.close()
    finally:
        eng.dispose()
