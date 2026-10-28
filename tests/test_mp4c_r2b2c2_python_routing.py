"""MP-4C-R2B-2C-2A — core approved Python routing (focused tests).

Reuses the real R2A-3 Python proposal fixture, the real worker/gateway and the
MP-3C2 counting MCP transport. NO real Python subprocess is launched: the MCP
transport is a counting fake and the Python seam is not reached in these tests.
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import update

from app.config import settings
from app.models.models import DispatchRecord, PendingApproval, Run, RunPayload, RunStep
from app.run_engine.worker import RunWorker
from tests.run_engine_helpers import mk_engine, mk_factory, mk_session
from tests.test_run_engine_mp3c2 import (
    FakeChat, Transport, _decide_run, _mk_marker_script, _park, _run, _tool_step,
)
from tests import test_mp4c_r2a3_python_proposal_worker as r2a3

PY_TOOL = "python_sandbox"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "mp4c-r2b2c2a-key-0123456789")
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


def _worker(eng, prov, tr, wid, python_executor=None):
    return RunWorker(mk_factory(eng), worker_id=wid,
                     chat_provider=(lambda ci, **_k: prov), mcp_transport=tr,
                     python_executor=python_executor)


def _park_python(eng, monkeypatch, *, code="print('PY-ROUTE-1')"):
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    tr = Transport()
    prov = FakeChat([r2a3._py_script("", code)])
    rid = r2a3._seed(eng)
    r2a3._worker(eng, prov).advance_chat_runs()
    assert _run(eng, rid).status == "waiting_for_approval"
    return rid, prov, tr


# ── CONTROL: legitimate MCP approve still reaches its transport ──────
def test_control_mcp_approve_still_uses_mcp_transport(eng):
    tr = Transport()
    prov = FakeChat([_mk_marker_script("checking. ", {"q": 1}), ["after"]])
    rid = _park(eng, prov, tr)
    aid = _run(eng, rid).pending_approval_id
    assert tr.calls == []
    _decide_run(eng, aid, approve=True)
    _worker(eng, prov, tr, "c-mcp").advance_chat_runs()
    assert len(tr.calls) == 1
    assert tr.calls[0][0] == "mcp__srv__echo"


# ── A: the reserved Python ID must NEVER reach the MCP transport ─────
def test_a_reserved_python_id_never_reaches_mcp_transport(eng, monkeypatch):
    """Genuinely RED pre-routing: today the approved python_sandbox step is
    dispatched through the MCP transport seam with tool_id='python_sandbox'."""
    rid, prov, tr = _park_python(eng, monkeypatch)
    aid = _run(eng, rid).pending_approval_id
    _decide_run(eng, aid, approve=True)
    _worker(eng, prov, tr, "c-py").advance_chat_runs()
    seen = [c[0] for c in tr.calls]
    assert PY_TOOL not in seen, (
        "reserved python_sandbox id was sent to the MCP transport: " + str(seen))
    assert tr.calls == [], "no MCP transport call may occur for a Python step"


# ── B: wrong origin on the reserved ID must fail closed ─────────────
def test_b_wrong_origin_on_reserved_id_fails_closed(eng, monkeypatch):
    rid, prov, tr = _park_python(eng, monkeypatch)
    step = r2a3._tool_step(eng, rid)

    def _corrupt(db):
        ref = json.loads(step.input_ref)
        assert ref["purpose"] == "python_execution"
        ref["purpose"] = "not_python"
        db.execute(update(RunStep).where(RunStep.id == step.id)
                   .values(input_ref=json.dumps(ref, sort_keys=True)))
        db.commit()

    _q(eng, _corrupt)
    aid = _run(eng, rid).pending_approval_id
    _decide_run(eng, aid, approve=True)
    _worker(eng, prov, tr, "c-origin").advance_chat_runs()
    assert tr.calls == [], "a wrong-origin reserved id must not use the MCP transport"


# ── C: tampered sealed args must not produce a successful dispatch ──
def test_c_tampered_sealed_python_args_do_not_succeed(eng, monkeypatch):
    rid, prov, tr = _park_python(eng, monkeypatch)
    step = r2a3._tool_step(eng, rid)

    def _tamper(db):
        row = db.query(RunPayload).filter(RunPayload.id == step.input_payload_id).one()
        row.ciphertext = ("B" if not row.ciphertext.startswith("B") else "C") + row.ciphertext[1:]
        db.commit()

    _q(eng, _tamper)
    aid = _run(eng, rid).pending_approval_id
    _decide_run(eng, aid, approve=True)
    _worker(eng, prov, tr, "c-tamper").advance_chat_runs()
    assert tr.calls == []
    assert _q(eng, lambda db: db.query(DispatchRecord).filter(
        DispatchRecord.outcome == "started").count()) == 0
    assert _run(eng, rid).status != "succeeded"

# ── D: approved Python runs ONCE with the reopened code ─────────────
def test_d_approved_python_runs_once_with_reopened_code(eng, monkeypatch):
    rid, prov, tr = _park_python(eng, monkeypatch)
    calls = []

    class _Out:
        def __init__(self, t, e):
            self.text, self.is_error = t, e

    def fake(code):
        started = _q(eng, lambda db: db.query(DispatchRecord).filter(
            DispatchRecord.outcome == "started").count())
        calls.append((code, started))
        return _Out("PY-OUT-OK", False)

    aid = _run(eng, rid).pending_approval_id
    _decide_run(eng, aid, approve=True)
    w = _worker(eng, prov, tr, "d-py", python_executor=fake)
    w.advance_chat_runs()

    assert len(calls) == 1, "exactly one executor call"
    code, started_seen = calls[0]
    assert code == "print('PY-ROUTE-1')", "code must be the reopened sealed args"
    assert started_seen == 1, "DispatchRecord.started must be committed first"
    assert tr.calls == [], "the reserved id must never use the MCP transport"
    sealed = _q(eng, lambda db: db.query(RunPayload).filter(
        RunPayload.payload_type == "tool_output").count())
    assert sealed == 1, "the result must be sealed as tool_output"


# ── E: canonical hash mismatch (valid ciphertext) must not execute ──
def test_e_hash_mismatch_blocks_execution(eng, monkeypatch):
    from app.models.models import ToolCall

    rid, prov, tr = _park_python(eng, monkeypatch)
    step = r2a3._tool_step(eng, rid)

    def _break(db):
        db.execute(update(ToolCall).where(ToolCall.id == step.tool_call_id)
                   .values(args_hash="0" * 64))
        db.commit()

    _q(eng, _break)
    calls = []
    aid = _run(eng, rid).pending_approval_id
    _decide_run(eng, aid, approve=True)
    w = _worker(eng, prov, tr, "e-hash", python_executor=lambda c: calls.append(c))
    w.advance_chat_runs()
    assert calls == [], "a hash mismatch must never reach the executor"
    assert tr.calls == []
    assert _q(eng, lambda db: db.query(DispatchRecord).filter(
        DispatchRecord.outcome == "started").count()) == 0

# ══ MP-4C-R2B-2C-2B — continuation + crash non-reexecution ══════════
class _Out:
    def __init__(self, text, is_error):
        self.text, self.is_error = text, is_error


def _drive_python(eng, monkeypatch, wid, *, final="FINAL-ANSWER-OK", boom=False):
    rid, prov, tr = _park_python(eng, monkeypatch)
    prov.scripts.append([final])
    calls = []

    def fake(code):
        calls.append(code)
        if boom:
            raise RuntimeError("simulated crash inside the gateway callback")
        return _Out("TOOL-RESULT-OK", False)

    aid = _run(eng, rid).pending_approval_id
    _decide_run(eng, aid, approve=True)
    w = _worker(eng, prov, tr, wid, python_executor=fake)
    for _ in range(8):
        w.advance_chat_runs()
        if _run(eng, rid).status in ("succeeded", "failed", "outcome_unknown", "denied"):
            break
    return rid, prov, tr, calls, w


def test_f_successful_continuation_same_run_one_final_message(eng, monkeypatch):
    from app.models.models import Message

    rid, prov, tr, calls, _w = _drive_python(eng, monkeypatch, "f-cont")
    run = _run(eng, rid)
    assert run.id == rid, "the SAME Run must continue"
    assert run.status == "succeeded", (run.status, run.status_reason)
    assert len(calls) == 1, "executor must run exactly once"
    assert calls[0] == "print('PY-ROUTE-1')"
    assert tr.calls == [], "reserved id must never use the MCP transport"
    assert _q(eng, lambda db: db.query(RunPayload).filter(
        RunPayload.payload_type == "tool_output").count()) == 1
    msgs = _q(eng, lambda db: db.query(Message).all())
    assistants = [m for m in msgs if str(m.role).endswith("assistant")]
    assert len(assistants) == 1, "exactly one final assistant Message"
    assert "FINAL-ANSWER-OK" in assistants[0].content
    assert len(prov.calls) == 2, "one marker segment + one continuation segment"
    blob = " || ".join(str(getattr(m, c)) for m in msgs
                       for c in m.__table__.columns.keys())
    assert "PY-ROUTE-1" not in blob and "python_execution" not in blob


def test_g_terminal_replay_does_not_reinvoke(eng, monkeypatch):
    rid, prov, tr, calls, w = _drive_python(eng, monkeypatch, "g-replay")
    assert len(calls) == 1
    before = (_run(eng, rid).status,
              _q(eng, lambda db: db.query(DispatchRecord).count()),
              _q(eng, lambda db: db.query(RunPayload).count()))
    w.advance_chat_runs()
    w.advance_chat_runs()
    assert len(calls) == 1, "a terminal Run must never re-invoke the executor"
    assert len(prov.calls) == 2, "no extra provider call on replay"
    assert (_run(eng, rid).status,
            _q(eng, lambda db: db.query(DispatchRecord).count()),
            _q(eng, lambda db: db.query(RunPayload).count())) == before


def test_h_crash_inside_callback_never_reexecutes(eng, monkeypatch):
    rid, prov, tr, calls, w = _drive_python(eng, monkeypatch, "h-crash", boom=True)
    assert len(calls) == 1, "the callback ran exactly once before 'crashing'"
    w.advance_chat_runs()
    w.advance_chat_runs()
    assert len(calls) == 1, "a crash after dispatch must never re-invoke Python"
    assert tr.calls == []
    # EXACT contract (gateway.py:533-534): a callback exception is CAUGHT by the
    # gateway, which marks the ledger outcome "failed" — a KNOWN failure, not an
    # unknown outcome. outcome="started" is reserved for a real process crash.
    outcomes = _q(eng, lambda db: [d.outcome for d in db.query(DispatchRecord).all()])
    assert outcomes == ["failed"], outcomes
    # The ledger records a KNOWN failure. Per _mcp_finalize a known tool error is
    # fed back to the model as a delimited data envelope, so the SAME Run still
    # continues and may legitimately finish successfully — what must never happen
    # is a second execution.
    run = _run(eng, rid)
    assert run.status in ("succeeded", "failed"), (run.status, run.status_reason)
    assert len(prov.calls) == 2, "the known failure is fed back and generation continues"


def test_i_durable_started_without_result_recovers_as_unknown(eng, monkeypatch):
    """The genuine crash interval: a ledger row at outcome="started" with no
    recorded result (exactly what gateway.py:500 commits before invoking the
    callback, had the process died there). Re-entry must NOT re-execute and must
    follow the existing bounded outcome_unknown contract (worker.py:1044-1047)."""
    import datetime
    import uuid

    rid, prov, tr = _park_python(eng, monkeypatch)
    step = r2a3._tool_step(eng, rid)

    def _started(db):
        db.add(DispatchRecord(
            id=str(uuid.uuid4()), tool_call_id=step.tool_call_id,
            action_hash="d" * 64, outcome="started", user_id=7,
            started_at=datetime.datetime.now(datetime.timezone.utc)))
        db.commit()

    _q(eng, _started)
    calls = []
    aid = _run(eng, rid).pending_approval_id
    _decide_run(eng, aid, approve=True)
    w = _worker(eng, prov, tr, "i-started", python_executor=lambda c: calls.append(c))
    w.advance_chat_runs()

    assert calls == [], "an unknown outcome must never re-execute Python"
    assert tr.calls == []
    run = _run(eng, rid)
    assert run.status == "outcome_unknown", (run.status, run.status_reason)
    assert run.status_reason == "outcome_unknown", run.status_reason
    st = r2a3._tool_step(eng, rid)
    assert st.status == "incomplete" and st.error_code == "dispatch_incomplete", \
        (st.status, st.error_code)
    from app.models.models import Message
    assistants = _q(eng, lambda db: [m for m in db.query(Message).all()
                                     if str(m.role).endswith("assistant")])
    assert assistants == [], "an unknown outcome must not produce a success Message"

# ══ MP-4C-R2B-2C-2B closeout — tool_output binding ═══════════════════
def test_j_tool_output_payload_opens_only_with_its_exact_binding(eng, monkeypatch):
    from app.run_engine.payloads import open_payload

    rid, prov, tr, calls, _w = _drive_python(eng, monkeypatch, "j-bind")
    row = _q(eng, lambda db: db.query(RunPayload).filter(
        RunPayload.payload_type == "tool_output").one())
    run = _run(eng, rid)
    good = dict(expected_tenant_id=run.tenant_id, expected_user_id=run.user_id,
                expected_run_id=run.id, expected_step_id=row.step_id,
                expected_payload_type="tool_output", expected_action_hash=None)

    text = _q(eng, lambda db: open_payload(db, row.id, **good))
    assert "TOOL-RESULT-OK" in text, "the sealed result must open under its own binding"

    for bad in ({"expected_user_id": 999}, {"expected_run_id": "other-run"},
                {"expected_step_id": "other-step"},
                {"expected_payload_type": "tool_args"}):
        kw = dict(good)
        kw.update(bad)
        try:
            _q(eng, lambda db: open_payload(db, row.id, **kw))
            raise AssertionError("wrong binding must fail closed: " + str(bad))
        except AssertionError:
            raise
        except Exception:
            pass

    # no plaintext exposure of the result
    assert "TOOL-RESULT-OK" not in (row.ciphertext or "")
    meta = " ".join(str(getattr(row, c)) for c in row.__table__.columns.keys()
                    if c != "ciphertext")
    assert "TOOL-RESULT-OK" not in meta, meta
