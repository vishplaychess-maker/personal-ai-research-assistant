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

Phase 1 uses a mock/local execution path: the worker sleeps for a short,
configurable delay (default 5 seconds) and writes a deterministic summary.
The row schema and the status contract stay identical when the real agent
lands behind this seam.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from app.database import SessionLocal
from app.models.models import OpenbotTask

logger = logging.getLogger(__name__)

#: Simulated agent runtime for the Phase 1 mock execution path. Read at
#: worker execution time, so tests can shrink it (monkeypatch this constant).
MOCK_COMPLETION_SECONDS = 5.0

#: The closed status set persisted in ``openbot_tasks.status``.
VALID_STATUSES = ("pending", "running", "completed", "failed")

# Phase 1 mock output — a fixed, query-independent placeholder summary. The
# real agent will replace this; deliberately not including the raw query text
# so it can never be mistaken for authoritative research.
_MOCK_SUMMARY = """## Autonomous Research Summary

**Status:** Phase 1 mock execution — the autonomous agent runtime is not yet
wired in, so this is a placeholder result proving the task queue round-trip.

**What happened:**
1. Your request was accepted and queued (`pending`).
2. An isolated worker picked it up and marked it `running`.
3. After the simulated research interval the task was marked `completed`
   and this summary was persisted.

**What lands next (Phase 2):** the isolated OpenBot agent runtime — real
multi-step planning, tool use, and sourced synthesis — will execute inside
this exact same status contract and this same row, so today's polling UI and
API need no changes.
"""


def start_research_task(user_id: int, query: str) -> str:
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

    _spawn_mock_worker(task_id, user_id)
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


# ── Phase 1 mock execution path ─────────────────────────────


def _spawn_mock_worker(task_id: str, user_id: int) -> None:
    """Hand the task to a short-lived daemon thread (Celery-free local path).

    Daemon threads keep a test/dev process free-exiting; the durable,
    acknowledged queue becomes the run engine in a later phase.
    """
    thread = threading.Thread(
        target=_run_mock_research,
        args=(task_id, user_id),
        name=f"openbot-mock-{task_id[:8]}",
        daemon=True,
    )
    thread.start()


def _set_status(task_id: str, status: str, result_text: str | None = None) -> None:
    db = SessionLocal()
    try:
        task = (
            db.query(OpenbotTask)
            .filter(OpenbotTask.task_id == task_id)
            .first()
        )
        if task is None:
            return
        task.status = status
        if result_text is not None:
            task.result_text = result_text
        db.commit()
    except Exception:  # noqa: BLE001 - a mock worker must never crash the app
        db.rollback()
        logger.exception("openbot mock worker could not set status=%s", status)
    finally:
        db.close()


def _run_mock_research(task_id: str, user_id: int) -> None:
    """pending -> running -> (sleep) -> completed with a mock summary."""
    _set_status(task_id, "running")
    try:
        delay = max(0.0, float(MOCK_COMPLETION_SECONDS))
    except (TypeError, ValueError):
        delay = MOCK_COMPLETION_SECONDS
    time.sleep(delay)
    _set_status(task_id, "completed", result_text=_MOCK_SUMMARY)
    logger.info("openbot task %s completed (mock, %.1fs)", task_id, delay)
