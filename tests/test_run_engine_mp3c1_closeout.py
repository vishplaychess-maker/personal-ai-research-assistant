"""Phase 1B.2R MP-3C1 targeted close-out.

1. reserved-marker boundary proof (repo-derived list, every char split)
2. incomplete marker prefix at EOF -> fail closed, never emitted
3. dedicated-thread / DB-session ownership proof
4. provider-key isolation spot check

Fake providers only; no live provider, no /messages/stream, no tool execution.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import inspect
import json
import threading
import time
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import update

from app.config import settings
from app.models.models import Message, MessageRole, Run, RunEvent, RunPayload, RunStep
from app.run_engine import chat_stream as cs
from app.run_engine import submission as sub
from app.run_engine.payloads import open_payload
from app.run_engine.worker import RunWorker
from app.skills.loader import _MARKER_OPENS
from tests.run_engine_helpers import ensure_user, make_ctx, mk_engine, mk_factory, mk_session

_HMAC = "unit-test-hmac-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 30)
    monkeypatch.setattr(settings, "run_worker_handler_timeout_s", 300)


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


class FakeProvider:
    def __init__(self, tokens, *, response=None, error=False, raises=None, raise_at=0,
                 on_chunk=None, async_sleep=0.0, presleep=0.0):
        self.tokens, self.response, self.error = list(tokens), response, error
        self.raises, self.raise_at, self.on_chunk = raises, raise_at, on_chunk
        self.async_sleep, self.presleep = async_sleep, presleep
        self.calls = 0
        self.closed = False
        self.iter_thread = None

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        self.calls += 1
        self.iter_thread = threading.get_ident()
        try:
            if self.presleep:
                await asyncio.sleep(self.presleep)
            for i, tok in enumerate(self.tokens):
                if self.raises is not None and i == self.raise_at:
                    raise self.raises
                yield {"type": "token", "token": tok}
                if self.on_chunk is not None:
                    self.on_chunk(i)
                if self.async_sleep:
                    await asyncio.sleep(self.async_sleep)
            if self.error:
                yield {"type": "error", "error": "boom UPSTREAM-SECRET"}
                return
            yield {"type": "done",
                   "response": self.response if self.response is not None
                   else "".join(self.tokens)}
        finally:
            self.closed = True


def _submission(*, message="hi there", provider="snap-prov", model="snap-model",
                system_prompt="SNAP-SP", options=None, session_id=1):
    return sub.build_chat_submission(
        session_id=session_id, message=message, image_url=None, provider=provider,
        model=model, mode="chat", system_prompt=system_prompt, options=options or {})


def _seed(eng, *, submission=None, uid=7, sid=1):
    db = mk_session(eng)
    try:
        ensure_user(db, uid)
        from app.models.models import ResearchSession
        if db.query(ResearchSession).filter(ResearchSession.id == sid).first() is None:
            db.add(ResearchSession(id=sid, user_id=uid, title="t"))
            db.commit()
        ctx = make_ctx(db, user_id=uid, sid="fam-alpha")
        run, created = sub.create_chat_run(db, ctx, submission=submission or _submission(),
                                           idempotency_key=uuid.uuid4().hex)
        assert created
        return run.id
    finally:
        db.close()


def _worker(eng, provider, **kw):
    return RunWorker(mk_factory(eng), worker_id=kw.pop("worker_id", "w-1"),
                     chat_provider=lambda ci, **_k: provider, **kw)


def _events_blob(eng, rid):
    db = mk_session(eng)
    try:
        return "\n".join(e.data or "" for e in
                         db.query(RunEvent).filter(RunEvent.run_id == rid).all())
    finally:
        db.close()


def _text(eng, rid):
    db = mk_session(eng)
    try:
        return "".join(json.loads(e.data)["text"] for e in
                       db.query(RunEvent).filter(RunEvent.run_id == rid,
                                                 RunEvent.category == "text")
                       .order_by(RunEvent.run_seq.asc()).all())
    finally:
        db.close()


def _assistant(eng, sid=1):
    db = mk_session(eng)
    try:
        return db.query(Message).filter(Message.session_id == sid,
                                        Message.role == MessageRole.assistant).all()
    finally:
        db.close()


def _run_step(eng, rid):
    db = mk_session(eng)
    try:
        return (db.query(Run).filter(Run.id == rid).first(),
                db.query(RunStep).filter(RunStep.run_id == rid).first())
    finally:
        db.close()


# ── 1. reserved-marker list is repo-derived ─────────────────────


def test_marker_list_is_repo_derived_execution_subset():
    assert set(cs.ACTION_MARKERS) == {"[mcp_call", "[python_code",
                                      "[proposed_command", "[browser_action"}
    assert all(m in _MARKER_OPENS for m in cs.ACTION_MARKERS)   # not an independent list
    src = inspect.getsource(cs)
    assert "_REPO_MARKER_OPENS" in src and "_MARKER_OPENS" in src


# ── 1. full split-boundary matrix over every reserved marker ────

_CANARY = "SEKRIT-ARG-9f3"
_INSTANCES = {
    "[mcp_call": f'[MCP_CALL: mcp__srv__tool {{"k":"{_CANARY}"}}]',
    "[python_code": f'[PYTHON_CODE: print("{_CANARY}")]',
    "[proposed_command": f'[PROPOSED_COMMAND: rm {_CANARY}]',
    "[browser_action": f'[BROWSER_ACTION: navigate {_CANARY}]',
}

_SPLIT_COUNT = 0
for _m, _inst in _INSTANCES.items():
    _SPLIT_COUNT += len(_inst) - 1  # every interior char boundary


@pytest.mark.parametrize("opener,instance", list(_INSTANCES.items()))
def test_marker_every_split_boundary_and_positions(opener, instance):
    # (a) every interior character split -> tripped, no marker/arg text emitted
    for cut in range(1, len(instance)):
        sc = cs.MarkerScanner()
        out = sc.feed(instance[:cut]) + sc.feed(instance[cut:])
        assert sc.tripped, f"{opener} not tripped at cut {cut}"
        assert "[" not in out and _CANARY not in out
        assert opener[1:4] not in out.lower()          # no partial opener chars leak

    # (b) at stream start (single chunk)
    sc = cs.MarkerScanner()
    assert sc.feed(instance) == "" and sc.tripped

    # (c) after ordinary clean text -> clean text preserved, marker not
    sc = cs.MarkerScanner()
    got = sc.feed("Here you go: ") + sc.feed(instance)
    assert got == "Here you go: " and sc.tripped

    # (d) many tiny 1-char chunks
    sc = cs.MarkerScanner()
    out = "".join(sc.feed(ch) for ch in ("prefix " + instance))
    assert sc.tripped and out == "prefix " and _CANARY not in out


def test_split_boundary_matrix_size():
    # documents how many distinct char-split boundaries the matrix above covers
    assert _SPLIT_COUNT >= 100 and len(_INSTANCES) == len(cs.ACTION_MARKERS)


def test_marker_args_absent_from_events_refs_status_and_logs(eng, caplog):
    # MP-3C2 supersedes the MP-3C1 "fail closed on any [MCP_CALL]" behavior: the
    # marker is now confidentially captured, its args sealed in an encrypted
    # RunPayload, and a tool_dispatch step opened. The tool here is unregistered
    # so the gateway blocks it (unknown_tool). The privacy invariant is
    # unchanged: the raw canary arg and the raw marker text never reach any
    # run_events row, run_steps ref, runs.status_reason, or the logs.
    rid = _seed(eng)
    fp = FakeProvider(["ok so far ", _INSTANCES["[mcp_call"], " trailing"])
    with caplog.at_level("DEBUG"):
        out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "failed" and out[0].detail == "unknown_tool"
    assert _assistant(eng) == []
    txt = _text(eng, rid)
    assert txt == "ok so far " and "[" not in txt
    db = mk_session(eng)
    try:
        run = db.query(Run).filter(Run.id == rid).first()
        refs = "\n".join((s.input_ref or "") + "\n" + (s.output_ref or "")
                         for s in db.query(RunStep).filter(RunStep.run_id == rid).all())
    finally:
        db.close()
    for field in (_events_blob(eng, rid), refs, run.status_reason or "", caplog.text):
        assert _CANARY not in field and "[MCP_CALL" not in field


# ── 2. incomplete marker prefix at EOF ─────────────────────────


@pytest.mark.parametrize("prefix", ["[", "[MCP_", "[PYTHON_", "[PROPOSED_COM", "[BROWSER_"])
def test_eof_incomplete_marker_prefix_blocks_and_is_not_emitted(eng, prefix):
    rid = _seed(eng)
    fp = FakeProvider(["clean body ", prefix])          # stream ends mid-marker
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "failed" and out[0].detail == "truncated_tool_marker"
    assert _assistant(eng) == []
    txt = _text(eng, rid)
    assert txt == "clean body " and prefix not in txt
    _run, step = _run_step(eng, rid)
    assert step.status == "incomplete" and step.error_code == "truncated_tool_marker"


def test_eof_benign_near_match_is_emitted(eng):
    rid = _seed(eng)
    fp = FakeProvider(["answer ", "[note: not a marker]"])   # "[n" is no execution prefix
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "succeeded"
    assert _assistant(eng)[0].content == "answer [note: not a marker]"
    assert _text(eng, rid) == "answer [note: not a marker]"


def test_eof_ordinary_bracketed_text_is_emitted(eng):
    rid = _seed(eng)
    fp = FakeProvider(["see [ref 12] and also ", "[x=1] done"])
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "succeeded"
    assert _assistant(eng)[0].content == "see [ref 12] and also [x=1] done"


def test_marker_scanner_finish_semantics():
    sc = cs.MarkerScanner()
    assert sc.feed("text [MCP") == "text "       # holds "[MCP"
    assert sc.finish() == "" and sc.truncated is True
    ok = cs.MarkerScanner()
    a = ok.feed("text [zz")                       # "[z" is no execution prefix -> released
    assert a == "text [zz" and ok.finish() == "" and ok.truncated is False
    done = cs.MarkerScanner()
    done.feed("[python_code: x]")
    assert done.tripped and done.finish() == "" and done.truncated is False  # tripped != truncated


# ── 3. dedicated-thread / DB-session ownership ─────────────────


def _instrument(monkeypatch, rec):
    import app.run_engine.worker as wmod
    import app.run_engine.session_binding as sbmod

    real_vsb = sbmod.validate_session_binding

    def vsb(db, ctx, run=None):
        rec.setdefault("session_check", []).append(threading.get_ident())
        return real_vsb(db, ctx, run=run)

    # worker.py imported the name directly
    monkeypatch.setattr(wmod, "validate_session_binding", vsb)

    real_renew = RunWorker.renew_lease

    def renew(self, run_id, owner):
        rec.setdefault("lease_renew", []).append(threading.get_ident())
        return real_renew(self, run_id, owner)

    monkeypatch.setattr(RunWorker, "renew_lease", renew)

    real_fin = RunWorker._finalize_chat_success

    def fin(self, db, claimed, step, final_text):
        rec["finalize_thread"] = threading.get_ident()
        rec["finalize_db_id"] = id(db)
        return real_fin(self, db, claimed, step, final_text)

    monkeypatch.setattr(RunWorker, "_finalize_chat_success", fin)


def test_thread_and_session_ownership_under_running_loop(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 3)     # HB interval = 1s
    rec = {}
    _instrument(monkeypatch, rec)
    rid = _seed(eng)
    fp = FakeProvider([f"t{i} " for i in range(14)], async_sleep=0.25)  # ~3.5s -> HB fires

    async def _outer():
        rec["loop_thread"] = threading.get_ident()
        # sync call from inside a RUNNING loop -> run_stream must NOT nested-asyncio.run
        return _worker(eng, fp).advance_chat_runs()

    out = asyncio.run(_outer())
    assert out[0].status == "succeeded"                       # no nested-asyncio RuntimeError

    assert fp.iter_thread is not None and fp.iter_thread != rec["loop_thread"]  # provider isolated
    assert rec["finalize_thread"] == rec["loop_thread"]       # final persistence on the owner thread
    # guard DB callbacks that run inside the bridge open their OWN session there
    assert set(rec.get("session_check", [])) <= {fp.iter_thread, rec["loop_thread"]}
    assert rec.get("lease_renew"), "heartbeat renewed at least once"
    assert all(t not in (rec["loop_thread"],) for t in rec["lease_renew"])  # dedicated HB thread

    alive = {t.name for t in threading.enumerate()}
    assert "chat-provider-bridge" not in alive and "chat-lease-hb" not in alive


def test_thread_ownership_no_running_loop_is_single_threaded_db(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 3)     # HB interval = 1s
    rec = {}
    _instrument(monkeypatch, rec)
    rid = _seed(eng)
    fp = FakeProvider([f"t{i} " for i in range(14)], async_sleep=0.25)
    me = threading.get_ident()
    out = _worker(eng, fp).advance_chat_runs()                # no loop running
    assert out[0].status == "succeeded"
    assert fp.iter_thread == me                               # inline, same thread
    assert rec["finalize_thread"] == me
    assert set(rec.get("session_check", [])) == {me}          # every DB callback on the owner thread
    assert all(t != me for t in rec["lease_renew"])           # only the HB thread is separate
    alive = {t.name for t in threading.enumerate()}
    assert "chat-provider-bridge" not in alive and "chat-lease-hb" not in alive


def test_stall_cancel_closes_iterator_no_thread_leak(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 1)   # idle tick = 1s
    rid = _seed(eng)

    def cancel_soon():
        d = mk_session(eng)
        try:
            from app.run_engine.engine import request_cancellation
            request_cancellation(d, make_ctx(d, user_id=7, sid="fam-alpha"), rid)
        finally:
            d.close()

    threading.Timer(0.4, cancel_soon).start()
    fp = FakeProvider(["t0"], async_sleep=3.0)
    started = time.monotonic()
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "cancelled"
    assert time.monotonic() - started < 2.5                   # reacted mid-stall
    assert fp.closed is True                                  # provider iterator was closed
    alive = {t.name for t in threading.enumerate()}
    assert "chat-provider-bridge" not in alive and "chat-lease-hb" not in alive


def test_no_thread_leak_on_provider_failure(eng):
    rid = _seed(eng)
    fp = FakeProvider(["x"], raises=RuntimeError("boom SECRET"))
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "failed" and out[0].detail == "provider_error"
    assert fp.closed is True
    alive = {t.name for t in threading.enumerate()}
    assert "chat-provider-bridge" not in alive and "chat-lease-hb" not in alive


# ── 4. provider-key isolation ─────────────────────────────────


def test_other_users_key_cannot_be_selected(eng, monkeypatch):
    cfg = {}
    monkeypatch.setattr("app.services.llm_providers.get_provider",
                        lambda config=None: cfg.update(config or {}) or FakeProvider(["x"]))

    def by_uid(db, uid):
        return {"provider": "pX", "api_key": "sk-USER-99"} if uid == 99 else None

    monkeypatch.setattr("app.services.settings_service.get_user_llm_config", by_uid,
                        raising=False)
    ci = cs.ChatInput(messages=[{"role": "user", "content": "h"}], system_prompt=None,
                      model="mX", provider="pX", mode="chat", options={})
    db = mk_session(eng)
    try:
        cs.default_chat_provider(ci, db=db, user_id=7)        # the run's owner is uid 7
    finally:
        db.close()
    assert "sk-USER-99" not in json.dumps(cfg) and "api_key" not in cfg


def test_live_key_never_enters_durable_fields_or_logs(eng, monkeypatch, caplog):
    key = "sk-LIVE-KZ-777"
    cap = {}
    monkeypatch.setattr("app.services.llm_providers.get_provider",
                        lambda config=None: cap.update({"config": dict(config or {})})
                        or FakeProvider(["hello ", "world"]))
    monkeypatch.setattr("app.services.settings_service.get_user_llm_config",
                        lambda db, uid: {"provider": "snap-prov", "api_key": key,
                                         "model": "z"}, raising=False)
    rid = _seed(eng, submission=_submission(provider="snap-prov"))
    w = RunWorker(mk_factory(eng), worker_id="w-1")           # real default_chat_provider path
    with caplog.at_level("DEBUG"):
        out = w.advance_chat_runs()
    assert out[0].status == "succeeded"
    assert cap["config"].get("api_key") == key                # key DID reach the provider object
    run, step = _run_step(eng, rid)
    row = mk_session(eng)
    try:
        pl = row.query(RunPayload).filter(RunPayload.run_id == rid).first()
        plain = open_payload(row, step.input_payload_id, expected_tenant_id=run.tenant_id,
                             expected_user_id=run.user_id, expected_run_id=run.id,
                             expected_step_id=step.id, expected_payload_type="other",
                             expected_action_hash=None)
    finally:
        row.close()
    for field in (pl.ciphertext, plain, _events_blob(eng, rid), step.input_ref or "",
                  step.output_ref or "", run.status_reason or "", caplog.text):
        assert key not in field
