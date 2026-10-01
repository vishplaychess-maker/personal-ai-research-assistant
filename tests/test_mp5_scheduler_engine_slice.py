"""MP-5 (first slice) - the flag-gated scheduler-on-RunEngine execution path.

What this suite pins down:

* flag OFF = byte-for-byte the legacy workflow path, and NOTHING durable is
  created (no Run, no Message, no approval);
* flag ON = exactly ONE canonical durable Run, bound to the family captured on
  the task row (never invented), owned by the task owner, with a deterministic
  per-fire idempotency key so a retry cannot create a second Run;
* a run parked for a real user decision is reported pending_approval and NO
  approval is created or decided by this path (approval-by-default preserved);
* a run still running at the deadline is reported pending, never success;
* a task without a captured binding, whose session belongs to another user, or
  whose login family was revoked or expired, fails closed with a bounded reason
  and creates no Run - which also documents the behavioural truth of this
  design: a scheduled job only runs while the owner still has a LIVE login
  family, exactly like an HTTP submission.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import (
    Message, PendingApproval, RefreshSession, ResearchSession, Run,
    ScheduledTask,
)
from app.services import scheduler_engine as se
from app.services import scheduler_service as ss
from tests.run_engine_helpers import ensure_user, mk_engine, mk_session

FAMILY = "3f2b1c8e-1111-4222-8333-444455556666"
UID = 7


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "mp5-scheduler-engine-hmac-0001")
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", False)
    monkeypatch.setattr(settings, "run_scheduler_engine_wait_deadline_s", 0.5)
    yield


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


def _bind_sessions(monkeypatch, eng):
    """Point BOTH the service layer and the legacy body at the test engine."""
    from app import database as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", lambda: mk_session(eng))
    monkeypatch.setattr(ss, "SessionLocal", lambda: mk_session(eng))


def _seed(eng, *, family=FAMILY, session_owner=UID, task_owner=UID,
          next_run_at=None, family_revoked=False, family_expired=False):
    """Seed the owner, the target session, the task, and - crucially - the LIVE
    refresh-session family that validate_session_binding demands. A fabricated
    family with no active refresh_sessions row is rejected (that is the point)."""
    db = mk_session(eng)
    try:
        ensure_user(db, UID)
        if task_owner != UID or session_owner != UID:
            ensure_user(db, task_owner)
            ensure_user(db, session_owner)
        if family:
            delta = dt.timedelta(days=-1 if family_expired else 1)
            db.add(RefreshSession(
                user_id=session_owner, family_id=family,
                token_hash=uuid.uuid4().hex,
                expires_at=dt.datetime.utcnow() + delta,
                revoked_at=(dt.datetime.utcnow() if family_revoked else None),
            ))
        db.add(ResearchSession(id=1, user_id=session_owner, title="t",
                               model="model-x", system_prompt="SP"))
        task = ScheduledTask(
            user_id=task_owner, session_id=1, prompt="nightly digest",
            cron_expression="0 8 * * *", is_active=True,
            auth_session_ref=family, tenant_id=None, next_run_at=next_run_at,
        )
        db.add(task)
        db.commit()
        return task.id
    finally:
        db.close()


def _patch_wait(monkeypatch, *, raise_status=None, terminal=None, reason=None):
    """Replace the observation primitive (no worker runs in these tests)."""
    import app.run_engine.sync_wait as sw

    if raise_status is not None:
        def _raise(db, run_id, **kw):
            row = db.query(Run).filter(Run.id == run_id).one()
            row.status = raise_status
            db.commit()
            raise sw.SyncWaitError(sw.SYNC_WAIT_TIMEOUT, run_id, raise_status)

        monkeypatch.setattr(sw, "wait_for_terminal", _raise)
        return

    def _terminal(db, run_id, **kw):
        row = db.query(Run).filter(Run.id == run_id).one()
        row.status = terminal
        row.status_reason = reason
        db.commit()
        return row

    monkeypatch.setattr(sw, "wait_for_terminal", _terminal)


# -- flag OFF: legacy behaviour, nothing durable --------------------------


def test_flag_off_runs_the_legacy_workflow_and_creates_nothing(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", False)
    _bind_sessions(monkeypatch, eng)
    tid = _seed(eng)
    calls: list = []
    monkeypatch.setattr(ss, "run_research_workflow",
                        lambda **kw: calls.append(kw) or {"ok": True})

    ss.execute_scheduled_task(tid)

    assert len(calls) == 1
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 0
        assert db.query(Message).count() == 0
        assert db.query(PendingApproval).count() == 0
    finally:
        db.close()


# -- flag ON: one bound Run, truthful outcomes ----------------------------


def test_parked_run_is_reported_pending_approval_and_never_auto_approved(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    tid = _seed(eng)
    _patch_wait(monkeypatch, raise_status="waiting_for_approval")

    out = se.execute_scheduled_task_via_engine(tid)

    assert out["status"] == se.PENDING_APPROVAL
    assert out["run_status"] == "waiting_for_approval"
    db = mk_session(eng)
    try:
        runs = db.query(Run).all()
        assert len(runs) == 1
        run = runs[0]
        assert run.user_id == UID and run.session_id == 1
        assert run.auth_session_ref == FAMILY          # the captured family only
        assert run.idempotency_key.startswith("sched:%d:" % tid)
        assert db.query(PendingApproval).count() == 0  # no approval created
        assert db.query(Message).count() == 1          # the job prompt was sealed
    finally:
        db.close()


def test_run_still_running_at_the_deadline_is_pending_not_success(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    tid = _seed(eng)
    _patch_wait(monkeypatch, raise_status="running")

    out = se.execute_scheduled_task_via_engine(tid)

    assert out["status"] == se.PENDING
    assert out["run_status"] == "running"


@pytest.mark.parametrize("state,reason", [
    ("succeeded", None),
    ("failed", "provider_error"),
    ("timed_out", None),
])
def test_terminal_states_are_reported_as_is(eng, monkeypatch, state, reason):
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    tid = _seed(eng)
    _patch_wait(monkeypatch, terminal=state, reason=reason)

    out = se.execute_scheduled_task_via_engine(tid)

    assert out["status"] == state
    assert out["run_status"] == state
    if reason:
        assert out["run_reason"] == reason


def test_a_retry_of_the_same_fire_does_not_create_a_second_run(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    fire = dt.datetime(2026, 1, 1, 8, 0, 0)
    tid = _seed(eng, next_run_at=fire)
    _patch_wait(monkeypatch, raise_status="running")

    first = se.execute_scheduled_task_via_engine(tid)
    second = se.execute_scheduled_task_via_engine(tid)

    assert first["run_id"] == second["run_id"]
    assert first["created"] is True and second["created"] is False
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 1
    finally:
        db.close()


def test_next_fire_gets_its_own_run(eng, monkeypatch):
    """A FINISHED fire releases the identity, so the next fire is a new Run. (An
    UNFINISHED fire keeps it - see the pending tests.)"""
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    tid = _seed(eng, next_run_at=dt.datetime(2026, 1, 1, 8, 0, 0))
    _patch_wait(monkeypatch, terminal="succeeded")

    first = se.execute_scheduled_task_via_engine(tid)
    db = mk_session(eng)
    try:                                  # simulate APScheduler advancing the fire
        task = db.query(ScheduledTask).filter(ScheduledTask.id == tid).one()
        task.next_run_at = dt.datetime(2026, 1, 2, 8, 0, 0)
        db.commit()
    finally:
        db.close()
    second = se.execute_scheduled_task_via_engine(tid)

    assert first["run_id"] != second["run_id"]
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 2
    finally:
        db.close()


# -- fail-closed paths ---------------------------------------------------


def test_task_without_a_captured_binding_fails_closed_and_creates_no_run(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    tid = _seed(eng, family=None)

    out = se.execute_scheduled_task_via_engine(tid)

    assert out["status"] == "failed" and out["run_id"] is None
    assert out["reason"] == se.SCHEDULER_NO_SESSION_BINDING
    assert isinstance(out["response"], str) and out["response"]
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 0
    finally:
        db.close()


def test_session_owned_by_another_user_fails_closed(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    tid = _seed(eng, session_owner=UID + 1)   # task owner UID, session not theirs

    out = se.execute_scheduled_task_via_engine(tid)

    assert out["status"] == "failed"
    assert out["reason"] == se.SCHEDULER_SESSION_MISSING
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 0
    finally:
        db.close()


def test_revoked_family_fails_closed_and_creates_no_run(eng, monkeypatch):
    """A task whose login family was revoked (logout / reuse detection) must NOT
    run: validate_session_binding is the authority and this path invents nothing."""
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    tid = _seed(eng, family_revoked=True)

    out = se.execute_scheduled_task_via_engine(tid)

    assert out["status"] == "failed"
    assert out["reason"] == se.SCHEDULER_SESSION_BINDING_INVALID
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 0
    finally:
        db.close()


def test_expired_family_fails_closed(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    tid = _seed(eng, family_expired=True)

    out = se.execute_scheduled_task_via_engine(tid)

    assert out["status"] == "failed"
    assert out["reason"] == se.SCHEDULER_SESSION_BINDING_INVALID


def test_inactive_task_is_skipped(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    tid = _seed(eng)
    db = mk_session(eng)
    try:
        db.query(ScheduledTask).filter(ScheduledTask.id == tid).one().is_active = False
        db.commit()
    finally:
        db.close()

    out = se.execute_scheduled_task_via_engine(tid)

    assert out["status"] == se.SKIPPED
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 0
    finally:
        db.close()


def test_fire_key_encodes_the_task_and_the_scheduled_instant(eng):
    db = mk_session(eng)
    try:
        task = ScheduledTask(id=42, user_id=1, session_id=1, prompt="p",
                             cron_expression="0 8 * * *",
                             next_run_at=dt.datetime(2026, 3, 4, 5, 6, 7))
        assert se.fire_key(task) == "sched:42:2026-03-04T05:06:07"
    finally:
        db.close()


def test_create_route_captures_the_live_login_family():
    """The scheduler POST stores the creating refresh-session family - the thing
    that later lets a scheduled fire pass validate_session_binding. Also proves
    the added AuthContext dependency did not break task creation (201)."""
    import uuid as _uuid

    from fastapi.testclient import TestClient

    from app.database import SessionLocal
    from app.main import app
    from tests.auth_helpers import auth_headers, register_and_login

    with TestClient(app) as c:
        uid, token = register_and_login(c, username="schedbind_" + _uuid.uuid4().hex[:8])
        h = auth_headers(token)
        csrf = c.cookies.get("research_assistant_csrf_token")
        if csrf:
            h["X-CSRF-Token"] = csrf
        sid = c.post("/api/sessions", json={"title": "sched"}, headers=h).json()["id"]
        r = c.post("/api/scheduler", headers=h, json={
            "session_id": sid, "prompt": "daily digest",
            "cron_expression": "0 6 * * *"})
        assert r.status_code == 201, r.text[:300]
        task_id = r.json()["id"]

    db = SessionLocal()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).one()
        assert task.user_id == uid
        assert task.auth_session_ref, "the creating family must be captured"
        assert db.query(RefreshSession).filter(
            RefreshSession.family_id == task.auth_session_ref,
            RefreshSession.user_id == uid,
            RefreshSession.revoked_at.is_(None),
        ).count() >= 1, "the captured family must be a LIVE login family"
    finally:
        db.close()

def _advanced_scheduler(monkeypatch, when):
    """A scheduler whose next fire time has moved on (restart / misfire)."""
    class _Job:
        pass

    _Job.next_run_time = when

    class _Sched:
        def get_job(self, job_id):
            return _Job()

    monkeypatch.setattr(ss, "get_scheduler", lambda: _Sched())


def test_retry_of_an_interrupted_fire_is_one_run_even_after_next_run_at_advances(eng, monkeypatch):
    """The guarantee the design must PROVE: the fire identity is persisted, so a
    retry of the same fire still hits the SAME Run after next_run_at advanced."""
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    fire = dt.datetime(2026, 1, 1, 8, 0, 0)
    tid = _seed(eng, next_run_at=fire)
    _advanced_scheduler(monkeypatch, fire + dt.timedelta(days=1))

    import app.run_engine.sync_wait as sw

    def _crash(db, run_id, **kw):
        raise RuntimeError("worker died mid-fire")

    monkeypatch.setattr(sw, "wait_for_terminal", _crash)
    first = se.execute_scheduled_task_via_engine(tid)
    assert first["status"] == "failed"            # truthful, not success

    db = mk_session(eng)
    try:
        runs = db.query(Run).all()
        assert len(runs) == 1
        run_id = runs[0].id
        task = db.query(ScheduledTask).filter(ScheduledTask.id == tid).one()
        assert task.pending_fire_key, "the fire identity must be PERSISTED"
        # what a restart load_scheduled_tasks() does to the row
        task.next_run_at = fire + dt.timedelta(days=1)
        db.commit()
    finally:
        db.close()

    _patch_wait(monkeypatch, raise_status="running")   # retry of the SAME fire
    second = se.execute_scheduled_task_via_engine(tid)

    assert second["run_id"] == run_id
    assert second["fire_reused"] is True
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 1
    finally:
        db.close()


def test_completed_fire_clears_the_pending_identity_and_the_next_fire_runs(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    fire = dt.datetime(2026, 1, 1, 8, 0, 0)
    tid = _seed(eng, next_run_at=fire)
    _patch_wait(monkeypatch, terminal="succeeded")

    first = se.execute_scheduled_task_via_engine(tid)
    assert first["status"] == "succeeded" and first["fire_reused"] is False

    db = mk_session(eng)
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == tid).one()
        assert task.pending_fire_key is None, "a finished fire clears its identity"
        task.next_run_at = fire + dt.timedelta(days=1)
        db.commit()
    finally:
        db.close()

    second = se.execute_scheduled_task_via_engine(tid)
    assert second["run_id"] != first["run_id"]
    assert second["fire_reused"] is False
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 2
    finally:
        db.close()


def test_scheduler_surfaces_never_expose_the_session_binding():
    """auth_session_ref / tenant_id / pending_fire_key never leave the scheduler
    APIs or the session export schema."""
    import uuid as _uuid

    from fastapi.testclient import TestClient

    from app.main import app
    from app.schemas.sessions import ExportSessionSchedule
    from tests.auth_helpers import auth_headers, register_and_login

    forbidden = {"auth_session_ref", "tenant_id", "pending_fire_key"}
    assert forbidden.isdisjoint(set(ExportSessionSchedule.model_fields))

    with TestClient(app) as c:
        _uid, token = register_and_login(c, username="schedleak_" + _uuid.uuid4().hex[:8])
        h = auth_headers(token)
        csrf = c.cookies.get("research_assistant_csrf_token")
        if csrf:
            h["X-CSRF-Token"] = csrf
        sid = c.post("/api/sessions", json={"title": "leak"}, headers=h).json()["id"]
        created = c.post("/api/scheduler", headers=h, json={
            "session_id": sid, "prompt": "daily", "cron_expression": "0 6 * * *"})
        assert created.status_code == 201, created.text[:200]
        assert forbidden.isdisjoint(created.json()), created.json()
        task_id = created.json()["id"]
        listing = c.get("/api/scheduler", headers=h)
        assert listing.status_code == 200
        for item in listing.json():
            assert forbidden.isdisjoint(item), item
        one = c.get("/api/scheduler/" + str(task_id), headers=h)
        assert one.status_code == 200 and forbidden.isdisjoint(one.json())

def test_pending_outcome_keeps_the_fire_identity(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    tid = _seed(eng, next_run_at=dt.datetime(2026, 1, 1, 8, 0, 0))
    _patch_wait(monkeypatch, raise_status="running")

    out = se.execute_scheduled_task_via_engine(tid)
    assert out["status"] == se.PENDING

    db = mk_session(eng)
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == tid).one()
        assert task.pending_fire_key, "an unfinished fire must KEEP its identity"
    finally:
        db.close()


def test_pending_approval_keeps_identity_and_a_restart_retry_reuses_the_run(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    fire = dt.datetime(2026, 1, 1, 8, 0, 0)
    tid = _seed(eng, next_run_at=fire)
    _patch_wait(monkeypatch, raise_status="waiting_for_approval")

    first = se.execute_scheduled_task_via_engine(tid)
    assert first["status"] == se.PENDING_APPROVAL

    db = mk_session(eng)
    try:
        run_id = db.query(Run).one().id
        task = db.query(ScheduledTask).filter(ScheduledTask.id == tid).one()
        assert task.pending_fire_key, "a parked fire must KEEP its identity"
        assert db.query(PendingApproval).count() == 0
        task.next_run_at = fire + dt.timedelta(days=1)   # a restart advanced it
        db.commit()
    finally:
        db.close()

    second = se.execute_scheduled_task_via_engine(tid)   # retry of the same fire
    assert second["run_id"] == run_id
    assert second["fire_reused"] is True
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 1
    finally:
        db.close()


def test_a_finished_parked_run_releases_the_identity_for_the_next_fire(eng, monkeypatch):
    """Once the parked Run terminalizes (a human decided it), the NEXT attempt must
    NOT replay it - it releases the identity and starts a fresh fire."""
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    tid = _seed(eng, next_run_at=dt.datetime(2026, 1, 1, 8, 0, 0))
    _patch_wait(monkeypatch, raise_status="waiting_for_approval")
    first = se.execute_scheduled_task_via_engine(tid)
    assert first["status"] == se.PENDING_APPROVAL

    db = mk_session(eng)
    try:
        parked = db.query(Run).one()
        parked_id = parked.id
        parked.status = "succeeded"        # the worker finished it after approval
        db.commit()
    finally:
        db.close()

    second = se.execute_scheduled_task_via_engine(tid)
    assert second["fire_reused"] is False
    assert second["run_id"] != parked_id
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 2
    finally:
        db.close()


def test_run_now_under_the_flag_uses_the_engine_and_keeps_the_response_contract(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    tid = _seed(eng, next_run_at=dt.datetime(2026, 1, 1, 8, 0, 0))
    _patch_wait(monkeypatch, terminal="succeeded")

    out = ss.run_task_now(tid)

    assert isinstance(out.get("response"), str) and out["response"]
    assert out["status"] == "succeeded"
    assert out["trigger"] == "manual"
    assert out["run_id"]
    db = mk_session(eng)
    try:
        run = db.query(Run).one()
        assert run.idempotency_key.startswith("manual:")
        assert run.session_id == 1 and run.user_id == UID
    finally:
        db.close()


def test_two_run_now_clicks_in_the_same_minute_create_two_runs(eng, monkeypatch):
    """A DELIBERATE second Run-now click must create its own Run. The first
    click terminalizes before the second (so no unfinished fire exists), and both
    clicks share one wall-clock minute - the old minute-resolution key replayed
    the finished Run instead of starting a new one."""
    import types as _types

    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", True)
    _bind_sessions(monkeypatch, eng)
    tid = _seed(eng, next_run_at=dt.datetime(2026, 1, 1, 8, 0, 0))
    frozen = dt.datetime(2026, 5, 6, 7, 8, 30)
    monkeypatch.setattr(se, "_dt", _types.SimpleNamespace(
        datetime=_types.SimpleNamespace(utcnow=lambda: frozen),
        timedelta=dt.timedelta))
    _patch_wait(monkeypatch, terminal="succeeded")

    first = ss.run_task_now(tid)
    assert first["status"] == "succeeded"
    db = mk_session(eng)
    try:
        assert db.query(ScheduledTask).filter(ScheduledTask.id == tid).one() \
            .pending_fire_key is None, "a finished fire releases its identity"
    finally:
        db.close()

    second = ss.run_task_now(tid)      # same frozen minute, deliberate click

    assert second["run_id"] != first["run_id"]
    db = mk_session(eng)
    try:
        runs = db.query(Run).all()
        assert len(runs) == 2
        keys = {r.idempotency_key for r in runs}
        assert len(keys) == 2, keys
        assert all(k.startswith("manual:") for k in keys)
    finally:
        db.close()


def test_run_now_flag_off_stays_legacy(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_scheduler_engine_enabled", False)
    _bind_sessions(monkeypatch, eng)
    tid = _seed(eng)
    calls: list = []
    monkeypatch.setattr(ss, "run_research_workflow",
                        lambda **kw: calls.append(kw) or {"response": "legacy"})

    out = ss.run_task_now(tid)

    assert out == {"response": "legacy"} and len(calls) == 1
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 0
    finally:
        db.close()


