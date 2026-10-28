"""MP-4C lifecycle review - Run.pending_approval_id on a TERMINAL run.

`Run.pending_approval_id` is written once, when the gateway parks the run at
`waiting_for_approval`, and is NEVER cleared. This file establishes, from the
source and through the REAL gateway rows, what that retained pointer IS:

  * the Run BOUND APPROVAL IDENTITY - every consumer is gated by the run status
    (`mcp_flow._MIRROR_STATES`) and/or the approval state machine, so it can never
    authorize anything on a terminal run;
  * NOT a live pending-state flag. It is therefore NOT cleared.

It also pins the one place where the retained pointer could still SURFACE:
`GET /api/runs/{run_id}/events` replays persisted events from `after_seq=0`, so a
replay of a terminal run re-walks the historical `waiting_for_approval` transition.
`messages_stream._map_event` must not turn that stale transition into a fresh
`tool_approval_required` frame for an already-decided approval.
"""
from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.approval import state_machine as _sm
from app.config import settings
from app.models.models import DispatchRecord, PendingApproval, Run, RunEvent
from app.run_engine import mcp_flow
from app.run_engine.messages_stream import _map_event
from app.routes.approvals import _decide
from app.run_engine.worker import RunWorker
from tests import test_mp4c_r2a3_python_proposal_worker as r2a3
from tests.run_engine_helpers import mk_engine, mk_factory
from tests.test_mp4c_predispatch_worker_order import (
    CountingBroker, _drive, _fake_executor)
from tests.test_mp4c_r2b2c2_python_routing import _park_python, _q
from tests.test_run_engine_mp3c2 import _decide_run


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "mp4c-pointer-lifecycle-key1")
    monkeypatch.setenv("EXECUTOR_SERVICE_GRANT_KEY", "pointer-lifecycle-grant-key-01")
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 30)
    monkeypatch.setattr(settings, "run_worker_handler_timeout_s", 300)
    monkeypatch.setattr(settings, "run_chat_max_tool_cycles", 3)
    monkeypatch.setattr(settings, "run_chat_tool_marker_max_bytes", 4096)
    monkeypatch.setattr(settings, "run_engine_sandbox", True)


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


def _park_event(eng, rid):
    """The persisted run.status -> waiting_for_approval event, or None."""
    def f(db):
        rows = (db.query(RunEvent)
                .filter(RunEvent.run_id == rid, RunEvent.category == "control",
                        RunEvent.type == "run.status")
                .order_by(RunEvent.run_seq.asc()).all())
        for r in rows:
            try:
                if json.loads(r.data or "{}").get("to_status") == "waiting_for_approval":
                    return r
            except (ValueError, TypeError):
                continue
        return None
    return _q(eng, f)


def _run(eng, rid):
    return _q(eng, lambda db: db.query(Run).filter(Run.id == rid).one())


def _approval(eng, aid):
    return _q(eng, lambda db: db.query(PendingApproval)
              .filter(PendingApproval.id == aid).one())


def _drive_succeeded(eng, monkeypatch, wid="ptr-ok"):
    """The approved journey end to end: park -> approve -> one real dispatch."""
    broker = CountingBroker()
    rid, _prov, _tr, worker = _drive(eng, monkeypatch, wid, broker)
    run = _run(eng, rid)
    assert run.status == "succeeded", (run.status, run.status_reason)
    return rid, run, broker


# ── 1. the retained pointer is the run BINDING IDENTITY, not a live flag ──


def test_the_pointer_is_retained_and_is_the_runs_bound_approval(eng, monkeypatch):
    rid, run, _broker = _drive_succeeded(eng, monkeypatch)
    assert run.pending_approval_id, "the binding identity is retained by design"
    row = _approval(eng, run.pending_approval_id)
    assert row.state == _sm.CONSUMED
    assert row.run_id == rid


def test_the_decision_mirror_refuses_a_terminal_run(eng, monkeypatch):
    """The mirror refuses a terminal run BEFORE it looks at the pointer, so the
    retained id cannot mint a new decision signal."""
    rid, run, _broker = _drive_succeeded(eng, monkeypatch)
    row = _approval(eng, run.pending_approval_id)
    assert run.status not in mcp_flow._MIRROR_STATES
    added = _q(eng, lambda db: mcp_flow.mirror_decision_signal(
        db=db, approval=row, decision=_sm.APPROVED))
    assert added is False


# ── 2. a consumed approval cannot be decided again ────────────────────────


def test_a_consumed_approval_cannot_be_decided_again(eng, monkeypatch):
    rid, run, _broker = _drive_succeeded(eng, monkeypatch)
    aid = run.pending_approval_id
    for approve in (True, False):
        def f(db, _a=approve):
            row = db.query(PendingApproval).filter(PendingApproval.id == aid).one()
            with pytest.raises(HTTPException) as ei:
                _decide(db, row, approve=_a)
            return ei.value.status_code, ei.value.detail
        code, detail = _q(eng, f)
        assert code == 409 and "consumed" in str(detail), (code, detail)


# ── 3. repeat advance must not execute again ──────────────────────────────


