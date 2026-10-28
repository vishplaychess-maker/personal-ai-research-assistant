"""Phase 1B.2R MP-4B — executable sentinel: the durable /messages/stream
route never invokes a legacy streaming helper.

Route-level EVIDENCE, not a static guard. Real FastAPI app, real TestClient,
real auth (register/login), real durable submission, real SQLite persistence.
Every legacy helper is replaced AT ITS ACTUAL ROUTE LOOKUP SITE
(app.routes.messages, where the names are bound at import time) by a loud,
counting sentinel; a durable submission must produce exactly ZERO legacy
invocations.

Determinism notes (stated honestly, because they bound what this proves):

* At lifespan time the durable flags are OFF, so no real RunWorker thread is
  started and no live provider is ever contacted by this module.
* At request time the flags are ON and app.state.run_worker_supervisor is a
  stub whose only behaviour is reporting the durable path ready/not-ready.
  That is a deployment-LIVENESS signal, not the code under test: the route
  decision, mode validation, canonical submission, idempotency and every DB
  write below are the real implementations.
* run_sse_max_seconds is 1 so the SSE stream self-terminates; because no
  worker runs, the run legitimately stays pending and is never executed.
"""

from __future__ import annotations

import uuid
from collections import Counter

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.run_engine import messages_stream as ms
from tests.auth_helpers import auth_headers, register_and_login

_HMAC = "mp4b-sentinel-hmac-key-0123456789abcdef"

#: name -> number of times a legacy helper was invoked on the durable route.
LEGACY: Counter = Counter()


class LegacyInvoked(AssertionError):
    """Raised by a sentinel when the durable route touches a legacy helper."""


def _sentinel(name):
    def _boom(*a, **k):
        LEGACY[name] += 1
        raise LegacyInvoked("legacy helper invoked on the durable route: " + name)
    return _boom


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    # Lifespan-safe defaults: nothing durable starts while the app boots.
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    monkeypatch.setattr(settings, "run_engine_enabled", False)
    monkeypatch.setattr(settings, "run_worker_enabled", False)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", False)
    monkeypatch.setattr(settings, "run_public_api_enabled", False)
    monkeypatch.setattr(settings, "run_deep_research_enabled", False)
    monkeypatch.setattr(settings, "run_multi_agent_enabled", False)
    monkeypatch.setattr(settings, "run_sse_poll_interval_s", 1)
    monkeypatch.setattr(settings, "run_sse_heartbeat_s", 0)
    monkeypatch.setattr(settings, "run_sse_max_seconds", 1)
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 3)
    monkeypatch.setattr(settings, "run_messages_stream_max_concurrent_per_user", 4)
    LEGACY.clear()
    ms._active_slots.clear()
    from app.services.rate_limiter import reset_rate_limiter
    reset_rate_limiter()
    yield
    LEGACY.clear()
    ms._active_slots.clear()


@pytest.fixture(autouse=True)
def _restore_supervisor():
    """Never leak a stub supervisor into another test module in this process."""
    from app.main import app

    orig = getattr(app.state, "run_worker_supervisor", None)
    yield
    app.state.run_worker_supervisor = orig


class _StubSup:
    """Deployment-liveness stub. start/stop are inert so the app lifespan's own
    lifecycle calls stay harmless; this class is NOT the code under test."""

    def __init__(self, ready):
        self._ready = ready

    def is_durable_path_ready(self):
        return self._ready

    async def start(self):
        return None

    async def stop(self):
        return None


class _ReadySup(_StubSup):
    def __init__(self):
        super().__init__(True)


class _DeadSup(_StubSup):
    def __init__(self):
        super().__init__(False)


def _arm_legacy_sentinels(monkeypatch):
    """Replace every legacy helper at its real lookup site with a loud sentinel."""
    import app.routes.messages as mod
    from app.services import streaming_service as ss
    from app.services import langgraph_workflow as lw
    from app.services import scheduler_service as sch

    for name in ("stream_chat_response", "run_research_workflow",
                 "prepare_chat_context", "format_sse",
                 "save_assistant_message", "run_memory_extraction"):
        assert hasattr(mod, name), "routes.messages no longer defines " + name
        monkeypatch.setattr(mod, name, _sentinel("routes.messages." + name))
    monkeypatch.setattr(
        ss, "stream_chat_response",
        _sentinel("streaming_service.stream_chat_response"))
    monkeypatch.setattr(
        ss, "stream_multi_agent_response",
        _sentinel("streaming_service.stream_multi_agent_response"))
    monkeypatch.setattr(
        lw, "run_research_workflow",
        _sentinel("langgraph_workflow.run_research_workflow"))
    monkeypatch.setattr(
        sch, "run_research_workflow",
        _sentinel("scheduler_service.run_research_workflow"))


def _enable_durable(monkeypatch, *, deep_research=True, multi_agent=True):
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_worker_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_deep_research_enabled", deep_research)
    monkeypatch.setattr(settings, "run_multi_agent_enabled", multi_agent)


