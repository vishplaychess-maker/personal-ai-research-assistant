"""Additive authenticated durable-run API (Phase 1B.2R MP-3A).

    POST /api/runs                        submit a durable chat run (Idempotency-Key required)
    GET  /api/runs/{run_id}               inspect its status
    POST /api/runs/{run_id}/cancel        request cancellation (RunEngine service)
    GET  /api/runs/{run_id}/events        SSE replay / reconnect (Last-Event-ID / ?after_seq)

Feature-flagged (`run_public_api_enabled`, OFF by default) — every route calls
`require_run_public_api()` first: 404 when disabled, 503 when enabled without
`RUN_IDEMPOTENCY_HMAC_KEY`. NO execution happens here — submission only persists
and queues; cancellation only records intent; the SSE route only reads the
`run_events` ledger. Cross-user / cross-session / cross-family lookups return a
non-disclosing 404. CSRF (double-submit) on the mutations, consistent with the
other cookie-auth routes.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal, get_db
from app.models.models import ResearchSession
from app.run_engine import sse as _sse
from app.run_engine import submission as _sub
from app.run_engine.context import AuthContext, get_auth_context
from app.run_engine.engine import get_run, request_cancellation
from app.run_engine.errors import (
    IdempotencyKeyConflict,
    IdempotencyKeyTooLong,
    RunNotFound,
    SessionBindingMismatch,
    SessionBindingRequired,
)
from app.services.cookie_service import require_csrf

router = APIRouter(tags=["runs"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _gate() -> None:
    try:
        _sub.require_run_public_api()
    except _sub.RunFeatureDisabled:
        raise HTTPException(status_code=404, detail="Not found")
    except _sub.RunFeatureMisconfigured:
        raise HTTPException(status_code=503, detail="durable run API is not configured")


def _map_owner_error(exc: Exception) -> HTTPException:
    if isinstance(exc, SessionBindingRequired):
        return HTTPException(status_code=401, detail="fresh authentication required")
    # RunNotFound and SessionBindingMismatch are BOTH non-disclosing 404s
    return HTTPException(status_code=404, detail="run not found")


class SubmitChatRunBody(BaseModel):
    """No client-supplied identity / fingerprint / hash fields are accepted."""

    model_config = {"extra": "forbid"}

    session_id: int = Field(..., gt=0)
    message: str = Field(..., min_length=1, max_length=10_000)
    image_url: Optional[str] = Field(default=None, max_length=2_000_000)
    model: Optional[str] = Field(default=None, max_length=100)
    mode: Optional[str] = Field(default=None, max_length=32)


@router.post("/api/runs", status_code=201)
def submit_run_endpoint(
    body: SubmitChatRunBody,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=200),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    _gate()
    # Fail closed BEFORE any persistence: a chat_stream run the durable driver
    # cannot execute must never be queued (MP-3C1).
    try:
        _sub.assert_chat_stream_executable()
    except _sub.RunFeatureNotExecutable:
        raise HTTPException(status_code=503, detail="durable chat execution is not enabled")

    sess = (
        db.query(ResearchSession)
        .filter(ResearchSession.id == body.session_id,
                ResearchSession.user_id == ctx.user_id)
        .first()
    )
    if sess is None:
        raise HTTPException(status_code=404, detail="session not found")

    # Server-resolved provider: the user's active provider, else the deployment
    # default — resolved ONCE here and frozen into the sealed execution snapshot
    # + the semantic identity. The api key is NOT part of the submission.
    try:
        from app.services.settings_service import get_user_llm_config

        resolved_provider = (get_user_llm_config(db, ctx.user_id) or {}).get("provider")
    except Exception:  # noqa: BLE001
        resolved_provider = None
    resolved_provider = resolved_provider or settings.llm_provider

    try:
        submission = _sub.build_chat_submission(
            session_id=body.session_id, message=body.message, image_url=body.image_url,
            provider=resolved_provider, model=(body.model or sess.model), mode=body.mode,
            system_prompt=sess.system_prompt, options={},
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Phase 1B.2R MP-3C3D3A — `resolve_mode` above already validated `mode`
    # against the full durable-engine vocabulary (submission._ALLOWED_MODES:
    # chat/deep_research/multi_agent), but the worker can only EXECUTE
    # "chat" today. Without this check, a recognized-but-unimplemented mode
    # would be queued as a placeholder run no sweep ever advances (MP-1B's
    # advance_chat_runs only drives chat_stream generation, never discriminates
    # on sealed mode). Reuses the SAME canonical check /messages/stream uses —
    # no second, drifting allowlist — before ANY persistence.
    from app.run_engine import messages_stream as _ms

    reason = _ms.unsupported_mode_reason(submission.resolved_mode)
    if reason is not None:
        raise HTTPException(status_code=422,
                            detail="This mode is not supported by the durable run API yet.")

    try:
        run, created = _sub.create_chat_run(
            db, ctx, submission=submission, idempotency_key=idempotency_key,
        )
    except _sub.MultiAgentActiveRunLimitExceeded:
        raise HTTPException(status_code=429, detail="Too many active multi-agent runs.")
    except IdempotencyKeyConflict:
        raise HTTPException(status_code=409,
                            detail="Idempotency-Key reused for a different submission")
    except IdempotencyKeyTooLong:
        raise HTTPException(status_code=422, detail="Idempotency-Key too long")
    except (SessionBindingRequired, SessionBindingMismatch, RunNotFound) as exc:
        raise _map_owner_error(exc)

    return {"run_id": run.id, "status": run.status, "created": created}


@router.get("/api/runs/{run_id}")
def get_run_endpoint(
    run_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    _gate()
    try:
        run = get_run(db, ctx, run_id)
    except (SessionBindingRequired, SessionBindingMismatch, RunNotFound) as exc:
        raise _map_owner_error(exc)
    return {
        "run_id": run.id,
        "status": run.status,
        "status_reason": run.status_reason,
        "kind": run.kind,
        "last_event_seq": run.last_event_seq,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }


@router.post("/api/runs/{run_id}/cancel")
def cancel_run_endpoint(
    run_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    _gate()
    try:
        res = request_cancellation(db, ctx, run_id)
    except (SessionBindingRequired, SessionBindingMismatch, RunNotFound) as exc:
        raise _map_owner_error(exc)
    return {
        "run_id": run_id,
        "status": res.status,
        "cancellation_requested": bool(res.signal_created or res.already_requested),
        "already_terminal": bool(res.already_terminal),
    }


@router.get("/api/runs/{run_id}/events")
async def run_events_sse_endpoint(
    run_id: str,
    request: Request,
    after_seq: Optional[int] = Query(default=None, ge=0),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    _gate()
    try:
        get_run(db, ctx, run_id)  # ownership + family gate BEFORE streaming
    except (SessionBindingRequired, SessionBindingMismatch, RunNotFound) as exc:
        raise _map_owner_error(exc)

    cursor = int(after_seq) if after_seq is not None else _sse.parse_last_event_id(last_event_id)
    generator = _sse.stream_run_events(
        request, SessionLocal, ctx, run_id, after_seq=cursor,
    )
    return StreamingResponse(generator, media_type="text/event-stream",
                             headers=_SSE_HEADERS)
