"""MP-4C-R2B-2A-T — approval-binding boundary tests (TEST ONLY).

Reuses the real RunWorker, MP-2 gateway, SQLAlchemy models and the existing
MP-3C2 / R2A-3 fixtures. A counting fake transport stands in for any external
call, so nothing launches Python or contacts an MCP server.

These tests interrogate the binding between the waiting Run, its tool_dispatch
step, and the approved action. Expected outcomes are recorded per test; a test
that PASSES because an existing protection blocks execution is reported as a
protection, not forced to RED.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import DispatchRecord, PendingApproval, Run, RunSignal, RunStep
from tests.run_engine_helpers import mk_engine, mk_session
from tests.test_run_engine_mp3c2 import (
    FakeChat, Transport, _approval_id, _decide_run, _mk_marker_script, _park,
    _run, _seed, _steps, _tool_step, _worker,
)

PY_TOOL = "python_sandbox"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "mp4c-r2b2at-key-0123456789")
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


def _q(eng, fn):
    db = mk_session(eng)
    try:
        return fn(db)
    finally:
        db.close()


def _mk_mcp(eng, args=None):
    tr = Transport()
    prov = FakeChat([_mk_marker_script("let me check. ", args or {"q": 1}),
                     ["after the tool"]])
    rid = _park(eng, prov, tr)
    return rid, prov, tr


def _started(eng, rid):
    return _q(eng, lambda db: db.query(DispatchRecord).count())


# ── 1. VALID CONTROL (must pass first) ───────────────────────────────
def test_1_valid_approved_action_dispatches_once(eng):
    rid, prov, tr = _mk_mcp(eng)
    aid = _approval_id(eng, rid)
    assert aid is not None
    assert tr.calls == [], "no external call before approval"
    _decide_run(eng, aid, approve=True)
    _worker(eng, prov, tr, wid="w-r2b2at-1").advance_chat_runs()
    assert len(tr.calls) == 1, "an authentic approval must reach the callback once"


# ── 2. Run.pending_approval_id mismatch ──────────────────────────────
def test_2_run_pending_approval_mismatch_blocks_execution(eng):
    rid, prov, tr = _mk_mcp(eng)
    step = _tool_step(eng, rid)
    real = _run(eng, rid).pending_approval_id

    def _swap(db):
        db.execute(__import__("sqlalchemy").update(Run)
                   .where(Run.id == rid)
                   .values(pending_approval_id=str(uuid.uuid4())))
        db.commit()

    _q(eng, _swap)
    _decide_run(eng, real, approve=True)
    _worker(eng, prov, tr, wid="w-r2b2at-2").advance_chat_runs()
    # EXPECTED (missing binding): the worker never reads pending_approval_id, so
    # the action still executes -> RED on current production code.
    assert tr.calls == [], "a mismatched Run.pending_approval_id must block execution"


# ── 3. RunStep.approval_id mismatch ──────────────────────────────────
def test_3_step_approval_id_mismatch_blocks_execution(eng):
    rid, prov, tr = _mk_mcp(eng)
    aid = _approval_id(eng, rid)
    step = _tool_step(eng, rid)
    assert step.approval_id == aid

    def _swap(db):
        import sqlalchemy as sa
        db.execute(sa.update(RunStep).where(RunStep.id == step.id)
                   .values(approval_id=str(uuid.uuid4())))
        db.commit()

    _q(eng, _swap)
    _decide_run(eng, aid, approve=True)
    _worker(eng, prov, tr, wid="w-r2b2at-3").advance_chat_runs()
    assert tr.calls == [], "a mismatched step.approval_id must block execution"


# ── 4. decision signal identity ──────────────────────────────────────
def test_4_decision_signal_payload_identity(eng):
    rid, prov, tr = _mk_mcp(eng)
    aid = _approval_id(eng, rid)
    _decide_run(eng, aid, approve=True)
    rows = _q(eng, lambda db: [dict(r) for r in db.execute(
        __import__("sqlalchemy").select(RunSignal.kind, RunSignal.payload)
        .where(RunSignal.run_id == rid)).mappings()])
    kinds = [r["kind"] for r in rows]
    assert "approval_decided" in kinds
    payload = [r["payload"] for r in rows if r["kind"] == "approval_decided"][0]
    # record, do not assume: does the durable signal carry an approval identity?
    print("DECISION_PAYLOAD=" + str(payload))
    print("PAYLOAD_CARRIES_APPROVAL_ID=" + str(aid in str(payload)))


# ── 5. Python routing observation ────────────────────────────────────
def test_5_python_proposal_routing_observation(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    from tests import test_mp4c_r2a3_python_proposal_worker as r2a3

    tr = Transport()
    prov = FakeChat([r2a3._py_script("", "print('APPROVED-PY-1')")])
    rid = r2a3._seed(eng)
    r2a3._worker(eng, prov).advance_chat_runs()
    run = _run(eng, rid)
    assert run.status == "waiting_for_approval", run.status
    aid = run.pending_approval_id
    assert aid is not None
    _decide_run(eng, aid, approve=True)
    r2a3._worker(eng, prov, wid="w-r2b2at-5").advance_chat_runs()
    print("PY_TOOL_CALLS=" + str([c[0] for c in tr.calls]))
    assert tr.calls == [], (
        "an approved Python action must NOT be routed through the MCP transport "
        "seam; tool_ids seen: " + str([c[0] for c in tr.calls]))

# ── 6. delivered-signal approval identity substitution ───────────────
def test_6_signal_approval_id_substitution_blocks_execution(eng):
    """R2B-2A-T3 converted to a permanent regression. ISOLATED integrity-fault
    test: a REAL second approval is minted through the gateway, then only the
    delivered signal's approval_id is changed. It is not evidence that an
    external user can forge a signal, and the original action WAS genuinely
    approved — the property under test is that the signal's identity is
    actually enforced."""
    import json
    import sqlalchemy as sa
    from app.approval import gateway as gw

    rid, prov, tr = _mk_mcp(eng)
    st = _tool_step(eng, rid)
    aid = _approval_id(eng, rid)

    def _mint(db):
        ctx = gw.GatewayContext(db=db, user_id=7, tenant_id=None, session_id=1,
                                run_id=rid, execution_turn_id=st.execution_turn_id,
                                tool_call_id=st.tool_call_id)
        return gw.dispatch(
            ctx, mcp_name="mcp__srv__echo", args={"q": "DIFFERENT-2"},
            execute_mcp=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("must never execute")), clock=None)

    res_b = _q(eng, _mint)
    assert res_b.status == "approval_required"
    aid_b = res_b.approval_id
    assert aid_b and aid_b != aid, "a real, distinct second approval is required"

    _decide_run(eng, aid, approve=True)

    def _substitute(db):
        row = db.query(RunSignal).filter(RunSignal.run_id == rid).first()
        payload = json.loads(row.payload)
        assert payload["approval_id"] == aid, "signal must start bound to the real approval"
        db.execute(sa.update(RunSignal).where(RunSignal.id == row.id).values(
            payload=json.dumps({"approval_id": aid_b, "decision": payload["decision"],
                                "server_ts": payload["server_ts"]})))
        db.commit()

    _q(eng, _substitute)
    _worker(eng, prov, tr, wid="w-r2b2at-6").advance_chat_runs()

    assert tr.calls == [], "a substituted signal approval_id must NOT authorize dispatch"
    assert _q(eng, lambda db: db.query(DispatchRecord).filter(
        DispatchRecord.outcome == "started").count()) == 0
    run = _run(eng, rid)
    assert run.status != "succeeded", run.status
    # control: the unrelated second approval is untouched, and the run's own
    # approval is NOT consumed by the valid decision path
    states = _q(eng, lambda db: {a.id: a.state for a in db.query(PendingApproval).all()})
    assert states[aid_b] == "requested", "the second approval must remain untouched"