def _bootstrap(c, app, monkeypatch, *, ready=True, deep_research=True,
               multi_agent=True, arm=True):
    """Register a real user, create a real session, then enable the durable path."""
    if arm:
        _arm_legacy_sentinels(monkeypatch)
    uid, token = register_and_login(c, username="mp4b_" + uuid.uuid4().hex[:10])
    h = auth_headers(token)
    csrf = c.cookies.get("research_assistant_csrf_token")
    if csrf:
        h["X-CSRF-Token"] = csrf
    s = c.post("/api/sessions", json={"title": "mp4b"}, headers=h)
    assert s.status_code in (200, 201), s.text[:300]
    app.state.run_worker_supervisor = _ReadySup() if ready else _DeadSup()
    _enable_durable(monkeypatch, deep_research=deep_research,
                    multi_agent=multi_agent)
    return s.json()["id"], h


def _stream(c, sid, h, *, body, key):
    return c.post("/api/sessions/" + str(sid) + "/messages/stream",
                  json=body, headers={**h, "Idempotency-Key": key})


def _db():
    from app.database import SessionLocal
    return SessionLocal()


def _owner_of(sid):
    """The CURRENT owning user of a session id, or None when the row is gone.

    Test-data isolation. `test.db` is a persistent, shared file, and session
    ids are reused across runs (a later run can create a session with an id an
    earlier run already used). Counting Run rows by session id ALONE therefore
    counts the history of a different user: observed as
    `assert len(_runs(sid)) == 1` -> 2 with the extra row owned by user_id=482.
    Scoping by the owner the session row names RIGHT NOW matches what these
    tests actually mean: the runs belonging to THIS submission.
    """
    from app.models.models import ResearchSession
    db = _db()
    try:
        sess = db.query(ResearchSession).filter(ResearchSession.id == sid).first()
        return None if sess is None else sess.user_id
    finally:
        db.close()


def _runs(sid):
    from app.models.models import Run
    uid = _owner_of(sid)
    db = _db()
    try:
        q = db.query(Run).filter(Run.session_id == sid)
        if uid is not None:
            q = q.filter(Run.user_id == uid)
        return q.all()
    finally:
        db.close()


def _messages(sid):
    from app.models.models import Message
    db = _db()
    try:
        return db.query(Message).filter(Message.session_id == sid).all()
    finally:
        db.close()


def _steps(run_id):
    from app.models.models import RunStep
    db = _db()
    try:
        return db.query(RunStep).filter(RunStep.run_id == run_id).all()
    finally:
        db.close()


def _payloads(run_id):
    from app.models.models import RunPayload
    db = _db()
    try:
        return db.query(RunPayload).filter(RunPayload.run_id == run_id).all()
    finally:
        db.close()


def _no_legacy():
    assert LEGACY == Counter(), "durable route invoked legacy helpers: " + repr(dict(LEGACY))


CASES = [
    ({}, "chat_stream"),
    ({"mode": "chat"}, "chat_stream"),
    ({"mode": "deep_research"}, "deep_research"),
    ({"mode": "multi_agent"}, "multi_agent"),
]


@pytest.mark.parametrize("body,expected_kind", CASES,
                         ids=["omitted", "explicit-chat", "deep-research", "multi-agent"])
