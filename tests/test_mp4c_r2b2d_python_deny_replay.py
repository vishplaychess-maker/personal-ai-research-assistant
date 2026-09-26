"""MP-4C-R2B-2D — Python denial and decision-replay (focused, no subprocess)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import DispatchRecord, Message, RunPayload, RunStep
from tests.run_engine_helpers import mk_engine, mk_session, revoke_family
from tests.test_run_engine_mp3c2 import _decide_run
from tests import test_mp4c_r2b2c2_python_routing as rt
from tests import test_mp4c_r2a3_python_proposal_worker as r2a3

OBSERVED = {}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "mp4c-r2b2d-key-0123456789")
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 30)
    monkeypatch.setattr(settings, "run_worker_handler_timeout_s", 300)
    monkeypatch.setattr(settings, "run_chat_max_tool_cycles", 3)
    monkeypatch.setattr(settings, "run_chat_tool_marker_max_bytes", 4096)
    monkeypatch.setattr(settings, "run_engine_sandbox", False)


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


def _snapshot(eng, rid):
    def _f(db):
        return {
            "dispatch": [d.outcome for d in db.query(DispatchRecord).all()],
            "payloads": db.query(RunPayload).filter(RunPayload.run_id == rid).count(),
            "assistants": len([m for m in db.query(Message).all()
                               if str(m.role).endswith("assistant")]),
        }
    return rt._q(eng, _f)


def _deny(eng, monkeypatch, wid="d-deny"):
    rid, prov, tr = rt._park_python(eng, monkeypatch)
    calls = []
    aid = rt._run(eng, rid).pending_approval_id
    _decide_run(eng, aid, approve=False)
    w = rt._worker(eng, prov, tr, wid, python_executor=lambda c: calls.append(c))
    w.advance_chat_runs()
    return rid, prov, tr, calls, w, aid


def test_deny_zero_execution_and_real_terminal_state(eng, monkeypatch):
    rid, prov, tr, calls, _w, aid = _deny(eng, monkeypatch)
    run = rt._run(eng, rid)
    st = r2a3._tool_step(eng, rid)
    OBSERVED["deny_run"] = (run.status, run.status_reason)
    OBSERVED["deny_step"] = (st.status, st.error_code)
    assert run.status == "denied", (run.status, run.status_reason)
    # The denied transition persists NO run-level reason; the bounded reason is
    # carried on the step as "approval_denied" (asserted next). Verified by
    # execution: runs.status_reason is None for the denied terminal.
    assert run.status_reason is None, run.status_reason
    assert st.status == "blocked" and st.error_code == "approval_denied", (st.status, st.error_code)
    assert calls == [], "denial must never call the Python executor"
    assert tr.calls == [], "denial must never use the MCP transport"
    snap = _snapshot(eng, rid)
    OBSERVED["deny_snapshot"] = snap
    assert "started" not in snap["dispatch"], snap
    assert snap["assistants"] == 0, "denial must not produce a success Message"
    assert len(prov.calls) == 1, "no continuation provider call after denial"


def test_deny_replay_is_idempotent_and_non_executing(eng, monkeypatch):
    rid, prov, tr, calls, w, aid = _deny(eng, monkeypatch, "d-replay")
    before = _snapshot(eng, rid)
    # EXACT contract, verified by execution: a repeated denial is an idempotent
    # no-op that RETURNS {'state': 'denied', 'idempotent': True}; it does not raise.
    res = _decide_run(eng, aid, approve=False)
    assert res["state"] == "denied" and res["idempotent"] is True, res
    w.advance_chat_runs()
    run = rt._run(eng, rid)
    assert run.status == "denied", (run.status, run.status_reason)
    assert calls == [] and tr.calls == []
    assert _snapshot(eng, rid) == before, "replay must change no rows"
    assert len(prov.calls) == 1


def test_approve_after_deny_cannot_execute(eng, monkeypatch):
    rid, prov, tr, calls, w, aid = _deny(eng, monkeypatch, "d-aad")
    before = _snapshot(eng, rid)
    # EXACT contract, verified by execution: approving after a denial RAISES
    # HTTPException 409 "already denied" (this is the decision layer; the HTTP
    # API surfaces it as a 409 response).
    import fastapi
    with pytest.raises(fastapi.HTTPException) as ei:
        _decide_run(eng, aid, approve=True)
    assert ei.value.status_code == 409, ei.value.status_code
    assert "already denied" in str(ei.value.detail), ei.value.detail
    w.advance_chat_runs()
    run = rt._run(eng, rid)
    assert run.status != "waiting_for_approval", run.status
    assert calls == [], "approve-after-deny must never execute Python"
    assert tr.calls == []
    assert "started" not in _snapshot(eng, rid)["dispatch"]
    assert _snapshot(eng, rid) == before


def test_advance_after_denial_adds_nothing(eng, monkeypatch):
    rid, prov, tr, calls, w, aid = _deny(eng, monkeypatch, "d-adv")
    before = _snapshot(eng, rid)
    for _ in range(3):
        w.advance_chat_runs()
    assert _snapshot(eng, rid) == before
    assert len(prov.calls) == 1 and calls == []


def test_approve_control_reaches_the_executor_seam(eng, monkeypatch):
    """Control: the injected Windows seam IS reachable, so the deny assertions
    above cannot pass merely because execution is unreachable."""
    rid, prov, tr = rt._park_python(eng, monkeypatch)
    prov.scripts.append(["AFTER-APPROVE"])
    calls = []
    aid = rt._run(eng, rid).pending_approval_id
    _decide_run(eng, aid, approve=True)
    class _O:
        def __init__(s, t, e):
            s.text, s.is_error = t, e

    w = rt._worker(eng, prov, tr, "d-ctrl",
                   python_executor=lambda c: (calls.append(c) or _O("CTRL-OK", False)))
    for _ in range(6):
        w.advance_chat_runs()
        if rt._run(eng, rid).status in ("succeeded", "failed", "outcome_unknown", "denied"):
            break
    assert len(calls) == 1, "the approve seam must be reachable"

# ══ MP-4C-R2B-2E — ActionLog denial chain ═══════════════════════════
def test_action_log_chain_verifies_and_denial_logs_no_execution(eng, monkeypatch):
    from app.approval.action_log import chain_scope_id, verify_chain
    from app.models.models import ActionLogEntry

    rid, prov, tr, calls, _w, aid = _deny(eng, monkeypatch, "al-deny")
    scope = chain_scope_id(tenant_id=None, user_id=7)
    rows = rt._q(eng, lambda db: [
        (e.seq, e.event_type) for e in db.query(ActionLogEntry)
        .filter(ActionLogEntry.chain_scope_id == scope)
        .order_by(ActionLogEntry.seq).all()])
    # Exact persisted events for a denied Python proposal (runtime-verified).
    assert rows == [(1, "approval.requested"), (2, "approval.denied")], rows
    assert not any("execution" in (e or "") for _, e in rows), \
        "denial must create NO tool execution record"
    report = rt._q(eng, lambda db: verify_chain(db, tenant_id=None, user_id=7))
    assert report.ok is True, report
    assert report.count == 2, report
    assert report.break_at is None, report


# ══ MP-4C-R2B-2E — Python approval expiry ═══════════════════════════
def test_expired_python_approval_cannot_execute(eng, monkeypatch):
    import datetime

    import fastapi
    from app.models.models import PendingApproval

    rid, prov, tr = rt._park_python(eng, monkeypatch)
    aid = rt._run(eng, rid).pending_approval_id
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)

    def _expire(db):
        db.query(PendingApproval).filter(PendingApproval.id == aid) \
            .update({"expires_at": past})
        db.commit()

    rt._q(eng, _expire)
    # Exact decision contract, runtime-verified: expired approval -> HTTP 409.
    with pytest.raises(fastapi.HTTPException) as ei:
        _decide_run(eng, aid, approve=True)
    assert ei.value.status_code == 409, ei.value.status_code
    assert "expired" in str(ei.value.detail).lower(), ei.value.detail

    calls = []
    w = rt._worker(eng, prov, tr, "e-exp", python_executor=lambda c: calls.append(c))
    w.advance_chat_runs()
    run = rt._run(eng, rid)
    # POLICY (shared lifecycle): an expired waiting approval terminalizes.
    assert run.status == "failed", (run.status, run.status_reason)
    assert run.status_reason == "approval_expired", run.status_reason
    st = r2a3._tool_step(eng, rid)
    assert st.status == "blocked" and st.error_code == "approval_expired", (st.status, st.error_code)
    assert run.lease_owner is None, "the waiting lease must be released"
    assert calls == [], "an expired approval must never reach the Python executor"
    assert tr.calls == [], "an expired approval must never use the MCP transport"
    snap = _snapshot(eng, rid)
    assert "started" not in snap["dispatch"], snap
    assert snap["assistants"] == 0, "no assistant success Message"
    assert len(prov.calls) == 1, "no continuation provider call"

# ══ MP-4C — shared expired-approval reconciliation (MCP) ════════════
def _expire_approval(eng, aid, hours=1):
    import datetime
    from app.models.models import PendingApproval
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    def _f(db):
        db.query(PendingApproval).filter(PendingApproval.id == aid) \
            .update({"expires_at": past})
        db.commit()
    rt._q(eng, _f)


def test_mcp_expired_waiting_approval_terminalizes_without_execution(eng):
    tr = rt.Transport()
    prov = rt.FakeChat([rt._mk_marker_script("checking. ", {"q": 1}), ["final"]])
    rid = rt._park(eng, prov, tr)
    aid = rt._run(eng, rid).pending_approval_id
    _expire_approval(eng, aid)
    w = rt._worker(eng, prov, tr, "mcp-exp")
    w.advance_chat_runs()
    run = rt._run(eng, rid)
    st = rt._tool_step(eng, rid)
    assert run.status == "failed" and run.status_reason == "approval_expired", \
        (run.status, run.status_reason)
    assert st.status == "blocked" and st.error_code == "approval_expired", (st.status, st.error_code)
    assert tr.calls == [], "expiry must never invoke the MCP transport"
    assert run.lease_owner is None, "lease must be released"
    snap = _snapshot(eng, rid)
    assert "started" not in snap["dispatch"], snap
    assert snap["assistants"] == 0
    assert len(prov.calls) == 1


def test_expiry_repeat_sweeps_and_late_approval_are_idempotent(eng, monkeypatch):
    rid, prov, tr = rt._park_python(eng, monkeypatch)
    aid = rt._run(eng, rid).pending_approval_id
    _expire_approval(eng, aid)
    calls = []
    w = rt._worker(eng, prov, tr, "exp-idem", python_executor=lambda c: calls.append(c))
    w.advance_chat_runs()
    after_first = _snapshot(eng, rid)
    for _ in range(3):
        w.advance_chat_runs()
    assert _snapshot(eng, rid) == after_first, "repeat sweeps must be idempotent"
    assert calls == [] and tr.calls == []
    assert rt._run(eng, rid).status == "failed"
    # a late approval attempt cannot resurrect or execute the action
    import fastapi
    try:
        _decide_run(eng, aid, approve=True)
    except fastapi.HTTPException as exc:
        assert exc.status_code in (409, 400), exc.status_code
    w.advance_chat_runs()
    assert calls == [] and tr.calls == []
    assert rt._run(eng, rid).status == "failed"


def test_expiry_does_not_overwrite_a_started_dispatch_record(eng, monkeypatch):
    """Pre-existing started ledger keeps its at-most-once recovery outcome."""
    import datetime
    import uuid
    from app.models.models import DispatchRecord

    rid, prov, tr = rt._park_python(eng, monkeypatch)
    step = r2a3._tool_step(eng, rid)
    aid = rt._run(eng, rid).pending_approval_id
    _expire_approval(eng, aid)

    def _started(db):
        db.add(DispatchRecord(id=str(uuid.uuid4()), tool_call_id=step.tool_call_id,
                              action_hash="d" * 64, outcome="started", user_id=7,
                              started_at=datetime.datetime.now(datetime.timezone.utc)))
        db.commit()

    rt._q(eng, _started)
    calls = []
    w = rt._worker(eng, prov, tr, "exp-ledger", python_executor=lambda c: calls.append(c))
    w.advance_chat_runs()
    run = rt._run(eng, rid)
    assert run.status_reason != "approval_expired", run.status_reason
    outcomes = rt._q(eng, lambda db: [d.outcome for d in db.query(DispatchRecord).all()])
    assert outcomes == ["started"], "the ledger outcome must not be overwritten"

# ══ MP-4C — signal present while its approval is expired ════════════
def _expire_after_signal(eng, aid):
    """INTEGRITY / RACE fixture (explicitly labelled).

    A real approval_decided signal can only be minted while the approval is
    still unexpired (the decision path raises ApprovalExpired otherwise), so
    this mutates the durable expires_at AFTER a real signal exists. It is NOT a
    fabricated signal and NOT an invalid public flow.
    """
    import datetime
    from app.models.models import PendingApproval
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)

    def _f(db):
        db.query(PendingApproval).filter(PendingApproval.id == aid) \
            .update({"expires_at": past})
        db.commit()

    rt._q(eng, _f)


def test_expired_approval_with_real_signal_does_not_execute_python(eng, monkeypatch):
    rid, prov, tr = rt._park_python(eng, monkeypatch)
    aid = rt._run(eng, rid).pending_approval_id
    _decide_run(eng, aid, approve=True)          # real signal, while unexpired
    _expire_after_signal(eng, aid)               # integrity/race mutation
    calls = []
    w = rt._worker(eng, prov, tr, "sig-exp-py", python_executor=lambda c: calls.append(c))
    w.advance_chat_runs()
    run = rt._run(eng, rid)
    st = r2a3._tool_step(eng, rid)
    snap = _snapshot(eng, rid)
    print("RACE_PY run=", run.status, run.status_reason, "step=", st.status,
          st.error_code, "dispatch=", snap["dispatch"], "assistants=", snap["assistants"],
          "exec=", len(calls), "transport=", len(tr.calls))
    assert calls == [], "an expired approval must NOT authorize Python execution"
    assert tr.calls == [], "an expired approval must not use the MCP transport"
    assert "started" not in snap["dispatch"], snap
    assert snap["assistants"] == 0, "no assistant success Message"
    w.advance_chat_runs()
    assert calls == [], "repeat advance must not execute"
    assert _snapshot(eng, rid)["assistants"] == 0
    # Runtime-verified states (executed): the signal is CONSUMED and the approval
    # is transitioned to EXPIRED by the gateway/state-machine expiry authority.
    from app.models.models import PendingApproval as _PA, RunSignal as _RS
    sigs = rt._q(eng, lambda db: [(s.kind, s.consumed_at is not None) for s in
                db.query(_RS).filter(_RS.run_id == rid).all()])
    assert sigs == [("approval_decided", True)], sigs
    appr = rt._q(eng, lambda db: db.query(_PA).filter(_PA.id == aid).one().state)
    assert appr == "expired", appr
    assert rt._q(eng, lambda db: db.query(DispatchRecord).count()) == 0


def test_expired_approval_with_real_signal_does_not_execute_mcp(eng):
    tr = rt.Transport()
    prov = rt.FakeChat([rt._mk_marker_script("checking. ", {"q": 1}), ["final"]])
    rid = rt._park(eng, prov, tr)
    aid = rt._run(eng, rid).pending_approval_id
    _decide_run(eng, aid, approve=True)
    _expire_after_signal(eng, aid)
    w = rt._worker(eng, prov, tr, "sig-exp-mcp")
    w.advance_chat_runs()
    run = rt._run(eng, rid)
    st = rt._tool_step(eng, rid)
    snap = _snapshot(eng, rid)
    print("RACE_MCP run=", run.status, run.status_reason, "step=", st.status,
          st.error_code, "dispatch=", snap["dispatch"], "assistants=", snap["assistants"],
          "transport=", len(tr.calls))
    assert tr.calls == [], "an expired approval must NOT invoke the MCP transport"
    assert "started" not in snap["dispatch"], snap
    assert snap["assistants"] == 0
    assert snap["dispatch"] == [], "an expired approval must write no dispatch ledger row"
    w.advance_chat_runs()
    assert tr.calls == [], "repeat advance must not invoke the transport"
    # Runtime-verified states (executed): the signal is CONSUMED and the approval
    # is transitioned to EXPIRED by the gateway/state-machine expiry authority.
    from app.models.models import PendingApproval as _PA, RunSignal as _RS
    sigs = rt._q(eng, lambda db: [(s.kind, s.consumed_at is not None) for s in
                db.query(_RS).filter(_RS.run_id == rid).all()])
    assert sigs == [("approval_decided", True)], sigs
    appr = rt._q(eng, lambda db: db.query(_PA).filter(_PA.id == aid).one().state)
    assert appr == "expired", appr
    assert rt._q(eng, lambda db: db.query(DispatchRecord).count()) == 0



# ══ MP-4C-R2B-2E — session revocation AFTER proposal ════════════════
def test_session_revocation_after_proposal_blocks_python_execution(eng, monkeypatch):
    """Guard exercised in _mcp_on_decision BEFORE the signal is consumed."""
    rid, prov, tr = rt._park_python(eng, monkeypatch)
    rt._q(eng, lambda db: revoke_family(db, "fam-alpha"))
    calls = []
    aid = rt._run(eng, rid).pending_approval_id
    _decide_run(eng, aid, approve=True)
    w = rt._worker(eng, prov, tr, "e-rev", python_executor=lambda c: calls.append(c))
    w.advance_chat_runs()
    run = rt._run(eng, rid)
    assert run.status == "failed", (run.status, run.status_reason)
    assert run.status_reason == "session_revoked", run.status_reason
    assert calls == [], "a revoked session must never reach the Python executor"
    assert tr.calls == [], "a revoked session must never use the MCP transport"
    snap = _snapshot(eng, rid)
    assert "started" not in snap["dispatch"], snap
    assert snap["assistants"] == 0, "no assistant success Message"
