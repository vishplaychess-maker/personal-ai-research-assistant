import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.services.auth_service import bearer_scheme

from app.database import get_db
from app.models.models import Message, ResearchSession, MessageRole, User
from app.schemas.documents import (
    MessageResponse,
    ChatRequest,
    ChatResponse,
    MemoryExtractionStatus,
    TerminalApprovalResponse,
)
from app.services.langgraph_workflow import run_research_workflow
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf
from app.services.streaming_service import (
    prepare_chat_context,
    stream_chat_response,
    save_assistant_message,
    run_memory_extraction,
    format_sse,
)
from app.config import settings


logger = logging.getLogger(__name__)
router = APIRouter(tags=["messages"])


def _get_session_or_404(db: Session, session_id: int, user_id: int) -> ResearchSession:
    session = db.query(ResearchSession).filter(
        ResearchSession.id == session_id,
        ResearchSession.user_id == user_id,
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return session


# ── Terminal approval helpers ─────────────────────────────


def _check_pending_approval(db: Session, session_id: int) -> Optional[dict]:
    """Check if there's a pending terminal approval for this session.

    Looks at the most recent assistant message for the approval marker
    stored in the citations JSON field.
    """
    if not settings.enable_terminal_tool:
        return None

    last_assistant = (
        db.query(Message)
        .filter(
            Message.session_id == session_id,
            Message.role == MessageRole.assistant,
        )
        .order_by(Message.created_at.desc())
        .first()
    )
    if not last_assistant or not last_assistant.citations:
        return None

    try:
        meta = json.loads(last_assistant.citations)
        if isinstance(meta, dict) and meta.get("pending_approval"):
            return meta
    except (json.JSONDecodeError, TypeError):
        pass

    return None


def _is_approval_response(message: str) -> Optional[bool]:
    """Check if a user message is an approval/denial response.

    Returns True for approval, False for denial, None if not recognized.
    """
    normalized = message.strip().lower()
    if normalized in ("yes", "y", "approve", "approved", "ok"):
        return True
    if normalized in ("no", "n", "deny", "denied", "cancel"):
        return False
    return None


# ── List messages ─────────────────────────────────────────


@router.get(
    "/api/sessions/{session_id}/messages",
    response_model=List[MessageResponse],
)
def list_messages(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all messages for a session, oldest first (scoped to current user)."""
    _get_session_or_404(db, session_id, current_user.id)
    messages = (
        db.query(Message)
        .filter(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
        .all()
    )
    return [MessageResponse.model_validate(m) for m in messages]


# ── Create message (non-streaming) ───────────────────────


@router.post(
    "/api/sessions/{session_id}/messages",
)
def create_message(
    session_id: int,
    payload: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """
    Send a user message and get an AI response via the LangGraph workflow.

    Supports terminal command approval flow:
      - If a previous assistant message has a pending approval, the user's
        message is treated as an approval/denial response.
      - The workflow is resumed with the user's decision.
    """
    session = _get_session_or_404(db, session_id, current_user.id)

    # Check for pending terminal approval
    pending = _check_pending_approval(db, session.id)
    approval_decision = _is_approval_response(payload.message) if pending else None

    user_msg = None  # Will be set for normal (non-approval) messages

    if pending and approval_decision is not None:
        # ── Resume workflow with approval/denial ────────────
        original_user_input = pending.get("original_user_input", payload.message)
        resume_value = "yes" if approval_decision else "no"

        logger.info(
            "Resuming workflow for session %d with approval=%s",
            session.id, resume_value,
        )

        result = run_research_workflow(
            session_id=session.id,
            user_input=original_user_input,
            db=db,
            user_id=current_user.id,
            resume_from=resume_value,
        )

        # Save the user's approval/denial message
        user_msg = Message(
            session_id=session.id,
            role=MessageRole.user,
            content=payload.message,
        )
        db.add(user_msg)
        db.commit()
        db.refresh(user_msg)

    elif pending and approval_decision is None:
        # ── Non-approval message while approval pending ──────
        # Treat as denial
        original_user_input = pending.get("original_user_input", payload.message)
        logger.info(
            "Non-approval message in session %d, treating as denial",
            session.id,
        )

        user_msg = Message(
            session_id=session.id,
            role=MessageRole.user,
            content=payload.message,
        )
        db.add(user_msg)
        db.commit()
        db.refresh(user_msg)

        result = run_research_workflow(
            session_id=session.id,
            user_input=original_user_input,
            db=db,
            user_id=current_user.id,
            resume_from="no",
        )

    else:
        # ── Normal message flow ────────────────────────────
        user_msg = Message(
            session_id=session.id,
            role=MessageRole.user,
            content=payload.message,
            image_url=payload.image_url,
        )
        db.add(user_msg)
        db.commit()
        db.refresh(user_msg)

        result = run_research_workflow(
            session_id=session.id,
            user_input=payload.message,
            db=db,
            user_id=current_user.id,
            image_url=payload.image_url,
        )

    # ── Handle approval response ──────────────────────────
    if result.get("pending_approval"):
        # The graph interrupted for terminal approval.
        # The assistant message with the LLM's explanation was already saved
        # inside generate_answer. Now update its citations with approval
        # metadata so the next request can detect and resume it.
        assistant_msg_id = result.get("assistant_message_id")
        if assistant_msg_id:
            assistant_msg = db.query(Message).filter(
                Message.id == assistant_msg_id
            ).first()
            if assistant_msg:
                approval_meta = json.dumps({
                    "pending_approval": True,
                    "pending_command": result.get("pending_command", ""),
                    "original_user_input": payload.message,
                })
                assistant_msg.citations = approval_meta
                db.commit()
                db.refresh(assistant_msg)

        return TerminalApprovalResponse(
            pending_command=result.get("pending_command", ""),
            approval_message=result.get("pending_approval", ""),
            session_id=session.id,
        )

    # ── Handle errors ─────────────────────────────────────
    if result.get("error"):
        assistant_msg = Message(
            session_id=session.id,
            role=MessageRole.assistant,
            content=f"\u26a0\ufe0f {result['error']}",
        )
        db.add(assistant_msg)
        db.commit()
        db.refresh(assistant_msg)
    else:
        assistant_msg_id = result.get("assistant_message_id")
        if assistant_msg_id is None:
            raise HTTPException(
                status_code=500,
                detail="Workflow did not produce an assistant message",
            )
        assistant_msg = db.query(Message).filter(
            Message.id == assistant_msg_id
        ).first()
        if assistant_msg is None:
            raise HTTPException(
                status_code=500,
                detail=f"Assistant message {assistant_msg_id} not found after workflow",
            )

    # Refresh session timestamp
    db.refresh(session)
    session.updated_at = __import__("datetime").datetime.utcnow()
    db.commit()

    # Build extraction status
    extract_raw = result.get("extraction_result")
    extraction_status: Optional[MemoryExtractionStatus] = None
    if extract_raw is not None:
        extraction_status = MemoryExtractionStatus(
            saved=extract_raw.get("saved", False),
            memory_id=extract_raw.get("memory_id"),
            reason=extract_raw.get("reason", "unknown"),
            content=extract_raw.get("content"),
            category=extract_raw.get("category"),
        )

    return ChatResponse(
        user_message=MessageResponse.model_validate(user_msg),
        assistant_message=MessageResponse.model_validate(assistant_msg),
        citations=result.get("citations", []),
        sources_used=result.get("sources_used", False),
        memories_used=result.get("memories_used", False),
        memory_extraction=extraction_status,
    )


# ── SSE Streaming Endpoint (Phase 5A) ────────────────────


@router.post(
    "/api/sessions/{session_id}/messages/stream",
    response_class=StreamingResponse,
)
async def stream_chat(
    session_id: int,
    payload: ChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
    _bearer: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    """
    Send a user message and stream the AI response token-by-token via SSE.

    This endpoint uses Server-Sent Events to deliver tokens as they are
    generated by Ollama. The event stream has this sequence:

      1. ``event: start`` -- Stream begins, includes session metadata
      2. ``event: token`` -- Zero or more token events (one per generated token)
      3. ``event: complete`` -- Stream finished successfully; assistant message saved
         OR ``event: error`` -- A non-recoverable error occurred
         OR ``event: cancelled`` -- Client disconnected before completion

    The non-streaming POST /api/sessions/{id}/messages endpoint remains
    available for backward compatibility.

    Memory extraction runs as a background step after the stream completes.
    On cancellation, partial responses are NOT saved to the database.

    Phase 1B.2R MP-3C3B: when ``run_messages_stream_durable_enabled`` is on, this
    route instead submits/reuses a durable ``chat_stream`` run and streams its
    persisted events adapted into the exact wire format below. When the flag is
    off (default) the code below is the unchanged legacy path.
    """
    # Phase 1B.2R MP-3C3D3A — validate/canonicalize the request-level mode
    # ONCE, before either branch, so an invalid mode fails identically (422,
    # before any persistence) regardless of the durable cutover flag. The
    # ONE authoritative allowlist/normalization lives in
    # app.run_engine.submission.validate_request_mode — reused here, not
    # duplicated. Omitted (None) is intentionally distinct from explicit
    # "chat" (see that function's docstring).
    from app.run_engine.submission import validate_request_mode
    try:
        resolved_mode = validate_request_mode(payload.mode)
    except ValueError:
        raise HTTPException(status_code=422, detail="Unrecognized request mode.")

    if settings.run_messages_stream_durable_enabled:
        return await _stream_chat_durable(
            session_id, payload, request, db, current_user, _bearer,
            resolved_mode=resolved_mode,
        )

    # Validate session ID format
    if session_id <= 0:
        raise HTTPException(status_code=422, detail="Invalid session ID")

    # Validate message length
    if len(payload.message) > 10000:
        raise HTTPException(
            status_code=422,
            detail="Message exceeds maximum length of 10,000 characters",
        )

    # Phase 1B.2R MP-3C3D3A — legacy capability gate: an explicitly requested
    # mode this deployment has not enabled must fail BEFORE persistence.
    # prepare_chat_context() below saves the user Message immediately (its
    # own step 2), so this check must run first, never inside it.
    if resolved_mode == "deep_research" and not settings.enable_deep_research:
        raise HTTPException(status_code=422,
                            detail="deep_research mode is not enabled on this deployment.")
    if resolved_mode == "multi_agent" and not settings.enable_multi_agent:
        raise HTTPException(status_code=422,
                            detail="multi_agent mode is not enabled on this deployment.")

    # Synchronous setup: validate session, save user message, prepare context
    try:
        context = prepare_chat_context(
            session_id=session_id,
            user_input=payload.message,
            db=db,
            user_id=current_user.id,
            image_url=payload.image_url,
            mode=resolved_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    # Capture user_id before async generator to avoid DetachedInstanceError
    user_id = current_user.id

    async def event_generator():
        """Async generator producing SSE events for the streaming response."""
        cancelled = False
        full_response_content = ""
        save_message_id = None

        try:
            async for sse_event in stream_chat_response(context):
                # Check for client disconnection between events
                if await request.is_disconnected():
                    cancelled = True
                    yield format_sse("cancelled", {
                        "detail": "Client disconnected",
                    })
                    return

                # Intercept complete events to save the message
                if sse_event.startswith("event: complete"):
                    # Parse the complete event data to get the content
                    lines = sse_event.strip().split("\n")
                    confidence = None
                    confidence_reason = None
                    for line in lines:
                        if line.startswith("data: "):
                            data = json.loads(line[6:])
                            full_response_content = data.get("content", "")
                            confidence = data.get("confidence")
                            confidence_reason = data.get("confidence_reason")
                            break

                    # save_memory tool: persist [SAVE_MEMORY: ...] markers the
                    # LLM emitted during streaming (strip them from saved text).
                    from app.tools.memory_tool import (
                        process_memory_markers,
                        process_use_memory_markers,
                    )
                    cleaned_content, saved_count = process_memory_markers(
                        full_response_content, db, user_id, session_id
                    )
                    if saved_count:
                        logger.info(
                            "save_memory tool saved %d memory(ies) for user %s",
                            saved_count, current_user.id,
                        )
                    full_response_content = cleaned_content

                    # use_memory tool (Phase 4): resolve [USE_MEMORY: <query>]
                    # recall markers (inject memories inline, strip markers).
                    cleaned_content, used_count = process_use_memory_markers(
                        full_response_content, db, user_id, session_id
                    )
                    if used_count:
                        logger.info(
                            "use_memory tool resolved %d marker(s) for user %s",
                            used_count, current_user.id,
                        )
                    full_response_content = cleaned_content

                    # save_directive tool (F6 Cap 3): persist [SAVE_DIRECTIVE: ...]
                    # markers the LLM emitted during streaming (strip them too).
                    from app.tools.directive_tool import process_directive_markers
                    cleaned_content, saved_directives = process_directive_markers(
                        full_response_content, db, user_id
                    )
                    if saved_directives:
                        logger.info(
                            "save_directive tool saved %d directive(s) for user %s",
                            saved_directives, current_user.id,
                        )
                    full_response_content = cleaned_content

                    # Save assistant message to database
                    try:
                        assistant_msg = save_assistant_message(
                            session_id=context.session_id,
                            content=full_response_content,
                            citations=context.citations,
                            db=db,
                            confidence=confidence,
                            confidence_reason=confidence_reason,
                        )
                        save_message_id = assistant_msg.id
                    except Exception as exc:
                        logger.error("Failed to save assistant message: %s", exc)
                        yield format_sse("error", {
                            "code": "SAVE_ERROR",
                            "detail": "Failed to save the assistant message.",
                        })
                        return

                    # Yield complete event with actual message_id
                    yield format_sse("complete", {
                        "message_id": save_message_id,
                        "citations": context.citations,
                        "sources_used": context.sources_used,
                        "memories_used": context.memories_used,
                    })

                    # Run background memory extraction (best-effort)
                    try:
                        run_memory_extraction(
                            user_input=payload.message,
                            db=db,
                            session_id=session_id,
                            user_id=user_id,
                        )
                    except Exception as exc:
                        logger.warning("Background memory extraction failed: %s", exc)

                    return

                # Pass through all other events (start, token, error)
                yield sse_event

        except Exception as exc:
            logger.error("Unhandled streaming error: %s", exc, exc_info=True)
            if not cancelled:
                yield format_sse("error", {
                    "code": "INTERNAL_ERROR",
                    "detail": "An unexpected error occurred.",
                })

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


_LEGACY_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


async def _stream_chat_durable(
    session_id: int,
    payload: "ChatRequest",
    request: Request,
    db: Session,
    current_user: User,
    bearer: Optional[HTTPAuthorizationCredentials],
    *,
    resolved_mode: Optional[str] = None,
) -> StreamingResponse:
    """Phase 1B.2R MP-3C3B durable cutover. Fails closed BEFORE any persistence
    unless every dependency is satisfied; submits (or idempotently reuses) a
    durable ``chat_stream`` run — NO executor is called here — and streams its
    persisted events in the legacy wire format. Reached only when the cutover
    flag is on.

    ``resolved_mode`` (MP-3C3D3A) is the caller's already-validated request
    mode — ``None`` when omitted (resolves to ``DURABLE_STREAM_MODE``, i.e.
    "chat", preserving MP-3C3D1 behavior exactly), or one of the canonical
    mode strings. ``deep_research``/``multi_agent`` are recognized but not
    yet executable durably and are rejected below, before any persistence."""
    from app.run_engine import messages_stream as _ms
    from app.run_engine.context import build_auth_context
    from app.services.auth_service import decode_access_token

    supervisor = getattr(request.app.state, "run_worker_supervisor", None)
    try:
        _ms.assert_durable_stream_ready(supervisor)
    except _ms.DurableStreamError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.detail)

    if session_id <= 0:
        raise HTTPException(status_code=422, detail="Invalid session ID")
    if len(payload.message) > 10000:
        raise HTTPException(status_code=422,
                            detail="Message exceeds maximum length of 10,000 characters")

    reason = _ms.unsupported_mode_code(
        has_image=payload.image_url is not None,
        mode=(resolved_mode or _ms.DURABLE_STREAM_MODE),
    )
    if reason is not None:
        logger.info("durable /messages/stream refused unsupported mode: %s", reason)
        raise HTTPException(
            status_code=422,
            detail="Durable streaming currently supports plain chat and "
                   "tool-approval conversations only.",
        )

    idem = request.headers.get("idempotency-key")
    if not _ms.valid_idempotency_key(idem):
        raise HTTPException(status_code=422,
                            detail="A valid Idempotency-Key header is required.")

    if bearer is None:
        raise HTTPException(status_code=401,
                            detail="A bearer access token is required for durable streaming.")
    try:
        claims = decode_access_token(bearer.credentials)
        ctx = build_auth_context(db, user=current_user, token_payload=claims)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid access token.")

    if _ms.submit_rate_limited(current_user.id):
        raise HTTPException(status_code=429,
                            detail="Too many durable stream submissions; please slow down.")

    try:
        run, _created = _ms.resolve_and_submit(
            db, ctx, session_id=session_id, message=payload.message,
            idempotency_key=idem, mode=resolved_mode,
        )
    except _ms.DurableStreamError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.detail)

    uid = current_user.id
    if not _ms.acquire_stream_slot(uid):
        raise HTTPException(status_code=429,
                            detail="Too many concurrent durable streams for this account.")

    cursor = _ms.parse_cursor(request.headers.get("last-event-id"),
                              request.query_params.get("after_seq"))

    async def _durable_gen():
        try:
            async for frame in _ms.stream_legacy_wire(
                request, SessionLocal, ctx, run.id, after_seq=cursor,
            ):
                yield frame
        finally:
            _ms.release_stream_slot(uid)

    return StreamingResponse(
        _durable_gen(),
        media_type="text/event-stream",
        headers={**_LEGACY_SSE_HEADERS, "X-Run-Id": run.id},
    )