def test_durable_route_never_calls_legacy_helpers(monkeypatch, body, expected_kind):
    """One durable submission per mode -> zero legacy helper invocations."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        sid, h = _bootstrap(c, app, monkeypatch)
        r = _stream(c, sid, h, body={**body, "message": "hello mp4b"},
                    key="mp4b-" + uuid.uuid4().hex)
        assert r.status_code == 200, r.text[:400]
        assert "event: start" in r.text
        run_id = r.headers.get("X-Run-Id")
        assert run_id, "durable route must return X-Run-Id"

    _no_legacy()
    runs = _runs(sid)
    assert len(runs) == 1
    assert runs[0].kind == expected_kind
    assert runs[0].id == run_id


@pytest.mark.parametrize("body,expected_kind", CASES,
                         ids=["omitted", "explicit-chat", "deep-research", "multi-agent"])
def test_canonical_submission_exactly_once_and_rows(monkeypatch, body, expected_kind):
    """resolve_and_submit runs exactly once and stages the expected rows once."""
    from fastapi.testclient import TestClient
    from app.main import app

    calls = []
    real = ms.resolve_and_submit

    def spy(db, ctx, **kw):
        calls.append(kw)
        return real(db, ctx, **kw)

    monkeypatch.setattr(ms, "resolve_and_submit", spy)

    with TestClient(app) as c:
        sid, h = _bootstrap(c, app, monkeypatch)
        r = _stream(c, sid, h, body={**body, "message": "once only"},
                    key="mp4b-" + uuid.uuid4().hex)
        assert r.status_code == 200, r.text[:400]
        run_id = r.headers["X-Run-Id"]

    _no_legacy()
    assert len(calls) == 1, "canonical submission must run exactly once"
    assert calls[0]["mode"] == body.get("mode")
    assert calls[0]["session_id"] == sid
    assert calls[0]["message"] == "once only"

    runs = _runs(sid)
    assert len(runs) == 1 and runs[0].kind == expected_kind
    users = [m for m in _messages(sid) if str(m.role).endswith("user")]
    assert len(users) == 1 and users[0].content == "once only"
    assert len(_payloads(run_id)) == 1
    steps = _steps(run_id)
    assert len(steps) == 1 and steps[0].kind == "load_context"
    if expected_kind == "chat_stream":
        assert steps[0].status == "pending"
    else:
        assert steps[0].status == "skipped"


def test_unsupported_mode_fails_validation_before_persistence(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        sid, h = _bootstrap(c, app, monkeypatch)
        r = _stream(c, sid, h, body={"message": "nope", "mode": "bogus_mode"},
                    key="mp4b-" + uuid.uuid4().hex)
        assert r.status_code == 422, r.text[:300]
        assert "X-Run-Id" not in r.headers

    _no_legacy()
    assert _runs(sid) == []
    assert _messages(sid) == []


def test_deep_research_not_ready_fails_before_persistence(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        sid, h = _bootstrap(c, app, monkeypatch, deep_research=False)
        r = _stream(c, sid, h, body={"message": "dr", "mode": "deep_research"},
                    key="mp4b-" + uuid.uuid4().hex)
        assert r.status_code == 422, r.text[:300]
        assert "X-Run-Id" not in r.headers

    _no_legacy()
    assert _runs(sid) == []
    assert _messages(sid) == []


def test_multi_agent_not_ready_fails_before_persistence(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        sid, h = _bootstrap(c, app, monkeypatch, multi_agent=False)
        r = _stream(c, sid, h, body={"message": "ma", "mode": "multi_agent"},
                    key="mp4b-" + uuid.uuid4().hex)
        assert r.status_code == 422, r.text[:300]
        assert "X-Run-Id" not in r.headers

    _no_legacy()
    assert _runs(sid) == []
    assert _messages(sid) == []


def test_unready_supervisor_503_before_persistence(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        sid, h = _bootstrap(c, app, monkeypatch, ready=False)
        r = _stream(c, sid, h, body={"message": "blocked"},
                    key="mp4b-" + uuid.uuid4().hex)
        assert r.status_code == 503, r.text[:300]
        assert "X-Run-Id" not in r.headers

    _no_legacy()
    assert _runs(sid) == []
    assert _messages(sid) == []


def test_identical_replay_reuses_run_without_duplication_or_legacy(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    key = "mp4b-replay-" + uuid.uuid4().hex
    with TestClient(app) as c:
        sid, h = _bootstrap(c, app, monkeypatch)
        r1 = _stream(c, sid, h, body={"message": "same"}, key=key)
        r2 = _stream(c, sid, h, body={"message": "same"}, key=key)
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.headers["X-Run-Id"] == r2.headers["X-Run-Id"]

    _no_legacy()
    assert len(_runs(sid)) == 1
    users = [m for m in _messages(sid) if str(m.role).endswith("user")]
    assert len(users) == 1
    assert len(_payloads(r1.headers["X-Run-Id"])) == 1


def test_changed_semantics_replay_conflicts_without_legacy(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    key = "mp4b-conflict-" + uuid.uuid4().hex
    with TestClient(app) as c:
        sid, h = _bootstrap(c, app, monkeypatch)
        r1 = _stream(c, sid, h, body={"message": "first"}, key=key)
        assert r1.status_code == 200, r1.text[:300]
        r2 = _stream(c, sid, h, body={"message": "different"}, key=key)
        assert r2.status_code == 409, r2.text[:300]

    _no_legacy()
    assert len(_runs(sid)) == 1
    users = [m for m in _messages(sid) if str(m.role).endswith("user")]
    assert len(users) == 1 and users[0].content == "first"


def test_legacy_branch_is_reachable_when_flag_off(monkeypatch):
    """Negative control: the sentinels are really armed and really loud.

    With the cutover flag off the legacy path IS taken, so the counter must
    trip. Without this, a silently-broken sentinel could make the suite above
    pass for the wrong reason.
    """
    from fastapi.testclient import TestClient
    from app.main import app

    # raise_server_exceptions=False so the sentinel's LegacyInvoked surfaces as a
    # 500 response instead of propagating out of the client call.
    with TestClient(app, raise_server_exceptions=False) as c:
        sid, h = _bootstrap(c, app, monkeypatch)
        monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
        r = _stream(c, sid, h, body={"message": "legacy"}, key="mp4b-" + uuid.uuid4().hex)
        assert r.status_code == 500, r.text[:300]

    assert LEGACY, "sentinels did not trip on the legacy branch — they are not armed"
    assert "routes.messages.prepare_chat_context" in LEGACY
