"""MP-5 (first slice) - the ONE flag-gated scheduled-job execution path that
submits through the canonical RunEngine.

Why this module exists, i.e. the constraints it satisfies:

* Ownership is preserved. The Run is submitted with an AuthContext rebuilt from
  the TASK row - the owner user_id/tenant plus the refresh-session family that
  was captured when the task was created - so validate_session_binding (the one
  authority, session_binding.py) still decides. There is no bypass, no service
  principal that skips the check, and no second approval authority.
* No duplicate Runs on a retry. The idempotency key is deterministic per
  SCHEDULED FIRE, so an APScheduler retry of the same fire returns the existing
  Run instead of creating a second one. The invariant is that next_run_at is
  refreshed after every attempt.
* Outcomes are truthful. Terminal run states are reported as-is (succeeded /
  failed / timed_out / cancelled / denied); a run that PARKS for a real user
  decision is reported as pending_approval and is NEVER auto-approved; a run
  still queued/running at the wait deadline is reported as pending; a task with
  no live binding is failed with the bounded reason scheduler_no_session_binding.

It never creates an approval, never decides one, and never advances a Run: the
embedded worker owns execution and the MP-2 approval gateway/dispatch path owns
approvals. Retrieval enrichment (RAG/memory) is deliberately NOT performed in
this slice - the sealed context snapshot is the canonical empty one, exactly as
for a direct Run API submission.
"""

from __future__ import annotations

import datetime as _dt
import logging
import uuid

logger = logging.getLogger(__name__)

# Bounded, machine-readable reasons (never free text, never user content).
SCHEDULER_NO_SESSION_BINDING = "scheduler_no_session_binding"
SCHEDULER_SESSION_BINDING_INVALID = "scheduler_session_binding_invalid"
SCHEDULER_SUBMIT_FAILED = "scheduler_submit_failed"
SCHEDULER_SESSION_MISSING = "scheduler_session_missing"
SCHEDULER_USER_MISSING = "scheduler_user_missing"
SCHEDULER_INVALID_SUBMISSION = "scheduler_invalid_submission"
SCHEDULER_RUN_MISSING = "scheduler_run_missing"

# Trigger vocabulary: the cron fire and the manual "Run now" path share this
# module; only the identity namespace of a NEW fire differs.
CRON = "cron"
MANUAL = "manual"

# A fire is FINISHED only on one of these outcomes. ANY other outcome
# (pending / pending_approval / queued / running) leaves the fire unfinished,
# so its persisted identity is KEPT and a retry reuses the same Run.
TERMINAL_OUTCOMES = frozenset({
    "succeeded", "failed", "timed_out", "cancelled", "denied",
})

# Outcome vocabulary reported to callers.
PENDING = "pending"
PENDING_APPROVAL = "pending_approval"
SKIPPED = "skipped"
FAILED = "failed"


def _status_line(status, run_id=None, reason=None) -> str:
    """Bounded, human-readable status for the run-now route response field.

    The route documented `{response: string}`; the engine path cannot return
    the model answer text there, so it returns an honest status line instead
    (the answer itself is in the session history).
    """
    rid = (run_id or "")[:8]
    if status == "succeeded":
        return "Scheduled run succeeded (run %s)." % rid
    if status == PENDING_APPROVAL:
        return "Scheduled run is waiting for your approval (run %s)." % rid
    if status == PENDING:
        return "Scheduled run is still in progress (run %s)." % rid
    if status == SKIPPED:
        return "Scheduled task is inactive or missing; nothing ran."
    if status == "cancelled" or status == "denied":
        return "Scheduled run %s (run %s)." % (status, rid)
    return "Scheduled run did not succeed: %s%s." % (
        reason or status, (" (run %s)" % rid) if rid else "")


def _result(status, run_id=None, reason=None, run_status=None,
            run_reason=None, created=None):
    out = {"status": status, "run_id": run_id}
    out["response"] = _status_line(status, run_id, reason or run_reason)
    if reason is not None:
        out["reason"] = reason
    if run_status is not None:
        out["run_status"] = run_status
    if run_reason is not None:
        out["run_reason"] = run_reason
    if created is not None:
        out["created"] = bool(created)
    return out


def fire_key(task) -> str:
    """Deterministic idempotency key for ONE scheduled fire of the task.

    Uses the scheduled instant (next_run_at, falling back to last_run_at) so a
    retry of the SAME fire dedupes while a later fire still gets its own key.
    When both are NULL (a task never registered with APScheduler) a minute
    bucket is used, which still dedupes a same-minute retry.
    """
    stamp = task.next_run_at or task.last_run_at
    if stamp is None:
        stamp = _dt.datetime.utcnow().replace(second=0, microsecond=0)
    return "sched:%d:%s" % (int(task.id), stamp.isoformat(timespec="seconds"))


