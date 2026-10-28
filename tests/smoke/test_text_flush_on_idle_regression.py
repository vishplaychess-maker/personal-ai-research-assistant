"""D6C-J4 idle text-flush regression (RED first, now GREEN).

PROVEN DEFECT (now FIXED): app/run_engine/text_events.py documents TextEventBuffer
as flushing when the buffer reaches run_event_text_flush_bytes OR
run_event_text_flush_ms elapse, and states that tick() is the time-based flush to
call periodically from the producer's loop. The chat producer DID call tick(), but
only from on_token, and the only OTHER periodic callback was on_idle - which runs
every max(1, run_worker_lease_ttl_s // 3) == 20 s and performs the expensive
DB-backed lease / cancellation / session / deadline guards. With flush_ms == 100
that is a 200x gap: a provider that published a small prefix and then stalled left
that text inside the buffer indefinitely.

Observed live in the D6C-J4 browser journey: 67 chunks published, then a stall; the
run still had 3 control RunEvents and ZERO text RunEvents after 60+ s, no token
reached the browser, and the UI showed "Generating..." with no partial response.

FIX: chat_stream._adrive now takes a separate lightweight on_tick / tick_interval
hook, woken at the buffer's own flush_ms bound, while on_idle keeps its original
poll_interval cadence (measured on a monotonic clock) so the guards are neither
slowed nor sped up into a DB polling loop.

Run: PYTHONPATH=backend python -m pytest tests/smoke/test_text_flush_on_idle_regression.py -q
"""
from __future__ import annotations

import ast
import asyncio
import datetime as _dt
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import ResearchSession, RunEvent
from app.run_engine import chat_stream
from app.run_engine import submission as sub
from app.run_engine.text_events import TextEventBuffer
from tests.run_engine_helpers import ensure_user, make_ctx, mk_engine, mk_factory, mk_session

_HMAC = "unit-test-hmac-key-0123456789abcdef"
REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER = REPO_ROOT / "backend" / "app" / "run_engine" / "worker.py"

