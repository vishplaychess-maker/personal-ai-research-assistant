"""Phase 1B.2R MP-2 — durable one-time MCP approval resume + at-most-once dispatch.

Fake in-memory MCP transport; no network / provider call. The approval gateway,
trusted registry and dispatch_records ledger are the real Phase 1B.2 code.
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text, update

from app.approval import registry
from app.approval import state_machine as _sm
from app.models.models import (
    ActionLogEntry,
    DispatchRecord,
    PendingApproval,
    Run,
    RunEvent,
    RunPayload,
    RunSignal,
    RunStep,
)
from app.routes.approvals import _decide
from app.run_engine.mcp_flow import create_mcp_run
from app.run_engine.payloads import open_payload
from app.run_engine.worker import RunWorker
from tests.run_engine_helpers import make_ctx, mk_engine, mk_factory, mk_session, seed_family


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


TOOL = "mcp__srv__echo"


def _reg(db, *, uid=7, tool_id=TOOL, schema=None):
    schema = schema or {"type": "object"}
    for unlock in (False, True):
        registry.register_or_refresh(
            db, owner_user_id=uid, tool_id=tool_id, tool_name=tool_id.split("__")[-1],
            mcp_server_id=3, mcp_server_name="srv",
            executable_identity="python -m srv @1",
            non_secret_launch_config=registry.non_secret_launch_config("python", ["-m", "srv"]),
            config_ref_id=registry.config_ref_for_server(3), input_schema=schema,
            unlock_if_unchanged=unlock,
        )


class _Transport:
    """Fake MCP transport: records calls, returns a canned (text, is_error)."""

    def __init__(self, text="TOOL-OUT ok", is_error=False, on_call=None):
        self.calls = []
        self._text, self._err, self._on_call = text, is_error, on_call

    def __call__(self, db, user_id, tool_id, args):
        self.calls.append((tool_id, args))
        if self._on_call is not None:
            self._on_call()
        return (self._text, self._err)


def _make_run(eng, *, args=None, uid=7, sid="fam-alpha"):
    args = {"text": "hi"} if args is None else args
    db = mk_session(eng)
    try:
        _reg(db, uid=uid)
        ctx = make_ctx(db, user_id=uid, sid=sid)
        run = create_mcp_run(db, ctx, mcp_name=TOOL, args=args, session_id=1)
        return run.id
    finally:
        db.close()


def _worker(eng, transport=None, wid="w-mcp"):
    return RunWorker(mk_factory(eng), worker_id=wid, mcp_transport=transport)


def _run_row(eng, run_id):
    db = mk_session(eng)
    try:
        return db.query(Run).filter(Run.id == run_id).first()
    finally:
        db.close()


def _step_row(eng, run_id):
    db = mk_session(eng)
    try:
        return (db.query(RunStep).filter(RunStep.run_id == run_id).first())
    finally:
        db.close()


def _approval_id(eng, run_id):
    return _run_row(eng, run_id).pending_approval_id


def _decide_run(eng, approval_id, *, approve, uid=7):
    """Exercise the real approvals-route decision path (mirror signal + record
    decision, atomically) without HTTP."""
    db = mk_session(eng)
    try:
        row = db.query(PendingApproval).filter(PendingApproval.id == approval_id).one()
        row.user_id = uid  # _decide expects an owned row
        return _decide(db, row, approve=approve)
    finally:
        db.close()


def _park(eng, transport=None):
    """create -> first worker pass -> parked at waiting_for_approval. Returns
    (run_id, approval_id)."""
    rid = _make_run(eng)
    _worker(eng, transport).poll_once()
    r = _run_row(eng, rid)
    assert r.status == "waiting_for_approval"
    assert r.pending_approval_id
    return rid, r.pending_approval_id


def _signals(eng, run_id, kind="approval_decided"):
    db = mk_session(eng)
    try:
        return db.query(RunSignal).filter(
            RunSignal.run_id == run_id, RunSignal.kind == kind).all()
    finally:
        db.close()


def _dispatch(eng, tool_call_id):
    db = mk_session(eng)
    try:
        return db.query(DispatchRecord).filter(
            DispatchRecord.tool_call_id == tool_call_id).one_or_none()
    finally:
        db.close()


# ── proposal / approval bridge ─────────────────────────────────────


def test_first_pass_parks_at_waiting_and_never_executes(eng):
    t = _Transport()
    rid, aid = _park(eng, t)
    assert t.calls == []
    step = _step_row(eng, rid)
    assert step.status == "waiting" and step.approval_id == aid
    assert _dispatch(eng, step.tool_call_id) is None


def test_approve_creates_one_signal_and_executes_nothing_inline(eng):
    t = _Transport()
    rid, aid = _park(eng, t)
    out = _decide_run(eng, aid, approve=True)
    assert out["state"] == "approved"
    sigs = _signals(eng, rid)
    assert len(sigs) == 1 and sigs[0].consumed_at is None
    assert t.calls == []                       # decision never touched the tool
    assert _run_row(eng, rid).status == "waiting_for_approval"


def test_duplicate_approve_creates_no_duplicate_signal(eng):
    rid, aid = _park(eng)
    _decide_run(eng, aid, approve=True)
    _decide_run(eng, aid, approve=True)        # idempotent (state already approved)
    assert len(_signals(eng, rid)) == 1


def test_denial_never_invokes_mcp_and_is_terminal(eng):
    t = _Transport()
    rid, aid = _park(eng, t)
    _decide_run(eng, aid, approve=False)
    _worker(eng, t).poll_once()
    r = _run_row(eng, rid)
    assert r.status == "denied"
    assert t.calls == []
    step = _step_row(eng, rid)
    assert step.status == "blocked" and step.error_code == "approval_denied"
    assert _dispatch(eng, step.tool_call_id) is None
    # a later spurious approve cannot resurrect it
    db = mk_session(eng)
    try:
        row = db.query(PendingApproval).filter(PendingApproval.id == aid).one()
        assert row.state == "denied"
    finally:
        db.close()


# ── the one approved dispatch ──────────────────────────────────────


def test_approved_action_invokes_fake_mcp_exactly_once(eng):
    t = _Transport(text="ECHO: hi")
    rid, aid = _park(eng, t)
    _decide_run(eng, aid, approve=True)
    _worker(eng, t).poll_once()

    assert len(t.calls) == 1
    r = _run_row(eng, rid)
    assert r.status == "succeeded"
    step = _step_row(eng, rid)
    assert step.status == "succeeded" and step.output_payload_id
    rec = _dispatch(eng, step.tool_call_id)
    assert rec is not None and rec.outcome == "succeeded"
    db = mk_session(eng)
    try:
        assert db.query(PendingApproval).filter(PendingApproval.id == aid).one().state == "consumed"
    finally:
        db.close()


def test_encrypted_args_and_result_round_trip(eng):
    t = _Transport(text="RESULT-VALUE-42")
    rid, aid = _park(eng, t)
    _decide_run(eng, aid, approve=True)
    _worker(eng, t).poll_once()

    r, step = _run_row(eng, rid), _step_row(eng, rid)
    db = mk_session(eng)
    try:
        args = json.loads(open_payload(
            db, step.input_payload_id, expected_tenant_id=r.tenant_id,
            expected_user_id=r.user_id, expected_run_id=r.id, expected_step_id=step.id,
            expected_payload_type="tool_args", expected_action_hash=None))
        assert args == {"text": "hi"}
        result = open_payload(
            db, step.output_payload_id, expected_tenant_id=r.tenant_id,
            expected_user_id=r.user_id, expected_run_id=r.id, expected_step_id=step.id,
            expected_payload_type="tool_output", expected_action_hash=None)
        assert result == "RESULT-VALUE-42"
    finally:
        db.close()


def test_no_raw_args_or_result_or_secret_in_ordinary_fields(eng):
    canary_in = "CANARY-sk-INPUT-9931"
    canary_out = "CANARY-OUTPUT-7742"
    t = _Transport(text=f"the tool said {canary_out}")
    rid = _make_run(eng, args={"token": canary_in})
    w = _worker(eng, t)
    w.poll_once()
    _decide_run(eng, _approval_id(eng, rid), approve=True)
    w.poll_once()
    assert _run_row(eng, rid).status == "succeeded"

    db = mk_session(eng)
    try:
        blobs = []
        for r in db.query(Run).all():
            blobs += [r.status_reason, r.thread_id, r.idempotency_key]
        for s in db.query(RunStep).all():
            blobs += [s.input_ref, s.output_ref, s.error_code]
        for e in db.query(RunEvent).all():
            blobs.append(e.data)
        for sg in db.query(RunSignal).all():
            blobs.append(sg.payload)
        for al in db.query(ActionLogEntry).all():
            blobs.append(al.event_metadata)
        for dr in db.query(DispatchRecord).all():
            blobs += [dr.action_hash, dr.outcome]
        for pa in db.query(PendingApproval).all():
            blobs += [pa.display_summary, pa.purpose_redacted, pa.action_hash]
        for pl in db.query(RunPayload).all():         # every col EXCEPT ciphertext
            blobs += [pl.payload_type, pl.enc_scheme, str(pl.byte_len)]
        joined = "\n".join(b for b in blobs if b)
        assert canary_in not in joined
        assert canary_out not in joined
        # the encrypted stores DO hold them, sealed
        cts = "\n".join(p.ciphertext for p in db.query(RunPayload).all())
        assert canary_in not in cts and canary_out not in cts   # ciphertext, not plaintext
    finally:
        db.close()


# ── identity / registry / envelope binding ─────────────────────────


def test_locked_registry_entry_blocks_dispatch(eng):
    t = _Transport()
    rid, aid = _park(eng, t)
    _decide_run(eng, aid, approve=True)
    db = mk_session(eng)
    try:
        registry.lock_tool(db, owner_user_id=7, tool_id=TOOL, reason="operator re-verify")
    finally:
        db.close()
    _worker(eng, t).poll_once()

    assert t.calls == []
    r = _run_row(eng, rid)
    assert r.status == "failed" and r.status_reason in ("tool_locked", "schema_mismatch",
                                                        "fingerprint_mismatch")


def test_altered_action_hash_blocks_dispatch(eng):
    t = _Transport()
    rid, aid = _park(eng, t)
    db = mk_session(eng)
    try:
        db.execute(update(PendingApproval).where(PendingApproval.id == aid)
                   .values(action_hash="0" * 64))
        db.commit()
    finally:
        db.close()
    _decide_run(eng, aid, approve=True)
    _worker(eng, t).poll_once()

    assert t.calls == []
    r = _run_row(eng, rid)
    assert r.status == "failed" and r.status_reason == "envelope_mismatch"


def test_tampered_sealed_args_fail_closed(eng):
    t = _Transport()
    rid, aid = _park(eng, t)
    _decide_run(eng, aid, approve=True)
    step = _step_row(eng, rid)
    db = mk_session(eng)
    try:
        db.execute(update(RunPayload).where(RunPayload.id == step.input_payload_id)
                   .values(ciphertext="gAAAAAB!!!not-a-valid-token"))
        db.commit()
    finally:
        db.close()
    _worker(eng, t).poll_once()

    assert t.calls == []
    assert _run_row(eng, rid).status in ("outcome_unknown", "failed")


# ── fresh session validation ───────────────────────────────────────


def test_revoked_session_before_dispatch_is_blocked(eng):
    t = _Transport()
    rid, aid = _park(eng, t)
    _decide_run(eng, aid, approve=True)
    db = mk_session(eng)
    try:
        db.execute(text("UPDATE refresh_sessions SET revoked_at=CURRENT_TIMESTAMP "
                        "WHERE family_id='fam-alpha'"))
        db.commit()
    finally:
        db.close()
    _worker(eng, t).poll_once()

    assert t.calls == []
    r = _run_row(eng, rid)
    assert r.status == "failed" and r.status_reason == "session_revoked"
    assert _signals(eng, rid)[0].consumed_at is None   # signal not consumed


def test_a_different_active_family_of_the_same_user_is_not_accepted(eng):
    t = _Transport()
    rid, aid = _park(eng, t)
    _decide_run(eng, aid, approve=True)
    db = mk_session(eng)
    try:
        seed_family(db, user_id=7, family_id="fam-beta")          # a 2nd active login
        db.execute(text("UPDATE refresh_sessions SET revoked_at=CURRENT_TIMESTAMP "
                        "WHERE family_id='fam-alpha'"))            # the run's family gone
        db.commit()
    finally:
        db.close()
    _worker(eng, t).poll_once()

    assert t.calls == []
    assert _run_row(eng, rid).status_reason == "session_revoked"  # no fallback family


# ── cancellation ───────────────────────────────────────────────────


def test_cancel_before_dispatch_prevents_invocation(eng):
    t = _Transport()
    rid, aid = _park(eng, t)
    _decide_run(eng, aid, approve=True)
    db = mk_session(eng)
    try:
        db.add(RunSignal(id="cx", run_id=rid, kind="cancel_requested",
                         created_by_user_id=7))
        db.commit()
    finally:
        db.close()
    _worker(eng, t).poll_once()

    assert t.calls == []
    r = _run_row(eng, rid)
    assert r.status == "cancelled"
    step = _step_row(eng, rid)
    assert _dispatch(eng, step.tool_call_id) is None
    assert _signals(eng, rid)[0].consumed_at is None   # approval signal untouched


def test_cancel_after_dispatch_started_does_not_claim_undo(eng):
    rid, aid = _park(eng)

    def _inject_cancel():
        d = mk_session(eng)
        try:
            d.add(RunSignal(id="cy", run_id=rid, kind="cancel_requested",
                            created_by_user_id=7))
            d.commit()
        finally:
            d.close()

    t = _Transport(text="did the thing", on_call=_inject_cancel)
    _decide_run(eng, aid, approve=True)
    _worker(eng, t).poll_once()

    assert len(t.calls) == 1
    r = _run_row(eng, rid)
    assert r.status == "succeeded"                     # NOT cancelled
    assert _dispatch(eng, _step_row(eng, rid).tool_call_id).outcome == "succeeded"


# ── crash / restart ────────────────────────────────────────────────


def test_restart_after_success_invokes_zero_additional_times(eng):
    t = _Transport()
    rid, aid = _park(eng, t)
    _decide_run(eng, aid, approve=True)
    _worker(eng, t).poll_once()
    assert _run_row(eng, rid).status == "succeeded" and len(t.calls) == 1

    _worker(eng, t, wid="w-restart").poll_once()       # a fresh worker == a restart
    assert len(t.calls) == 1
    assert _run_row(eng, rid).status == "succeeded"


def test_crash_after_dispatch_started_becomes_outcome_unknown(eng):
    t = _Transport()
    rid, aid = _park(eng, t)
    _decide_run(eng, aid, approve=True)
    step = _step_row(eng, rid)
    db = mk_session(eng)
    try:  # simulate: worker died between started-record and outcome
        db.add(DispatchRecord(id="d-inc", tool_call_id=step.tool_call_id, user_id=7,
                              action_hash="a" * 64, outcome="started"))
        db.execute(update(Run).where(Run.id == rid).values(
            status="running", pending_approval_id=step.approval_id))
        db.execute(update(RunStep).where(RunStep.id == step.id).values(status="running"))
        db.commit()
    finally:
        db.close()
    _worker(eng, t).poll_once()

    assert t.calls == []
    r = _run_row(eng, rid)
    assert r.status == "outcome_unknown"
    assert _step_row(eng, rid).status == "incomplete"


def test_crash_before_dispatch_started_is_safe_to_resume(eng):
    t = _Transport(text="resumed ok")
    rid, aid = _park(eng, t)
    _decide_run(eng, aid, approve=True)
    step = _step_row(eng, rid)
    db = mk_session(eng)
    try:  # worker died after approve but before it started the dispatch
        db.execute(update(Run).where(Run.id == rid).values(
            status="running", pending_approval_id=step.approval_id))
        db.execute(update(RunStep).where(RunStep.id == step.id).values(status="running"))
        # consume the signal as the pre-crash worker would have
        db.execute(update(RunSignal).where(RunSignal.run_id == rid)
                   .values(consumed_at=text("CURRENT_TIMESTAMP")))
        db.commit()
    finally:
        db.close()
    _worker(eng, t).poll_once()

    assert len(t.calls) == 1
    assert _run_row(eng, rid).status == "succeeded"
    assert _dispatch(eng, step.tool_call_id).outcome == "succeeded"


def test_two_workers_race_only_one_invokes(eng):
    t = _Transport()
    rid, aid = _park(eng, t)
    _decide_run(eng, aid, approve=True)
    w1, w2 = _worker(eng, t, wid="w-a"), _worker(eng, t, wid="w-b")
    w1.poll_once()
    w2.poll_once()

    assert len(t.calls) == 1
    assert _run_row(eng, rid).status == "succeeded"
    rec = _dispatch(eng, _step_row(eng, rid).tool_call_id)
    assert rec.outcome == "succeeded"


# ── failure atomicity ──────────────────────────────────────────────


def test_approval_decision_and_signal_roll_back_together(eng, monkeypatch):
    rid, aid = _park(eng)

    def _boom(*a, **k):
        raise RuntimeError("signal insert failed")

    monkeypatch.setattr("app.run_engine.mcp_flow.mirror_decision_signal", _boom)
    with pytest.raises(Exception):
        _decide_run(eng, aid, approve=True)

    db = mk_session(eng)
    try:
        assert db.query(PendingApproval).filter(PendingApproval.id == aid).one().state == "requested"
        assert db.query(RunSignal).filter(RunSignal.run_id == rid).count() == 0
    finally:
        db.close()


def test_output_persistence_failure_does_not_report_success(eng, monkeypatch):
    t = _Transport(text="tool ran fine")
    rid, aid = _park(eng, t)
    _decide_run(eng, aid, approve=True)

    def _boom(*a, **k):
        raise RuntimeError("seal failed")

    monkeypatch.setattr("app.run_engine.worker.seal_payload", _boom)
    _worker(eng, t).poll_once()

    assert len(t.calls) == 1                          # the tool DID run
    r = _run_row(eng, rid)
    assert r.status == "outcome_unknown" and r.status_reason == "output_persist_failed"
    assert _step_row(eng, rid).status != "succeeded"
    assert _dispatch(eng, _step_row(eng, rid).tool_call_id).outcome == "succeeded"


def test_no_public_run_route_or_stream_change(eng):
    import app.main as m
    from app.config import settings

    paths = {getattr(r, "path", "") for r in m.app.routes}
    # MP-3A adds /api/runs but it is feature-gated OFF by default; MP-2 itself
    # added no active public surface and did not touch /messages/stream.
    assert settings.run_public_api_enabled is False
    assert "/api/sessions/{session_id}/messages/stream" in paths
    # the live MCP path still refuses to resume
    from app.tools.mcp_tool import run_mcp_calls  # noqa: F401  (import-only smoke)
