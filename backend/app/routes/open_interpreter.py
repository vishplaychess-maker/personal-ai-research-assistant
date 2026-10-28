"""Open Interpreter routes — LLM-powered code execution (optional feature).

GET  /api/interpreter/status   — availability probe + per-user enable state
POST /api/interpreter/execute  — natural-language prompt -> code
POST /api/interpreter/run      — execute an EXPLICIT user-approved code block

Permission mapping (existing Ask/Auto/Full system):
* ``ask_every_time`` (fail-safe default): ``/execute`` NEVER runs code — it
  returns the proposed code for review. Running it requires the explicit
  ``/run`` call with the exact reviewed code.
* ``auto_approve`` / ``full_access``: ``/execute`` runs the agent loop
  automatically under a hard 60s timeout.

Access is fail-closed: the request is rejected with 403 unless the CURRENT
user enabled ``open_interpreter`` in their SkillPreference. When the optional
``interpreter`` package is missing, the endpoints answer 503 with install
instructions instead of crashing.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import app.services.open_interpreter_service as open_interpreter_service
from app.database import get_db
from app.models.models import User
from app.services.auth_service import get_current_user
from app.services.capability_preferences import (
    get_capability_config,
    is_capability_enabled,
    parse_permission_level,
)
from app.services.cookie_service import require_csrf

router = APIRouter(prefix="/api/interpreter", tags=["open-interpreter"])

CAPABILITY_ID = "open_interpreter"

#: Levels that are allowed to auto-execute; anything else fails safe to ask.
_ALLOWED_LEVELS = {"ask_every_time", "auto_approve", "full_access"}

#: Languages accepted by /run (Open Interpreter's core computer languages).
_ALLOWED_LANGUAGES = {"python", "javascript", "shell", "bash", "powershell", "r"}


class InterpreterExecuteRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)


class InterpreterRunRequest(BaseModel):
    code: str = Field(min_length=1, max_length=40000)
    language: str = Field(default="python", max_length=40)


class InterpreterStatusResponse(BaseModel):
    available: bool
    enabled: bool
    permission_level: str
    version: Optional[str] = None
    install_hint: Optional[str] = None


def _resolve_permission_level(db: Session, user_id: int) -> str:
    """Stored per-user level, failing safe to ``ask_every_time``."""
    stored = get_capability_config(db, user_id, CAPABILITY_ID).get("permission_level")
    parsed = parse_permission_level(stored)
    return parsed.value if parsed is not None else "ask_every_time"


def _require_enabled(db: Session, user_id: int) -> None:
    if not is_capability_enabled(db, user_id, CAPABILITY_ID):
        raise HTTPException(
            status_code=403,
            detail=(
                "Enable Open Interpreter in Settings > Skills before using it."
            ),
        )


def _service_result_or_raise(result: Dict[str, Any]) -> Dict[str, Any]:
    status = result.get("status")
    if status == "unavailable":
        raise HTTPException(status_code=503, detail=result.get("message"))
    if status == "timeout":
        raise HTTPException(status_code=504, detail=result.get("message"))
    if status == "error":
        raise HTTPException(status_code=500, detail=result.get("message"))
    return result


@router.get("/status", response_model=InterpreterStatusResponse)
def interpreter_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Availability + this user's enable state and permission level."""
    service_status = open_interpreter_service.interpreter_status()
    return InterpreterStatusResponse(
        available=bool(service_status["available"]),
        enabled=is_capability_enabled(db, current_user.id, CAPABILITY_ID),
        permission_level=_resolve_permission_level(db, current_user.id),
        version=service_status.get("version"),
        install_hint=service_status.get("install_hint"),
    )


@router.post("/execute")
async def execute_prompt(
    payload: InterpreterExecuteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Natural-language prompt -> proposed code (Ask) or executed result
    (Auto/Full), per the user's stored permission level."""
    _require_enabled(db, current_user.id)
    level = _resolve_permission_level(db, current_user.id)
    result = await asyncio.to_thread(
        open_interpreter_service.execute_code,
        payload.prompt,
        current_user.id,
        level,
    )
    return _service_result_or_raise(result)


@router.post("/run")
async def run_approved_code(
    payload: InterpreterRunRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Execute an explicitly reviewed code block (the Ask-mode approval path).

    Only the exact posted code runs — the user saw it before this call.
    """
    _require_enabled(db, current_user.id)
    level = _resolve_permission_level(db, current_user.id)
    if payload.language not in _ALLOWED_LANGUAGES:
        raise HTTPException(status_code=422, detail="Unsupported language")
    result = await asyncio.to_thread(
        open_interpreter_service.run_approved_code,
        payload.code,
        payload.language,
        current_user.id,
        level,
    )
    return _service_result_or_raise(result)
