"""Phase 1B.2R MP-1B — RunWorker claim / lease / cancellation core."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.config import settings
from app.run_engine.engine import request_cancellation, submit_run, transition_run
from app.run_engine.worker import (
    HandlerContext,
    HandlerResult,
    RunWorker,
    clear_handlers,
    register_handler,
    resolve_handler,
)
from tests.run_engine_helpers import (
    Clock,
    event_blob,
    make_ctx,
    mk_engine,
    mk_factory,
    mk_session,
    revoke_family,
    run_snapshot,
    signal_rows,
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


def _queued_run(eng, *, user_id=7, sid="fam-alpha", kind="chat_sync") -> str:
    db = mk_session(eng)
    try:
        ctx = make_ctx(db, user_id=user_id, sid=sid)
        run = submit_run(db, ctx, idempotency_key=uuid4().hex, session_id=1, kind=kind)
        transition_run(db, ctx, run.id, to="queued", expected_version=0)
        return run.id
    finally:
        db.close()


def _add_cancel(eng, run_id, *, user_id=7, sid="fam-alpha"):
    db = mk_session(eng)
    try:
        request_cancellation(db, make_ctx(db, user_id=user_id, sid=sid), run_id,
                             reason_code="user_abort")
    finally:
        db.close()


class _Recorder:
    def __init__(self, *, ok=True, error_code=None, boom=None):
        self.calls: list[HandlerContext] = []
        self._ok, self._code, self._boom = ok, error_code, boom

    def __call__(self, ctx: HandlerContext) -> HandlerResult:
        self.calls.append(ctx)
        if self._boom is not None:
            raise RuntimeError(self._boom)
        return HandlerResult(ok=self._ok, error_code=self._code)


# ── claim ───────────────────────────────────────────────────────────


def test_claim_moves_queued_to_running_under_a_lease(eng):
    rid = _queued_run(eng)
    w = RunWorker(mk_factory(eng), worker_id="w-a")
    claimed = w.claim_next_queued()

    assert claimed is not None and claimed.run_id == rid
    assert claimed.lease_owner.startswith("w-a/")
    snap = run_snapshot(eng, rid)
    assert snap["status"] == "running"
    assert snap["lease_owner"] == claimed.lease_owner
    assert snap["version"] == claimed.version >= 2       # submit(0) -> queued(1) -> running(2)
    assert snap["last_event_seq"] >= 2                   # run.created + run.status
    assert snap["run_deadline_at"] is not None


def test_two_claimers_produce_one_owner(eng):
    rid = _queued_run(eng)
    w1 = RunWorker(mk_factory(eng), worker_id="w-one")
    w2 = RunWorker(mk_factory(eng), worker_id="w-two")

    a = w1.claim_next_queued()
    b = w2.claim_next_queued()

    assert a is not None and b is None
    assert run_snapshot(eng, rid)["lease_owner"].startswith("w-one/")


def test_current_owner_can_renew_stale_token_cannot(eng):
    rid = _queued_run(eng)
    clock = Clock()
    w = RunWorker(mk_factory(eng), worker_id="w-r", now_fn=clock)
    claimed = w.claim_next_queued()
    before = run_snapshot(eng, rid)["lease_expires_at"]

    clock.advance(10)
    assert w.renew_lease(rid, claimed.lease_owner) is True
    assert run_snapshot(eng, rid)["lease_expires_at"] > before

    assert w.renew_lease(rid, "w-r/deadbeef") is False   # stale token
    assert w.renew_lease(rid, "someone-else/x") is False


def test_stale_worker_cannot_touch_a_reclaimed_run(eng):
    """A worker holding an expired lease cannot run the handler or mutate a run
    that another worker reclaimed — its execute() abandons before invocation,
    the reclaimer's result stands."""
    rid = _queued_run(eng)
    rec = _Recorder(ok=True)
    register_handler("chat_sync", rec, retry_safe=True)
    clock = Clock()

    w1 = RunWorker(mk_factory(eng), worker_id="w-old", now_fn=clock)
    stale = w1.claim_next_queued()

    clock.advance(int(settings.run_worker_lease_ttl_s) + 5)
    w2 = RunWorker(mk_factory(eng), worker_id="w-new", now_fn=clock)
    (out,) = w2.recover_expired()
    assert out.status == "succeeded"

    abandoned = w1.execute(stale)                # stale lease token + stale version
    assert abandoned.status == "abandoned"
    assert len(rec.calls) == 1                   # only the reclaimer ran the handler
    assert run_snapshot(eng, rid)["status"] == "succeeded"   # unchanged by w1


# ── handler resolution ─────────────────────────────────────────────


def test_registry_ships_empty(eng):
    assert resolve_handler("chat_sync") is None
    assert resolve_handler("anything") is None


def test_unknown_handler_fails_closed_and_runs_nothing(eng):
    rid = _queued_run(eng)
    w = RunWorker(mk_factory(eng), worker_id="w-u")
    out = w.execute(w.claim_next_queued())

    assert out.status == "failed" and out.detail == "unknown_handler"
    snap = run_snapshot(eng, rid)
    assert snap["status"] == "failed" and snap["status_reason"] == "unknown_handler"
    assert snap["lease_owner"] is None and snap["finished_at"] is not None


