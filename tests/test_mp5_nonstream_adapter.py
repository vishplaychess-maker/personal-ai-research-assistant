"""MP-5 prerequisite: the non-streaming adapter (canonical engine, no second
approval authority).

The adapter is reachable ONLY through the flag-gated route wiring: the route
calls it only while `messages_nonstream_engine_enabled` is on, the flag-OFF
path stays legacy, and an unresolved canonical approval keeps the ONE canonical
authority regardless of the flag. The adapter itself never touches the legacy
approval encoding and never decides or persists an approval."""

from __future__ import annotations

import datetime as dt
import inspect
import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import (
    Message, MessageRole, PendingApproval, RefreshSession, ResearchSession, Run,
    User,
)
from app.run_engine import nonstream as ns
from app.run_engine.context import AuthContext
from tests.run_engine_helpers import ensure_user, mk_engine, mk_session

FAMILY = "9c1d2e3f-aaaa-4bbb-8ccc-ddddeeeeffff"
UID = 7
SESSION_ID = 1


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "mp5-nonstream-hmac-key-0001")
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    yield


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


def _seed(eng, *, session_owner=UID, family=FAMILY, family_revoked=False):
    db = mk_session(eng)
    try:
        ensure_user(db, UID)
        if session_owner != UID:
            ensure_user(db, session_owner)
        if family:
            db.add(RefreshSession(
                user_id=UID, family_id=family, token_hash=uuid.uuid4().hex,
                expires_at=dt.datetime.utcnow() + dt.timedelta(days=1),
                revoked_at=(dt.datetime.utcnow() if family_revoked else None)))
        db.add(ResearchSession(id=SESSION_ID, user_id=session_owner, title="t",
                               model="model-x", system_prompt="SP"))
        db.commit()
    finally:
        db.close()


def _ctx(eng, *, family=FAMILY, uid=UID):
    db = mk_session(eng)
    try:
        user = db.query(User).filter(User.id == uid).first()
        return AuthContext(user=user, user_id=uid, tenant_id=None,
                           auth_session_ref=family,
                           durable_session_binding_valid=False)
    finally:
        db.close()


def _session(eng):
    db = mk_session(eng)
    try:
        return db.query(ResearchSession).filter(
            ResearchSession.id == SESSION_ID).one()
    finally:
        db.close()


def _patch_wait(monkeypatch, *, terminal=None, raise_status=None,
                write_assistant=False):
    import app.run_engine.sync_wait as sw

    if raise_status is not None:
        def _raise(db, run_id, **kw):
            row = db.query(Run).filter(Run.id == run_id).one()
            row.status = raise_status
            if raise_status == "waiting_for_approval":
                row.pending_approval_id = "PA-CANONICAL-1"
            db.commit()
            raise sw.SyncWaitError(sw.SYNC_WAIT_TIMEOUT, run_id, raise_status)

        monkeypatch.setattr(sw, "wait_for_terminal", _raise)
        return

    def _terminal(db, run_id, **kw):
        row = db.query(Run).filter(Run.id == run_id).one()
        row.status = terminal
        if write_assistant:
            db.add(Message(session_id=SESSION_ID, role=MessageRole.assistant,
                           content="answer"))
        db.commit()
        return row

    monkeypatch.setattr(sw, "wait_for_terminal", _terminal)


def _run(eng, monkeypatch, *, key, message="hi", deadline=1.0):
    db = mk_session(eng)
    try:
        return ns.execute_chat_sync(db, _ctx(eng), session=_session(eng),
                                    message=message, idempotency_key=key,
                                    wait_deadline_s=deadline)
    finally:
        db.close()


def test_success_returns_succeeded_with_the_run_and_message_ids(eng, monkeypatch):
    _seed(eng)
    _patch_wait(monkeypatch, terminal="succeeded", write_assistant=True)

    out = _run(eng, monkeypatch, key="k-success")

    assert out.status == ns.SUCCEEDED
    assert out.run_id and out.created is True and out.approval_id is None
    assert out.assistant_message_id is not None
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 1
        assert db.query(PendingApproval).count() == 0
    finally:
        db.close()


def test_parked_run_reports_the_canonical_approval_id_and_decides_nothing(eng, monkeypatch):
    _seed(eng)
    _patch_wait(monkeypatch, raise_status="waiting_for_approval")

    out = _run(eng, monkeypatch, key="k-parked")

    assert out.status == ns.PENDING_APPROVAL
    assert out.approval_id == "PA-CANONICAL-1"
    assert out.run_status == "waiting_for_approval"
    assert out.assistant_message_id is None
    db = mk_session(eng)
    try:
        assert db.query(PendingApproval).count() == 0   # created/decided elsewhere
    finally:
        db.close()


