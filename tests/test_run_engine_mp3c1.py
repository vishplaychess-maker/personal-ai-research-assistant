"""Phase 1B.2R MP-3C1 — durable chat parity, stateful stream safety, feature-flag
hardening. Fake providers only; no live provider, no /messages/stream cutover.
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
from app.run_engine.errors import IdempotencyKeyConflict
from app.run_engine.payloads import open_payload
from app.run_engine.worker import RunWorker
from tests.run_engine_helpers import (
    ensure_user,
    make_ctx,
    mk_engine,
    mk_factory,
    mk_session,
)

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
                 on_chunk=None, sleep_between=0.0, async_sleep=0.0, presleep=0.0):
        self.tokens = list(tokens)
        self.response = response
        self.error = error
        self.raises = raises
        self.raise_at = raise_at
        self.on_chunk = on_chunk
        self.sleep_between = sleep_between
        self.async_sleep = async_sleep
        self.presleep = presleep
        self.calls = 0
        self.seen = None

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        self.calls += 1
        self.seen = {"messages": messages, "system_prompt": system_prompt, "model": model_name}
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
            if self.sleep_between:
                time.sleep(self.sleep_between)
        if self.error:
            yield {"type": "error", "error": "boom UPSTREAM-SECRET"}
            return
        yield {"type": "done",
               "response": self.response if self.response is not None else "".join(self.tokens)}


def _submission(*, message="hello world", provider="snap-prov", model="snap-model",
                mode="chat", system_prompt="SNAP-SYSPROMPT", options=None, session_id=1):
    return sub.build_chat_submission(
        session_id=session_id, message=message, image_url=None, provider=provider,
        model=model, mode=mode, system_prompt=system_prompt, options=options or {},
    )


def _seed(eng, *, submission=None, uid=7, sid=1):
    db = mk_session(eng)
    try:
        ensure_user(db, uid)
        from app.models.models import ResearchSession
        if db.query(ResearchSession).filter(ResearchSession.id == sid).first() is None:
            db.add(ResearchSession(id=sid, user_id=uid, title="t"))
            db.commit()
        ctx = make_ctx(db, user_id=uid, sid="fam-alpha")
        run, created = sub.create_chat_run(
            db, ctx, submission=submission or _submission(),
            idempotency_key=uuid.uuid4().hex,
        )
        assert created
        return run.id
    finally:
        db.close()


def _worker(eng, provider, **kw):
    cap = {}

    def factory(ci, **_k):
        cap["chat_input"] = ci
        return provider

    w = RunWorker(mk_factory(eng), worker_id=kw.pop("worker_id", "w-1"),
                  chat_provider=factory, **kw)
    w._captured = cap
    return w


def _text(eng, rid):
    db = mk_session(eng)
    try:
        return "".join(
            json.loads(e.data)["text"]
            for e in db.query(RunEvent).filter(RunEvent.run_id == rid,
                                               RunEvent.category == "text")
            .order_by(RunEvent.run_seq.asc()).all()
        )
    finally:
        db.close()


def _assistant(eng, sid=1):
    db = mk_session(eng)
    try:
        return db.query(Message).filter(Message.session_id == sid,
                                        Message.role == MessageRole.assistant).all()
    finally:
        db.close()


# ── 1. execution snapshot ────────────────────────────────────────


def test_exec_snapshot_sealed_and_rebuilt(eng):
    rid = _seed(eng, submission=_submission(options={"temperature": 0.3, "max_tokens": 64}))
    db = mk_session(eng)
    try:
        run = db.query(Run).filter(Run.id == rid).first()
        step = db.query(RunStep).filter(RunStep.run_id == rid).first()
        plain = open_payload(
            db, step.input_payload_id, expected_tenant_id=run.tenant_id,
            expected_user_id=run.user_id, expected_run_id=run.id, expected_step_id=step.id,
            expected_payload_type="other", expected_action_hash=None,
        )
        body = json.loads(plain)
        snap = body["exec"]
        assert snap["exec_snapshot_version"] == 1
        assert snap["provider"] == "snap-prov" and snap["model"] == "snap-model"
        assert snap["mode"] == "chat" and snap["system_prompt"] == "SNAP-SYSPROMPT"
        assert snap["options"] == {"max_tokens": 64, "temperature": 0.3}
        assert "api_key" not in json.dumps(body) and "token" not in snap

        ci = cs.build_chat_input(db, run, step)
        assert ci.provider == "snap-prov" and ci.model == "snap-model"
        assert ci.system_prompt == "SNAP-SYSPROMPT" and ci.mode == "chat"
        assert ci.options == {"max_tokens": 64, "temperature": 0.3}
    finally:
        db.close()


def test_snapshot_never_contains_credentials(eng, monkeypatch):
    monkeypatch.setattr(
        "app.services.settings_service.get_user_llm_config",
        lambda db, uid: {"provider": "openrouter", "api_key": "sk-LIVE-SECRET", "model": "m"},
        raising=False,
    )
    rid = _seed(eng, submission=_submission(provider="openrouter"))
    db = mk_session(eng)
    try:
        run = db.query(Run).filter(Run.id == rid).first()
        step = db.query(RunStep).filter(RunStep.run_id == rid).first()
        row = db.query(RunPayload).filter(RunPayload.run_id == rid).first()
        assert "sk-LIVE-SECRET" not in row.ciphertext
        plain = open_payload(
            db, step.input_payload_id, expected_tenant_id=run.tenant_id,
            expected_user_id=run.user_id, expected_run_id=run.id, expected_step_id=step.id,
            expected_payload_type="other", expected_action_hash=None,
        )
        assert "sk-LIVE-SECRET" not in plain and "api_key" not in plain
    finally:
        db.close()


def test_unknown_snapshot_version_fails_closed(eng, monkeypatch):
    rid = _seed(eng)
    db = mk_session(eng)
    try:
        run = db.query(Run).filter(Run.id == rid).first()
        step = db.query(RunStep).filter(RunStep.run_id == rid).first()
        monkeypatch.setattr(
            "app.run_engine.chat_stream.open_payload",
            lambda *a, **k: json.dumps(
                {"message": "h", "image_url": None, "exec": {"exec_snapshot_version": 0}}),
        )
        with pytest.raises(ValueError):
            cs.build_chat_input(db, run, step)     # fail closed — no deployment fallback
    finally:
        db.close()


# ── 2. semantic HMAC covers provider ────────────────────────────


def test_semantic_hmac_covers_provider():
    a = sub.semantic_identity(_submission(provider="prov-a"))
    b = sub.semantic_identity(_submission(provider="prov-b"))
    assert a != b


def test_changed_provider_same_key_conflicts(eng):
    db = mk_session(eng)
    try:
        ensure_user(db, 7)
        from app.models.models import ResearchSession
        db.add(ResearchSession(id=1, user_id=7, title="t"))
        db.commit()
        ctx = make_ctx(db, user_id=7, sid="fam-alpha")
        sub.create_chat_run(db, ctx, submission=_submission(provider="p1"), idempotency_key="K")
        with pytest.raises(IdempotencyKeyConflict):
            sub.create_chat_run(db, ctx, submission=_submission(provider="p2"),
                                idempotency_key="K")
    finally:
        db.close()


# ── 3. worker uses the snapshot, not deployment / None ──────────


def test_worker_uses_snapshot_provider_and_system_prompt(eng):
    rid = _seed(eng, submission=_submission(provider="snap-prov", model="snap-model",
                                           system_prompt="SNAP-SYSPROMPT"))
    fp = FakeProvider(["ok"])
    w = _worker(eng, fp)
    out = w.advance_chat_runs()
    assert out[0].status == "succeeded"
    ci = w._captured["chat_input"]
    assert ci.provider == "snap-prov" and ci.model == "snap-model"
    assert ci.system_prompt == "SNAP-SYSPROMPT"
    assert fp.seen["system_prompt"] == "SNAP-SYSPROMPT" and fp.seen["model"] == "snap-model"


def test_default_chat_provider_builds_from_snapshot_not_deployment(eng, monkeypatch):
    captured = {}

    def fake_get_provider(config=None):
        captured["config"] = dict(config or {})
        return FakeProvider(["x"])

    monkeypatch.setattr("app.services.llm_providers.get_provider", fake_get_provider)
    monkeypatch.setattr("app.services.settings_service.get_user_llm_config",
                        lambda db, uid: None, raising=False)
    monkeypatch.setattr(settings, "llm_provider", "DEPLOYMENT-DEFAULT")

    ci = cs.ChatInput(messages=[{"role": "user", "content": "h"}], system_prompt="s",
                      model="mX", provider="pX", mode="chat", options={})
    db = mk_session(eng)
    try:
        cs.default_chat_provider(ci, db=db, user_id=7)
    finally:
        db.close()
    assert captured["config"]["provider"] == "pX"          # snapshot, NOT DEPLOYMENT-DEFAULT
    assert captured["config"]["model"] == "mX"
    assert "api_key" not in captured["config"]             # no matching live credential

    # live credential applied ONLY when the live provider matches the snapshot
    monkeypatch.setattr("app.services.settings_service.get_user_llm_config",
                        lambda db, uid: {"provider": "pX", "api_key": "sk-live", "model": "z"},
                        raising=False)
    db = mk_session(eng)
    try:
        cs.default_chat_provider(ci, db=db, user_id=7)
    finally:
        db.close()
    assert captured["config"]["provider"] == "pX" and captured["config"]["api_key"] == "sk-live"


# ── 4. bounded stateful marker scanner ─────────────────────────


def test_marker_scanner_across_chunk_boundaries():
    sc = cs.MarkerScanner()
    out = []
    out.append(sc.feed("Answer: "))
    out.append(sc.feed("["))
    out.append(sc.feed("MC"))
    out.append(sc.feed("P_CALL: mcp__s__t]"))
    joined = "".join(out)
    assert sc.tripped is True
    assert joined == "Answer: "                      # clean prefix only
    assert "[" not in joined and "MCP" not in joined
    assert sc.feed("more") == "" and sc.finish() == ""   # emits nothing after a trip


def test_marker_scanner_benign_near_miss_is_released():
    sc = cs.MarkerScanner()
    a = sc.feed("text [")          # holds "["
    b = sc.feed("notamarker")      # "[n" is not a marker prefix -> release
    assert sc.tripped is False
    assert (a + b) == "text [notamarker"
    assert sc.finish() == ""


def test_marker_at_stream_start():
    sc = cs.MarkerScanner()
    assert sc.feed("[PYTHON_CODE: print(1)]") == ""
    assert sc.tripped is True


def test_worker_marker_split_across_tokens_not_shown(eng):
    # MP-3C2: the split [MCP_CALL] is confidentially captured, not shown. The
    # tool is unregistered so the gateway blocks it fail-closed (unknown_tool);
    # no assistant Message, no marker text in the persisted stream.
    rid = _seed(eng)
    fp = FakeProvider(["Here: ", "[", "MCP", "_CALL: mcp__s__t]", " tail"])
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "failed" and out[0].detail == "unknown_tool"
    assert _assistant(eng) == []
    txt = _text(eng, rid)
    assert txt == "Here: " and "[" not in txt and "MCP" not in txt


# ── 5. provider bridge under a running event loop ──────────────


def test_run_stream_safe_under_running_event_loop(eng):
    fp = FakeProvider(["a", "b", "c"])
    ci = cs.ChatInput(messages=[{"role": "user", "content": "h"}], system_prompt=None,
                      model="m", provider="p", mode="chat", options={})
    got = []

    async def _drive():
        # we are now inside a running loop -> run_stream must NOT call asyncio.run here
        return cs.run_stream(
            fp, ci, on_token=got.append, on_idle=lambda: None,
            deadline=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=30),
            now_fn=lambda: _dt.datetime.now(_dt.timezone.utc), poll_interval=1,
        )

    result = asyncio.run(_drive())
    assert result == "abc" and "".join(got) == "abc"


# ── 6. wall-clock checks even when the provider is stalled ─────


def test_stalled_provider_still_cancels_on_wall_clock(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 1)   # idle tick = 1s
    rid = _seed(eng)

    def cancel_soon():
        d = mk_session(eng)
        try:
            ctx = make_ctx(d, user_id=7, sid="fam-alpha")
            from app.run_engine.engine import request_cancellation
            request_cancellation(d, ctx, rid)
        finally:
            d.close()

    threading.Timer(0.4, cancel_soon).start()
    fp = FakeProvider(["t0"], async_sleep=3.0)   # stalls ~3s after the first token
    started = time.monotonic()
    out = _worker(eng, fp).advance_chat_runs()
    elapsed = time.monotonic() - started
    assert out[0].status == "cancelled"
    assert elapsed < 2.5                          # reacted mid-stall, not after 3s
    assert _assistant(eng) == []


def test_stalled_provider_hits_deadline_on_wall_clock(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 1)
    monkeypatch.setattr(settings, "run_worker_handler_timeout_s", 0)  # deadline == start
    rid = _seed(eng)
    fp = FakeProvider(["never"], presleep=3.0)
    started = time.monotonic()
    out = _worker(eng, fp).advance_chat_runs()
    elapsed = time.monotonic() - started
    assert out[0].status == "failed" and out[0].detail == "provider_timeout"
    assert elapsed < 2.5
    assert _assistant(eng) == []


# ── 7. feature-flag hardening ─────────────────────────────────


def test_chat_stream_always_reserved(eng):
    rid = _seed(eng)
    w = RunWorker(mk_factory(eng), worker_id="w-1")
    assert w.claim_next_queued() is None          # flags ON -> still reserved
    # and it IS processable by the dedicated driver
    out = _worker(eng, FakeProvider(["hi"])).advance_chat_runs()
    assert out[0].status == "succeeded"


@pytest.mark.parametrize("engine_on,chat_on", [(True, True), (False, True), (True, False)])
def test_assert_chat_stream_executable_combos(engine_on, chat_on, monkeypatch):
    monkeypatch.setattr(settings, "run_engine_enabled", engine_on)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", chat_on)
    if engine_on and chat_on:
        sub.assert_chat_stream_executable()       # ok
    else:
        with pytest.raises(sub.RunFeatureNotExecutable):
            sub.assert_chat_stream_executable()


def test_invalid_flag_combo_fails_closed_no_run(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from tests.auth_helpers import auth_headers, register_and_login

    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", False)   # invalid combo
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)

    with TestClient(app) as c:
        uid, token = register_and_login(
            c, username=f"c1_{_dt.datetime.now().timestamp():.6f}".replace(".", ""))
        h = auth_headers(token)
        csrf = c.cookies.get("research_assistant_csrf_token")
        if csrf:
            h["X-CSRF-Token"] = csrf
        s = c.post("/api/sessions", json={"title": "c1"}, headers=h)
        sid = s.json()["id"]
        before = c.get("/api/runs/does-not-exist", headers=h).status_code  # 404, harmless
        r = c.post("/api/runs", json={"session_id": sid, "message": "hi"},
                   headers={**h, "Idempotency-Key": "k"})
        assert r.status_code == 503, r.text[:200]
    assert before == 404


# ── 8. no implicit paid fallback ─────────────────────────────


def test_no_fallback_chain_in_chat_stream(eng):
    src = inspect.getsource(cs)
    assert "build_fallback_chain" not in src
    rid = _seed(eng)
    fp = FakeProvider(["a"], error=True)
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "failed" and out[0].detail == "provider_error"
    assert fp.calls == 1                          # exactly one provider, no fallback


# ── 9. preserved guarantees (bridge/scanner rewrite regression) ─


def test_one_final_message_and_text_parity(eng):
    rid = _seed(eng)
    fp = FakeProvider(["Über ", "café ", "😀 done"])
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "succeeded"
    final = "Über café 😀 done"
    msgs = _assistant(eng)
    assert len(msgs) == 1 and msgs[0].content == final
    assert _text(eng, rid) == final
    db = mk_session(eng)
    try:
        r = db.query(Run).filter(Run.id == rid).first()
        s = db.query(RunStep).filter(RunStep.run_id == rid).first()
        assert r.status == "succeeded" and s.status == "succeeded"
        assert str(msgs[0].id) in (s.output_ref or "")
    finally:
        db.close()
