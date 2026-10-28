"""Desktop automation routes (opt-in; sandboxed file + command surface).

Every endpoint requires an authenticated session and CSRF on the mutating ones,
and every one answers 503 while ``DESKTOP_AUTOMATION_ENABLED`` is false. A
sandbox refusal is a 403 carrying the policy's own bounded copy — never a stack
trace and never the content that was being read or run.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.models import User
from app.services.auth_service import get_current_user
from app.services.capability_preferences import get_permission_level
from app.services.cookie_service import require_csrf
from app.services.desktop_automation import (
    ALLOWED_PROGRAMS,
    BLOCKED_COMMANDS,
    DEFAULT_PERMISSION_LEVEL,
    INTERNAL_COMMANDS,
    DesktopAutomationService,
    PermissionLevel,
    SandboxViolationError,
)

router = APIRouter(prefix="/api/desktop", tags=["desktop"])

logger = logging.getLogger(__name__)


# ── schemas ──────────────────────────────────


class DesktopStatusResponse(BaseModel):
    enabled: bool
    workspace: str = ""
    commands: List[str] = []
    internal_commands: List[str] = []
    blocked_commands: List[str] = []
    timeout_s: int = 30
    permission_level: str = DEFAULT_PERMISSION_LEVEL.value


class TreeEntry(BaseModel):
    path: str
    name: str
    is_dir: bool
    size: int = 0
    depth: int = 0


class TreeResponse(BaseModel):
    root: str
    entries: List[TreeEntry] = []


class ReadRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class ReadResponse(BaseModel):
    path: str
    content: str


class WriteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    #: Bounded by the service (5 MiB); the schema only stops absurd payloads.
    content: str = Field(default="", max_length=8_388_608)


class WriteResponse(BaseModel):
    path: str
    ok: bool
    bytes_written: int


class RunRequest(BaseModel):
    command: str = Field(min_length=1, max_length=64)
    args: List[str] = []
    timeout_s: Optional[int] = Field(default=None, ge=1, le=300)


class RunResponse(BaseModel):
    command: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    duration_ms: int = 0
    timed_out: bool = False


class ScreenshotResponse(BaseModel):
    status: str
    description: Optional[str] = None
    notice: str = ""
    #: Size only. The image itself never leaves this process except into the
    #: vision pipeline, and is never written to disk.
    image_bytes: int = 0


# ── helpers ──────────────────────────────────


def _gate() -> None:
    """Fail closed while the feature is off."""
    if not settings.desktop_automation_enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "Desktop automation is disabled. Set DESKTOP_AUTOMATION_ENABLED=true "
                "and restart to enable it."
            ),
        )


def _service(
    permission_level: PermissionLevel = DEFAULT_PERMISSION_LEVEL,
) -> DesktopAutomationService:
    """Built per request; tests monkeypatch this to inject OS seams."""
    _gate()
    return DesktopAutomationService(
        settings.workspace_dir,
        timeout_s=int(settings.command_timeout_s),
        permission_level=permission_level,
    )


def _refused(exc: SandboxViolationError) -> HTTPException:
    """A policy refusal is a 403 with the policy's own copy.

    ``CommandBlockedError`` is a subclass, so both the path sandbox and the
    command whitelist land here; the message already says which rule fired.
    """
    return HTTPException(status_code=403, detail=str(exc))


# ── endpoints ────────────────────────────────


@router.get("/status", response_model=DesktopStatusResponse)
def desktop_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DesktopStatusResponse:
    """Whether the feature is armed, and the policy the UI renders."""
    enabled = bool(settings.desktop_automation_enabled)
    return DesktopStatusResponse(
        enabled=enabled,
        workspace=settings.workspace_dir if enabled else "",
        commands=list(ALLOWED_PROGRAMS),
        internal_commands=list(INTERNAL_COMMANDS),
        blocked_commands=list(BLOCKED_COMMANDS),
        timeout_s=int(settings.command_timeout_s),
        permission_level=get_permission_level(db, current_user.id).value,
    )


@router.get("/tree", response_model=TreeResponse)
def desktop_tree(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TreeResponse:
    """Bounded listing of the workspace for the file-tree panel."""
    service = _service(get_permission_level(db, current_user.id))
    try:
        entries = service.list_workspace()
    except SandboxViolationError as exc:
        raise _refused(exc)
    return TreeResponse(
        root=str(service.workspace),
        entries=[TreeEntry(**entry) for entry in entries],
    )


@router.post("/read", response_model=ReadResponse)
def desktop_read(
    payload: ReadRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> ReadResponse:
    service = _service(get_permission_level(db, current_user.id))
    try:
        content = service.read_file(payload.path)
    except SandboxViolationError as exc:
        raise _refused(exc)
    return ReadResponse(path=payload.path, content=content)


@router.post("/write", response_model=WriteResponse)
def desktop_write(
    payload: WriteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> WriteResponse:
    service = _service(get_permission_level(db, current_user.id))
    try:
        ok = service.write_file(payload.path, payload.content)
    except SandboxViolationError as exc:
        raise _refused(exc)
    return WriteResponse(
        path=payload.path, ok=bool(ok), bytes_written=len(payload.content.encode("utf-8"))
    )


@router.post("/run", response_model=RunResponse)
def desktop_run(
    payload: RunRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> RunResponse:
    service = _service(get_permission_level(db, current_user.id))
    try:
        result = service.run_command(
            payload.command, payload.args, timeout=payload.timeout_s
        )
    except SandboxViolationError as exc:
        raise _refused(exc)
    return RunResponse(
        command=result.command,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        truncated=result.truncated,
        duration_ms=result.duration_ms,
        timed_out=result.timed_out,
    )


@router.post("/screenshot", response_model=ScreenshotResponse)
def desktop_screenshot(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> ScreenshotResponse:
    """Capture the screen and describe it with the existing vision pipeline.

    The bytes exist only in memory: they are handed straight to
    ``local_vision_service.describe_image`` (local Ollama, or Gemini when that
    is the configured vision provider) and are never written to disk or
    returned to the client.
    """
    service = _service(get_permission_level(db, current_user.id))
    try:
        raw = service.take_screenshot()
    except SandboxViolationError as exc:
        raise _refused(exc)

    from app.services.local_vision_service import (
        FAILED_NOTICE,
        STATUS_FAILED,
        describe_image,
    )

    try:
        result = describe_image(raw)
    except Exception as exc:  # noqa: BLE001 - a vision failure is never a 500
        logger.warning("desktop: vision failed (%s)", type(exc).__name__)
        return ScreenshotResponse(
            status=STATUS_FAILED, notice=FAILED_NOTICE, image_bytes=len(raw)
        )
    return ScreenshotResponse(
        status=result.status,
        description=result.description,
        notice=result.notice,
        image_bytes=len(raw),
    )


__all__ = ["router", "DesktopStatusResponse"]
