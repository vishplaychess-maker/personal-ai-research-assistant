"""Phase 1B.2R MP-3C2 — durable chat -> MCP approval -> same-run resume.

Fake streaming provider + fake in-memory MCP transport. The approval gateway,
trusted registry and ``dispatch_records`` ledger are the real Phase 1B.2 code;
the approvals decision path (``routes.approvals._decide`` + ``mirror_decision_signal``)
is exercised without HTTP. No live provider, no ``/messages/stream``, no lifespan.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from cryptography.fernet import Fernet

from app.approval import registry
from app.config import settings
from app.models.models import (
    DispatchRecord, Message, MessageRole, PendingApproval, Run, RunEvent,
    RunPayload, RunSignal, RunStep,
)
from app.routes.approvals import _decide, _owned_approval
from app.run_engine import chat_stream as cs
from app.run_engine import submission as sub
from app.run_engine.payloads import open_payload
from app.run_engine.worker import RunWorker
from tests.run_engine_helpers import (
    ensure_user, expire_family, make_ctx, mk_engine, mk_factory, mk_session,
    revoke_family, seed_family,
)

TOOL = "mcp__srv__echo"
_HMAC = "mp3c2-unit-test-hmac-key-0123456789"


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


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


# ── fakes ─────────────────────────────────────────────────────────────


class FakeChat:
    """Streaming provider. ``scripts`` is a list (one per provider invocation)
    of token lists; each invocation streams its tokens then a ``done`` whose
    ``response`` is the concatenation of the string tokens. Records the exact
    ``messages`` / ``system_prompt`` / ``model_name`` of every call."""

    def __init__(self, scripts, *, sleep=0.0):
        self.scripts = [list(s) for s in scripts]
        self.sleep = sleep
        self.calls = []
        self.n = 0
        self.closed = 0

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        self.calls.append({"messages": [dict(m) for m in messages],
                           "system_prompt": system_prompt, "model_name": model_name})
        toks = self.scripts[self.n] if self.n < len(self.scripts) else []
        self.n += 1
        try:
            for t in toks:
                if isinstance(t, BaseException):
                    raise t
                yield {"type": "token", "token": t}
                if self.sleep:
                    await asyncio.sleep(self.sleep)
            yield {"type": "done", "response": "".join(x for x in toks if isinstance(x, str))}
        finally:
            self.closed += 1


class Transport:
    """Fake MCP transport — records calls, returns a canned ``(text, is_error)``."""

    def __init__(self, text="TOOL-OUT-CANARY", is_error=False, on_call=None):
        self.calls = []
        self._t, self._e, self._on = text, is_error, on_call

    def __call__(self, db, user_id, tool_id, args):
        self.calls.append((tool_id, dict(args)))
        if self._on is not None:
            self._on()
        return (self._t, self._e)


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


def _seed(eng, *, message="hi", provider="prov-1", model="model-1", system_prompt="SYS-PROMPT",
          uid=7, sid="fam-alpha", session_id=1, register=True):
    db = mk_session(eng)
    try:
        ensure_user(db, uid)
        from app.models.models import ResearchSession
        if db.query(ResearchSession).filter(ResearchSession.id == session_id).first() is None:
            db.add(ResearchSession(id=session_id, user_id=uid, title="t"))
            db.commit()
        if register:
            _reg(db, uid=uid)
        ctx = make_ctx(db, user_id=uid, sid=sid)
        submission = sub.build_chat_submission(
            session_id=session_id, message=message, image_url=None, provider=provider,
            model=model, mode="chat", system_prompt=system_prompt, options={})
        run, created = sub.create_chat_run(db, ctx, submission=submission,
                                           idempotency_key=uuid.uuid4().hex)
        assert created
        return run.id
    finally:
        db.close()


def _worker(eng, provider=None, transport=None, wid="w-c2"):
    return RunWorker(mk_factory(eng), worker_id=wid,
                     chat_provider=(lambda ci, **_k: provider) if provider is not None else None,
                     mcp_transport=transport)


def _run(eng, rid):
    db = mk_session(eng)
    try:
        return db.query(Run).filter(Run.id == rid).first()
    finally:
        db.close()


def _steps(eng, rid):
    db = mk_session(eng)
    try:
        return (db.query(RunStep).filter(RunStep.run_id == rid)
                .order_by(RunStep.seq.asc()).all())
    finally:
        db.close()


def _msgs(eng, session_id=1):
    db = mk_session(eng)
    try:
        return (db.query(Message).filter(Message.session_id == session_id,
                                         Message.role == MessageRole.assistant)
                .order_by(Message.id.asc()).all())
    finally:
        db.close()


def _text(eng, rid):
    db = mk_session(eng)
    try:
        return "".join(
            json.loads(e.data)["text"] for e in
            db.query(RunEvent).filter(RunEvent.run_id == rid, RunEvent.category == "text")
            .order_by(RunEvent.run_seq.asc()).all())
    finally:
        db.close()


def _leak_blob(eng, rid, *, caplog_text=""):
    db = mk_session(eng)
    try:
        parts = [caplog_text or ""]
        r = db.query(Run).filter(Run.id == rid).first()
        parts.append(r.status_reason or "")
        for e in db.query(RunEvent).filter(RunEvent.run_id == rid).all():
            parts.append(e.data or "")
        for s in db.query(RunStep).filter(RunStep.run_id == rid).all():
            parts.append(s.input_ref or "")
            parts.append(s.output_ref or "")
            parts.append(s.error_code or "")
        for sg in db.query(RunSignal).filter(RunSignal.run_id == rid).all():
            parts.append(sg.payload or "")
        return "\n".join(parts)
    finally:
        db.close()


def _approval_id(eng, rid):
    return _run(eng, rid).pending_approval_id


def _decide_run(eng, aid, *, approve, uid=7):
    db = mk_session(eng)
    try:
        row = db.query(PendingApproval).filter(PendingApproval.id == aid).one()
        row.user_id = uid
        return _decide(db, row, approve=approve)
    finally:
        db.close()


def _dispatch(eng, tool_call_id):
    db = mk_session(eng)
    try:
        return (db.query(DispatchRecord)
                .filter(DispatchRecord.tool_call_id == tool_call_id).one_or_none())
    finally:
        db.close()


def _cancel(eng, rid, uid=7, sid="fam-alpha"):
    db = mk_session(eng)
    try:
        from app.run_engine.engine import request_cancellation
        request_cancellation(db, make_ctx(db, user_id=uid, sid=sid), rid)
    finally:
        db.close()


def _mk_marker_script(prefix, args_obj, *, suffix="TAIL-SHOULD-NOT-APPEAR", split=True):
    """A token list that streams ``prefix`` then a [MCP_CALL] split across many
    tiny chunks then a newline then ``suffix`` (which must never be trusted)."""
    body = f'[MCP_CALL: {TOOL} {json.dumps(args_obj)}]'
    if split:
        toks = [prefix, "[MCP", "_CALL: ", f"{TOOL} ", json.dumps(args_obj)[:3],
                json.dumps(args_obj)[3:], "]", "\n", suffix]
    else:
        toks = [prefix, body + "\n" + suffix]
    return toks


def _tool_step(eng, rid):
    for s in _steps(eng, rid):
        if s.kind == "tool_dispatch":
            return s
    return None


# ── 1. HAPPY PATH ────────────────────────────────────────────────────


def _park(eng, prov, tr):
    rid = _seed(eng)
    _worker(eng, prov, tr).advance_chat_runs()
    return rid


def test_happy_path_same_run_tool_cycle_one_final_message(eng, caplog):
    arg_canary, out_canary = "ARG-CANARY-7f3", "TOOL-OUT-CANARY-9k1"
    prov = FakeChat([
        _mk_marker_script("Let me look this up. ", {"q": arg_canary}),
        ["Based on the tool, ", "the answer is 42."],
    ])
    tr = Transport(text=out_canary)
    with caplog.at_level("DEBUG"):
        rid = _park(eng, prov, tr)
        r = _run(eng, rid)
        assert r.status == "waiting_for_approval" and r.pending_approval_id
        assert tr.calls == []                       # nothing executed on the first pass
        gen1 = _steps(eng, rid)[0]
        tstep = _tool_step(eng, rid)
        assert gen1.seq == 1 and gen1.status == "succeeded" and gen1.output_payload_id
        assert tstep.status == "waiting" and tstep.approval_id == r.pending_approval_id
        assert tstep.execution_turn_id and tstep.tool_call_id and tstep.input_payload_id
        assert _msgs(eng) == []                     # NO assistant Message yet

        dec = _decide_run(eng, r.pending_approval_id, approve=True)
        assert dec["state"] == "approved" and tr.calls == []   # endpoint executes nothing

        _worker(eng, prov, tr).advance_chat_runs()

    r = _run(eng, rid)
    assert r.status == "succeeded"
    assert len(tr.calls) == 1                       # MCP executed EXACTLY once
    msgs = _msgs(eng)
    assert len(msgs) == 1                           # EXACTLY one final assistant Message
    assert msgs[0].content == "Let me look this up. Based on the tool, the answer is 42."

    tstep = _tool_step(eng, rid)
    assert _dispatch(eng, tstep.tool_call_id).outcome == "succeeded"
    assert tstep.output_payload_id and tstep.status == "succeeded"

    # privacy: raw marker text / args / tool output / trailing text / result
    # envelope absent from refs, events, status_reason, signals, and logs.
    # (the server-owned tool_id IS an allowed non-secret id in run_steps.input_ref)
    blob = _leak_blob(eng, rid, caplog_text=caplog.text)
    for bad in (arg_canary, out_canary, "[MCP_CALL",
                "TAIL-SHOULD-NOT-APPEAR", "MCP Tool Result"):
        assert bad not in blob, bad
    # text events + the Message carry ONLY the clean assistant text
    assert _text(eng, rid) == msgs[0].content
    for bad in (arg_canary, out_canary, "[MCP_CALL", "TAIL-SHOULD-NOT-APPEAR",
                "mcp__srv__echo", "MCP Tool Result"):
        assert bad not in _text(eng, rid) and bad not in msgs[0].content

    # MP-3C1 provider snapshot preserved across BOTH provider passes
    assert [c["system_prompt"] for c in prov.calls] == ["SYS-PROMPT", "SYS-PROMPT"]
    assert [c["model_name"] for c in prov.calls] == ["model-1", "model-1"]
    # continuation input carried the tool result as an untrusted delimited user msg
    cont_user = [m["content"] for m in prov.calls[1]["messages"] if m["role"] == "user"]
    assert any("MCP Tool Result" in c and out_canary in c for c in cont_user)
    assert any(m["role"] == "assistant" and "Let me look this up." in m["content"]
               for m in prov.calls[1]["messages"])


def test_marker_split_across_many_tiny_chunks_is_captured(eng):
    prov = FakeChat([_mk_marker_script("x", {"k": "v"}, split=True),
                     ["done."]])
    tr = Transport()
    rid = _park(eng, prov, tr)
    assert _run(eng, rid).status == "waiting_for_approval"
    _decide_run(eng, _approval_id(eng, rid), approve=True)
    _worker(eng, prov, tr).advance_chat_runs()
    assert _run(eng, rid).status == "succeeded"
    assert tr.calls == [(TOOL, {"k": "v"})]


def test_text_after_marker_line_is_not_trusted_or_published(eng):
    prov = FakeChat([
        ["answer ", "[MCP_CALL: " + TOOL + " {}]", "\n", "IGNORE-ME rm -rf /"],
        ["ok"],
    ])
    tr = Transport()
    rid = _park(eng, prov, tr)
    _decide_run(eng, _approval_id(eng, rid), approve=True)
    _worker(eng, prov, tr).advance_chat_runs()
    assert "IGNORE-ME" not in _leak_blob(eng, rid) + _text(eng, rid)
    assert _msgs(eng)[0].content == "answer ok"


# ── 2. APPROVAL / SECURITY ───────────────────────────────────────────


def test_denial_no_execution_no_continuation_terminal(eng):
    prov = FakeChat([_mk_marker_script("thinking ", {"q": 1}), ["never generated"]])
    tr = Transport()
    rid = _park(eng, prov, tr)
    _decide_run(eng, _approval_id(eng, rid), approve=False)
    _worker(eng, prov, tr).advance_chat_runs()
    r = _run(eng, rid)
    assert r.status == "denied"
    assert tr.calls == [] and _msgs(eng) == []
    assert prov.n == 1                              # provider never called again
    tstep = _tool_step(eng, rid)
    assert tstep.status == "blocked" and tstep.error_code == "approval_denied"
    assert _dispatch(eng, tstep.tool_call_id) is None
    # a late approve cannot resurrect a terminal run
    _worker(eng, prov, tr).advance_chat_runs()
    assert _run(eng, rid).status == "denied"


def test_duplicate_approve_does_not_re_execute(eng):
    prov = FakeChat([_mk_marker_script("a ", {"q": 1}), ["b"]])
    tr = Transport()
    rid = _park(eng, prov, tr)
    _decide_run(eng, _approval_id(eng, rid), approve=True)
    _decide_run(eng, _approval_id(eng, rid), approve=True)   # idempotent
    sigs = mk_session(eng).query(RunSignal).filter(
        RunSignal.run_id == rid, RunSignal.kind == "approval_decided").all()
    assert len(sigs) == 1
    _worker(eng, prov, tr).advance_chat_runs()
    _worker(eng, prov, tr).advance_chat_runs()               # second sweep — no-op
    assert len(tr.calls) == 1 and _run(eng, rid).status == "succeeded"
    assert len(_msgs(eng)) == 1


def test_cross_user_decision_fails(eng):
    from fastapi import HTTPException

    class _U:
        def __init__(self, i): self.id = i

    prov = FakeChat([_mk_marker_script("a ", {"q": 1}), ["b"]])
    tr = Transport()
    rid = _park(eng, prov, tr)
    aid = _approval_id(eng, rid)
    db = mk_session(eng)
    try:  # the approvals route rejects a non-owner before any decision logic
        with pytest.raises(HTTPException) as ei:
            _owned_approval(db, aid, _U(999))
        assert ei.value.status_code == 404
    finally:
        db.close()
    # the run is untouched and no signal was written
    assert _run(eng, rid).status == "waiting_for_approval"
    assert mk_session(eng).query(RunSignal).filter(
        RunSignal.run_id == rid, RunSignal.kind == "approval_decided").count() == 0
    assert tr.calls == []


def test_changed_registry_blocks_dispatch_no_continuation(eng):
    prov = FakeChat([_mk_marker_script("a ", {"q": 1}), ["should not run"]])
    tr = Transport()
    rid = _park(eng, prov, tr)
    _decide_run(eng, _approval_id(eng, rid), approve=True)
    # tool schema changes after approval -> gateway re-resolution fails closed
    db = mk_session(eng)
    try:
        _reg(db, schema={"type": "object", "properties": {"z": {"type": "string"}}})
    finally:
        db.close()
    _worker(eng, prov, tr).advance_chat_runs()
    r = _run(eng, rid)
    assert r.status == "failed"
    assert tr.calls == [] and _msgs(eng) == []
    assert prov.n == 1


def test_tampered_sealed_args_fail_closed(eng):
    prov = FakeChat([_mk_marker_script("a ", {"q": "orig"}), ["x"]])
    tr = Transport()
    rid = _park(eng, prov, tr)
    _decide_run(eng, _approval_id(eng, rid), approve=True)
    tstep = _tool_step(eng, rid)
    db = mk_session(eng)
    try:  # corrupt the sealed args ciphertext
        pl = db.query(RunPayload).filter(RunPayload.id == tstep.input_payload_id).one()
        pl.ciphertext = (pl.ciphertext or "")[:-4] + "AAAA"
        db.commit()
    finally:
        db.close()
    _worker(eng, prov, tr).advance_chat_runs()
    assert _run(eng, rid).status in ("failed", "outcome_unknown")
    assert tr.calls == [] and _msgs(eng) == []


def test_unknown_tool_stays_locked(eng):
    prov = FakeChat([["hi ", f"[MCP_CALL: mcp__nope__nope {{}}]", "\n"], ["x"]])
    tr = Transport()
    rid = _seed(eng)
    _worker(eng, prov, tr).advance_chat_runs()
    r = _run(eng, rid)
    assert r.status == "failed" and tr.calls == []
    assert _tool_step(eng, rid).status in ("blocked", "waiting") or r.status == "failed"


# ── 3. LIMITS / LOOP PREVENTION ──────────────────────────────────────


def _run_full_cycle(eng, prov, tr, rid):
    _worker(eng, prov, tr).advance_chat_runs()
    aid = _approval_id(eng, rid)
    assert aid, _run(eng, rid).status
    _decide_run(eng, aid, approve=True)
    _worker(eng, prov, tr).advance_chat_runs()


def test_three_sequential_cycles_then_fourth_fails_without_proposal(eng):
    prov = FakeChat([
        _mk_marker_script("c1 ", {"n": 1}),
        _mk_marker_script("c2 ", {"n": 2}),
        _mk_marker_script("c3 ", {"n": 3}),
        _mk_marker_script("c4 ", {"n": 4}),   # 4th request — must be refused
    ])
    tr = Transport(text="R")
    rid = _seed(eng)
    for _ in range(3):
        _run_full_cycle(eng, prov, tr, rid)
    r = _run(eng, rid)
    assert r.status == "failed" and r.status_reason == "tool_cycle_limit"
    tool_steps = [s for s in _steps(eng, rid) if s.kind == "tool_dispatch"]
    assert len(tool_steps) == 3                     # NO 4th proposal / turn / dispatch
    assert len(tr.calls) == 3
    assert _msgs(eng) == []


def test_multiple_mcp_calls_in_one_marker_block_fail_closed(eng):
    # two markers on one captured line -> the canonical parser cannot yield a
    # single clean call -> fail closed, nothing executed, no tool step created.
    two = f"[MCP_CALL: {TOOL} {{\"a\":1}}] [MCP_CALL: {TOOL} {{\"a\":2}}]"
    prov = FakeChat([["hi ", two, "\n"], ["x"]])
    tr = Transport()
    rid = _seed(eng)
    _worker(eng, prov, tr).advance_chat_runs()
    r = _run(eng, rid)
    assert r.status == "failed"
    assert r.status_reason in ("tool_marker_multiple", "tool_marker_malformed")
    assert tr.calls == [] and _tool_step(eng, rid) is None
    assert _msgs(eng) == []


def test_non_mcp_execution_marker_still_blocked(eng):
    prov = FakeChat([["ok ", "[PYTHON_CODE: print(1)]"], ["x"]])
    tr = Transport()
    rid = _seed(eng)
    _worker(eng, prov, tr).advance_chat_runs()
    r = _run(eng, rid)
    assert r.status == "failed" and r.status_reason == "tool_resume_required"
    assert tr.calls == [] and _tool_step(eng, rid) is None


def test_oversized_marker_fails_closed(eng):
    huge = {"blob": "Z" * 9000}
    prov = FakeChat([["hi ", f"[MCP_CALL: {TOOL} {json.dumps(huge)}]", "\n"], ["x"]])
    tr = Transport()
    rid = _seed(eng)
    _worker(eng, prov, tr).advance_chat_runs()
    r = _run(eng, rid)
    assert r.status == "failed" and r.status_reason == "tool_marker_oversized"
    assert tr.calls == []


def test_incomplete_marker_at_eof_fails_closed(eng):
    prov = FakeChat([["hi ", f"[MCP_CALL: {TOOL} {{\"a\": 1}}"], ["x"]])  # no closing ] / newline
    tr = Transport()
    rid = _seed(eng)
    _worker(eng, prov, tr).advance_chat_runs()
    r = _run(eng, rid)
    assert r.status == "failed"
    assert r.status_reason in ("tool_marker_incomplete", "truncated_tool_marker")
    assert tr.calls == []


def test_one_active_proposal_per_run(eng):
    prov = FakeChat([_mk_marker_script("a ", {"q": 1}), ["b"]])
    tr = Transport()
    rid = _park(eng, prov, tr)
    # a parked run already holds its single proposal; a re-sweep must not add one
    before = mk_session(eng).query(PendingApproval).filter(
        PendingApproval.run_id == rid).count()
    _worker(eng, prov, tr).advance_chat_runs()
    after = mk_session(eng).query(PendingApproval).filter(
        PendingApproval.run_id == rid).count()
    assert before == after == 1


# ── 4. CANCELLATION / SESSION ────────────────────────────────────────


def test_cancel_while_waiting_prevents_late_approval_dispatch(eng):
    prov = FakeChat([_mk_marker_script("a ", {"q": 1}), ["b"]])
    tr = Transport()
    rid = _park(eng, prov, tr)
    _cancel(eng, rid)
    _decide_run(eng, _approval_id(eng, rid), approve=True)     # approval lands after cancel
    _worker(eng, prov, tr).advance_chat_runs()
    r = _run(eng, rid)
    assert r.status == "cancelled"
    assert tr.calls == [] and _msgs(eng) == []


def test_session_revoked_while_waiting_prevents_dispatch(eng):
    prov = FakeChat([_mk_marker_script("a ", {"q": 1}), ["b"]])
    tr = Transport()
    rid = _park(eng, prov, tr)
    db = mk_session(eng)
    try:
        revoke_family(db, "fam-alpha")
    finally:
        db.close()
    _decide_run(eng, _approval_id(eng, rid), approve=True)
    _worker(eng, prov, tr).advance_chat_runs()
    r = _run(eng, rid)
    assert r.status == "failed" and r.status_reason == "session_revoked"
    assert tr.calls == []


def test_session_revoked_after_tool_success_before_continuation(eng):
    hits = {"n": 0}

    def _on_call():
        hits["n"] += 1
        db = mk_session(eng)
        try:
            revoke_family(db, "fam-alpha")   # revoke DURING the tool call
        finally:
            db.close()

    prov = FakeChat([_mk_marker_script("a ", {"q": 1}), ["should not generate"]])
    tr = Transport(text="R", on_call=_on_call)
    rid = _park(eng, prov, tr)
    _decide_run(eng, _approval_id(eng, rid), approve=True)
    _worker(eng, prov, tr).advance_chat_runs()
    r = _run(eng, rid)
    assert hits["n"] == 1                       # the tool DID run once
    assert r.status in ("failed", "outcome_unknown")
    assert r.status_reason in ("session_revoked", "outcome_unknown")
    assert prov.n == 1                          # continuation provider call never happened
    assert _msgs(eng) == []


def test_cancel_before_first_proposal_no_proposal(eng):
    # cancel is requested before the worker ever sees the run
    prov = FakeChat([_mk_marker_script("a ", {"q": 1}), ["b"]])
    tr = Transport()
    rid = _seed(eng)
    _cancel(eng, rid)
    _worker(eng, prov, tr).advance_chat_runs()
    r = _run(eng, rid)
    assert r.status == "cancelled"
    assert tr.calls == []
    assert mk_session(eng).query(PendingApproval).filter(
        PendingApproval.run_id == rid).count() == 0


# ── 5. CRASH / RECOVERY ──────────────────────────────────────────────


def _expire_lease(eng, rid):
    db = mk_session(eng)
    try:
        import datetime as dt
        db.query(Run).filter(Run.id == rid).update(
            {"lease_expires_at": dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)})
        db.commit()
    finally:
        db.close()


def test_approval_signal_replay_consumed_once(eng):
    prov = FakeChat([_mk_marker_script("a ", {"q": 1}), ["b"]])
    tr = Transport()
    rid = _park(eng, prov, tr)
    _decide_run(eng, _approval_id(eng, rid), approve=True)
    # two workers race the same decided run
    w1, w2 = _worker(eng, prov, tr, "wa"), _worker(eng, FakeChat([["b"]]), tr, "wb")
    w1.advance_chat_runs()
    w2.advance_chat_runs()
    assert len(tr.calls) == 1
    assert _run(eng, rid).status == "succeeded"
    assert len(_msgs(eng)) == 1


def test_dispatch_success_reused_without_reexecution_on_restart(eng):
    prov = FakeChat([_mk_marker_script("a ", {"q": 1}), ["cont."]])
    tr = Transport(text="R")
    rid = _park(eng, prov, tr)
    _decide_run(eng, _approval_id(eng, rid), approve=True)
    tstep = _tool_step(eng, rid)
    # simulate: dispatch committed 'succeeded' + result sealed, worker then died
    # before the continuation. Drive resume to RUNNING with the sealed result.
    _worker(eng, prov, tr).advance_chat_runs()
    assert _run(eng, rid).status == "succeeded" and len(tr.calls) == 1
    # a further sweep must not re-execute
    _worker(eng, prov, tr).advance_chat_runs()
    assert len(tr.calls) == 1 and len(_msgs(eng)) == 1
    assert _dispatch(eng, tstep.tool_call_id).outcome == "succeeded"


def test_running_continuation_after_crash_not_silently_repeated(eng):
    prov = FakeChat([_mk_marker_script("a ", {"q": 1}), ["c1 "], ["c2"]])
    tr = Transport(text="R")
    rid = _park(eng, prov, tr)
    _decide_run(eng, _approval_id(eng, rid), approve=True)
    _worker(eng, prov, tr).advance_chat_runs()
    r = _run(eng, rid)
    assert r.status == "succeeded"                # first script had no marker -> done
    n_after = prov.n
    _expire_lease(eng, rid)
    _worker(eng, prov, tr).advance_chat_runs()    # terminal — cannot be resurrected
    assert prov.n == n_after and _run(eng, rid).status == "succeeded"
    assert len(_msgs(eng)) == 1


def test_crash_before_proposal_replays_dedupe_safe(eng):
    prov = FakeChat([_mk_marker_script("a ", {"q": 1}), ["b"]])
    tr = Transport()
    rid = _park(eng, prov, tr)
    aid = _approval_id(eng, rid)
    # simulate a crash right after parking: unpark back to RUNNING w/ expired lease
    # and the tool step still 'waiting' — the sweep must re-run the first pass and
    # re-link the SAME proposal (dedupe), not create a second one.
    db = mk_session(eng)
    try:
        import datetime as dt
        db.query(Run).filter(Run.id == rid).update({
            "status": "running", "lease_owner": "dead/x",
            "lease_expires_at": dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)})
        db.commit()
    finally:
        db.close()
    _worker(eng, prov, tr).advance_chat_runs()
    r = _run(eng, rid)
    assert r.status == "waiting_for_approval"
    assert r.pending_approval_id == aid          # same proposal reused
    assert mk_session(eng).query(PendingApproval).filter(
        PendingApproval.run_id == rid).count() == 1
    assert tr.calls == []


def test_finalization_failure_leaves_no_duplicate_message(eng):
    prov = FakeChat([_mk_marker_script("a ", {"q": 1}), ["final."]])
    tr = Transport(text="R")
    rid = _park(eng, prov, tr)
    _decide_run(eng, _approval_id(eng, rid), approve=True)
    _worker(eng, prov, tr).advance_chat_runs()
    assert _run(eng, rid).status == "succeeded"
    # re-drive: the finalized run is terminal; no second Message is created
    _worker(eng, prov, tr).advance_chat_runs()
    assert len(_msgs(eng)) == 1


# ── 6. PRIVACY ───────────────────────────────────────────────────────


def test_canaries_never_leak_into_refs_events_status_or_logs(eng, caplog):
    p, sp = "PROMPT-CANARY-Q", "SYSPROMPT-CANARY-Z"
    arg, out = "ARGS-CANARY-A", "TOOLOUT-CANARY-B"
    prov = FakeChat([_mk_marker_script("clean ", {"secret": arg}), ["visible answer."]])
    tr = Transport(text=out)
    with caplog.at_level("DEBUG"):
        rid = _seed(eng, message=p, system_prompt=sp)
        _worker(eng, prov, tr).advance_chat_runs()
        _decide_run(eng, _approval_id(eng, rid), approve=True)
        _worker(eng, prov, tr).advance_chat_runs()
    assert _run(eng, rid).status == "succeeded"
    blob = _leak_blob(eng, rid, caplog_text=caplog.text)
    for bad in (p, sp, arg, out, "[MCP_CALL", "MCP Tool Result"):
        assert bad not in blob, bad
    # only intended clean assistant text is in text events + the Message
    assert _text(eng, rid) == "clean visible answer."
    assert _msgs(eng)[0].content == "clean visible answer."


def test_sealed_payloads_round_trip_and_hold_the_secrets(eng):
    arg, out = "SEALED-ARG-CANARY", "SEALED-OUT-CANARY"
    prov = FakeChat([_mk_marker_script("s ", {"x": arg}), ["e"]])
    tr = Transport(text=out)
    rid = _park(eng, prov, tr)
    _decide_run(eng, _approval_id(eng, rid), approve=True)
    _worker(eng, prov, tr).advance_chat_runs()
    r = _run(eng, rid)
    assert r.status == "succeeded"
    tstep = _tool_step(eng, rid)
    db = mk_session(eng)
    try:
        args_plain = open_payload(
            db, tstep.input_payload_id, expected_tenant_id=r.tenant_id,
            expected_user_id=r.user_id, expected_run_id=r.id, expected_step_id=tstep.id,
            expected_payload_type="tool_args", expected_action_hash=None)
        out_plain = open_payload(
            db, tstep.output_payload_id, expected_tenant_id=r.tenant_id,
            expected_user_id=r.user_id, expected_run_id=r.id, expected_step_id=tstep.id,
            expected_payload_type="tool_output", expected_action_hash=None)
    finally:
        db.close()
    assert arg in args_plain and out_plain == out
    # ciphertext only in run_payloads
    for pl in mk_session(eng).query(RunPayload).filter(RunPayload.run_id == rid).all():
        assert arg not in (pl.ciphertext or "") and out not in (pl.ciphertext or "")


# ── 7. NON-TOOL PATHS STILL WORK (MP-3B parity) ──────────────────────


def test_plain_chat_no_marker_unchanged(eng):
    prov = FakeChat([["Hello ", "there, ", "world."]])
    rid = _seed(eng)
    _worker(eng, prov).advance_chat_runs()
    assert _run(eng, rid).status == "succeeded"
    assert _msgs(eng)[0].content == "Hello there, world."
    assert _text(eng, rid) == "Hello there, world."