def test_handler_success_completes_the_run(eng):
    rid = _queued_run(eng)
    rec = _Recorder(ok=True)
    register_handler("chat_sync", rec, retry_safe=True)

    w = RunWorker(mk_factory(eng), worker_id="w-ok")
    out = w.execute(w.claim_next_queued())

    assert out.status == "succeeded"
    assert [c.run_id for c in rec.calls] == [rid]
    assert rec.calls[0].attempt == 0
    snap = run_snapshot(eng, rid)
    assert snap["status"] == "succeeded" and snap["lease_owner"] is None


def test_handler_exception_stores_only_a_bounded_code(eng):
    rid = _queued_run(eng)
    register_handler("chat_sync", _Recorder(boom="boom SECRET-abc123 detail"),
                     retry_safe=True)
    w = RunWorker(mk_factory(eng), worker_id="w-e")
    out = w.execute(w.claim_next_queued())

    assert out.status == "failed" and out.detail == "handler_error"
    snap = run_snapshot(eng, rid)
    assert snap["status_reason"] == "handler_error"
    assert "SECRET" not in (snap["status_reason"] or "")
    assert "SECRET" not in event_blob(eng, rid)
    assert "boom" not in event_blob(eng, rid)


def test_handler_reject_code_is_bounded(eng):
    rid = _queued_run(eng)
    register_handler("chat_sync", _Recorder(ok=False, error_code="made-up-code"),
                     retry_safe=True)
    w = RunWorker(mk_factory(eng), worker_id="w-x")
    w.execute(w.claim_next_queued())
    assert run_snapshot(eng, rid)["status_reason"] == "handler_error"

    rid2 = _queued_run(eng)
    register_handler("chat_sync", _Recorder(ok=False, error_code="handler_rejected"),
                     retry_safe=True)
    w.execute(w.claim_next_queued())
    assert run_snapshot(eng, rid2)["status_reason"] == "handler_rejected"


# ── cancellation ───────────────────────────────────────────────────


def test_cancel_before_invocation_blocks_the_handler(eng):
    rid = _queued_run(eng)
    rec = _Recorder(ok=True)
    register_handler("chat_sync", rec, retry_safe=True)

    w = RunWorker(mk_factory(eng), worker_id="w-c")
    claimed = w.claim_next_queued()
    _add_cancel(eng, rid)

    out = w.execute(claimed)
    assert out.status == "cancelled"
    assert rec.calls == []                       # handler never ran
    snap = run_snapshot(eng, rid)
    assert snap["status"] == "cancelled" and snap["status_reason"] == "cancelled"


def test_cancellation_signal_is_consumed_exactly_once(eng):
    rid = _queued_run(eng)
    w = RunWorker(mk_factory(eng), worker_id="w-c1")
    claimed = w.claim_next_queued()
    _add_cancel(eng, rid)

    w.execute(claimed)
    rows = signal_rows(eng, rid)
    assert len(rows) == 1 and rows[0]["consumed_at"] is not None
    consumed_at = rows[0]["consumed_at"]

    # a second sweep must not re-consume or re-touch the terminal run
    w.poll_once()
    rows2 = signal_rows(eng, rid)
    assert len(rows2) == 1 and rows2[0]["consumed_at"] == consumed_at
    assert run_snapshot(eng, rid)["status"] == "cancelled"


def test_signal_event_and_state_update_are_atomic(eng, monkeypatch):
    """If the second control-event write fails mid-cancel, nothing sticks:
    the signal stays unconsumed and the run stays running."""
    rid = _queued_run(eng)
    w = RunWorker(mk_factory(eng), worker_id="w-atom")
    claimed = w.claim_next_queued()
    _add_cancel(eng, rid)

    real = RunWorker._emit_status
    seen = {"n": 0}

    def flaky(self, db, run_id, tenant_id, user_id, frm, to):
        seen["n"] += 1
        if seen["n"] == 2:          # running->cancelling ok; cancelling->cancelled fails
            return False
        return real(self, db, run_id, tenant_id, user_id, frm, to)

    monkeypatch.setattr(RunWorker, "_emit_status", flaky)
    out = w.execute(claimed)

    assert out.status == "abandoned"
    rows = signal_rows(eng, rid)
    assert len(rows) == 1 and rows[0]["consumed_at"] is None      # NOT consumed
    snap = run_snapshot(eng, rid)
    assert snap["status"] == "running"                            # unchanged
    assert snap["version"] == claimed.version


def test_cancel_after_side_effect_free_handler_still_wins(eng):
    rid = _queued_run(eng)

    def handler(ctx):
        # a cancel lands while this side-effect-free handler is "running"
        _add_cancel(eng, ctx.run_id)
        return HandlerResult(ok=True)

    register_handler("chat_sync", handler, retry_safe=True)
    w = RunWorker(mk_factory(eng), worker_id="w-cw")
    out = w.execute(w.claim_next_queued())

    assert out.status == "cancelled"
    assert signal_rows(eng, rid)[0]["consumed_at"] is not None


# ── session-family revalidation ────────────────────────────────────


def test_revoked_session_prevents_handler_invocation(eng):
    rid = _queued_run(eng)
    rec = _Recorder(ok=True)
    register_handler("chat_sync", rec, retry_safe=True)

    w = RunWorker(mk_factory(eng), worker_id="w-s")
    claimed = w.claim_next_queued()

    db = mk_session(eng)
    try:
        revoke_family(db, "fam-alpha")
    finally:
        db.close()

    out = w.execute(claimed)
    assert out.status == "failed" and out.detail == "session_revoked"
    assert rec.calls == []
    assert run_snapshot(eng, rid)["status_reason"] == "session_revoked"
