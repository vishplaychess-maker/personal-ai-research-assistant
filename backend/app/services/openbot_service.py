"""OpenBot Autonomous Research Agent — Phase 1 task queue.

Offloads complex, multi-step research to an isolated task so the main chat
stays responsive. The whole pipeline is asynchronous by construction:

* ``start_research_task`` only creates a ``pending`` row and hands it to a
  background worker — the HTTP response never waits for the work.
* The background worker transitions the row ``running`` -> ``completed`` and
  fills ``result_text``.
* ``get_task_status`` is the single read path for the frontend's poll loop,
  and it filters on ``user_id`` so a task id belonging to another account is
  indistinguishable from a nonexistent one (no existence leak).

The worker runs ``orchestrator.run_task``: plan -> read-only context (workspace,
git, enabled MCP providers) -> optional web research -> deterministic synthesis.
Progress frames are published to an in-memory event bus for the SSE route.
"""

from __future__ import annotations

import logging
import threading
import uuid

from app.database import SessionLocal
from app.models.models import OpenbotTask
from app.services import agent_events, orchestrator

logger = logging.getLogger(__name__)

#: The closed status set persisted in ``openbot_tasks.status``.
VALID_STATUSES = ("pending", "running", "completed", "failed", "cancelled")


def start_research_task(user_id: int, query: str, use_swarm: bool = False) -> str:
    """Create a queued research task and return its public id immediately.

    Nothing here blocks on the (mock) research itself: the background worker
    is spawned after the row is committed, so the caller — a FastAPI route —
    can return the task id right away.
    """
    task_id = uuid.uuid4().hex
    db = SessionLocal()
    try:
        db.add(
            OpenbotTask(
                user_id=user_id,
                task_id=task_id,
                query=query,
                status="pending",
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    _spawn_worker(task_id, user_id, query, use_swarm)
    return task_id


def get_task_status(task_id: str, user_id: int) -> dict | None:
    """Return ``{task_id, status, result_text, created_at, query}``, or ``None``.

    Strict user isolation: the row is matched on ``task_id`` AND ``user_id``,
    so another account's task looks identical to a task that does not exist.
    The caller (route layer) turns ``None`` into a 404 — never a 403.
    """
    db = SessionLocal()
    try:
        task = (
            db.query(OpenbotTask)
            .filter(
                OpenbotTask.task_id == task_id,
                OpenbotTask.user_id == user_id,
            )
            .first()
        )
        if task is None:
            return None
        return {
            "task_id": task.task_id,
            "status": task.status,
            "result_text": task.result_text,
            "created_at": task.created_at,
            "query": task.query,
        }
    finally:
        db.close()


# ── execution path ──────────────────────────────────────────


def _spawn_worker(
    task_id: str, user_id: int, query: str, use_swarm: bool = False
) -> None:
    """Run the orchestrator on a daemon thread (Celery-free local worker).

    The HTTP response never waits for the pipeline. Progress frames go to the
    in-memory event bus; the DB row remains the durable status/result source.
    """
    thread = threading.Thread(
        target=orchestrator.run_task,
        args=(task_id, user_id, query),
        kwargs={"use_swarm": use_swarm},
        name=f"openbot-{task_id[:8]}",
        daemon=True,
    )
    thread.start()


def get_task_events(
    task_id: str, user_id: int, since: int = 0
) -> list[dict] | None:
    """Progress frames for an owned task, or ``None`` for a 404."""
    if get_task_status(task_id, user_id) is None:
        return None
    return agent_events.events_since(task_id, since)


def cancel_task(task_id: str, user_id: int) -> dict | None:
    """Request cancellation; the worker stops at its next checkpoint."""
    status = get_task_status(task_id, user_id)
    if status is None:
        return None
    if status["status"] in ("completed", "failed", "cancelled"):
        return status
    agent_events.request_cancel(task_id)
    return status
