"""MP-5 slice 1 - the synchronous wait-for-terminal adapter contract."""
from __future__ import annotations

import inspect

import pytest
from sqlalchemy import update

from app.models.models import Run
from app.run_engine import sync_wait
from app.run_engine.sync_wait import (SYNC_WAIT_TIMEOUT, SyncWaitError,
                                      wait_for_terminal)
from tests.run_engine_helpers import mk_engine, mk_session
from tests.test_run_engine_worker import _queued_run


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


def test_a_terminal_run_is_returned_immediately(eng):
    rid = _queued_run(eng)
    db = mk_session(eng)
    try:
        db.execute(update(Run).where(Run.id == rid).values(status="succeeded"))
        db.commit()
        run = wait_for_terminal(db, rid, deadline_s=5,
                                sleep_fn=lambda _s: pytest.fail("must not sleep"))
        assert run.id == rid and run.status == "succeeded"
    finally:
        db.close()


def test_the_deadline_fails_closed_with_the_observed_status(eng):
    rid = _queued_run(eng)
    db = mk_session(eng)
    try:
        with pytest.raises(SyncWaitError) as ei:
            wait_for_terminal(db, rid, deadline_s=0)
        assert ei.value.code == SYNC_WAIT_TIMEOUT
        assert ei.value.run_id == rid
        assert ei.value.status == "queued", ei.value.status
        assert ei.value.status not in ("succeeded",), "never a success claim"
    finally:
        db.close()


def test_a_missing_run_never_claims_success(eng):
    db = mk_session(eng)
    try:
        with pytest.raises(SyncWaitError) as ei:
            wait_for_terminal(db, "no-such-run", deadline_s=5)
        assert ei.value.status is None and ei.value.code == SYNC_WAIT_TIMEOUT
    finally:
        db.close()


def test_a_run_that_becomes_terminal_while_waiting_is_returned(eng):
    """The wait observes COMMITTED worker state, exactly like the worker writes it."""
    rid = _queued_run(eng)
    db = mk_session(eng)
    seen = {"sleeps": 0}

    def sleeper(_s):
        seen["sleeps"] += 1
        db.execute(update(Run).where(Run.id == rid).values(status="succeeded"))
        db.commit()

    try:
        run = wait_for_terminal(db, rid, deadline_s=5, poll_interval_s=0.01,
                                sleep_fn=sleeper)
        assert run.status == "succeeded"
        assert seen["sleeps"] == 1, "one poll, then the committed state was seen"
    finally:
        db.close()


def test_the_adapter_reuses_the_one_terminal_vocabulary():
    src = inspect.getsource(sync_wait)
    assert "is_run_terminal" in src, "the ONE terminal authority is reused"
    assert "RUN_TERMINAL = " not in src, "no second terminal set may exist here"
    assert "\"succeeded\"" not in src, "no literal terminal-state list in this module"


def test_the_adapter_never_creates_or_advances_a_run():
    src = inspect.getsource(sync_wait)
    for forbidden in ("RunWorker", "create_chat_run", "submit_run", "db.add("):
        assert forbidden not in src, forbidden