def test_wait_timeout_is_never_an_answer(eng, monkeypatch):
    _seed(eng)
    _patch_wait(monkeypatch, raise_status="running")

    out = _run(eng, monkeypatch, key="k-timeout", deadline=0.01)

    assert out.status == ns.PENDING
    assert out.run_status == "running"
    assert out.assistant_message_id is None


def test_same_idempotency_key_replays_the_same_run(eng, monkeypatch):
    _seed(eng)
    _patch_wait(monkeypatch, terminal="succeeded")

    first = _run(eng, monkeypatch, key="k-same")
    second = _run(eng, monkeypatch, key="k-same")

    assert first.run_id == second.run_id
    assert first.created is True and second.created is False
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 1
    finally:
        db.close()


def test_foreign_session_fails_closed_and_creates_no_run(eng, monkeypatch):
    _seed(eng, session_owner=UID + 1)
    _patch_wait(monkeypatch, terminal="succeeded")

    out = _run(eng, monkeypatch, key="k-foreign")

    assert out.status == ns.FAILED and out.reason == ns.REASON_SESSION_MISSING
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 0
    finally:
        db.close()


def test_nonstream_module_contains_no_legacy_approval_encoding():
    """The adapter must never grow a second approval encoding or authority."""
    src = inspect.getsource(ns)
    for banned in ("run_research_workflow", "resume_from", "_check_pending_approval",
                   "_is_approval_response"):
        assert banned not in src, banned
    # the legacy encoding is a dict stashed in an assistant Message's citations
    assert ".citations =" not in src and "assistant_msg" not in src
    # it decides/persists nothing itself: the canonical authority does that
    for banned in ("record_decision", "mirror_decision_signal", "consume_approval",
                   "PendingApproval("):
        assert banned not in src, banned


def test_route_uses_the_adapter_only_under_the_flag_gate():
    """Exactly one call site, inside the flag branch; the adapter is invoked only
    from the gate helper (no second, ungated entry point)."""
    from app.routes import messages as messages_route

    src = inspect.getsource(messages_route)
    endpoint = inspect.getsource(messages_route.create_message)

    assert "from app.run_engine import nonstream as _ns" in src
    assert endpoint.count("_engine_cutover_gate(") == 1
    assert "if _settings.messages_nonstream_engine_enabled:" in endpoint
    flag_at = endpoint.index("messages_nonstream_engine_enabled")
    call_at = endpoint.index("_engine_cutover_gate(")
    assert flag_at < call_at, "gate helper must be called INSIDE the flag branch"

    gate = inspect.getsource(messages_route._engine_cutover_gate)
    assert gate.count("_ns.execute_chat_sync(") == 1
    assert src.count("_ns.execute_chat_sync(") == 1


def test_flag_off_retains_the_legacy_path():
    """The declared default is OFF, and the legacy marker decision/flow is still
    present and reachable after the (skipped) flag branch."""
    from app.config import Settings

    assert Settings.model_fields["messages_nonstream_engine_enabled"].default is False

    from app.routes import messages as messages_route

    endpoint = inspect.getsource(messages_route.create_message)
    assert "_check_pending_approval(db, session.id)" in endpoint
    assert "run_research_workflow(" in endpoint
    assert "resume_from=" in endpoint


def test_unresolved_canonical_approval_keeps_one_authority():
    """The canonical parked-run guard is FLAG-INDEPENDENT: it precedes the flag
    branch and the legacy pending-approval check, returns the ONE canonical
    approval id, and the route decides no approval itself."""
    from app.routes import messages as messages_route

    endpoint = inspect.getsource(messages_route.create_message)
    canon_at = endpoint.index("_active_canonical_run(")
    flag_at = endpoint.index("messages_nonstream_engine_enabled")
    legacy_at = endpoint.index("_check_pending_approval(db, session.id)")
    assert canon_at < flag_at < legacy_at, "canonical guard must be hoisted, flag-independent"

    guard = inspect.getsource(messages_route._active_canonical_run)
    assert "WAITING_FOR_APPROVAL" in guard
    parked = inspect.getsource(messages_route._parked_response)
    assert "TerminalApprovalResponse(" in parked

    src = inspect.getsource(messages_route)
    for banned in ("record_decision", "mirror_decision_signal", "consume_approval"):
        assert banned not in src, banned
    import app.routes.messages as _m

    assert not any(
        getattr(r, "path", "").startswith("/api/approvals")
        for r in _m.router.routes
    ), "approval decisions have exactly one route authority"
