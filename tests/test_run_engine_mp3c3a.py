"""Phase 1B.2R MP-3C3A — FastAPI worker lifespan supervisor + operational safety.

Fake worker for the supervisor-lifecycle tests; a real in-memory engine + fake
streaming provider for the shutdown-semantics tests; direct calls to the
canonical MCP capture/parser for the adversarial spot check. No live provider, no
`/messages/stream`, no frontend.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import settings
from app.models.models import Message, MessageRole, Run, RunStep
from app.run_engine import chat_stream as cs
from app.run_engine import chat_tools as ct
from app.run_engine import submission as sub
from app.run_engine.worker import RunWorker
from app.run_engine.worker_supervisor import (
    RunWorkerSupervisor, SupervisorFatalError, _classify, _FAILURE_CEILING,
)
from tests.run_engine_helpers import ensure_user, make_ctx, mk_engine, mk_factory, mk_session

_HMAC = "mp3c3a-unit-test-hmac-key-0123456789"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    # keep every supervisor timing knob tiny so the async tests are quick
    monkeypatch.setattr(settings, "run_worker_poll_interval_s", 1)
    monkeypatch.setattr(settings, "run_worker_backoff_min_s", 1)
    monkeypatch.setattr(settings, "run_worker_backoff_max_s", 2)
    monkeypatch.setattr(settings, "run_worker_shutdown_grace_s", 5)
    monkeypatch.setattr(settings, "run_worker_web_concurrency", 1)


def _enable(monkeypatch, *, engine=True, worker=True, chat=False, public=False):
    monkeypatch.setattr(settings, "run_engine_enabled", engine)
    monkeypatch.setattr(settings, "run_worker_enabled", worker)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", chat)
    monkeypatch.setattr(settings, "run_public_api_enabled", public)


def _run(coro):
    return asyncio.run(coro)


def _sup_threads():
    return [t.name for t in threading.enumerate()
            if t.name in ("run-worker", "run-worker-supervisor", "chat-lease-hb",
                          "chat-provider-bridge")]


# ── fake worker ──────────────────────────────────────────────────────


class FakeWorker:
    def __init__(self, *, behavior="ok", on_poll=None, base_exc=None, gate=None):
        # ok | raise | raise_schema | watch_stop | raise_base | block_until
        self.behavior = behavior
        self.polls = 0
        self.stop_fn = None
        self.saw_stop = False
        self._on_poll = on_poll
        self._base_exc = base_exc          # a BaseException-not-Exception to raise
        self._gate = gate                 # threading.Event an "uncooperative" sweep blocks on
        self.in_poll = threading.Event()  # set while inside poll_once

    def attach_stop(self, fn):
        self.stop_fn = fn

    def poll_once(self):
        self.polls += 1
        self.in_poll.set()
        try:
            if self._on_poll is not None:
                self._on_poll(self)
            if self.behavior == "raise":
                raise RuntimeError("kaboom SECRET-boom")
            if self.behavior == "raise_schema":
                raise type("OperationalError", (Exception,), {})("no such table: runs")
            if self.behavior == "raise_base":
                raise self._base_exc
            if self.behavior == "block_until":   # ignores the cooperative stop signal
                self._gate.wait(timeout=10)
                return []
            if self.behavior == "watch_stop":    # cooperative — exits on the stop signal
                for _ in range(500):
                    if self.stop_fn and self.stop_fn():
                        self.saw_stop = True
                        return []
                    time.sleep(0.02)
            return []
        finally:
            self.in_poll.clear()


# ═══════════════════ 1. STARTUP ═══════════════════════════════════════


def test_flags_off_no_task(monkeypatch):
    _enable(monkeypatch, engine=False, worker=False)
    fw = FakeWorker()
    sup = RunWorkerSupervisor(worker_factory=lambda: fw)

    async def scenario():
        await sup.start()
        assert sup._thread is None and sup.alive is False
        st = sup.status()
        assert st["enabled"] is False and st["started"] is False
        await sup.stop()

    _run(scenario())
    assert fw.polls == 0
    assert _sup_threads() == []


def test_valid_flags_one_supervisor_task(monkeypatch):
    _enable(monkeypatch)
    fw = FakeWorker()
    sup = RunWorkerSupervisor(worker_factory=lambda: fw)

    async def scenario():
        await sup.start()
        t1 = sup._thread
        assert t1 is not None and sup.alive
        await sup.start()                       # idempotent
        assert sup._thread is t1
        await asyncio.sleep(0.2)
        assert fw.polls >= 1
        await sup.stop()
        assert not sup._thread.is_alive() and sup._started is False

    _run(scenario())
    assert _sup_threads() == []


def test_worker_constructed_only_at_start(monkeypatch):
    _enable(monkeypatch)
    made = []
    sup = RunWorkerSupervisor(worker_factory=lambda: made.append(1) or FakeWorker())
    assert made == []                          # NOT built at __init__

    async def scenario():
        await sup.start()
        await sup.start()                       # still one worker
        await sup.stop()

    _run(scenario())
    assert made == [1]


def test_invalid_topology_fails_closed(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(settings, "run_worker_web_concurrency", 2)
    monkeypatch.setattr(settings, "database_url", "sqlite:////data/app.db")
    sup = RunWorkerSupervisor(worker_factory=lambda: FakeWorker())

    async def scenario():
        with pytest.raises(SupervisorFatalError):
            await sup.start()

    _run(scenario())
    assert sup._thread is None and sup._started is False
    assert _sup_threads() == []


def test_topology_ok_on_postgres_url(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(settings, "run_worker_web_concurrency", 4)
    monkeypatch.setattr(settings, "database_url", "postgresql://x/y")
    sup = RunWorkerSupervisor(worker_factory=lambda: FakeWorker())

    async def scenario():
        await sup.start()                       # PG topology may run >1 web proc
        assert sup.alive
        await sup.stop()

    _run(scenario())


def test_startup_worker_construction_failure_no_partial(monkeypatch):
    _enable(monkeypatch)

    def boom():
        raise RuntimeError("factory failed")

    sup = RunWorkerSupervisor(worker_factory=boom)

    async def scenario():
        with pytest.raises(RuntimeError):
            await sup.start()

    _run(scenario())
    assert sup._thread is None and sup._started is False
    assert _sup_threads() == []


# ═══════════════════ 2. POLLING / FAILURE ════════════════════════════


def test_sweep_uses_worker_poll_once(monkeypatch):
    _enable(monkeypatch)
    fw = FakeWorker()
    sup = RunWorkerSupervisor(worker_factory=lambda: fw)

    async def scenario():
        await sup.start()
        await asyncio.sleep(0.3)
        await sup.stop()

    _run(scenario())
    assert fw.polls >= 1
    st = sup.status()
    assert st["last_successful_poll_at"] is not None
    assert st["consecutive_failure_count"] == 0
    assert st["last_bounded_error_code"] is None


def test_recoverable_failure_backoff_then_recovers(monkeypatch):
    _enable(monkeypatch)
    state = {"fail": 2}

    def on_poll(w):
        if state["fail"] > 0:
            state["fail"] -= 1
            raise RuntimeError("transient DB-SECRET blip")

    fw = FakeWorker(on_poll=on_poll)
    sup = RunWorkerSupervisor(worker_factory=lambda: fw)

    async def scenario():
        await sup.start()
        await asyncio.sleep(0.3)               # first failing sweep
        assert sup.status()["consecutive_failure_count"] >= 1
        assert sup.status()["last_bounded_error_code"] == "sweep_error"
        await asyncio.sleep(3.5)               # backoff + retry -> eventual success
        st = sup.status()
        assert st["consecutive_failure_count"] == 0
        assert st["last_bounded_error_code"] is None
        assert st["last_successful_poll_at"] is not None
        await sup.stop()

    _run(scenario())


def test_fatal_schema_error_stops_supervisor(monkeypatch):
    _enable(monkeypatch)
    fw = FakeWorker(behavior="raise_schema")
    sup = RunWorkerSupervisor(worker_factory=lambda: fw)

    async def scenario():
        await sup.start()
        await asyncio.sleep(0.5)
        assert sup.alive is False              # failed closed, task ended
        assert sup.status()["last_bounded_error_code"] == "schema_error"
        await sup.stop()

    _run(scenario())
    assert _sup_threads() == []


def test_failure_ceiling_stops_without_spinning(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(settings, "run_worker_backoff_min_s", 1)
    monkeypatch.setattr(settings, "run_worker_backoff_max_s", 1)
    fw = FakeWorker(behavior="raise")
    sup = RunWorkerSupervisor(worker_factory=lambda: fw)

    async def scenario():
        await sup.start()
        # ceiling failures at ~1s backoff each
        await asyncio.sleep(_FAILURE_CEILING + 3)
        assert sup.alive is False
        assert sup.status()["last_bounded_error_code"] == "max_consecutive_failures"
        await sup.stop()

    _run(scenario())
    assert fw.polls <= _FAILURE_CEILING + 2    # bounded — no tight error loop


def test_stop_interrupts_backoff(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(settings, "run_worker_backoff_min_s", 30)   # long backoff
    fw = FakeWorker(behavior="raise")
    sup = RunWorkerSupervisor(worker_factory=lambda: fw)

    async def scenario():
        await sup.start()
        await asyncio.sleep(0.4)               # into the 30s backoff sleep
        t0 = time.monotonic()
        await sup.stop()
        assert time.monotonic() - t0 < 3.0     # returned promptly, not after 30s

    _run(scenario())
    assert _sup_threads() == []


def test_bounded_error_code_never_carries_raw_text(monkeypatch):
    _enable(monkeypatch)
    fw = FakeWorker(behavior="raise")          # message contains "SECRET-boom"
    sup = RunWorkerSupervisor(worker_factory=lambda: fw)

    async def scenario():
        await sup.start()
        await asyncio.sleep(0.4)
        code = sup.status()["last_bounded_error_code"]
        assert code == "sweep_error" and "SECRET" not in code
        await sup.stop()

    _run(scenario())


# ═══════════════════ 3. SHUTDOWN ════════════════════════════════════


def test_idle_worker_stops_and_joins(monkeypatch):
    _enable(monkeypatch)
    sup = RunWorkerSupervisor(worker_factory=lambda: FakeWorker())

    async def scenario():
        await sup.start()
        await asyncio.sleep(0.2)
        await sup.stop()
        assert not sup._thread.is_alive() and not sup.alive
        assert sup.status()["stopping"] is False

    _run(scenario())
    assert _sup_threads() == []


def test_stop_signals_worker_and_is_seen_mid_sweep(monkeypatch):
    _enable(monkeypatch)
    fw = FakeWorker(behavior="watch_stop")
    sup = RunWorkerSupervisor(worker_factory=lambda: fw)

    async def scenario():
        await sup.start()
        await asyncio.sleep(0.2)               # a sweep is now watching the stop fn
        await sup.stop(grace_s=5)
        assert fw.saw_stop is True             # cooperative signal reached the worker

    _run(scenario())
    assert _sup_threads() == []


# ═══════════════ 2b. CANCELLATION / BaseException BOUNDARY (close-out) ══


def _base_exc_case(monkeypatch, exc):
    _enable(monkeypatch)
    fw = FakeWorker(behavior="raise_base", base_exc=exc)
    sup = RunWorkerSupervisor(worker_factory=lambda: fw)

    async def scenario():
        await sup.start()
        await asyncio.sleep(0.4)
        return sup.status()

    st = _run(scenario())
    # the BaseException propagated out of the loop → thread ended, no retry,
    # no operational classification, no failure count bump
    assert fw.polls == 1
    assert st["consecutive_failure_count"] == 0
    assert st["last_bounded_error_code"] == "worker_interrupted"   # NOT sweep_error/db_unavailable
    assert sup.alive is False
    assert _sup_threads() == []
    return st


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_cancelled_error_from_poll_exits_no_backoff(monkeypatch):
    _base_exc_case(monkeypatch, asyncio.CancelledError())


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_systemexit_from_poll_not_recoverable(monkeypatch):
    _base_exc_case(monkeypatch, SystemExit(3))


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_keyboardinterrupt_from_poll_not_recoverable(monkeypatch):
    _base_exc_case(monkeypatch, KeyboardInterrupt())


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_generatorexit_from_poll_not_recoverable(monkeypatch):
    _base_exc_case(monkeypatch, GeneratorExit())


def test_operational_exception_still_classified_and_backs_off(monkeypatch):
    # the contrast case: an ordinary Exception IS classified + backed off
    _enable(monkeypatch)
    monkeypatch.setattr(settings, "run_worker_backoff_min_s", 5)
    fw = FakeWorker(behavior="raise")
    sup = RunWorkerSupervisor(worker_factory=lambda: fw)

    async def scenario():
        await sup.start()
        await asyncio.sleep(0.4)
        st = sup.status()
        assert st["consecutive_failure_count"] >= 1
        assert st["last_bounded_error_code"] == "sweep_error"
        assert sup.alive is True                    # still alive, in backoff
        await sup.stop()

    _run(scenario())
    assert _sup_threads() == []


def test_classify_only_accepts_operational_exceptions():
    # _classify never sees a non-Exception; a plain Exception maps to a slug
    from app.run_engine.worker_supervisor import _classify as clf
    assert clf(RuntimeError("x")) == ("sweep_error", False)
    assert clf(type("OperationalError", (Exception,), {})("no such table")) == \
        ("schema_error", True)


# ═══════════════ 3b. TRUTHFUL THREAD SHUTDOWN (close-out) ══════════════


def test_uncooperative_worker_thread_shutdown_is_reported_truthfully(monkeypatch):
    _enable(monkeypatch, public=True)           # readiness must reflect stuck-stopping
    gate = threading.Event()                    # NOT the stop signal — sweep ignores stop
    fw = FakeWorker(behavior="block_until", gate=gate)
    sup = RunWorkerSupervisor(worker_factory=lambda: fw)

    async def scenario():
        await sup.start()
        assert fw.in_poll.wait(timeout=2)       # deterministically inside poll_once
        await sup.stop(grace_s=0.3)             # bounded; grace WILL expire
        st = sup.status()
        # the truth: the thread is still running — do NOT claim it stopped
        assert sup.alive is True
        assert st["alive"] is True
        assert st["started"] is True
        assert st["stopping"] is True
        assert st["last_bounded_error_code"] == "shutdown_deadline_exceeded"
        assert sup.is_durable_path_ready() is False
        # release the uncooperative call — the thread then finishes on its own
        gate.set()
        for _ in range(200):
            if not sup.alive:
                break
            await asyncio.sleep(0.02)
        assert sup.alive is False

    _run(scenario())
    assert _sup_threads() == []


def test_cooperative_worker_thread_exits_and_leaves_nothing(monkeypatch):
    _enable(monkeypatch)
    fw = FakeWorker(behavior="watch_stop")
    sup = RunWorkerSupervisor(worker_factory=lambda: fw)

    async def scenario():
        await sup.start()
        assert fw.in_poll.wait(timeout=2)
        assert "run-worker" in _sup_threads()   # the owned thread is real + alive
        await sup.stop(grace_s=5)
        assert fw.saw_stop is True
        st = sup.status()
        assert sup.alive is False and st["alive"] is False
        assert st["started"] is False and st["stopping"] is False

    _run(scenario())
    assert _sup_threads() == []                 # nothing left behind


def test_status_alive_tracks_the_real_thread(monkeypatch):
    _enable(monkeypatch)
    fw = FakeWorker()
    sup = RunWorkerSupervisor(worker_factory=lambda: fw)

    async def scenario():
        assert sup.alive is False               # before start
        await sup.start()
        await asyncio.sleep(0.1)
        assert sup.alive is True and sup._thread.is_alive()
        await sup.stop()
        assert sup.alive is False and not sup._thread.is_alive()

    _run(scenario())


def test_web_concurrency_mismatch_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(settings, "run_worker_web_concurrency", 1)
    monkeypatch.setattr(settings, "database_url", "postgresql://x/y")
    monkeypatch.setenv("WEB_CONCURRENCY", "3")
    from app.run_engine.worker_supervisor import assert_embedded_topology_ok
    with caplog.at_level("WARNING"):
        assert_embedded_topology_ok()           # PG + >1 proc: allowed, but warned
    assert any("disagrees with a detected web-process count" in r.message
               for r in caplog.records)


# ---- shutdown semantics with a real engine ----


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


class FakeChat:
    def __init__(self, scripts, *, stall=0.0):
        self.scripts = [list(s) for s in scripts]
        self.stall = stall
        self.n = 0
        self.closed = 0

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        toks = self.scripts[self.n] if self.n < len(self.scripts) else []
        self.n += 1
        try:
            for t in toks:
                yield {"type": "token", "token": t}
                if self.stall:
                    await asyncio.sleep(self.stall)
            yield {"type": "done", "response": "".join(x for x in toks if isinstance(x, str))}
        finally:
            self.closed += 1


def _seed_chat(eng, *, msg="hi", uid=7, sid="fam-alpha", session_id=1, monkeypatch=None):
    db = mk_session(eng)
    try:
        ensure_user(db, uid)
        from app.models.models import ResearchSession
        if db.query(ResearchSession).filter(ResearchSession.id == session_id).first() is None:
            db.add(ResearchSession(id=session_id, user_id=uid, title="t"))
            db.commit()
        ctx = make_ctx(db, user_id=uid, sid=sid)
        submission = sub.build_chat_submission(
            session_id=session_id, message=msg, image_url=None, provider="p1",
            model="m1", mode="chat", system_prompt="SP", options={})
        run, created = sub.create_chat_run(db, ctx, submission=submission,
                                           idempotency_key=uuid.uuid4().hex)
        assert created
        return run.id
    finally:
        db.close()


def _status(eng, rid):
    db = mk_session(eng)
    try:
        return db.query(Run).filter(Run.id == rid).first().status
    finally:
        db.close()


def test_stalled_provider_receives_shutdown_and_no_false_success(eng, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    _enable(monkeypatch, chat=True)
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 3)      # idle tick ~1s
    monkeypatch.setattr(settings, "run_worker_handler_timeout_s", 300)

    rid = _seed_chat(eng)
    prov = FakeChat([["partial answer ", "still going "]], stall=3.0)  # stalls mid-stream
    sup = RunWorkerSupervisor(
        worker_factory=lambda: RunWorker(mk_factory(eng), worker_id="w-c3a",
                                         chat_provider=lambda ci, **_k: prov))

    async def scenario():
        await sup.start()
        await asyncio.sleep(1.2)               # sweep is now inside the stalled stream
        assert _status(eng, rid) == "running"
        t0 = time.monotonic()
        await sup.stop(grace_s=8)
        assert time.monotonic() - t0 < 8.0     # unwound within grace
        assert not sup._thread.is_alive()

    _run(scenario())
    assert _status(eng, rid) == "running"      # NOT succeeded / cancelled
    assert mk_session(eng).query(Message).filter(
        Message.role == MessageRole.assistant).count() == 0
    assert prov.closed >= 1                    # provider iterator was closed
    assert _sup_threads() == []                # no hb / bridge / supervisor thread left


def test_queued_and_waiting_runs_survive_shutdown(eng, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    _enable(monkeypatch, chat=True)
    rid = _seed_chat(eng)                      # a fresh queued chat run
    assert _status(eng, rid) == "queued"

    sup = RunWorkerSupervisor(
        worker_factory=lambda: RunWorker(mk_factory(eng), worker_id="w-q",
                                         chat_provider=lambda ci, **_k: FakeChat([["x"]])))

    async def scenario():
        await sup.start()
        await sup.stop()                       # stop immediately — guards block new work

    _run(scenario())
    # stop was set before / during the first sweep; the run must still be runnable
    assert _status(eng, rid) in ("queued", "running")
    # and a fresh worker (no stop) can still finish it
    RunWorker(mk_factory(eng), chat_provider=lambda ci, **_k: FakeChat([["done"]])
              ).advance_chat_runs()
    assert _status(eng, rid) == "succeeded"


# ═══════════════════ 4. HEALTH / READINESS ══════════════════════════


@pytest.fixture(autouse=True)
def _clear_app_supervisor():
    from app.main import app
    app.state.run_worker_supervisor = None
    yield
    app.state.run_worker_supervisor = None


class _FakeSup:
    def __init__(self, *, enabled, alive, ready, err=None):
        self._enabled, self._alive, self._ready, self._err = enabled, alive, ready, err

    async def start(self):
        return None

    async def stop(self, **_kw):
        return None

    def status(self):
        return {"enabled": self._enabled, "started": self._alive, "alive": self._alive,
                "stopping": False, "last_successful_poll_at": None,
                "last_bounded_error_code": self._err, "consecutive_failure_count": 0}

    def is_durable_path_ready(self):
        return self._ready


def test_health_worker_enabled_alive_is_healthy():
    # the injected _FakeSup fully drives /api/health/worker — no real worker task
    from app.main import app
    with TestClient(app) as c:
        app.state.run_worker_supervisor = _FakeSup(enabled=True, alive=True, ready=True)
        r = c.get("/api/health/worker")
    assert r.status_code == 200
    body = r.json()
    assert body["durable_path_ready"] is True
    assert body["durable_run"]["alive"] is True


def test_health_worker_public_api_dead_worker_is_unready():
    from app.main import app
    with TestClient(app) as c:
        app.state.run_worker_supervisor = _FakeSup(enabled=True, alive=False, ready=False,
                                                   err="schema_error")
        r = c.get("/api/health/worker")
    assert r.status_code == 503
    assert r.json()["durable_path_ready"] is False


def test_health_worker_status_has_no_secrets_or_identifiers():
    from app.main import app
    with TestClient(app) as c:
        app.state.run_worker_supervisor = _FakeSup(enabled=True, alive=True, ready=True)
        body = c.get("/api/health/worker").json()
    allowed = {"enabled", "started", "alive", "stopping", "last_successful_poll_at",
               "last_bounded_error_code", "consecutive_failure_count"}
    assert set(body["durable_run"]) == allowed
    blob = json.dumps(body).lower()
    for bad in ("run_id", "user_id", "session_id", "prompt", "api_key", "database_url",
                "sqlite", "postgres", "token", "mcp__"):
        assert bad not in blob


def test_legacy_health_unchanged_when_flags_off(monkeypatch):
    from app.main import app
    _enable(monkeypatch, engine=False, worker=False, public=False)
    with TestClient(app) as c:
        legacy = c.get("/api/health")
        worker = c.get("/api/health/worker")
    assert legacy.status_code == 200
    assert set(legacy.json()) == {"backend", "database", "database_dialect",
                                  "chromadb", "ollama"}
    assert worker.status_code == 200                     # public API off -> ready
    assert worker.json()["durable_run"]["enabled"] is False


def test_lifespan_does_not_start_worker_when_flags_off(monkeypatch):
    from app.main import app
    _enable(monkeypatch, engine=False, worker=False)
    with TestClient(app):
        sup = app.state.run_worker_supervisor
        assert sup is not None and sup.alive is False and sup._thread is None
    assert app.state.run_worker_supervisor is None       # cleared on shutdown
    assert _sup_threads() == []


# ═══════════════════ 5. MARKER ADVERSARIAL SPOT CHECK ═══════════════


def _feed_all(text, *, chunk=1):
    sc = cs.MarkerScanner(capture=True, max_capture_bytes=ct.marker_max_bytes())
    out = "".join(sc.feed(text[i:i + chunk]) for i in range(0, len(text), chunk))
    sc.finish()
    return sc, out


def test_marker_json_array_with_internal_bracket():
    args = {"items": ["a]b", "c]", "]"]}
    line = f'[MCP_CALL: mcp__srv__echo {json.dumps(args)}]\n'
    sc, out = _feed_all(line)
    assert sc.capture_status == "complete" and out == ""
    assert ct.parse_one_mcp_call(sc.captured) == ("mcp__srv__echo", args)


def test_marker_quoted_string_with_bracket_and_escapes():
    args = {"q": 'she said "]" \\ and [ ok', "p": "tail]"}
    line = f'note: [MCP_CALL: mcp__srv__echo {json.dumps(args)}]\nafter'
    sc, out = _feed_all(line)
    assert out == "note: "                                # clean prefix preserved
    name, parsed = ct.parse_one_mcp_call(sc.captured)
    assert name == "mcp__srv__echo" and parsed == args
    assert "]" not in out and "MCP_CALL" not in out


def test_marker_nested_objects_and_arrays():
    args = {"a": {"b": [1, {"c": "]"}], "d": [[2], {"e": "}"}]}}
    line = f'[MCP_CALL: mcp__srv__echo {json.dumps(args)}]\n'
    sc, _ = _feed_all(line)
    assert ct.parse_one_mcp_call(sc.captured) == ("mcp__srv__echo", args)


def test_marker_split_char_by_char_around_brackets():
    args = {"x": ["p]q"]}
    line = f'prefix [MCP_CALL: mcp__srv__echo {json.dumps(args)}] trailing]\n'
    sc, out = _feed_all(line, chunk=1)
    assert out == "prefix "
    assert ct.parse_one_mcp_call(sc.captured) == ("mcp__srv__echo", args)
    # nothing of the marker / args / trailing leaked into the emitted stream
    for bad in ("[MCP_CALL", "p]q", "trailing", "mcp__srv__echo"):
        assert bad not in out


def test_marker_trailing_text_after_close_is_discarded():
    line = '[MCP_CALL: mcp__srv__echo {}] then junk ] more ]\n'
    sc, out = _feed_all(line)
    assert out == ""
    assert ct.parse_one_mcp_call(sc.captured) == ("mcp__srv__echo", {})


def test_marker_multiline_json_fails_closed():
    # a marker whose JSON body wraps a newline: the canonical parser is
    # line-based, the capture is one line -> fail closed, no leak.
    sc = cs.MarkerScanner(capture=True, max_capture_bytes=ct.marker_max_bytes())
    out = "".join(sc.feed(c) for c in '[MCP_CALL: mcp__srv__echo {"a": 1,\n"b": 2}]\n')
    sc.finish()
    with pytest.raises(ct.ChatToolMarkerError):
        ct.parse_one_mcp_call(sc.captured or "")
    assert "b\": 2" not in out and "[MCP_CALL" not in out


def test_marker_stray_brace_in_trailing_text_parses_first_valid_call():
    # regex backtracking locks `\{.*\}` onto the FIRST `}]`-terminated marker;
    # the stray `}` in the trailing text is ignored, trailing text is discarded.
    line = '[MCP_CALL: mcp__srv__echo {"a":1}] oops }\n'
    sc, out = _feed_all(line)
    assert ct.parse_one_mcp_call(sc.captured) == ("mcp__srv__echo", {"a": 1})
    assert out == "" and "oops" not in out


def test_marker_truly_unparseable_body_fails_closed():
    # a body that never forms valid JSON on the line -> 0 calls -> fail closed
    line = '[MCP_CALL: mcp__srv__echo {"a": 1 "b" 2}]\n'
    sc, out = _feed_all(line)
    with pytest.raises(ct.ChatToolMarkerError):
        ct.parse_one_mcp_call(sc.captured or "")
    assert out == "" and "[MCP_CALL" not in out


def test_unsupported_multiline_marker_kind_fails_closed():
    sc = cs.MarkerScanner(capture=True, max_capture_bytes=ct.marker_max_bytes())
    out = "".join(sc.feed(c) for c in "ok [PYTHON_CODE: print(1)]\nmore")
    assert out == "ok " and sc.capture_status == "unsupported"
    assert "[PYTHON_CODE" not in out


def test_classify_maps_schema_error_fatal():
    exc = type("OperationalError", (Exception,), {})("no such column: runs.foo")
    assert _classify(exc) == ("schema_error", True)
    assert _classify(RuntimeError("random"))[1] is False