def resolve_fire_key(task, trigger: str = CRON) -> tuple:
    """Return (key, reused) for the fire being executed.

    A PENDING identity persisted by an earlier attempt WINS: that fire is
    unfinished, so a retry must finish THAT fire - even when next_run_at has
    already advanced (process restart, misfire replay). This is what makes the
    identity persisted rather than re-derived from a mutable field.
    """
    pending = getattr(task, "pending_fire_key", None)
    if pending:
        return pending, True
    if trigger == MANUAL:
        # Each deliberate "Run now" CLICK is its own fire. A minute-resolution
        # key collided for two clicks inside one minute, so the second click
        # replayed the first (already finished) Run instead of creating a new
        # one. An UNFINISHED manual fire is still reused through the pending
        # identity returned above, so retries of unfinished work are unchanged.
        return "manual:%d:%s" % (int(task.id), uuid.uuid4().hex[:16]), False
    return fire_key(task), False


def _existing_run_for_key(db, task, key):
    """The Run this fire identity already produced, if any (same scope the
    idempotency key is unique in)."""
    from sqlalchemy import func

    from app.models.models import Run

    return (db.query(Run)
            .filter(Run.user_id == task.user_id,
                    Run.session_id == task.session_id,
                    Run.idempotency_key == key,
                    func.coalesce(Run.tenant_id, "") == (task.tenant_id or ""))
            .order_by(Run.created_at.desc()).first())


def _refresh_next_run_at(task, task_id: int) -> None:
    """Mirror the legacy path: copy the scheduler job next run time onto the task
    after an attempt, so the next fire gets a DIFFERENT idempotency key. Tolerates
    an absent scheduler (tests, lifespan down)."""
    try:
        from app.services.scheduler_service import get_scheduler

        sched = get_scheduler()
        job = sched.get_job("task_%d" % int(task_id)) if sched else None
        if job is not None and getattr(job, "next_run_time", None):
            task.next_run_at = job.next_run_time
    except Exception as exc:  # bounded: never block the outcome report
        logger.warning("scheduled task %s: next_run_at refresh skipped (%s)",
                       task_id, type(exc).__name__)


def _observe(db, run, created) -> dict:
    """Observe the submitted Run to a TRUTHFUL outcome. Strictly read-only."""
    from app.config import settings
    from app.run_engine.state_machine import WAITING_FOR_APPROVAL
    from app.run_engine.sync_wait import SyncWaitError, wait_for_terminal

    deadline = float(getattr(settings, "run_scheduler_engine_wait_deadline_s", 180.0))
    try:
        terminal = wait_for_terminal(db, run.id, deadline_s=deadline)
    except SyncWaitError as exc:
        if exc.status is None:
            return _result(FAILED, run.id, SCHEDULER_RUN_MISSING, created=created)
        if exc.status == WAITING_FOR_APPROVAL:
            # Parked for a REAL user decision. The MP-2 gateway owns it; this
            # path never approves, denies, or dispatches anything.
            return _result(PENDING_APPROVAL, run.id, run_status=exc.status,
                           created=created)
        return _result(PENDING, run.id, run_status=exc.status, created=created)
    return _result(terminal.status, run.id, run_status=terminal.status,
                   run_reason=terminal.status_reason, created=created)


