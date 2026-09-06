"""In-app Terminal panel API (separate from the agent [PROPOSED_COMMAND] HITL).

POST /api/terminal/execute    — validate + stream a command's output over SSE
GET  /api/terminal/sessions   — list the caller's running commands
POST /api/terminal/stop/{id}  — kill one running command

All routes require auth + CSRF and are gated behind
``settings.enable_terminal_panel`` (404 when off).
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.models.models import User
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf
from app.services import terminal_panel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/terminal", tags=["terminal"])


class ExecuteRequest(BaseModel):
    command: str = Field(..., min_length=1, max_length=2000)
    dry_run: bool = False


def _require_enabled() -> None:
    if not settings.enable_terminal_panel:
        raise HTTPException(status_code=404, detail="Terminal panel is disabled")


@router.post("/execute")
async def execute(
    payload: ExecuteRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> StreamingResponse:
    """Run an allowlisted command and stream stdout/stderr as SSE.

    Pre-flight validation runs here so a blocked command is a plain
    **HTTP 403** (not a stream carrying an error event).
    """
    _require_enabled()
    try:
        terminal_panel.validate_command(payload.command)
    except terminal_panel.CommandRejected as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    uid = current_user.id

    async def gen():
        try:
            async for ev in terminal_panel.stream_command(
                payload.command, user_id=uid, dry_run=payload.dry_run
            ):
                yield f"event: {ev['event']}\ndata: {json.dumps(ev)}\n\n"
                if await request.is_disconnected():
                    break
        except Exception as exc:  # noqa: BLE001 — surface, don't 500 mid-stream
            logger.warning("terminal stream failed: %s", exc)
            yield f'event: error\ndata: {json.dumps({"detail": str(exc)})}\n\n'

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions")
def sessions(
    current_user: User = Depends(get_current_user),
):
    """List the caller's currently-running terminal commands."""
    _require_enabled()
    return {"sessions": terminal_panel.list_sessions(user_id=current_user.id)}


@router.post("/stop/{run_id}")
def stop(
    run_id: str,
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Kill one running command owned by the caller."""
    _require_enabled()
    ok = terminal_panel.stop_session(run_id, user_id=current_user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="No such running command")
    return {"stopped": run_id}
