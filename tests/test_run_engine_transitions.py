"""Phase 1B.2R MP-1A — optimistic-concurrency state transitions."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.run_engine.engine import submit_run, transition_run
from app.run_engine.errors import (
    CancellationInProgress,
    InvalidRunTransition,
    RunAlreadyTerminal,
    RunStateConflict,
    SessionBindingRequired,
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


def _new_run(db, ctx):
    return submit_run(db, ctx, idempotency_key="k", session_id=1)


def test_legal_transition_bumps_version_exactly_once(db):
    ctx = make_ctx(db)
    run = _new_run(db, ctx)
    assert run.version == 0

    out = transition_run(db, ctx, run.id, to="queued", expected_version=0)
    assert out.status == "queued"
    assert out.version == 1

    out = transition_run(db, ctx, run.id, to="running", expected_version=1)
    assert out.status == "running"
    assert out.version == 2
    assert out.started_at is not None


def test_stale_version_raises_conflict(db):
    ctx = make_ctx(db)
    run = _new_run(db, ctx)
    transition_run(db, ctx, run.id, to="queued", expected_version=0)

    with pytest.raises(RunStateConflict) as ei:
        transition_run(db, ctx, run.id, to="running", expected_version=0)
    assert ei.value.expected_version == 0
    assert ei.value.actual_version == 1


def test_illegal_transition_is_refused(db):
    ctx = make_ctx(db)
    run = _new_run(db, ctx)
    with pytest.raises(InvalidRunTransition):
        transition_run(db, ctx, run.id, to="running", expected_version=0)


def test_terminal_state_is_protected(db):
    ctx = make_ctx(db)
    run = _new_run(db, ctx)
    transition_run(db, ctx, run.id, to="queued", expected_version=0)
    transition_run(db, ctx, run.id, to="running", expected_version=1)
    done = transition_run(db, ctx, run.id, to="succeeded", expected_version=2)
    assert done.status == "succeeded"
    assert done.finished_at is not None

    # re-asserting the SAME terminal state is an idempotent no-op
    same = transition_run(db, ctx, run.id, to="succeeded", expected_version=3)
    assert same.status == "succeeded"

    # any OTHER transition out of terminal is refused
    with pytest.raises(RunAlreadyTerminal):
        transition_run(db, ctx, run.id, to="failed", expected_version=3)


def test_cancellation_wins_a_transition_race(db):
    ctx = make_ctx(db)
    run = _new_run(db, ctx)
    transition_run(db, ctx, run.id, to="queued", expected_version=0)

    # simulate a concurrent cancellation landing first
    db.execute(
        text("UPDATE runs SET status='cancelling' WHERE id=:i"), {"i": run.id}
    )
    db.commit()

    with pytest.raises(CancellationInProgress):
        transition_run(db, ctx, run.id, to="running", expected_version=1)


def test_no_active_binding_fails_closed(db):
    ctx = make_ctx(db)
    run = _new_run(db, ctx)
    stale = make_ctx(db, binding_valid=False)
    with pytest.raises(SessionBindingRequired):
        transition_run(db, stale, run.id, to="queued", expected_version=0)