def execute_scheduled_task_via_engine(task_id: int, *, trigger: str = CRON) -> dict:
    """Submit ONE scheduled fire through the canonical RunEngine and report the
    truthful outcome. Returns a bounded dict and never raises for expected
    failures (an unexpected error is logged as a bounded class name and reported
    as scheduler_submit_failed)."""
    from app.config import settings
    from app.database import SessionLocal
    from app.models.models import ResearchSession, ScheduledTask, User
    from app.run_engine.context import AuthContext
    from app.run_engine.state_machine import is_run_terminal

    db = SessionLocal()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if task is None or not task.is_active:
            return _result(SKIPPED, reason="task_missing_or_inactive")

        # Ownership: the target session must belong to the task owner (the same
        # check the legacy path performs) - never submit into another session.
        session = db.query(ResearchSession).filter(
            ResearchSession.id == task.session_id,
            ResearchSession.user_id == task.user_id,
        ).first()
        if session is None:
            return _result(FAILED, reason=SCHEDULER_SESSION_MISSING)

        if not task.auth_session_ref:
            logger.warning(
                "scheduled task %s: engine path refused - no auth_session_ref "
                "(task predates the binding or was created without a sid claim)",
                task_id)
            return _result(FAILED, reason=SCHEDULER_NO_SESSION_BINDING)

        user = db.query(User).filter(User.id == task.user_id).first()
        if user is None:
            return _result(FAILED, reason=SCHEDULER_USER_MISSING)

        ctx = AuthContext(
            user=user, user_id=task.user_id, tenant_id=task.tenant_id,
            auth_session_ref=task.auth_session_ref,
            durable_session_binding_valid=False,  # never trusted; DB is authority
        )

        provider = None
        try:
            from app.services.settings_service import get_user_llm_config

            provider = (get_user_llm_config(db, task.user_id) or {}).get("provider")
        except Exception:  # bounded; fall back to the deployment provider
            provider = None
        provider = provider or settings.llm_provider

        from app.run_engine import submission as _sub

        try:
            submission = _sub.build_chat_submission(
                session_id=task.session_id, message=task.prompt, image_url=None,
                provider=provider, model=session.model, mode="chat",
                system_prompt=session.system_prompt, options={},
            )
        except ValueError:
            return _result(FAILED, reason=SCHEDULER_INVALID_SUBMISSION)

        key, reused = resolve_fire_key(task, trigger)
        if reused:
            existing = _existing_run_for_key(db, task, key)
            if existing is not None and is_run_terminal(existing.status):
                # The unfinished fire FINISHED outside this path (the worker
                # ran it to terminal, or a human approved/denied it). Release
                # the identity so THIS fire proceeds as a new one - and never
                # replay the finished Run as this fire result.
                task.pending_fire_key = None
                key, reused = resolve_fire_key(task, trigger)
                if key == (existing.idempotency_key or ""):
                    key = "%s:released:%s" % (
                        key, _dt.datetime.utcnow().strftime("%H%M%S"))
                try:
                    db.commit()
                except Exception:
                    db.rollback()
        if not reused:
            # Persist the fire identity BEFORE submitting, so an interrupted
            # attempt can never mint a second identity for the same fire.
            task.pending_fire_key = key
            try:
                db.commit()
            except Exception:
                db.rollback()
                return _result(FAILED, reason=SCHEDULER_SUBMIT_FAILED)

        try:
            run, created = _sub.create_chat_run(
                db, ctx, submission=submission, idempotency_key=key)
        except Exception as exc:
            from app.run_engine.errors import (SessionBindingMismatch,
                                               SessionBindingRequired)

            if isinstance(exc, (SessionBindingRequired, SessionBindingMismatch)):
                logger.warning("scheduled task %s: binding invalid (%s)",
                               task_id, type(exc).__name__)
                return _result(FAILED, reason=SCHEDULER_SESSION_BINDING_INVALID)
            logger.warning("scheduled task %s: engine submission failed (%s)",
                           task_id, type(exc).__name__)
            return _result(FAILED, reason=SCHEDULER_SUBMIT_FAILED)

        outcome = _observe(db, run, created)
        outcome["fire_reused"] = bool(reused)
        outcome["trigger"] = trigger
        if outcome.get("status") in TERMINAL_OUTCOMES:
            # The fire is FINISHED: release the persisted identity so the NEXT
            # fire gets its own.
            task.pending_fire_key = None
        # Otherwise (pending / pending_approval / queued / running) the fire is
        # UNFINISHED and the identity is KEPT, so a retry of this SAME fire -
        # including after a process restart, or once a human decides the parked
        # approval - reuses this Run instead of minting another one.
        task.last_run_at = _dt.datetime.utcnow()
        _refresh_next_run_at(task, task_id)
        try:
            db.commit()
        except Exception:  # bounded: the Run already exists and is authoritative
            db.rollback()
        logger.info("scheduled task %s: engine outcome=%s run=%s reason=%s",
                    task_id, outcome.get("status"), outcome.get("run_id"),
                    outcome.get("reason") or outcome.get("run_reason"))
        return outcome
    except Exception as exc:
        logger.error("scheduled task %s: engine path error (%s)", task_id,
                     type(exc).__name__)
        db.rollback()
        return _result(FAILED, reason=SCHEDULER_SUBMIT_FAILED)
    finally:
        db.close()


__all__ = [
    "SCHEDULER_INVALID_SUBMISSION", "SCHEDULER_NO_SESSION_BINDING",
    "SCHEDULER_RUN_MISSING", "SCHEDULER_SESSION_BINDING_INVALID",
    "SCHEDULER_SESSION_MISSING", "SCHEDULER_SUBMIT_FAILED",
    "SCHEDULER_USER_MISSING", "FAILED", "PENDING", "PENDING_APPROVAL", "SKIPPED",
    "execute_scheduled_task_via_engine", "fire_key",
]
