"""Phase 1B.2R MP-3C3B — durable /messages/stream backend cutover + notifier.

Route-decision tests via TestClient with the adapter internals faked (no real
worker/DB-run); durable-wire / notifier / approval-journey tests drive the real
adapter + a real RunWorker against an in-memory engine (MP-3C2 pattern). No live
provider, no frontend, the cutover flag is never left on across tests.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import (
    Message, MessageRole, PendingApproval, ResearchSession, Run, RunEvent, RunStep,
)
from app.run_engine import messages_stream as ms
from app.run_engine import submission as sub
from app.run_engine.notifier import run_notifier
from app.run_engine.worker import RunWorker
from tests.run_engine_helpers import ensure_user, make_ctx, mk_engine, mk_factory, mk_session

_HMAC = "mp3c3b-unit-test-hmac-key-0123456789"
TOOL = "mcp__srv__echo"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    # a fully-valid durable config by default; individual tests flip pieces off
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_worker_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "enable_deep_research", False)
    monkeypatch.setattr(settings, "enable_multi_agent", False)
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 3)
    monkeypatch.setattr(settings, "run_worker_handler_timeout_s", 300)
    monkeypatch.setattr(settings, "run_sse_poll_interval_s", 1)
    monkeypatch.setattr(settings, "run_sse_heartbeat_s", 0)
    monkeypatch.setattr(settings, "run_sse_max_seconds", 30)
    monkeypatch.setattr(settings, "run_messages_stream_max_concurrent_per_user", 4)
    # reset the in-process guards between tests
    ms._active_slots.clear()
    from app.services.rate_limiter import reset_rate_limiter
    reset_rate_limiter()
    yield
    ms._active_slots.clear()


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


# ── fakes ────────────────────────────────────────────────────────────


class FakeReq:
    """Minimal ASGI-ish request for the adapter (is_disconnected + app.state)."""

    def __init__(self, *, disconnect_after=None):
        self._n = 0
        self._da = disconnect_after

    async def is_disconnected(self):
        self._n += 1
        return self._da is not None and self._n > self._da


class FakeChat:
    def __init__(self, scripts, *, stall=0.0):
        self.scripts = [list(s) for s in scripts]
        self.stall = stall
        self.n = 0

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        toks = self.scripts[self.n] if self.n < len(self.scripts) else []
        self.n += 1
        for t in toks:
            yield {"type": "token", "token": t}
            if self.stall:
                await asyncio.sleep(self.stall)
        yield {"type": "done", "response": "".join(x for x in toks if isinstance(x, str))}


class Transport:
    def __init__(self, text="TOOL-OUT", is_error=False):
        self.calls = []
        self._t, self._e = text, is_error

    def __call__(self, db, user_id, tool_id, args):
        self.calls.append((tool_id, dict(args)))
        return (self._t, self._e)


def _reg(db, *, uid=7):
    from app.approval import registry
    for unlock in (False, True):
        registry.register_or_refresh(
            db, owner_user_id=uid, tool_id=TOOL, tool_name="echo",
            mcp_server_id=3, mcp_server_name="srv", executable_identity="python -m srv @1",
            non_secret_launch_config=registry.non_secret_launch_config("python", ["-m", "srv"]),
            config_ref_id=registry.config_ref_for_server(3), input_schema={"type": "object"},
            unlock_if_unchanged=unlock)


def _prep(eng, *, uid=7, sid=1, model="model-x", system_prompt="SESSION-SP"):
    db = mk_session(eng)
    try:
        ensure_user(db, uid)
        if db.query(ResearchSession).filter(ResearchSession.id == sid).first() is None:
            db.add(ResearchSession(id=sid, user_id=uid, title="t", model=model,
                                   system_prompt=system_prompt))
            db.commit()
        _reg(db, uid=uid)
    finally:
        db.close()


def _ctx(eng, *, uid=7, family="fam-alpha"):
    db = mk_session(eng)
    try:
        return make_ctx(db, user_id=uid, sid=family)
    finally:
        db.close()


def _submit(eng, ctx, *, message="hello there", key=None):
    db = mk_session(eng)
    try:
        return ms.resolve_and_submit(db, ctx, session_id=1, message=message,
                                     idempotency_key=key or uuid.uuid4().hex)
    finally:
        db.close()


def _worker(eng, provider, transport=None):
    return RunWorker(mk_factory(eng), worker_id="w-c3b",
                     chat_provider=lambda ci, **_k: provider, mcp_transport=transport)


def _drive(eng, provider, transport=None, sweeps=1):
    for _ in range(sweeps):
        _worker(eng, provider, transport).advance_chat_runs()


def _mk_marker(prefix, args):
    body = f'[MCP_CALL: {TOOL} {json.dumps(args)}]'
    return [prefix, "[MCP", "_CALL: ", f"{TOOL} ", json.dumps(args), "]", "\n", "TAIL"]


async def _collect(gen, *, limit=200):
    out = []
    try:
        async for f in gen:
            out.append(f)
            if len(out) >= limit:
                break
    except Exception as exc:  # surface adapter crashes in the test
        out.append(f"__EXC__ {type(exc).__name__}")
    return out


def _events(frames):
    """[(event_name, data_dict), ...] parsed from legacy-wire frames."""
    evs = []
    for fr in frames:
        name = None
        data = {}
        for ln in fr.split("\n"):
            if ln.startswith("event: "):
                name = ln[7:]
            elif ln.startswith("data: "):
                try:
                    data = json.loads(ln[6:])
                except ValueError:
                    data = {}
        if name:
            evs.append((name, data))
    return evs


def _run_row(eng, rid):
    db = mk_session(eng)
    try:
        return db.query(Run).filter(Run.id == rid).first()
    finally:
        db.close()


def _decide(eng, aid, *, approve, uid=7):
    from app.routes.approvals import _decide as _d
    db = mk_session(eng)
    try:
        row = db.query(PendingApproval).filter(PendingApproval.id == aid).one()
        row.user_id = uid
        return _d(db, row, approve=approve)
    finally:
        db.close()


# ═══════════════ A. FLAG / COMPATIBILITY (route decision) ════════════


def _client(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app), app


def test_flag_off_invokes_exact_legacy_path(monkeypatch):
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    monkeypatch.setattr(settings, "run_engine_enabled", False)
    monkeypatch.setattr(settings, "run_worker_enabled", False)
    called = {"legacy": 0, "durable": 0}

    import app.routes.messages as mod

    class _Ctx:
        session_id = 1
        citations = []
        sources_used = False
        memories_used = False

    monkeypatch.setattr(mod, "prepare_chat_context",
                        lambda **kw: called.__setitem__("legacy", called["legacy"] + 1) or _Ctx())

    async def _fake_stream(ctx):
        yield mod.format_sse("start", {})
        yield mod.format_sse("complete", {"message_id": 7, "citations": []})

    monkeypatch.setattr(mod, "stream_chat_response", _fake_stream)
    monkeypatch.setattr(mod, "save_assistant_message",
                        lambda **kw: type("M", (), {"id": 7})())
    monkeypatch.setattr(mod, "run_memory_extraction", lambda **kw: None)
    monkeypatch.setattr(mod, "_stream_chat_durable",
                        lambda *a, **k: called.__setitem__("durable", 1))

    c, app = _client(monkeypatch)
    with c:
        from app.services.auth_service import get_current_user
        from app.services.cookie_service import require_csrf
        from app.database import get_db
        app.dependency_overrides[get_current_user] = lambda: type("U", (), {"id": 7})()
        app.dependency_overrides[require_csrf] = lambda: None
        try:
            r = c.post("/api/sessions/1/messages/stream", json={"message": "hi"})
        finally:
            app.dependency_overrides.clear()
    assert r.status_code == 200
    assert "event: complete" in r.text and "text/event-stream" in r.headers["content-type"]
    assert called["legacy"] == 1 and called["durable"] == 0
    assert "X-Run-Id" not in r.headers


def test_flag_on_invalid_dependency_fails_before_persistence(monkeypatch, eng):
    monkeypatch.setattr(settings, "run_chat_stream_enabled", False)   # dependency missing

    class _Sup:
        def is_durable_path_ready(self):
            return True

    with pytest.raises(ms.DurableStreamError) as ei:
        ms.assert_durable_stream_ready(_Sup())
    assert ei.value.http_status == 503
    # no Run created
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 0
    finally:
        db.close()


def test_flag_on_unhealthy_worker_returns_503(monkeypatch):
    class _DeadSup:
        def is_durable_path_ready(self):
            return False

    with pytest.raises(ms.DurableStreamError) as ei:
        ms.assert_durable_stream_ready(_DeadSup())
    assert ei.value.http_status == 503
    assert "temporarily unavailable" in ei.value.detail.lower()
    with pytest.raises(ms.DurableStreamError) as ei2:
        ms.assert_durable_stream_ready(None)          # no supervisor at all
    assert ei2.value.http_status == 503


def test_ready_supervisor_passes(monkeypatch):
    class _OkSup:
        def is_durable_path_ready(self):
            return True

    ms.assert_durable_stream_ready(_OkSup())          # no raise


def test_unsupported_modes_fail_before_persistence(monkeypatch):
    # MP-3C3D1: routing is server-owned (DURABLE_STREAM_MODE), never the
    # unrelated global enable_deep_research/enable_multi_agent settings —
    # those flags describe the LEGACY pipeline's capability, not this
    # request's mode. See test_run_engine_mp3c3d1.py for the full fix
    # coverage (global-flag independence, injected-mode rejection, etc.).
    assert ms.unsupported_mode_code(has_image=True) == "mode_unsupported_image"
    assert ms.unsupported_mode_code(has_image=False) is None
    monkeypatch.setattr(settings, "enable_deep_research", True)
    monkeypatch.setattr(settings, "enable_multi_agent", True)
    assert ms.unsupported_mode_code(has_image=False) is None, (
        "global capability flags must never block durable plain chat"
    )
    monkeypatch.setattr(settings, "enable_deep_research", False)
    monkeypatch.setattr(settings, "enable_multi_agent", False)
    # MP-3C3D4C: "deep_research" now has its OWN dedicated, config-dependent
    # bounded reason (computed via the one authoritative readiness
    # predicate) rather than the generic "mode_unsupported_" prefix every
    # other unimplemented/unrecognized mode still gets — see
    # test_deep_research_readiness.py for the full readiness-predicate
    # coverage.
    assert ms.unsupported_mode_code(has_image=False, mode="deep_research") == (
        "deep_research_not_executable"
    )
    # MP-3C3D5C closeout: "multi_agent" is NO LONGER generic/unimplemented —
    # it now has the SAME dedicated, config-AND-implementation-dependent
    # reason shape as deep_research (computed fresh via the one authoritative
    # multi_agent_readiness.multi_agent_executable() predicate, see
    # test_multi_agent_readiness.py). This expectation was stale: it still
    # asserted the pre-D5C generic slug. Updated to the superseding contract —
    # the assertion is equally exact, just for the correct code.
    assert ms.unsupported_mode_code(has_image=False, mode="multi_agent") == (
        "multi_agent_not_executable"
    )


def test_idempotency_key_validation(monkeypatch):
    assert ms.valid_idempotency_key(None) is False
    assert ms.valid_idempotency_key("") is False
    assert ms.valid_idempotency_key("   ") is False
    assert ms.valid_idempotency_key("a" * 5000) is False        # too long
    assert ms.valid_idempotency_key("client-key-abc-123") is True


# ═══════════════ B. SUBMISSION (adapter) ════════════════════════════


def test_identical_retry_same_run_no_dup_rows(eng):
    _prep(eng)
    ctx = _ctx(eng)
    key = "retry-key-1"
    run1, c1 = _submit(eng, ctx, message="same message", key=key)
    run2, c2 = _submit(eng, ctx, message="same message", key=key)
    assert c1 is True and c2 is False and run1.id == run2.id
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 1
        assert db.query(Message).filter(Message.role == MessageRole.user).count() == 1
        assert db.query(RunStep).filter(RunStep.run_id == run1.id).count() == 1
    finally:
        db.close()


def test_changed_semantic_input_conflicts(eng):
    _prep(eng)
    ctx = _ctx(eng)
    key = "retry-key-2"
    _submit(eng, ctx, message="first", key=key)
    with pytest.raises(ms.DurableStreamError) as ei:
        _submit(eng, ctx, message="DIFFERENT", key=key)
    assert ei.value.http_status == 409


def test_cross_user_same_key_isolated(eng):
    _prep(eng, uid=7)
    _prep(eng, uid=99, sid=1)   # same session id belongs to user 7; user 99 has no session 1
    key = "shared-key"
    _submit(eng, _ctx(eng, uid=7), message="u7 msg", key=key)
    with pytest.raises(ms.DurableStreamError) as ei:
        _submit(eng, _ctx(eng, uid=99, family="fam-99"), message="u99 msg", key=key)
    assert ei.value.http_status == 404          # non-disclosing (session not theirs)
    db = mk_session(eng)
    try:
        runs = db.query(Run).all()
        assert len(runs) == 1 and runs[0].user_id == 7
    finally:
        db.close()


def test_submit_creates_no_execution(eng):
    _prep(eng)
    run, _ = _submit(eng, _ctx(eng), message="no exec please")
    r = _run_row(eng, run.id)
    assert r.status == "queued"                 # queued, not running/succeeded
    db = mk_session(eng)
    try:
        assert db.query(Message).filter(Message.role == MessageRole.assistant).count() == 0
        assert db.query(RunEvent).filter(RunEvent.run_id == run.id,
                                         RunEvent.category == "text").count() == 0
    finally:
        db.close()


def test_bad_session_is_404(eng):
    _prep(eng)
    with pytest.raises(ms.DurableStreamError) as ei:
        db = mk_session(eng)
        try:
            ms.resolve_and_submit(db, _ctx(eng), session_id=999, message="x",
                                  idempotency_key="k")
        finally:
            db.close()
    assert ei.value.http_status == 404


# ═══════════════ C. STREAMING / WIRE (adapter) ═════════════════════


def _stream_frames(eng, ctx, run_id, *, after_seq=0, disconnect_after=None, limit=200):
    async def go():
        gen = ms.stream_legacy_wire(FakeReq(disconnect_after=disconnect_after),
                                    mk_factory(eng), ctx, run_id, after_seq=after_seq)
        return await _collect(gen, limit=limit)
    return asyncio.run(go())


def test_plain_chat_text_then_complete_in_order(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="say hi")
    _drive(eng, FakeChat([["Hello ", "there, ", "world."]]))
    assert _run_row(eng, run.id).status == "succeeded"
    evs = _events(_stream_frames(eng, ctx, run.id))
    names = [n for n, _ in evs]
    assert names[0] == "start"
    assert "token" in names and names[-1] == "complete"
    text = "".join(d["token"] for n, d in evs if n == "token")
    assert text == "Hello there, world."
    comp = [d for n, d in evs if n == "complete"][0]
    assert isinstance(comp["message_id"], int) and comp["message_id"] > 0
    assert comp["citations"] == [] and comp["sources_used"] is False


def test_id_lines_present_and_reconnect_has_no_gap_or_dup(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="hi")
    _drive(eng, FakeChat([["aa ", "bb ", "cc"]]))
    frames = _stream_frames(eng, ctx, run.id)
    ids = [int(ln[4:]) for fr in frames for ln in fr.split("\n") if ln.startswith("id: ")]
    assert ids == sorted(ids) and len(ids) == len(set(ids))     # monotonic, unique
    mid = ids[len(ids) // 2]
    # reconnect from the middle cursor — nothing <= mid is resent
    frames2 = _stream_frames(eng, ctx, run.id, after_seq=mid)
    ids2 = [int(ln[4:]) for fr in frames2 for ln in fr.split("\n") if ln.startswith("id: ")]
    assert all(i > mid for i in ids2)
    ev2 = _events(frames2)
    assert ev2 and ev2[-1][0] == "complete"                     # still terminates


def test_disconnect_does_not_cancel_run(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="hi")
    # stream BEFORE the worker runs; disconnect immediately
    _stream_frames(eng, ctx, run.id, disconnect_after=0, limit=5)
    r = _run_row(eng, run.id)
    assert r.status in ("queued", "running")          # NOT cancelled
    # the worker can still finish it
    _drive(eng, FakeChat([["done."]]))
    assert _run_row(eng, run.id).status == "succeeded"
    assert mk_session(eng).query(Message).filter(
        Message.role == MessageRole.assistant).count() == 1


def test_heartbeat_not_persisted(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="hi")
    # run stays queued; stream a couple polls then disconnect
    frames = _stream_frames(eng, ctx, run.id, disconnect_after=2, limit=10)
    assert any(fr.startswith(": heartbeat") for fr in frames)
    db = mk_session(eng)
    try:
        blob = "\n".join(e.data or "" for e in
                         db.query(RunEvent).filter(RunEvent.run_id == run.id).all())
        assert "heartbeat" not in blob
    finally:
        db.close()


def test_revoked_session_terminates_stream_safely(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="hi")
    from tests.run_engine_helpers import revoke_family
    db = mk_session(eng)
    try:
        revoke_family(db, "fam-alpha")
    finally:
        db.close()
    evs = _events(_stream_frames(eng, ctx, run.id, limit=10))
    assert evs and evs[-1][0] == "error"
    assert evs[-1][1].get("code") == "stream_closed"


def test_cross_user_stream_is_non_disclosing(eng):
    _prep(eng, uid=7)
    run, _ = _submit(eng, _ctx(eng, uid=7), message="secret prompt")
    _prep(eng, uid=42, sid=2)
    other = make_ctx(mk_session(eng), user_id=42, sid="fam-42")
    evs = _events(_stream_frames(eng, other, run.id, limit=10))
    # binding check fails -> generic stream_closed error, no run data leaked
    assert evs and evs[-1][0] == "error" and evs[-1][1].get("code") == "stream_closed"
    assert not any("secret prompt" in json.dumps(d) for _, d in evs)


def test_terminal_run_replay_emits_terminal_frame(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="hi")
    _drive(eng, FakeChat([["final answer"]]))
    r = _run_row(eng, run.id)
    high = r.last_event_seq
    # reconnect with a cursor AT the last seq -> no rows, but terminal frame synth
    evs = _events(_stream_frames(eng, ctx, run.id, after_seq=high, limit=10))
    assert evs and evs[-1][0] == "complete"


def test_future_cursor_terminates_cleanly(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="hi")
    _drive(eng, FakeChat([["ok"]]))
    evs = _events(_stream_frames(eng, ctx, run.id, after_seq=999999, limit=10))
    assert evs and evs[-1][0] == "complete"      # terminal synth still fires


def test_parse_cursor_handles_bad_values():
    assert ms.parse_cursor(None, None) == 0
    assert ms.parse_cursor("5", None) == 5
    assert ms.parse_cursor(None, "12") == 12
    assert ms.parse_cursor("-3", "junk") == 0
    assert ms.parse_cursor("junk", "7") == 7


def test_failed_run_maps_to_bounded_error(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="hi")
    # provider raises -> run failed(provider_error)
    class _Boom:
        async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
            raise RuntimeError("upstream 500 SECRET")
            yield  # pragma: no cover
    _drive(eng, _Boom())
    assert _run_row(eng, run.id).status == "failed"
    evs = _events(_stream_frames(eng, ctx, run.id, limit=10))
    assert evs[-1][0] == "error"
    assert evs[-1][1]["code"] == "provider_error"
    assert "SECRET" not in json.dumps(evs)


# ═══════════════ D. NOTIFIER ═══════════════════════════════════════


def test_commit_then_notify_and_missed_wake_recovered(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="hi")

    async def go():
        run_notifier.bind_loop()
        ev = run_notifier.subscribe(run.id)
        assert not ev.is_set()
        # commit a text event via the real worker path -> after_commit hook fires
        from app.run_engine.text_events import append_text_event
        d = mk_factory(eng)()
        try:
            append_text_event(d, run_id=run.id, tenant_id=None, user_id=7,
                              text="hello", commit=True)
        finally:
            d.close()
        await asyncio.sleep(0.05)
        assert ev.is_set()                       # commit -> notify
        ev.clear()
        # a wake with no commit is harmless; a DB re-query still finds the row
        run_notifier.notify(run.id)
        await asyncio.sleep(0.02)
        run_notifier.unsubscribe(run.id, ev)
        run_notifier.unbind_loop()

    asyncio.run(go())


def test_duplicate_wake_no_duplicate_event(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="hi")
    _drive(eng, FakeChat([["one ", "two"]]))

    async def go():
        run_notifier.bind_loop()
        for _ in range(5):
            run_notifier.notify(run.id)
        frames = await _collect(
            ms.stream_legacy_wire(FakeReq(), mk_factory(eng), ctx, run.id, after_seq=0),
            limit=100)
        run_notifier.unbind_loop()
        return frames

    frames = asyncio.run(go())
    toks = [d["token"] for n, d in _events(frames) if n == "token"]
    assert "".join(toks) == "one two"           # each text event exactly once


def test_thread_safe_worker_publication(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="hi")

    async def go():
        run_notifier.bind_loop()
        ev = run_notifier.subscribe(run.id)
        # publish from ANOTHER thread (as the owned worker thread would)
        threading.Thread(target=lambda: run_notifier.notify(run.id), daemon=True).start()
        await asyncio.wait_for(ev.wait(), timeout=2)
        assert ev.is_set()
        run_notifier.unsubscribe(run.id, ev)
        run_notifier.unbind_loop()

    asyncio.run(go())


def test_subscribers_bounded_and_cleaned(eng):
    run_id = "rid-bound"

    async def go():
        run_notifier.bind_loop()
        evs = [run_notifier.subscribe(run_id) for _ in range(200)]
        assert run_notifier.waiter_count(run_id) <= 64      # capped
        for e in evs:
            run_notifier.unsubscribe(run_id, e)
        assert run_notifier.waiter_count(run_id) == 0       # key dropped
        run_notifier.unbind_loop()

    asyncio.run(go())


def test_notify_without_bound_loop_is_noop():
    run_notifier.unbind_loop()
    run_notifier.notify("whatever")   # must not raise


def test_notifier_failure_falls_back_to_polling(eng, monkeypatch):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="hi")
    _drive(eng, FakeChat([["poll ", "fallback"]]))
    # simulate a broken notifier: subscribe returns an Event that is never set
    monkeypatch.setattr(run_notifier, "notify", lambda rid: None)

    async def go():
        run_notifier.bind_loop()
        frames = await _collect(
            ms.stream_legacy_wire(FakeReq(), mk_factory(eng), ctx, run.id, after_seq=0),
            limit=100)
        run_notifier.unbind_loop()
        return frames

    frames = asyncio.run(go())
    evs = _events(frames)
    assert "".join(d["token"] for n, d in evs if n == "token") == "poll fallback"
    assert evs[-1][0] == "complete"             # DB polling still completed it


# ═══════════════ E. APPROVAL JOURNEY ═══════════════════════════════


def test_wait_approve_at_most_once_continuation_one_message(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="use the tool")
    prov = FakeChat([_mk_marker("Let me check. ", {"q": "ARG-CANARY"}),
                     ["The answer is 42."]])
    tr = Transport(text="TOOL-OUT-CANARY")

    _drive(eng, prov, tr)                        # sweep 1 -> parks for approval
    r = _run_row(eng, run.id)
    assert r.status == "waiting_for_approval" and r.pending_approval_id
    assert tr.calls == []

    evs1 = _events(_stream_frames(eng, ctx, run.id, limit=50))
    names1 = [n for n, _ in evs1]
    assert "tool_approval_required" in names1
    appr = [d for n, d in evs1 if n == "tool_approval_required"][0]
    assert appr["approval_id"] == r.pending_approval_id
    assert appr.get("tool_id") == TOOL          # server id only
    assert "ARG-CANARY" not in json.dumps(evs1) and "[MCP_CALL" not in json.dumps(evs1)
    assert not any(n == "complete" for n in names1)

    _decide(eng, r.pending_approval_id, approve=True)
    assert Transport.__init__ and tr.calls == []  # endpoint executed nothing

    _drive(eng, prov, tr)                        # sweep 2 -> dispatch + continuation
    assert _run_row(eng, run.id).status == "succeeded"
    assert len(tr.calls) == 1                    # MCP executed exactly once

    evs2 = _events(_stream_frames(eng, ctx, run.id, after_seq=0, limit=80))
    text = "".join(d["token"] for n, d in evs2 if n == "token")
    assert text == "Let me check. The answer is 42."
    assert evs2[-1][0] == "complete"
    msgs = mk_session(eng).query(Message).filter(Message.role == MessageRole.assistant).all()
    assert len(msgs) == 1 and msgs[0].content == text
    assert "TOOL-OUT-CANARY" not in json.dumps(evs2)


def test_denial_no_dispatch_no_continuation(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="use tool")
    prov = FakeChat([_mk_marker("thinking ", {"q": 1}), ["never"]])
    tr = Transport()
    _drive(eng, prov, tr)
    aid = _run_row(eng, run.id).pending_approval_id
    _decide(eng, aid, approve=False)
    _drive(eng, prov, tr)
    r = _run_row(eng, run.id)
    assert r.status == "denied" and tr.calls == [] and prov.n == 1
    evs = _events(_stream_frames(eng, ctx, run.id, limit=50))
    assert evs[-1][0] == "error" and evs[-1][1]["code"] == "approval_denied"


def test_cancel_while_waiting_prevents_late_dispatch(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="use tool")
    prov = FakeChat([_mk_marker("a ", {"q": 1}), ["b"]])
    tr = Transport()
    _drive(eng, prov, tr)
    aid = _run_row(eng, run.id).pending_approval_id
    db = mk_session(eng)
    try:
        from app.run_engine.engine import request_cancellation
        request_cancellation(db, make_ctx(db, user_id=7, sid="fam-alpha"), run.id)
    finally:
        db.close()
    _decide(eng, aid, approve=True)              # approval lands AFTER cancel
    _drive(eng, prov, tr)
    r = _run_row(eng, run.id)
    assert r.status == "cancelled" and tr.calls == []
    evs = _events(_stream_frames(eng, ctx, run.id, limit=50))
    assert evs[-1][0] == "cancelled"


def test_reconnect_during_resume_gets_continuation(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="use tool")
    prov = FakeChat([_mk_marker("pre ", {"q": 1}), ["post continuation"]])
    tr = Transport(text="R")
    _drive(eng, prov, tr)
    # first connection: up to the approval
    frames1 = _stream_frames(eng, ctx, run.id, after_seq=0, limit=40)
    cur = max((int(ln[4:]) for fr in frames1 for ln in fr.split("\n")
               if ln.startswith("id: ")), default=0)
    _decide(eng, _run_row(eng, run.id).pending_approval_id, approve=True)
    _drive(eng, prov, tr)
    # reconnect from the saved cursor -> sees only continuation + complete
    evs2 = _events(_stream_frames(eng, ctx, run.id, after_seq=cur, limit=40))
    text2 = "".join(d["token"] for n, d in evs2 if n == "token")
    assert "post continuation" in text2 and evs2[-1][0] == "complete"


# ═══════════════ F. FAILURE / RACE ════════════════════════════════


def test_submission_commit_failure_no_partial(eng, monkeypatch):
    _prep(eng)
    ctx = _ctx(eng)
    import app.run_engine.submission as smod
    real = smod.transition_run

    def boom(db, c, rid, **kw):
        raise RuntimeError("commit boom")

    monkeypatch.setattr(smod, "transition_run", boom)
    with pytest.raises(Exception):
        _submit(eng, ctx, message="will fail")
    monkeypatch.setattr(smod, "transition_run", real)
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 0
        assert db.query(Message).count() == 0
        assert db.query(RunStep).count() == 0
    finally:
        db.close()


def test_event_between_query_and_wait_not_lost(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="hi")

    async def go():
        run_notifier.bind_loop()
        gen = ms.stream_legacy_wire(FakeReq(), mk_factory(eng), ctx, run.id, after_seq=0)
        first = []
        # pull the `start` frame, then let the worker produce + terminate
        first.append(await gen.__anext__())
        _drive(eng, FakeChat([["late ", "text"]]))
        rest = await _collect(gen, limit=60)
        run_notifier.unbind_loop()
        return first + rest

    frames = asyncio.run(go())
    evs = _events(frames)
    assert "".join(d["token"] for n, d in evs if n == "token") == "late text"
    assert evs[-1][0] == "complete"


def test_terminal_failure_cannot_become_success(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="hi")

    class _Boom:
        async def generate_stream_async(self, m, system_prompt=None, model_name=None):
            raise RuntimeError("x")
            yield  # pragma: no cover

    _drive(eng, _Boom())
    assert _run_row(eng, run.id).status == "failed"
    # a later worker sweep cannot resurrect it
    _drive(eng, FakeChat([["should not run"]]))
    assert _run_row(eng, run.id).status == "failed"
    evs = _events(_stream_frames(eng, ctx, run.id, limit=20))
    assert evs[-1][0] == "error"


def test_late_approval_cannot_resurrect_terminal_run(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="use tool")
    prov = FakeChat([_mk_marker("a ", {"q": 1}), ["b"]])
    tr = Transport()
    _drive(eng, prov, tr)
    aid = _run_row(eng, run.id).pending_approval_id
    _decide(eng, aid, approve=False)             # denied -> terminal
    _drive(eng, prov, tr)
    assert _run_row(eng, run.id).status == "denied"
    # a spurious approve now
    db = mk_session(eng)
    try:
        row = db.query(PendingApproval).filter(PendingApproval.id == aid).one()
        assert row.state == "denied"
    finally:
        db.close()
    _drive(eng, prov, tr)
    assert _run_row(eng, run.id).status == "denied" and tr.calls == []


def test_concurrent_stream_slots_bounded(monkeypatch):
    monkeypatch.setattr(settings, "run_messages_stream_max_concurrent_per_user", 2)
    assert ms.acquire_stream_slot(7) is True
    assert ms.acquire_stream_slot(7) is True
    assert ms.acquire_stream_slot(7) is False     # 3rd blocked
    ms.release_stream_slot(7)
    assert ms.acquire_stream_slot(7) is True
    ms.release_stream_slot(7); ms.release_stream_slot(7)
    assert ms._active_slots.get(7) is None         # fully released -> key gone


def test_submit_rate_limit(monkeypatch):
    monkeypatch.setattr(settings, "run_messages_stream_submits_per_min", 3)
    from app.services.rate_limiter import reset_rate_limiter
    reset_rate_limiter()
    hits = [ms.submit_rate_limited(7) for _ in range(6)]
    assert hits[:3] == [False, False, False]
    assert any(hits[3:])                            # eventually limited


# ═══════════════ G. PRIVACY ══════════════════════════════════════


def test_canaries_absent_from_wire_and_logs(eng, caplog):
    _prep(eng, system_prompt="SYSPROMPT-CANARY")
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="PROMPT-CANARY do it")
    prov = FakeChat([_mk_marker("clean ", {"secret": "ARGS-CANARY"}), ["visible answer."]])
    tr = Transport(text="TOOLOUT-CANARY")
    with caplog.at_level("DEBUG"):
        _drive(eng, prov, tr)
        _decide(eng, _run_row(eng, run.id).pending_approval_id, approve=True)
        _drive(eng, prov, tr)
        frames = _stream_frames(eng, ctx, run.id, after_seq=0, limit=120)
    blob = json.dumps(_events(frames)) + "\n" + caplog.text + "\n" + "".join(frames)
    for bad in ("PROMPT-CANARY", "SYSPROMPT-CANARY", "ARGS-CANARY", "TOOLOUT-CANARY",
                "[MCP_CALL", "MCP Tool Result", "TAIL"):
        assert bad not in blob, bad
    # only the intended clean assistant text is in token events
    toks = "".join(d["token"] for n, d in _events(frames) if n == "token")
    assert toks == "clean visible answer."


def test_approval_event_is_redacted(eng):
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="use tool")
    prov = FakeChat([_mk_marker("x ", {"password": "hunter2", "path": "/etc/passwd"}), ["y"]])
    _drive(eng, prov, Transport())
    evs = _events(_stream_frames(eng, ctx, run.id, limit=40))
    appr = [d for n, d in evs if n == "tool_approval_required"][0]
    assert set(appr) <= {"approval_id", "tool_id"}
    assert "hunter2" not in json.dumps(evs) and "/etc/passwd" not in json.dumps(evs)
