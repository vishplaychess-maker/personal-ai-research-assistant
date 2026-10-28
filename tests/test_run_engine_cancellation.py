"""Phase 1B.2R MP-1A — cancellation-request signal creation (service layer)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models.models import RunEvent, RunSignal
from app.run_engine.engine import request_cancellation, submit_run
from app.run_engine.errors import (
    RunNotFound,
    SessionBindingMismatch,
    SessionBindingRequired,
)
from app.run_engine.signals import parse_signal_payload
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


@pytest.fixture
def run(db):
    return submit_run(db, make_ctx(db, sid="fam-alpha"), idempotency_key="k", session_id=1)


def _signals(db, run_id):
    return db.query(RunSignal).filter(RunSignal.run_id == run_id).all()


def _cancel_events(db, run_id):
    return (
        db.query(RunEvent)
        .filter(RunEvent.run_id == run_id, RunEvent.type == "run.cancel.requested")
        .all()
    )


def test_first_request_creates_one_live_signal_and_one_event(db, run):
    ctx = make_ctx(db, sid="fam-alpha")
    res = request_cancellation(db, ctx, run.id, reason_code="user_abort")

    assert res.signal_created is True
    assert res.already_requested is False
    assert res.already_terminal is False

    sigs = _signals(db, run.id)
    assert len(sigs) == 1
    assert sigs[0].kind == "cancel_requested"
    assert sigs[0].consumed_at is None          # NOT consumed
    assert sigs[0].created_by_user_id == ctx.user_id
    assert parse_signal_payload("cancel_requested", sigs[0].payload)["reason_code"] == "user_abort"

    assert len(_cancel_events(db, run.id)) == 1

    db.refresh(run)
    assert run.status == "created"              # run is NOT marked cancelled


def test_duplicate_request_is_idempotent(db, run):
    ctx = make_ctx(db, sid="fam-alpha")
    request_cancellation(db, ctx, run.id)
    res = request_cancellation(db, ctx, run.id)

    assert res.signal_created is False
    assert res.already_requested is True
    assert len(_signals(db, run.id)) == 1
    assert len(_cancel_events(db, run.id)) == 1
    assert _signals(db, run.id)[0].consumed_at is None


def test_terminal_run_yields_deterministic_result_with_no_signal(db, run):
    db.execute(text("UPDATE runs SET status='succeeded' WHERE id=:i"), {"i": run.id})
    db.commit()

    res = request_cancellation(db, make_ctx(db, sid="fam-alpha"), run.id)
    assert res.already_terminal is True
    assert res.signal_created is False
    assert res.status == "succeeded"
    assert _signals(db, run.id) == []
    assert _cancel_events(db, run.id) == []

    db.refresh(run)
    assert run.status == "succeeded"            # untouched


def test_cross_user_request_is_not_found(db, run):
    with pytest.raises(RunNotFound):
        request_cancellation(db, make_ctx(db, user_id=999, sid="fam-alpha"), run.id)
    assert _signals(db, run.id) == []


def test_session_family_mismatch_is_refused(db, run):
    with pytest.raises(SessionBindingMismatch):
        request_cancellation(db, make_ctx(db, sid="fam-other"), run.id)
    assert _signals(db, run.id) == []


def test_no_active_binding_fails_closed(db, run):
    with pytest.raises(SessionBindingRequired):
        request_cancellation(db, make_ctx(db, binding_valid=False), run.id)  # legacy token, no sid
    assert _signals(db, run.id) == []
