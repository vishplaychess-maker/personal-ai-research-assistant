"""MP-5 reversible /messages cutover - authority rules.

Authority rule under test: while a canonical PendingApproval is unresolved in a
session NO new Run is started, and the approval stays decidable only through the
approvals API. Flag OFF is the legacy normal path. The canonical parked state
here is CONSTRUCTED (a real PendingApproval row + a real approve/deny decision is
covered by the MP-4C deny/approve suites); no claim is made that this file proves
a real row.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.database import SessionLocal
from app.models.models import Message, MessageRole, ResearchSession, Run
from tests.auth_helpers import auth_headers, register_and_login

UID = None  # resolved per test


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "mp5-cutover-hmac-key-0001")
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    monkeypatch.setattr(settings, "run_engine_enabled", False)
    monkeypatch.setattr(settings, "run_worker_enabled", False)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", False)
    monkeypatch.setattr(settings, "run_public_api_enabled", False)
    monkeypatch.setattr(settings, "run_deep_research_enabled", False)
    monkeypatch.setattr(settings, "run_multi_agent_enabled", False)
    monkeypatch.setattr(settings, "messages_nonstream_engine_enabled", False)
    from app.services.rate_limiter import reset_rate_limiter
    reset_rate_limiter()
    yield


def _login(c):
    uid, token = register_and_login(c, username="cutover_" + uuid.uuid4().hex[:8])
    h = auth_headers(token)
    csrf = c.cookies.get("research_assistant_csrf_token")
    if csrf:
        h["X-CSRF-Token"] = csrf
    return uid, h


def _session(c, h):
    return c.post("/api/sessions", json={"title": "cutover"}, headers=h).json()["id"]


def _bomb(monkeypatch):
    import app.run_engine.nonstream as ns

    calls = []

    def _fake(*a, **k):
        calls.append(k)
        raise AssertionError("canonical path must not be used here")

    monkeypatch.setattr(ns, "execute_chat_sync", _fake)
    return calls




def _park_run(db, *, uid, sid, approval_id, key="ns-park"):
    run_id = str(uuid.uuid4())
    db.add(Run(id=run_id, user_id=uid, session_id=sid, thread_id="t",
               kind="chat_stream", status="waiting_for_approval",
               idempotency_key=key, idempotency_identity_hash="0" * 64,
               pending_approval_id=approval_id, version=0, last_event_seq=0))
    return run_id


def _real_approval(db, *, uid, sid, run_id, approval_id):
    import datetime as _dt

    from app.models.models import PendingApproval

    db.add(PendingApproval(
        id=approval_id, action_hash="a" * 64, args_hash="b" * 64,
        tool_id="builtin:python_sandbox", server_fingerprint="c" * 64,
        tool_schema_hash="d" * 64, source_type="python",
        user_id=uid, session_id=sid, run_id=run_id,
        action_nonce=uuid.uuid4().hex, policy_version="v1",
        risk_level="high", state="requested",
        display_summary="{}", purpose_redacted="",
        expires_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)))


def _idempotent_terminal(db, run_id, **kw):
    row = db.query(Run).filter(Run.id == run_id).one()
    if row.status != "succeeded":          # one assistant Message per Run, ever
        row.status = "succeeded"
        db.add(Message(session_id=row.session_id, role=MessageRole.assistant,
                       content="answer"))
        db.commit()
    return row


def _max_message_id(db, sid):
    last = (db.query(Message).filter(Message.session_id == sid)
            .order_by(Message.id.desc()).first())
    return last.id if last is not None else 0


def test_same_idempotency_key_replays_the_same_run_without_duplicate_messages(monkeypatch):
    monkeypatch.setattr(settings, "messages_nonstream_engine_enabled", True)
    from fastapi.testclient import TestClient
    from app.main import app
    import app.run_engine.sync_wait as sw

    monkeypatch.setattr(sw, "wait_for_terminal", _idempotent_terminal)
    with TestClient(app) as c:
        uid, h = _login(c)
        sid = _session(c, h)
        key = "cutover-" + uuid.uuid4().hex
        hh = dict(h)
        hh["Idempotency-Key"] = key
        first = c.post("/api/sessions/%d/messages" % sid, headers=hh,
                       json={"message": "hello"})
        second = c.post("/api/sessions/%d/messages" % sid, headers=hh,
                        json={"message": "hello"})
        assert first.status_code == 200, first.text[:300]
        assert second.status_code == 200, second.text[:300]
        assert (first.json()["assistant_message"]["id"]
                == second.json()["assistant_message"]["id"])
        db = SessionLocal()
        try:
            runs = db.query(Run).filter(Run.session_id == sid,
                                       Run.user_id == uid).all()
            assert len(runs) == 1 and runs[0].idempotency_key == key
            assert db.query(Message).filter(
                Message.session_id == sid, Message.role == MessageRole.assistant,
            ).count() == 1
        finally:
            db.close()


def test_wait_timeout_never_returns_an_answer(monkeypatch):
    monkeypatch.setattr(settings, "messages_nonstream_engine_enabled", True)
    from fastapi.testclient import TestClient
    from app.main import app
    import app.run_engine.sync_wait as sw

    def _still_running(db, run_id, **kw):
        raise sw.SyncWaitError(sw.SYNC_WAIT_TIMEOUT, run_id, "running")

    monkeypatch.setattr(sw, "wait_for_terminal", _still_running)
    with TestClient(app) as c:
        uid, h = _login(c)
        sid = _session(c, h)
        db = SessionLocal()
        try:
            before = _max_message_id(db, sid)
        finally:
            db.close()
        r = c.post("/api/sessions/%d/messages" % sid, headers=h,
                   json={"message": "hello"})
        assert r.status_code == 504, "status=%d body=%s" % (r.status_code, r.text[:300])
        assert "still in progress" in r.text
        db = SessionLocal()
        try:
            assert db.query(Run).filter(Run.session_id == sid,
                                        Run.user_id == uid).count() == 1
            assert db.query(Message).filter(Message.session_id == sid,
                                            Message.id > before).count() == 1
        finally:
            db.close()


def test_another_users_session_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "messages_nonstream_engine_enabled", True)
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        uid_a, h_a = _login(c)
        sid = _session(c, h_a)
        uid_b, h_b = _login(c)
        r = c.post("/api/sessions/%d/messages" % sid, headers=h_b,
                   json={"message": "hi"})
        assert r.status_code == 404, "status=%d" % r.status_code
        db = SessionLocal()
        try:
            assert db.query(Run).filter(Run.session_id == sid,
                                        Run.user_id == uid_b).count() == 0
        finally:
            db.close()


def test_flag_on_to_off_keeps_the_canonical_approval_authoritative(monkeypatch):
    monkeypatch.setattr(settings, "messages_nonstream_engine_enabled", True)
    from fastapi.testclient import TestClient
    from app.main import app
    import app.routes.messages as msg

    legacy = []
    monkeypatch.setattr(msg, "run_research_workflow",
                        lambda **kw: legacy.append(kw) or {"assistant_message_id": None})
    with TestClient(app) as c:
        uid, h = _login(c)
        sid = _session(c, h)
        db = SessionLocal()
        try:
            _park_run(db, uid=uid, sid=sid, approval_id="PA-FLIP-1")
            db.commit()
        finally:
            db.close()
        on = c.post("/api/sessions/%d/messages" % sid, headers=h,
                    json={"message": "hello"})
        assert on.status_code == 200, on.text[:300]
        assert on.json()["approval_id"] == "PA-FLIP-1"
        monkeypatch.setattr(settings, "messages_nonstream_engine_enabled", False)
        off = c.post("/api/sessions/%d/messages" % sid, headers=h,
                     json={"message": "hello"})
        assert off.status_code == 200, off.text[:300]
        assert off.json().get("approval_id") == "PA-FLIP-1"
        assert legacy == []            # the legacy path was never entered
        db = SessionLocal()
        try:
            assert db.query(Run).filter(Run.session_id == sid,
                                        Run.user_id == uid).count() == 1
        finally:
            db.close()


def test_real_pending_approval_is_decided_through_the_approvals_api(monkeypatch):
    monkeypatch.setattr(settings, "messages_nonstream_engine_enabled", True)
    from fastapi.testclient import TestClient
    from app.main import app
    from app.models.models import PendingApproval

    approval_id = str(uuid.uuid4())
    with TestClient(app) as c:
        uid, h = _login(c)
        sid = _session(c, h)
        db = SessionLocal()
        try:
            run_id = _park_run(db, uid=uid, sid=sid, approval_id=approval_id)
            _real_approval(db, uid=uid, sid=sid, run_id=run_id,
                           approval_id=approval_id)
            db.commit()
        finally:
            db.close()
        parked = c.post("/api/sessions/%d/messages" % sid, headers=h,
                        json={"message": "run it"})
        assert parked.status_code == 200, parked.text[:300]
        assert parked.json()["approval_id"] == approval_id
        denied = c.post("/api/approvals/%s/deny" % approval_id, headers=h)
        assert denied.status_code == 200, denied.text[:300]
        db = SessionLocal()
        try:
            row = db.query(PendingApproval).filter(
                PendingApproval.id == approval_id).one()
            assert row.state == "denied"
        finally:
            db.close()


def test_flag_off_uses_the_legacy_branch_and_no_canonical_call(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    import app.routes.messages as msg

    calls = _bomb(monkeypatch)
    legacy = []
    monkeypatch.setattr(msg, "run_research_workflow",
                        lambda **kw: legacy.append(kw) or {"assistant_message_id": None})
    with TestClient(app) as c:
        uid, h = _login(c)
        sid = _session(c, h)
        db = SessionLocal()
        try:
            m = Message(session_id=sid, role=MessageRole.assistant, content="legacy answer")
            db.add(m)
            db.commit()
            db.refresh(m)
            aid = m.id
        finally:
            db.close()
        monkeypatch.setattr(msg, "run_research_workflow",
                            lambda **kw: legacy.append(kw) or {"assistant_message_id": aid})
        r = c.post("/api/sessions/%d/messages" % sid, headers=h, json={"message": "hello"})
        assert r.status_code == 200, r.text[:300]
        assert len(legacy) == 1 and "resume_from" not in legacy[0]
        assert calls == []
        db = SessionLocal()
        try:
            # test.db is SHARED: scope by THIS user, not by session id alone
            assert db.query(Run).filter(Run.session_id == sid,
                                        Run.user_id == uid).count() == 0
        finally:
            db.close()


def test_flag_on_success_returns_the_chat_response_and_one_run(monkeypatch):
    monkeypatch.setattr(settings, "messages_nonstream_engine_enabled", True)
    from fastapi.testclient import TestClient
    from app.main import app
    import app.run_engine.sync_wait as sw

    def _terminal(db, run_id, **kw):
        row = db.query(Run).filter(Run.id == run_id).one()
        row.status = "succeeded"
        db.add(Message(session_id=row.session_id, role=MessageRole.assistant, content="canonical answer"))
        db.commit()
        return row

    monkeypatch.setattr(sw, "wait_for_terminal", _terminal)
    with TestClient(app) as c:
        uid, h = _login(c)
        sid = _session(c, h)
        db = SessionLocal()
        try:
            last = (db.query(Message).filter(Message.session_id == sid)
                    .order_by(Message.id.desc()).first())
            max_before = last.id if last is not None else 0
        finally:
            db.close()
        r = c.post("/api/sessions/%d/messages" % sid, headers=h, json={"message": "hello"})
        assert r.status_code == 200, "status=%d body=%s" % (r.status_code, r.text[:300])
        body = r.json()
        assert body["assistant_message"]["content"] == "canonical answer"
        assert body["user_message"]["content"] == "hello"
        db = SessionLocal()
        try:
            # test.db is SHARED: scope every count by THIS user, never by session id
            runs = (db.query(Run)
                    .filter(Run.session_id == sid, Run.user_id == uid).all())
            assert len(runs) == 1 and runs[0].status == "succeeded"
            # proves the CANONICAL branch ran (legacy creates no Run at all)
            assert runs[0].idempotency_key.startswith("ns:")
            assert db.query(Message).filter(Message.session_id == sid,
                                            Message.id > max_before).count() == 2
        finally:
            db.close()


def test_flag_on_canonical_parked_blocks_new_runs_and_reports_the_approval(monkeypatch):
    monkeypatch.setattr(settings, "messages_nonstream_engine_enabled", True)
    from fastapi.testclient import TestClient
    from app.main import app

    calls = _bomb(monkeypatch)
    with TestClient(app) as c:
        uid, h = _login(c)
        sid = _session(c, h)
        db = SessionLocal()
        try:
            db.add(Run(id=str(uuid.uuid4()), user_id=uid, session_id=sid,
                       thread_id="t", kind="chat_stream",
                       status="waiting_for_approval",
                       idempotency_key="ns-park",
                       idempotency_identity_hash="0" * 64,
                       pending_approval_id="PA-CANONICAL-1",
                       version=0, last_event_seq=0))
            db.commit()
        finally:
            db.close()
        r = c.post("/api/sessions/%d/messages" % sid, headers=h,
                   json={"message": "yes please run it"})
        assert r.status_code == 200, "status=%d body=%s" % (r.status_code, r.text[:300])
        body = r.json()
        assert body["approval_id"] == "PA-CANONICAL-1"
        assert "approvals panel" in body["approval_message"]
        assert calls == []                      # no canonical submission attempted
        db = SessionLocal()
        try:
            runs = (db.query(Run)
                    .filter(Run.session_id == sid, Run.user_id == uid).all())
            assert len(runs) == 1               # no second Run, no second authority
        finally:
            db.close()

