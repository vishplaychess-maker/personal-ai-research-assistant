"""Phase 1B.2R MP-3B — worker-backed durable chat generation + durable text
streaming. Fake streaming providers only; no live provider, no /messages/stream.
"""

from __future__ import annotations

import datetime as _dt
import time
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import update

from app.config import settings
from app.models.models import Message, MessageRole, Run, RunEvent, RunStep
from app.run_engine import submission as sub
from app.run_engine.worker import RunWorker, _ChatHeartbeat
from tests.run_engine_helpers import (
    ensure_user,
    make_ctx,
    mk_engine,
    mk_factory,
    mk_session,
    revoke_family,
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


# ── fakes ────────────────────────────────────────────────────────────


class FakeProvider:
    """Minimal ``LLMProvider.generate_stream_async`` shape."""

    def __init__(self, tokens, *, response=None, error=False, raises=None,
                 raise_at=0, on_chunk=None, sleep_between=0.0):
        self.tokens = list(tokens)
        self.response = response
        self.error = error
        self.raises = raises
        self.raise_at = raise_at
        self.on_chunk = on_chunk
        self.sleep_between = sleep_between
        self.calls = 0
        self.messages = None

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        self.calls += 1
        self.messages = messages
        for i, tok in enumerate(self.tokens):
            if self.raises is not None and i == self.raise_at:
                raise self.raises
            yield {"type": "token", "token": tok}
            if self.on_chunk is not None:
                self.on_chunk(i)
            if self.sleep_between:
                time.sleep(self.sleep_between)
        if self.error:
            yield {"type": "error", "error": "boom UPSTREAM-SECRET-DETAIL"}
            return
        yield {"type": "done",
               "response": self.response if self.response is not None else "".join(self.tokens)}


def _seed_chat_run(eng, *, message="hello world", model="m1", uid=7, sid=1):
    db = mk_session(eng)
    try:
        ensure_user(db, uid)
        from app.models.models import ResearchSession
        if db.query(ResearchSession).filter(ResearchSession.id == sid).first() is None:
            db.add(ResearchSession(id=sid, user_id=uid, title="t"))
            db.commit()
        ctx = make_ctx(db, user_id=uid, sid="fam-alpha")
        submission = sub.build_chat_submission(
            session_id=sid, message=message, image_url=None, model=model,
            mode="chat", system_prompt="sp", options={},
        )
        run, created = sub.create_chat_run(
            db, ctx, submission=submission, idempotency_key=uuid.uuid4().hex,
        )
        assert created
        return run.id
    finally:
        db.close()


def _worker(eng, provider, **kw):
    return RunWorker(mk_factory(eng), worker_id=kw.pop("worker_id", "w-1"),
                     chat_provider=lambda ci, **_k: provider, **kw)


def _text(eng, rid):
    import json
    db = mk_session(eng)
    try:
        parts = []
        for e in (db.query(RunEvent).filter(RunEvent.run_id == rid,
                                            RunEvent.category == "text")
                  .order_by(RunEvent.run_seq.asc()).all()):
            parts.append(json.loads(e.data)["text"])
        return "".join(parts)
    finally:
        db.close()


def _assistant_msgs(eng, sid=1):
    db = mk_session(eng)
    try:
        return db.query(Message).filter(Message.session_id == sid,
                                        Message.role == MessageRole.assistant).all()
    finally:
        db.close()


def _run(eng, rid):
    db = mk_session(eng)
    try:
        r = db.query(Run).filter(Run.id == rid).first()
        s = db.query(RunStep).filter(RunStep.run_id == rid).order_by(RunStep.seq.asc()).first()
        return r, s
    finally:
        db.close()


# ── success ──────────────────────────────────────────────────────────


def test_successful_multi_chunk_utf8_stream_one_message(eng):
    rid = _seed_chat_run(eng)
    fp = FakeProvider(["Ünïcode ", "café ", "😀 ", "tail"])
    out = _worker(eng, fp).advance_chat_runs()
    assert [o.status for o in out] == ["succeeded"]
    final = "Ünïcode café 😀 tail"
    assert _text(eng, rid) == final
    msgs = _assistant_msgs(eng)
    assert len(msgs) == 1 and msgs[0].content == final
    r, s = _run(eng, rid)
    assert r.status == "succeeded" and s.status == "succeeded"
    assert str(msgs[0].id) in (s.output_ref or "")
    assert fp.calls == 1


def test_final_partial_buffer_flush_multi_event(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_event_text_flush_bytes", 4)
    rid = _seed_chat_run(eng)
    fp = FakeProvider(["ab", "cd", "ef", "g"])   # 7 bytes -> several flushes + a partial
    _worker(eng, fp).advance_chat_runs()
    db = mk_session(eng)
    try:
        n = db.query(RunEvent).filter(RunEvent.run_id == rid,
                                      RunEvent.category == "text").count()
    finally:
        db.close()
    assert n >= 2
    assert _text(eng, rid) == "abcdefg"
    assert _assistant_msgs(eng)[0].content == "abcdefg"


def test_done_response_used_when_no_tokens_streamed(eng):
    rid = _seed_chat_run(eng)
    fp = FakeProvider([], response="")
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "failed" and out[0].detail == "empty_generation"
    assert _assistant_msgs(eng) == []


# ── idempotency / atomicity ─────────────────────────────────────────


def test_exactly_one_message_on_resweep(eng):
    rid = _seed_chat_run(eng)
    fp = FakeProvider(["one ", "two"])
    w = _worker(eng, fp)
    w.advance_chat_runs()
    w.advance_chat_runs()   # run is terminal now -> already_terminal, no re-gen
    assert len(_assistant_msgs(eng)) == 1
    assert fp.calls == 1


def test_final_persistence_rollback_leaves_run_non_successful(eng, monkeypatch):
    rid = _seed_chat_run(eng)
    fp = FakeProvider(["hi ", "there"])
    w = _worker(eng, fp)
    from app.run_engine.worker import RunWorker as _RW
    real = _RW._apply_transition

    def boom(self, db, *, claimed, frm, to, status_reason=None):
        if to == "succeeded":
            from app.run_engine.errors import RunLeaseLost
            raise RunLeaseLost(claimed.run_id)
        return real(self, db, claimed=claimed, frm=frm, to=to, status_reason=status_reason)

    monkeypatch.setattr(_RW, "_apply_transition", boom)
    out = w.advance_chat_runs()
    assert out[0].status == "abandoned"
    assert _assistant_msgs(eng) == []
    r, _ = _run(eng, rid)
    assert r.status == "running"          # never marked successful


def test_commit_failure_becomes_outcome_unknown(eng, monkeypatch):
    """The assistant-Message-carrying commit fails => run must NOT be successful;
    it becomes outcome_unknown and no Message is persisted."""
    rid = _seed_chat_run(eng)
    fp = FakeProvider(["x ", "y"])
    w = _worker(eng, fp)

    from sqlalchemy.orm import Session as _S
    real = _S.commit
    seen = {}  # per-session commit counter; the 4th commit on the driver session
    #          # is the finalize commit (claim, queued->running, step->running, finalize)

    class _Boom(Exception):
        pass

    def flaky(self):
        n = seen.get(id(self), 0) + 1
        seen[id(self)] = n
        if any(isinstance(o, Message) for o in list(self.new) + list(self.dirty)) \
                or n == 4:
            raise _Boom()
        return real(self)

    monkeypatch.setattr(_S, "commit", flaky, raising=True)
    out = w.advance_chat_runs()
    monkeypatch.undo()
    assert out[0].status == "outcome_unknown"
    assert _assistant_msgs(eng) == []
    r, _ = _run(eng, rid)
    assert r.status == "outcome_unknown"


# ── cancellation ────────────────────────────────────────────────────


def test_cancel_before_invocation_never_calls_provider(eng):
    rid = _seed_chat_run(eng)
    db = mk_session(eng)
    try:
        ctx = make_ctx(db, user_id=7, sid="fam-alpha")
        from app.run_engine.engine import request_cancellation
        request_cancellation(db, ctx, rid)
    finally:
        db.close()
    fp = FakeProvider(["nope"])
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "cancelled"
    assert fp.calls == 0
    assert _assistant_msgs(eng) == []


def test_cancel_during_stream_no_message_partial_kept(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_event_text_flush_bytes", 2)
    rid = _seed_chat_run(eng)

    def cancel_after_first(i):
        if i == 0:
            d = mk_session(eng)
            try:
                ctx = make_ctx(d, user_id=7, sid="fam-alpha")
                from app.run_engine.engine import request_cancellation
                request_cancellation(d, ctx, rid)
            finally:
                d.close()

    fp = FakeProvider([f"t{n} " for n in range(12)], on_chunk=cancel_after_first)
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "cancelled"
    assert _assistant_msgs(eng) == []
    assert _text(eng, rid) != ""          # already-streamed text stays as history
    r, _ = _run(eng, rid)
    assert r.status == "cancelled"


# ── session revocation ─────────────────────────────────────────────


def test_revoked_session_before_invocation(eng):
    rid = _seed_chat_run(eng)
    db = mk_session(eng)
    try:
        revoke_family(db, "fam-alpha")
    finally:
        db.close()
    fp = FakeProvider(["hi"])
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "failed" and out[0].detail == "session_revoked"
    assert fp.calls == 0
    assert _assistant_msgs(eng) == []


def test_revoked_session_during_stream(eng):
    rid = _seed_chat_run(eng)

    def revoke_after_first(i):
        if i == 0:
            d = mk_session(eng)
            try:
                revoke_family(d, "fam-alpha")
            finally:
                d.close()

    fp = FakeProvider([f"w{n} " for n in range(12)], on_chunk=revoke_after_first)
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "failed" and out[0].detail == "session_revoked"
    assert _assistant_msgs(eng) == []


# ── lease / heartbeat ─────────────────────────────────────────────


def test_heartbeat_renews_only_current_owner_and_stops_on_lease_loss(eng):
    rid = _seed_chat_run(eng)
    w = RunWorker(mk_factory(eng), worker_id="w-1")
    s = mk_session(eng)
    try:
        s.execute(update(Run).where(Run.id == rid).values(
            status="running", lease_owner="w-1/tok", version=3,
            lease_expires_at=None))
        s.commit()
    finally:
        s.close()
    good = _ChatHeartbeat(w, rid, "w-1/tok", interval_s=1)
    assert good.beat() is True                       # current owner renews
    wrong = _ChatHeartbeat(w, rid, "someone/else", interval_s=1)
    assert wrong.beat() is False and wrong.lease_lost is True
    s = mk_session(eng)
    try:
        s.execute(update(Run).where(Run.id == rid).values(status="succeeded"))
        s.commit()
    finally:
        s.close()
    assert good.beat() is False and good.lease_lost is True   # terminal -> no renew


def test_slow_provider_survives_lease_ttl(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 1)   # hb interval = 1s
    rid = _seed_chat_run(eng)
    fp = FakeProvider(["slow ", "but ", "steady"], sleep_between=0.8)  # ~2.4s > ttl
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "succeeded"
    assert _assistant_msgs(eng)[0].content == "slow but steady"


def test_lease_loss_blocks_further_events_and_message(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_event_text_flush_bytes", 2)
    rid = _seed_chat_run(eng)

    def steal_after_first(i):
        if i == 0:
            d = mk_session(eng)
            try:
                d.execute(update(Run).where(Run.id == rid).values(
                    lease_owner="thief/xyz", version=Run.version + 1))
                d.commit()
            finally:
                d.close()

    fp = FakeProvider([f"c{n} " for n in range(12)], on_chunk=steal_after_first)
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "abandoned" and out[0].detail == "RunLeaseLost"
    assert _assistant_msgs(eng) == []


# ── crash / attempt policy ────────────────────────────────────────


def test_crash_before_invocation_is_reclaimable(eng):
    rid = _seed_chat_run(eng)
    # simulate: claimed running, lease expired, step still pending (never invoked)
    s = mk_session(eng)
    try:
        s.execute(update(Run).where(Run.id == rid).values(
            status="running", lease_owner="dead/w", version=1,
            lease_expires_at=_dt.datetime(2000, 1, 1)))
        s.commit()
    finally:
        s.close()
    fp = FakeProvider(["recovered ", "ok"])
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "succeeded"
    assert fp.calls == 1
    assert _assistant_msgs(eng)[0].content == "recovered ok"


def test_crash_after_invocation_is_outcome_unknown_no_retry(eng):
    rid = _seed_chat_run(eng)
    s = mk_session(eng)
    try:
        s.execute(update(Run).where(Run.id == rid).values(
            status="running", lease_owner="dead/w", version=1,
            lease_expires_at=_dt.datetime(2000, 1, 1)))
        s.execute(update(RunStep).where(RunStep.run_id == rid).values(status="running"))
        s.commit()
    finally:
        s.close()
    fp = FakeProvider(["should not run"])
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "outcome_unknown"
    assert fp.calls == 0
    assert _assistant_msgs(eng) == []


# ── provider failure ─────────────────────────────────────────────


def test_provider_error_stores_bounded_code_only(eng):
    rid = _seed_chat_run(eng)
    fp = FakeProvider(["a", "b"], error=True)
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "failed" and out[0].detail == "provider_error"
    blob = _all_event_data(eng, rid)
    assert "UPSTREAM-SECRET-DETAIL" not in blob


def test_provider_timeout_stores_bounded_code(eng, monkeypatch):
    from tests.run_engine_helpers import Clock
    clk = Clock()
    rid = _seed_chat_run(eng)

    def tick(i):
        clk.advance(10_000)

    fp = FakeProvider(["t0 ", "t1 ", "t2"], on_chunk=tick)
    out = _worker(eng, fp, now_fn=clk).advance_chat_runs()
    assert out[0].status == "failed" and out[0].detail == "provider_timeout"
    assert _assistant_msgs(eng) == []


def test_provider_raises_is_bounded_and_canary_free(eng, caplog):
    canary = "CANARY-7f3-xyz"
    rid = _seed_chat_run(eng, message=f"remember {canary} please")
    fp = FakeProvider(["x"], raises=RuntimeError(f"upstream 500 {canary} leaked"))
    with caplog.at_level("DEBUG"):
        out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "failed" and out[0].detail == "provider_error"
    r, s = _run(eng, rid)
    assert canary not in (r.status_reason or "")
    assert canary not in (s.error_code or "")
    assert canary not in (s.input_ref or "") and canary not in (s.output_ref or "")
    assert canary not in _all_event_data(eng, rid)
    assert canary not in caplog.text


# ── tool-marker boundary ────────────────────────────────────────


def test_action_marker_is_not_executed_or_shown(eng):
    # MP-3C2: a valid [MCP_CALL] marker now suspends the run for approval instead
    # of failing outright — but an UNREGISTERED tool is still blocked fail-closed
    # by the gateway, nothing executes, and the marker is never shown.
    rid = _seed_chat_run(eng)
    fp = FakeProvider(["Here is ", "code ", '[MCP_CALL: mcp__s__t {"a":1}]', " trailing"])
    out = _worker(eng, fp).advance_chat_runs()
    assert out[0].status == "failed" and out[0].detail == "unknown_tool"
    assert _assistant_msgs(eng) == []
    txt = _text(eng, rid)
    assert "[MCP_CALL" not in txt and "MCP_CALL" not in txt
    assert txt == "Here is code "        # only the clean prefix persisted


# ── coexistence with the legacy path ───────────────────────────


def test_chat_stream_always_reserved_from_generic_worker(eng, monkeypatch):
    rid = _seed_chat_run(eng)                    # created with both flags on (autouse)
    fp = FakeProvider(["unused"])
    w = _worker(eng, fp)
    assert w.claim_next_queued() is None         # reserved even with the flag ON
    monkeypatch.setattr(settings, "run_chat_stream_enabled", False)
    assert w.advance_chat_runs() == []           # driver is a no-op when the flag is off
    assert w.claim_next_queued() is None         # STILL reserved — never generic-claimed


def test_messages_stream_module_untouched():
    import app.services.streaming_service as ss
    assert hasattr(ss, "stream_chat_response")
    assert hasattr(ss, "save_assistant_message")


# ── helpers ────────────────────────────────────────────────────────


def _all_event_data(eng, rid):
    db = mk_session(eng)
    try:
        return "\n".join(e.data or "" for e in
                         db.query(RunEvent).filter(RunEvent.run_id == rid).all())
    finally:
        db.close()
