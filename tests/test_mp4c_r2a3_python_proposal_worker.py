"""Phase 1B.2R MP-4C-R2A-3 — durable Python proposal / suspension (worker level).

Drives the REAL RunWorker, state machine, approval gateway, trusted registry,
encrypted payloads and SQLAlchemy models. Only the chat provider is a
deterministic fake, and the sandbox executor is a loud forbidden sentinel.
No Python is ever executed in this phase.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from app.config import settings
from app.models.models import (
    DispatchRecord, ExecutionTurn, Message, PendingApproval, Run, RunEvent,
    RunPayload, RunStep, ToolCall, TrustedTool,
)
from app.approval import registry
from app.approval.hashing import args_hash
from app.run_engine import python_approval_identity as pai
from app.run_engine import submission as sub
from app.run_engine.payloads import open_payload
from app.run_engine.state_machine import WAITING_FOR_APPROVAL
from app.run_engine.worker import RunWorker
from tests.run_engine_helpers import (
    ensure_user, make_ctx, mk_engine, mk_factory, mk_session, revoke_family,
)

_HMAC = "mp4c-r2a3-unit-test-hmac-key-0123456789"
CODE_CANARY = "PYTHON_SECRET_SENTINEL_R2A3"
CODE = "print('" + CODE_CANARY + "')"
TAIL = "TAIL-MUST-NOT-APPEAR"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
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


@pytest.fixture
def sandbox_sentinel(monkeypatch):
    """Loud forbidden executor — any call is a hard failure."""
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("SANDBOX EXECUTED — forbidden during proposal pass")

    import app.tools.python_sandbox as ps
    monkeypatch.setattr(ps, "run_python_code", _boom)
    monkeypatch.setattr(ps, "run_python_code_async", _boom)
    return calls


class FakeChat:
    def __init__(self, scripts):
        self.scripts = [list(s) for s in scripts]
        self.calls = []
        self.n = 0

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        self.calls.append({"messages": [dict(m) for m in messages]})
        toks = self.scripts[self.n] if self.n < len(self.scripts) else []
        self.n += 1
        for t in toks:
            yield {"type": "token", "token": t}
        yield {"type": "done", "response": "".join(toks)}


def _py_script(prefix, code, *, suffix=TAIL, split=True):
    body = "[PYTHON_CODE: " + code + "]"
    if split:
        mid = len(body) // 2
        return [prefix, body[:3], body[3:mid], body[mid:], "\n", suffix]
    return [prefix, body + "\n" + suffix]


def _seed(eng, *, uid=7, sid="fam-alpha", session_id=1):
    db = mk_session(eng)
    try:
        ensure_user(db, uid)
        from app.models.models import ResearchSession
        if db.query(ResearchSession).filter(ResearchSession.id == session_id).first() is None:
            db.add(ResearchSession(id=session_id, user_id=uid, title="t"))
            db.commit()
        ctx = make_ctx(db, user_id=uid, sid=sid)
        submission = sub.build_chat_submission(
            session_id=session_id, message="hi", image_url=None, provider="prov-1",
            model="model-1", mode="chat", system_prompt="SYS", options={})
        run, created = sub.create_chat_run(db, ctx, submission=submission,
                                           idempotency_key=uuid.uuid4().hex)
        assert created
        return run.id
    finally:
        db.close()


class _FakeOutcome:
    """Typed fake outcome for the test-only seam (never a subprocess)."""

    def __init__(self, text: str = "FAKE-PY-OUT", is_error: bool = False):
        self.text, self.is_error = text, is_error


def _fake_executor(code: str):  # test-only: launches nothing
    return _FakeOutcome()


def _worker(eng, provider=None, wid="w-r2a3", python_executor=_fake_executor):
    # MP-4C: real execution needs a host-isolated runtime, so these approval-flow
    # fixtures inject the test-only fake AT CONSTRUCTION (a later assignment does
    # not bypass the refusal).
    return RunWorker(mk_factory(eng), worker_id=wid,
                     chat_provider=(lambda ci, **_k: provider) if provider is not None else None,
                     python_executor=python_executor)


def _q(eng, fn):
    db = mk_session(eng)
    try:
        return fn(db)
    finally:
        db.close()


def _run(eng, rid):
    return _q(eng, lambda db: db.query(Run).filter(Run.id == rid).first())


def _steps(eng, rid):
    return _q(eng, lambda db: db.query(RunStep).filter(RunStep.run_id == rid).all())


def _counts(eng, rid):
    def f(db):
        return {
            "turns": db.query(ExecutionTurn).count(),
            "calls": db.query(ToolCall).count(),
            "approvals": db.query(PendingApproval).count(),
            "steps": db.query(RunStep).filter(RunStep.run_id == rid,
                                              RunStep.kind == "tool_dispatch").count(),
            "payloads": db.query(RunPayload).filter(RunPayload.run_id == rid).count(),
            "started_dispatch": db.query(DispatchRecord).filter(
                DispatchRecord.outcome == "started").count(),
            "messages": db.query(Message).count(),
            "events": db.query(RunEvent).filter(RunEvent.run_id == rid).count(),
        }
    return _q(eng, f)


def _tool_step(eng, rid):
    for s in _steps(eng, rid):
        if s.kind == "tool_dispatch":
            return s
    return None


def _leak_blob(eng, rid, *, caplog=None):
    def f(db):
        out = []
        for r in db.query(Run).filter(Run.id == rid).all():
            out += [str(getattr(r, c)) for c in r.__table__.columns.keys()]
        for r in db.query(RunStep).filter(RunStep.run_id == rid).all():
            out += [str(getattr(r, c)) for c in r.__table__.columns.keys()]
        for r in db.query(RunEvent).filter(RunEvent.run_id == rid).all():
            out += [str(getattr(r, c)) for c in r.__table__.columns.keys()]
        for m in db.query(Message).all():
            out += [str(getattr(m, c)) for c in m.__table__.columns.keys()]
        for r in db.query(ExecutionTurn).all():
            out += [str(getattr(r, c)) for c in r.__table__.columns.keys()]
        for r in db.query(ToolCall).all():
            out += [str(getattr(r, c)) for c in r.__table__.columns.keys()]
        for r in db.query(PendingApproval).all():
            out += [str(getattr(r, c)) for c in r.__table__.columns.keys()]
        for r in db.query(DispatchRecord).all():
            out += [str(getattr(r, c)) for c in r.__table__.columns.keys()]
        for r in db.query(TrustedTool).all():
            out += [str(getattr(r, c)) for c in r.__table__.columns.keys()]
        return " || ".join(out)
    blob = _q(eng, f)
    if caplog is not None:
        blob += " || " + " ".join(r.getMessage() for r in caplog.records)
    return blob


# ── Test 1 — flag off ────────────────────────────────────────────────
def test_flag_off_python_marker_stays_fail_closed(eng, sandbox_sentinel):
    prov = FakeChat([_py_script("safe prefix ", CODE)])
    rid = _seed(eng)
    _worker(eng, prov).advance_chat_runs()
    run = _run(eng, rid)
    assert run.status == "failed"
    assert run.status_reason == "tool_resume_required"
    c = _counts(eng, rid)
    assert c["turns"] == 0 and c["calls"] == 0 and c["approvals"] == 0
    assert c["steps"] == 0 and c["started_dispatch"] == 0
    assert sandbox_sentinel["n"] == 0
    blob = _leak_blob(eng, rid)
    assert CODE_CANARY not in blob and "PYTHON_CODE" not in blob


# ── Test 2 — happy path ──────────────────────────────────────────────
def test_flag_on_creates_one_proposal_and_parks(eng, monkeypatch, sandbox_sentinel, caplog):
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    prov = FakeChat([_py_script("safe prefix ", CODE)])
    rid = _seed(eng)
    _worker(eng, prov).advance_chat_runs()

    assert len(prov.calls) == 1, "exactly one provider invocation"
    run = _run(eng, rid)
    assert run.status == WAITING_FOR_APPROVAL, (
        "status=%r reason=%r" % (run.status, getattr(run, "status_reason", None)))
    c = _counts(eng, rid)
    assert c["turns"] == 1 and c["calls"] == 1
    assert c["approvals"] == 1 and c["steps"] == 1 and c["started_dispatch"] == 0
    assert sandbox_sentinel["n"] == 0

    step = _tool_step(eng, rid)
    approval = _q(eng, lambda db: db.query(PendingApproval).one())
    assert run.pending_approval_id == approval.id
    assert step.approval_id == approval.id
    assert step.status == "waiting"
    assert step.input_ref and CODE_CANARY not in step.input_ref
    assert "PYTHON_CODE" not in (step.input_ref or "")


# ── Test 3 — encryption + plaintext canary ───────────────────────────
def test_payload_roundtrip_and_no_plaintext_leak(eng, monkeypatch, sandbox_sentinel, caplog):
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    prov = FakeChat([_py_script("", CODE)])
    rid = _seed(eng)
    _worker(eng, prov).advance_chat_runs()

    step = _tool_step(eng, rid)
    run = _run(eng, rid)
    opened = _q(eng, lambda db: open_payload(
        db, step.input_payload_id, expected_tenant_id=run.tenant_id,
        expected_user_id=run.user_id, expected_run_id=run.id,
        expected_step_id=step.id, expected_payload_type="tool_args",
        expected_action_hash=None))
    assert json.loads(opened) == {"code": CODE}
    assert CODE in opened

    blob = _leak_blob(eng, rid, caplog=caplog)
    assert CODE_CANARY not in blob, "raw code leaked into a plaintext surface"
    assert "print(" not in blob


def test_payload_binding_and_tamper_fail_closed(eng, monkeypatch, sandbox_sentinel):
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    prov = FakeChat([_py_script("", CODE)])
    rid = _seed(eng)
    _worker(eng, prov).advance_chat_runs()
    step = _tool_step(eng, rid)
    run = _run(eng, rid)

    def _open(db, **over):
        kw = dict(expected_tenant_id=run.tenant_id, expected_user_id=run.user_id,
                  expected_run_id=run.id, expected_step_id=step.id,
                  expected_payload_type="tool_args", expected_action_hash=None)
        kw.update(over)
        return open_payload(db, step.input_payload_id, **kw)

    for over in ({"expected_user_id": 999}, {"expected_run_id": "other"},
                 {"expected_step_id": "other"}, {"expected_payload_type": "tool_output"}):
        with pytest.raises(Exception) as ei:
            _q(eng, lambda db, o=over: _open(db, **o))
        assert CODE_CANARY not in str(ei.value)


def test_tampered_ciphertext_fails_closed(eng, monkeypatch, sandbox_sentinel):
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    prov = FakeChat([_py_script("", CODE)])
    rid = _seed(eng)
    _worker(eng, prov).advance_chat_runs()
    step = _tool_step(eng, rid)
    run = _run(eng, rid)

    def _tamper(db):
        row = db.query(RunPayload).filter(RunPayload.id == step.input_payload_id).one()
        row.ciphertext = ("A" if not row.ciphertext.startswith("A") else "B") + row.ciphertext[1:]
        db.commit()

    _q(eng, _tamper)
    with pytest.raises(Exception) as ei:
        _q(eng, lambda db: open_payload(
            db, step.input_payload_id, expected_tenant_id=run.tenant_id,
            expected_user_id=run.user_id, expected_run_id=run.id,
            expected_step_id=step.id, expected_payload_type="tool_args",
            expected_action_hash=None))
    assert CODE_CANARY not in str(ei.value)


# ── Test 5 — replay while waiting ────────────────────────────────────
def test_replay_while_waiting_creates_no_duplicates(eng, monkeypatch, sandbox_sentinel):
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    prov = FakeChat([_py_script("", CODE)])
    rid = _seed(eng)
    _worker(eng, prov).advance_chat_runs()
    before = _counts(eng, rid)
    pid = _run(eng, rid).pending_approval_id

    _worker(eng, prov, wid="w-r2a3-2").advance_chat_runs()
    after = _counts(eng, rid)
    assert after == before
    assert len(prov.calls) == 1, "replay must not re-invoke the provider"
    assert _run(eng, rid).pending_approval_id == pid
    assert _run(eng, rid).status == WAITING_FOR_APPROVAL


# ── Test 6 — changed code cannot reuse the existing action identity ──
def test_changed_code_cannot_reuse_existing_tool_call_identity(eng, monkeypatch, sandbox_sentinel):
    """Same persisted ToolCall / approval, DIFFERENT args -> the gateway must
    fail closed, never mint a second executable action."""
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    rid = _seed(eng)
    _worker(eng, FakeChat([_py_script("", CODE)])).advance_chat_runs()
    step = _tool_step(eng, rid)
    run = _run(eng, rid)
    approval_id = run.pending_approval_id
    before = _counts(eng, rid)
    original_hash = json.loads(step.input_ref)["args_hash"]

    other = "print('DIFFERENT-CODE-XYZ')"
    assert args_hash({"code": other}) != original_hash

    def _boom(*a, **k):
        raise AssertionError("executor called on a mismatched action")

    def _attempt(db):
        from app.approval.gateway import GatewayContext, dispatch as gw_dispatch
        ctx = GatewayContext(db=db, user_id=run.user_id, tenant_id=run.tenant_id,
                             session_id=step.session_id, run_id=run.id,
                             execution_turn_id=step.execution_turn_id,
                             tool_call_id=step.tool_call_id)
        return gw_dispatch(ctx, mcp_name=pai.PYTHON_TOOL_ID, args={"code": other},
                           execute_mcp=_boom, clock=None)

    res = _q(eng, _attempt)
    # REPOSITORY CONTRACT (observed, not invented): the gateway binds an action to
    # the args it is given, so changed code mints a DISTINCT action that needs its
    # own approval. The security property under test is that the ORIGINAL approval
    # is never reused, consumed, repointed or made to execute the new code.
    assert res.status == "approval_required"
    assert res.approval_id != approval_id, "changed code reused the original approval"
    after = _counts(eng, rid)
    assert after["calls"] == before["calls"] == 1, "no second ToolCall"
    assert after["turns"] == before["turns"] == 1, "no second ExecutionTurn"
    assert after["started_dispatch"] == 0
    assert sandbox_sentinel["n"] == 0
    orig = _q(eng, lambda db: db.query(PendingApproval)
              .filter(PendingApproval.id == approval_id).one())
    assert orig.state == "requested", "the original approval was consumed/reused"
    assert _run(eng, rid).pending_approval_id == approval_id, "Run repointed away"
    blob = _leak_blob(eng, rid)
    assert "DIFFERENT-CODE-XYZ" not in blob and CODE_CANARY not in blob


# ── Test 9 — operator-locked identity ────────────────────────────────
def test_operator_locked_identity_blocks_the_proposal(eng, monkeypatch, sandbox_sentinel):
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    rid = _seed(eng)

    def _lock(db):
        pai.ensure_python_approval_identity(db, 7)
        registry.lock_tool(db, owner_user_id=7, tool_id=pai.PYTHON_TOOL_ID,
                           reason="operator review")

    _q(eng, _lock)
    _worker(eng, FakeChat([_py_script("", CODE)])).advance_chat_runs()

    row = _q(eng, lambda db: db.query(TrustedTool)
             .filter(TrustedTool.tool_id == pai.PYTHON_TOOL_ID).one())
    assert row.locked is True and "newly discovered" not in (row.lock_reason or "")
    run = _run(eng, rid)
    assert run.status == "failed"
    assert run.status_reason in ("tool_locked", "gateway_error")
    assert _q(eng, lambda db: db.query(PendingApproval).count()) == 0
    assert sandbox_sentinel["n"] == 0
    assert _counts(eng, rid)["started_dispatch"] == 0
    blob = _leak_blob(eng, rid)
    assert CODE_CANARY not in blob and "PYTHON_CODE" not in blob


# ── Test 10 — drifted identity ───────────────────────────────────────
@pytest.mark.parametrize("kind", ["schema", "fingerprint"])
def test_drifted_identity_blocks_the_proposal(eng, monkeypatch, sandbox_sentinel, kind):
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    rid = _seed(eng)

    def _drift(db):
        pai.ensure_python_approval_identity(db, 7)
        kw = dict(owner_user_id=7, tool_id=pai.PYTHON_TOOL_ID,
                  tool_name=pai.PYTHON_TOOL_NAME,
                  mcp_server_name=pai.PYTHON_SERVER_MANIFEST_ID,
                  non_secret_launch_config={})
        if kind == "schema":
            registry.register_or_refresh(
                db, executable_identity=pai.PYTHON_EXECUTABLE_IDENTITY,
                config_ref_id=pai.PYTHON_CONFIG_REF_ID,
                input_schema={"type": "object",
                              "properties": {"code": {"type": "string"}},
                              "required": ["code"]}, **kw)
        else:
            registry.register_or_refresh(
                db, executable_identity="builtin:app.tools.python_sandbox@2",
                config_ref_id="builtin:python_sandbox:v2",
                input_schema=dict(pai.PYTHON_INPUT_SCHEMA), **kw)

    _q(eng, _drift)
    _worker(eng, FakeChat([_py_script("", CODE)])).advance_chat_runs()

    row = _q(eng, lambda db: db.query(TrustedTool)
             .filter(TrustedTool.tool_id == pai.PYTHON_TOOL_ID).one())
    assert row.locked is True, kind
    expected = "schema" if kind == "schema" else "fingerprint"
    assert expected in (row.lock_reason or ""), (kind, row.lock_reason)
    run = _run(eng, rid)
    assert run.status == "failed", kind
    assert run.status_reason in ("schema_mismatch", "fingerprint_mismatch",
                                 "tool_locked", "gateway_error"), run.status_reason
    assert _q(eng, lambda db: db.query(PendingApproval).count()) == 0
    assert sandbox_sentinel["n"] == 0
    assert _counts(eng, rid)["started_dispatch"] == 0
    blob = _leak_blob(eng, rid)
    assert CODE_CANARY not in blob and "PYTHON_CODE" not in blob


# ── Test 7 — cancellation before proposal (canonical helper) ─────────
def test_cancellation_before_proposal_creates_nothing(eng, monkeypatch, sandbox_sentinel):
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    rid = _seed(eng)

    def _cancel(db):
        from app.run_engine.engine import request_cancellation
        request_cancellation(db, make_ctx(db, user_id=7, sid="fam-alpha"), rid)

    _q(eng, _cancel)
    prov = FakeChat([_py_script("", CODE)])
    _worker(eng, prov).advance_chat_runs()
    c = _counts(eng, rid)
    assert c["turns"] == 0 and c["calls"] == 0 and c["approvals"] == 0
    assert c["steps"] == 0 and c["started_dispatch"] == 0
    assert sandbox_sentinel["n"] == 0
    assert _run(eng, rid).status != WAITING_FOR_APPROVAL
    blob = _leak_blob(eng, rid)
    assert CODE_CANARY not in blob and "PYTHON_CODE" not in blob


# ── Test 8 — revoked session before proposal (canonical helper) ──────
def test_revoked_session_before_proposal_creates_nothing(eng, monkeypatch, sandbox_sentinel):
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    rid = _seed(eng)
    _q(eng, lambda db: revoke_family(db, "fam-alpha"))
    prov = FakeChat([_py_script("", CODE)])
    _worker(eng, prov).advance_chat_runs()
    run = _run(eng, rid)
    c = _counts(eng, rid)
    assert c["turns"] == 0 and c["calls"] == 0 and c["approvals"] == 0
    assert c["steps"] == 0 and c["started_dispatch"] == 0
    assert sandbox_sentinel["n"] == 0
    assert run.status == "failed" and run.status_reason == "session_revoked"
    assistants = _q(eng, lambda db: db.query(Message)
                    .filter(Message.role == "assistant").count() if hasattr(Message.role, "desc") else
                    len([m for m in db.query(Message).all() if str(m.role).endswith("assistant")]))
    assert assistants == 0, "no assistant success Message"
    blob = _leak_blob(eng, rid)
    assert CODE_CANARY not in blob and "PYTHON_CODE" not in blob


# ── Test 11 — malformed / multiple / mixed / oversized ───────────────
@pytest.mark.parametrize("chunks,label", [
    (["ok [PYTHON_CODE: print(1)"], "incomplete"),
    (["ok [PYTHON_CODE: a] [PYTHON_CODE: b]\n" + TAIL], "multiple"),
    (["ok [PYTHON_CODE: a] [MCP_CALL: mcp__s__t]\n" + TAIL], "mixed"),
    (["ok [PYTHON_CODE: " + "x" * 8000 + "]\n" + TAIL], "oversized"),
])
def test_malformed_python_markers_fail_closed(eng, monkeypatch, sandbox_sentinel, chunks, label):
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    prov = FakeChat([chunks])
    rid = _seed(eng)
    _worker(eng, prov).advance_chat_runs()
    run = _run(eng, rid)
    assert run.status == "failed", label
    c = _counts(eng, rid)
    assert c["approvals"] == 0 and c["steps"] == 0 and c["calls"] == 0, label
    assert sandbox_sentinel["n"] == 0
    blob = _leak_blob(eng, rid)
    assert "PYTHON_CODE" not in blob, label
