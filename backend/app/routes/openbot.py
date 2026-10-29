"""OpenBot Autonomous Research Agent routes (Phase 1).

POST /api/agents/openbot/research       — queue a delegated research task
GET  /api/agents/openbot/tasks/{task_id} — poll its status / fetch the result

The queue is fully asynchronous: ``POST`` returns the ``task_id`` immediately
(the mock agent runs in a background worker), and the frontend polls ``GET``
until the task turns ``completed``/``failed``. The main chat stream is never
blocked by an OpenBot task.

Isolation: ``GET`` matches the row on ``task_id`` AND ``user_id`` — another
account's task is a plain 404, never a 403, so row existence is not leaked.
Both routes require the user's ``openbot_research`` capability to be enabled.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.database import get_db
from app.models.models import User
from app.run_engine.sse import format_sse
from app.services import openbot_service
from app.services.auth_service import get_current_user
from app.services.capability_preferences import is_capability_enabled
from app.services.cookie_service import require_csrf

router = APIRouter(prefix="/api/agents/openbot", tags=["openbot"])

#: The capability id gating this agent, kept in one place (registry + UI).
OPENBOT_CAPABILITY_ID = "openbot_research"


class ResearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)


class ResearchStartResponse(BaseModel):
    task_id: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    result_text: Optional[str] = None
    created_at: Optional[datetime] = None


def _require_openbot_enabled(db, user_id: int) -> None:
    """403 unless this user enabled the autonomous-research capability."""
    if not is_capability_enabled(db, user_id, OPENBOT_CAPABILITY_ID):
        raise HTTPException(
            status_code=403,
            detail="The Autonomous Research (OpenBot) capability is not enabled. "
            "Turn it on under Settings > Skills.",
        )


@router.post("/research", response_model=ResearchStartResponse)
def start_research(
    payload: ResearchRequest,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Queue a delegated research task. Non-blocking by design."""
    _require_openbot_enabled(db, current_user.id)
    task_id = openbot_service.start_research_task(current_user.id, payload.query)
    return {"task_id": task_id}


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
def get_task(
    task_id: str,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
): 
    """Poll one task. Cross-user (and unknown) ids are a uniform 404.

    Deliberately NOT gated by the capability flag: disabling the capability
    must never strand a result for a task that was already accepted — a user
    can still poll tasks that exist and belong to them.
    """
    if not task_id or len(task_id) > 64 or not task_id.isalnum():
        raise HTTPException(status_code=404, detail="Task not found")
    task = openbot_service.get_task_status(task_id, current_user.id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "result_text": task["result_text"],
        "created_at": task["created_at"],
    }


@router.post("/tasks/{task_id}/cancel", response_model=TaskStatusResponse)
def cancel_task(
    task_id: str,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Request cancellation; the worker stops at its next checkpoint."""
    _require_openbot_enabled(db, current_user.id)
    task = openbot_service.cancel_task(task_id, current_user.id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskStatusResponse(
        task_id=task["task_id"],
        status=task["status"],
        result_text=task["result_text"],
        created_at=task["created_at"],
    )


@router.get("/tasks/{task_id}/events")
async def task_events(
    task_id: str,
    request: Request,
    since: int = 0,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SSE tail of the live progress frames for an owned task.

    Ends when the task reaches a terminal status (after flushing the final
    frames) or after a bounded 2-minute window; the client can reconnect with
    ``?since=<last seq>``.
    """
    _require_openbot_enabled(db, current_user.id)
    if openbot_service.get_task_status(task_id, current_user.id) is None:
        raise HTTPException(status_code=404, detail="Task not found")

    async def _stream():
        last = max(0, int(since))
        deadline = time.monotonic() + 120.0
        terminal = ("completed", "failed", "cancelled")
        while True:
            if await request.is_disconnected():
                return
            for event in openbot_service.get_task_events(
                task_id, current_user.id, since=last
            ) or []:
                last = max(last, int(event["seq"]))
                yield format_sse("agent_event", event)
            status = openbot_service.get_task_status(task_id, current_user.id)
            if status is None or status["status"] in terminal:
                # Flush frames emitted in the same instant as the status change.
                for event in openbot_service.get_task_events(
                    task_id, current_user.id, since=last
                ) or []:
                    last = max(last, int(event["seq"]))
                    yield format_sse("agent_event", event)
                return
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