def test_repeat_advance_does_not_execute_again(eng, monkeypatch):
    from app.run_engine.worker import RunWorker
    from tests.run_engine_helpers import mk_factory
    rid, run, broker = _drive_succeeded(eng, monkeypatch)
    executes_before = len(broker.execute_calls)
    dispatches_before = _q(eng, lambda db: db.query(DispatchRecord).count())
    for _ in range(5):
        w = RunWorker(mk_factory(eng), worker_id="ptr-again",
                      chat_provider=(lambda ci, **_k: None), python_broker=broker,
                      broker_ready=(lambda: True))
        w.advance_chat_runs()
    assert len(broker.execute_calls) == executes_before
    assert _q(eng, lambda db: db.query(DispatchRecord).count()) == dispatches_before
    assert _run(eng, rid).status == "succeeded"


# ── 4. the stale park transition must not re-emit an approval frame ───────


def test_replaying_the_stale_park_event_does_not_re_emit_an_approval_frame(
        eng, monkeypatch):
    """GET /api/runs/{id}/events replays from after_seq=0. For a TERMINAL run the
    historical waiting_for_approval transition is stale and must not produce a
    fresh tool_approval_required frame carrying an already-consumed approval."""
    rid, run, _broker = _drive_succeeded(eng, monkeypatch)
    ev = _park_event(eng, rid)
    assert ev is not None, "the historical park event must exist"
    frame, terminal = _q(eng, lambda db: _map_event(db, run, ev))
    assert terminal is False
    assert frame is None or "tool_approval_required" not in frame, \
        "a replay of a terminal run must not re-open the approval dialog: %r" % (frame,)


def test_a_genuinely_waiting_run_still_emits_the_approval_frame(eng, monkeypatch):
    """The guard must not silence the LIVE case."""
    rid, _prov, _tr = _park_python(eng, monkeypatch)
    run = _run(eng, rid)
    assert run.status == "waiting_for_approval"
    ev = _park_event(eng, rid)
    frame, terminal = _q(eng, lambda db: _map_event(db, run, ev))
    assert terminal is False
    assert frame is not None and "tool_approval_required" in frame, frame
    assert run.pending_approval_id in frame


# ── 5. TWO parks in one Run: only the CURRENT approval may be presented ────


def _park_events(eng, rid):
    """Every persisted run.status -> waiting_for_approval event, in order."""
    def f(db):
        out = []
        rows = (db.query(RunEvent)
                .filter(RunEvent.run_id == rid, RunEvent.category == "control",
                        RunEvent.type == "run.status")
                .order_by(RunEvent.run_seq.asc()).all())
        for r in rows:
            try:
                if json.loads(r.data or "{}").get("to_status") == "waiting_for_approval":
                    out.append(r)
            except (ValueError, TypeError):
                continue
        return out
    return _q(eng, f)


def _drive_two_parks(eng, monkeypatch, wid="ptr-two"):
    """Real shape from the failed journey: park #1 -> approve -> execute -> the
    model proposes AGAIN -> park #2. The Run is left WAITING on approval #2 while
    approval #1 is already consumed."""
    rid, prov, tr = _park_python(eng, monkeypatch)
    prov.scripts.append(r2a3._py_script("", "print(3 + 3)"))
    aid1 = _run(eng, rid).pending_approval_id
    assert aid1
    _decide_run(eng, aid1, approve=True)
    broker = CountingBroker()
    w = RunWorker(mk_factory(eng), worker_id=wid,
                  chat_provider=(lambda ci, **_k: prov), mcp_transport=tr,
                  python_executor=_fake_executor, python_broker=broker,
                  broker_ready=(lambda: True))
    for _ in range(8):
        w.advance_chat_runs()
        cur = _run(eng, rid)
        if cur.pending_approval_id and cur.pending_approval_id != aid1:
            break
    run = _run(eng, rid)
    assert run.status == "waiting_for_approval", (run.status, run.status_reason)
    assert run.pending_approval_id != aid1, "the Run must be waiting on a SECOND approval"
    return rid, run, aid1, broker


def test_a_replayed_first_park_event_does_not_re_present_the_approval(eng, monkeypatch):
    """GET /api/runs/{id}/events replays from after_seq=0. A Run that already
    consumed approval #1 and is CURRENTLY waiting on approval #2 must present the
    actionable approval ONCE - the first historical park event must stay silent."""
    rid, run, aid1, _broker = _drive_two_parks(eng, monkeypatch)
    parks = _park_events(eng, rid)
    assert len(parks) == 2, "expected two park transitions, got %d" % len(parks)
    first, second = parks

    f1, _t1 = _q(eng, lambda db: _map_event(db, run, first))
    f2, _t2 = _q(eng, lambda db: _map_event(db, run, second))

    assert f1 is None or "tool_approval_required" not in f1, \
        "the superseded first park must not re-present an approval: %r" % (f1,)
    assert f2 is not None and "tool_approval_required" in f2, \
        "the CURRENT park must still present the approval: %r" % (f2,)
    assert run.pending_approval_id in f2
    assert aid1 not in f2, "the consumed first approval must not be presented"
