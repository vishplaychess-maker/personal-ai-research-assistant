"""Phase 1B.2R MP-1B — expired-lease recovery / restart semantics."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.config import settings
from app.run_engine.engine import submit_run, transition_run
from app.run_engine.worker import (
    HandlerContext,
    HandlerResult,
    RunWorker,
    clear_handlers,
    register_handler,
)
from tests.run_engine_helpers import (
    Clock,
    make_ctx,
    mk_engine,
    mk_factory,
    mk_session,
    run_snapshot,
)


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_handlers()
    yield
    clear_handlers()


class _Recorder:
    def __init__(self, *, ok=True):
        self.calls: list[HandlerContext] = []
        self._ok = ok

    def __call__(self, ctx: HandlerContext) -> HandlerResult:
        self.calls.append(ctx)
        return HandlerResult(ok=self._ok)


def _queued_run(eng, *, kind="chat_sync") -> str:
    db = mk_session(eng)
    try:
        ctx = make_ctx(db, sid="fam-alpha")
        run = submit_run(db, ctx, idempotency_key=uuid4().hex, session_id=1, kind=kind)
        transition_run(db, ctx, run.id, to="queued", expected_version=0)
        return run.id
    finally:
        db.close()


def _claim_then_expire(eng, rid, clock, *, worker_id="w-old"):
    RunWorker(mk_factory(eng), worker_id=worker_id, now_fn=clock).claim_next_queued()
    clock.advance(int(settings.run_worker_lease_ttl_s) + 5)


# ── recovery outcomes ─────────────────────────────────────────────


def test_expired_retry_safe_work_is_reclaimed(eng):
    rid = _queued_run(eng)
    rec = _Recorder(ok=True)
    register_handler("chat_sync", rec, retry_safe=True)
    clock = Clock()
    _claim_then_expire(eng, rid, clock)

    (out,) = RunWorker(mk_factory(eng), worker_id="w-new", now_fn=clock).recover_expired()

    assert out.status == "succeeded"
    assert [c.attempt for c in rec.calls] == [1]           # reclaim marks a retry
    snap = run_snapshot(eng, rid)
    assert snap["status"] == "succeeded" and snap["lease_owner"] is None


def test_expired_unknown_work_becomes_outcome_unknown(eng):
    rid = _queued_run(eng)                                 # no handler registered
    clock = Clock()
    _claim_then_expire(eng, rid, clock)

    (out,) = RunWorker(mk_factory(eng), worker_id="w-n", now_fn=clock).recover_expired()

    assert out.status == "outcome_unknown"
    snap = run_snapshot(eng, rid)
    assert snap["status"] == "outcome_unknown"
    assert snap["status_reason"] == "outcome_unknown"
    assert snap["lease_owner"] is None and snap["finished_at"] is not None


def test_expired_non_retry_safe_work_is_not_rerun(eng):
    rid = _queued_run(eng)
    rec = _Recorder(ok=True)
    register_handler("chat_sync", rec, retry_safe=False)
    clock = Clock()
    _claim_then_expire(eng, rid, clock)

    (out,) = RunWorker(mk_factory(eng), worker_id="w-n", now_fn=clock).recover_expired()

    assert out.status == "outcome_unknown"
    assert rec.calls == []                                 # never auto-retried
    assert run_snapshot(eng, rid)["status"] == "outcome_unknown"


def test_restart_resumes_only_retry_safe_work(eng):
    safe = _queued_run(eng, kind="chat_sync")       # has a retry-safe handler
    unsafe = _queued_run(eng, kind="scheduled")     # no handler registered
    rec = _Recorder(ok=True)
    register_handler("chat_sync", rec, retry_safe=True)
    clock = Clock()
    _claim_then_expire(eng, safe, clock, worker_id="w-1")
    RunWorker(mk_factory(eng), worker_id="w-2", now_fn=clock).claim_next_queued()  # claims 'unsafe'
    clock.advance(int(settings.run_worker_lease_ttl_s) + 5)

    outcomes = {o.run_id: o.status
                for o in RunWorker(mk_factory(eng), worker_id="w-fresh",
                                   now_fn=clock).recover_expired()}

    assert outcomes[safe] == "succeeded"
    assert outcomes[unsafe] == "outcome_unknown"
    assert [c.run_id for c in rec.calls] == [safe]


def test_recovery_past_deadline_stops_retrying(eng):
    rid = _queued_run(eng)
    rec = _Recorder(ok=True)
    register_handler("chat_sync", rec, retry_safe=True)
    clock = Clock()
    RunWorker(mk_factory(eng), worker_id="w-old", now_fn=clock).claim_next_queued()
    # run_deadline_at was set to now + handler_timeout * max_attempts at claim
    clock.advance(int(settings.run_worker_handler_timeout_s)
                  * int(settings.run_worker_max_attempts) + 60)

    (out,) = RunWorker(mk_factory(eng), worker_id="w-new", now_fn=clock).recover_expired()

    assert out.status == "failed" and out.detail == "deadline_exceeded"
    assert rec.calls == []
    assert run_snapshot(eng, rid)["status_reason"] == "deadline_exceeded"


def test_recover_batch_is_bounded(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_worker_recover_batch", 1)
    clock = Clock()
    for _ in range(2):
        rid = _queued_run(eng)
        RunWorker(mk_factory(eng), now_fn=clock).claim_next_queued()
    clock.advance(int(settings.run_worker_lease_ttl_s) + 5)

    outs = RunWorker(mk_factory(eng), now_fn=clock).recover_expired()
    assert len(outs) == 1