#: Comfortably below run_event_text_flush_bytes (4096): ONLY the time bound can
#: flush it, which is exactly the behaviour under test.
SMALL_TEXT = "idle-flush-sub-threshold"
#: Multi-byte content, published in pieces that split codepoints across chunks.
UTF8_PIECES = ["\u0c28\u0c2e", "\u0c38\u0c4d\u0c15\u0c3e", "\u0c30", "\u0c02\U0001f32a", "ok"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


def _seed(db, *, uid=7, sid_int=1):
    ensure_user(db, uid)
    if db.query(ResearchSession).filter(ResearchSession.id == sid_int).first() is None:
        db.add(ResearchSession(id=sid_int, user_id=uid, title="t"))
        db.commit()


def _mk_run(eng, key="FLUSH-KEY-1"):
    db = mk_session(eng)
    try:
        _seed(db)
        ctx = make_ctx(db, sid="fam-flush")
        run, _created = sub.create_chat_run(
            db, ctx,
            submission=sub.build_chat_submission(
                session_id=1, message="hello", image_url=None, model="m1",
                mode="chat", system_prompt="sp", options={}),
            idempotency_key=key,
        )
        return run.id
    finally:
        db.close()


class _RecordingBuffer(TextEventBuffer):
    """TextEventBuffer that records WHEN the first real flush happened."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.first_flush_at = None

    def _flush(self):
        had_text = bool(self._buf)
        super()._flush()
        if had_text and self.first_flush_at is None:
            self.first_flush_at = time.monotonic()


class _StallProvider:
    """One small chunk, then stays OPEN (never completes)."""

    def __init__(self, pieces):
        self.pieces = list(pieces)
        self.started = 0
        self.completed = 0
        self.closed = 0

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        try:
            for piece in self.pieces:
                yield {"type": "token", "token": piece}
            await asyncio.sleep(600)
            self.completed += 1
            yield {"type": "done", "response": "SHOULD-NOT-COMPLETE"}
        finally:
            self.closed += 1


def _chat_input():
    return SimpleNamespace(messages=[{"role": "user", "content": "hi"}],
                           system_prompt=None, model="m1")


class _Drive:
    def __init__(self, eng, buffered):
        self.eng = eng
        self.buffered = buffered

    def run(self, provider, *, buf, on_idle, poll_interval=20.0, tick_interval=None):
        ci = _chat_input()
        now = lambda: _dt.datetime.now(_dt.timezone.utc)
        return chat_stream._adrive(
            provider, ci,
            on_token=lambda tok: buf.publish(tok) if self.buffered else None,
            on_idle=on_idle,
            deadline=now() + _dt.timedelta(seconds=300),
            now_fn=now,
            poll_interval=poll_interval,
            on_tick=buf.tick,
            tick_interval=buf.flush_interval_s if tick_interval is None else tick_interval,
        )


def _text_events(eng, run_id):
    db = mk_session(eng)
    try:
        return (db.query(RunEvent)
                .filter(RunEvent.run_id == run_id, RunEvent.category == "text")
                .order_by(RunEvent.run_seq.asc())
                .all())
    finally:
        db.close()


def test_sub_threshold_text_flushes_during_a_provider_stall_within_bound(eng):
    """The core contract: text below flush_bytes becomes DURABLE while stalled."""
    run_id = _mk_run(eng)
    buf = _RecordingBuffer(mk_factory(eng), run_id, tenant_id=None, user_id=7,
                           flush_bytes=4096, flush_ms=100)
    provider = _StallProvider([SMALL_TEXT])
    published_at = {}

    def on_tick():
        buf.tick()
        if buf.first_flush_at is not None:
            raise chat_stream.StreamAborted()   # done: flush observed

    def on_idle():
        # poll_interval below is the PRODUCTION guard cadence (20 s). If this ever
        # fires, the flush did not happen on its own bound, so fail loudly rather
        # than let the guard mask a regression.
        raise AssertionError("guard fired before the text flushed")

    def on_token(tok):
        published_at["t"] = time.monotonic()
        buf.publish(tok)

    ci = _chat_input()
    now = lambda: _dt.datetime.now(_dt.timezone.utc)

    async def main():
        with pytest.raises(chat_stream.StreamAborted):
            await chat_stream._adrive(
                provider, ci, on_token=on_token, on_idle=on_idle,
                deadline=now() + _dt.timedelta(seconds=300), now_fn=now,
                # production guard cadence: only the lightweight tick can flush
                # inside the bound asserted below.
                poll_interval=20.0, on_tick=on_tick,
                tick_interval=buf.flush_interval_s)

    asyncio.run(main())

    assert buf.first_flush_at is not None, "buffered text never flushed during the stall"
    elapsed = buf.first_flush_at - published_at["t"]
    assert elapsed < 1.0, "flush took %.3fs, expected to honour flush_ms (~0.1s)" % elapsed
    assert provider.completed == 0, "the provider must never have completed"
    assert provider.closed == 1

    events = _text_events(eng, run_id)
    assert len(events) == 1
    import json as _json
    assert _json.loads(events[0].data)["text"] == SMALL_TEXT


def test_no_empty_text_event_when_the_buffer_is_empty(eng):
    run_id = _mk_run(eng, key="FLUSH-KEY-EMPTY")
    buf = _RecordingBuffer(mk_factory(eng), run_id, tenant_id=None, user_id=7,
                           flush_bytes=4096, flush_ms=50)
    provider = _StallProvider([])          # no tokens at all

    async def main():
        with pytest.raises(chat_stream.StreamAborted):
            await chat_stream._adrive(
                provider, _chat_input(),
                on_token=lambda tok: buf.publish(tok),
                on_idle=lambda: (_ for _ in ()).throw(chat_stream.StreamAborted()),
                deadline=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=300),
                now_fn=lambda: _dt.datetime.now(_dt.timezone.utc),
                poll_interval=0.3, on_tick=buf.tick, tick_interval=buf.flush_interval_s)

    asyncio.run(main())
    assert buf.flushed_events == 0
    assert _text_events(eng, run_id) == []


def test_repeated_ticks_after_a_token_do_not_duplicate_text(eng):
    run_id = _mk_run(eng, key="FLUSH-KEY-DUP")
    buf = _RecordingBuffer(mk_factory(eng), run_id, tenant_id=None, user_id=7,
                           flush_bytes=4096, flush_ms=10)
    buf.publish(SMALL_TEXT)
    time.sleep(0.05)
    buf.tick()                              # the real flush
    buf.tick()                              # empty buffer: must be a no-op
    buf.tick()
    buf.close()                             # close on an empty buffer: also a no-op
    assert _text_events(eng, run_id).__len__() == 1


def test_utf8_text_reconstructs_exactly_across_a_flush(eng):
    run_id = _mk_run(eng, key="FLUSH-KEY-UTF8")
    buf = _RecordingBuffer(mk_factory(eng), run_id, tenant_id=None, user_id=7,
                           flush_bytes=4096, flush_ms=10)
    for piece in UTF8_PIECES:
        buf.publish(piece)
    time.sleep(0.05)
    buf.tick()
    import json as _json
    got = "".join(_json.loads(e.data)["text"] for e in _text_events(eng, run_id))
    assert got == "".join(UTF8_PIECES)


def test_guards_keep_their_own_cadence_while_ticks_are_more_frequent(eng):
    """on_idle must NOT be promoted to the tick rate (no DB polling loop)."""
    run_id = _mk_run(eng, key="FLUSH-KEY-CADENCE")
    buf = _RecordingBuffer(mk_factory(eng), run_id, tenant_id=None, user_id=7,
                           flush_bytes=4096, flush_ms=50)
    counts = {"idle": 0, "tick": 0}
    provider = _StallProvider([])

    def on_tick():
        counts["tick"] += 1
        buf.tick()

    def on_idle():
        counts["idle"] += 1
        if counts["idle"] >= 3:
            raise chat_stream.StreamAborted()

    async def main():
        with pytest.raises(chat_stream.StreamAborted):
            await chat_stream._adrive(
                provider, _chat_input(), on_token=lambda tok: None, on_idle=on_idle,
                deadline=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=300),
                now_fn=lambda: _dt.datetime.now(_dt.timezone.utc),
                poll_interval=0.3, on_tick=on_tick, tick_interval=buf.flush_interval_s)

    asyncio.run(main())
    assert counts["idle"] == 3, counts
    assert counts["tick"] > counts["idle"] * 3, counts


def test_no_pending_asyncio_task_after_the_stalled_drive(eng):
    run_id = _mk_run(eng, key="FLUSH-KEY-TASKS")
    buf = _RecordingBuffer(mk_factory(eng), run_id, tenant_id=None, user_id=7,
                           flush_bytes=4096, flush_ms=50)
    provider = _StallProvider([])

    async def main():
        with pytest.raises(chat_stream.StreamAborted):
            await chat_stream._adrive(
                provider, _chat_input(), on_token=lambda tok: None,
                on_idle=lambda: (_ for _ in ()).throw(chat_stream.StreamAborted()),
                deadline=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=300),
                now_fn=lambda: _dt.datetime.now(_dt.timezone.utc),
                poll_interval=0.2, on_tick=buf.tick, tick_interval=buf.flush_interval_s)
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    assert asyncio.run(main()) == []


def test_both_buffer_drivers_wire_the_lightweight_tick():
    """Wiring guard: every run_stream( call that has a buffer passes on_tick."""
    tree = ast.parse(WORKER.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("run_stream")]
    assert calls, "no run_stream call found in worker.py"
    wired = 0
    for call in calls:
        kwargs = {k.arg for k in call.keywords}
        if "on_tick" in kwargs:
            wired += 1
            assert "tick_interval" in kwargs, "on_tick without tick_interval"
    assert wired == 2, "expected both TextEventBuffer drivers to tick (got %d)" % wired
