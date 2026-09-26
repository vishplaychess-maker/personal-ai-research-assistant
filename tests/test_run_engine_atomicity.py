"""Phase 1B.2R MP-1A — transaction atomicity of state / event / signal writes."""

from __future__ import annotations

import pytest

import app.run_engine.engine as eng_mod
from app.models.models import Run, RunEvent, RunSignal
from app.run_engine.engine import request_cancellation, submit_run, transition_run
from app.run_engine.errors import (
    CancellationInProgress,
    RunStateConflict,
    RunSubmitContention,
)
from tests.run_engine_helpers import make_ctx, mk_engine, mk_session


@pytest.fixture
def db():
    eng = mk_engine()
    s = mk_session(eng)
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def test_submit_rolls_back_run_when_run_created_event_cannot_be_written(db, monkeypatch):
    ctx = make_ctx(db)
    monkeypatch.setattr(eng_mod, "_try_append_event", lambda *a, **k: None)
    with pytest.raises(RunSubmitContention):
        submit_run(db, ctx, idempotency_key="k", session_id=1)
    db.rollback()
    assert db.query(Run).count() == 0
    assert db.query(RunEvent).count() == 0


def test_failed_cas_writes_no_status_event_and_no_version_bump(db):
    ctx = make_ctx(db)
    run = submit_run(db, ctx, idempotency_key="k", session_id=1)
    transition_run(db, ctx, run.id, to="queued", expected_version=0)

    before_events = db.query(RunEvent).filter(RunEvent.run_id == run.id).count()
    with pytest.raises(RunStateConflict):
        transition_run(db, ctx, run.id, to="running", expected_version=0)  # stale
    db.rollback()
    assert db.query(RunEvent).filter(RunEvent.run_id == run.id).count() == before_events
    assert db.query(Run).filter(Run.id == run.id).one().version == 1  # only the one bump


def test_cancellation_signal_and_event_commit_together_or_not_at_all(db, monkeypatch):
    ctx = make_ctx(db, sid="fam-alpha")
    run = submit_run(db, ctx, idempotency_key="k", session_id=1)

    # make the run.cancel.requested event impossible to write
    monkeypatch.setattr(eng_mod, "_try_append_event", lambda *a, **k: None)
    with pytest.raises(CancellationInProgress):
        request_cancellation(db, ctx, run.id)
    db.rollback()

    # neither the live signal nor the event survived
    assert db.query(RunSignal).filter(RunSignal.run_id == run.id).count() == 0
    assert (
        db.query(RunEvent)
        .filter(RunEvent.run_id == run.id, RunEvent.type == "run.cancel.requested")
        .count()
        == 0
    )
    db.refresh(run)
    assert run.status == "created"  # run itself untouched


def test_successful_cancellation_commits_exactly_one_signal_and_one_event(db):
    ctx = make_ctx(db, sid="fam-alpha")
    run = submit_run(db, ctx, idempotency_key="k", session_id=1)
    res = request_cancellation(db, ctx, run.id, reason_code="user_abort")
    assert res.signal_created is True
    assert db.query(RunSignal).filter(RunSignal.run_id == run.id).count() == 1
    assert (
        db.query(RunEvent)
        .filter(RunEvent.run_id == run.id, RunEvent.type == "run.cancel.requested")
        .count()
        == 1
    )
