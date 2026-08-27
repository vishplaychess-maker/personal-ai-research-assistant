import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Message, ResearchSession, MessageRole, User
from app.schemas.documents import (
    MessageResponse,
    ChatRequest,
    ChatResponse,
    MemoryExtractionStatus,
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


@router.post(
    "/api/sessions/{session_id}/messages",
    response_model=ChatResponse,
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

    1. Saves the user message
    2. Runs the research workflow (load_context → retrieve_context → generate_answer → save_output)
    3. Returns both messages with citations if documents were used

    This is the non-streaming endpoint, kept for backward compatibility.
    For streaming responses, use POST /api/sessions/{id}/messages/stream.
    """
    session = _get_session_or_404(db, session_id, current_user.id)

    # 1. Save user message
    user_msg = Message(
        session_id=session.id,
        role=MessageRole.user,
        content=payload.message,
    )
    db.add(user_msg)
    db.commit()
    db.refresh(user_msg)

    # 2. Run the LangGraph workflow (pass user_id so memory extraction
    #    is attributed to the authenticated user, not a hardcoded user 1)
    result = run_research_workflow(
        session_id=session.id,
        user_input=payload.message,
        db=db,
        user_id=current_user.id,
    )

    if result.get("error"):
        # If Ollama is unavailable, save a friendly error message
        assistant_msg = Message(
            session_id=session.id,
            role=MessageRole.assistant,
            content=f"⚠️ {result['error']}",
        )
        db.add(assistant_msg)
        db.commit()
        db.refresh(assistant_msg)
    else:
        # Assistant message was saved inside the workflow. The workflow
        # returns the message ID to avoid ORM session-detach issues
        # caused by subsequent db.commit() calls in the extract_memory node.
        assistant_msg_id = result.get("assistant_message_id")
        if assistant_msg_id is None:
            raise HTTPException(status_code=500, detail="Workflow did not produce an assistant message")
        assistant_msg = db.query(Message).filter(Message.id == assistant_msg_id).first()
        if assistant_msg is None:
            raise HTTPException(status_code=500, detail=f"Assistant message {assistant_msg_id} not found after workflow")

    # Refresh the session object (may have been expired by workflow commits)
    # before updating its timestamp, then commit.
    db.refresh(session)
    session.updated_at = __import__("datetime").datetime.utcnow()
    db.commit()

    # Build extraction status if the workflow ran memory extraction
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
):
    """
    Send a user message and stream the AI response token-by-token via SSE.

    This endpoint uses Server-Sent Events to deliver tokens as they are
    generated by Ollama. The event stream has this sequence:

      1. ``event: start`` — Stream begins, includes session metadata
      2. ``event: token`` — Zero or more token events (one per generated token)
      3. ``event: complete`` — Stream finished successfully; assistant message saved
         OR ``event: error`` — A non-recoverable error occurred
         OR ``event: cancelled`` — Client disconnected before completion

    The non-streaming POST /api/sessions/{id}/messages endpoint remains
    available for backward compatibility.

    Memory extraction runs as a background step after the stream completes.
    On cancellation, partial responses are NOT saved to the database.
    """
    # Validate session ID format
    if session_id <= 0:
        raise HTTPException(status_code=422, detail="Invalid session ID")

    # Validate message length
    if len(payload.message) > 10000:
        raise HTTPException(
            status_code=422,
            detail="Message exceeds maximum length of 10,000 characters",
        )

    # Synchronous setup: validate session, save user message, prepare context
    try:
        context = prepare_chat_context(
            session_id=session_id,
            user_input=payload.message,
            db=db,
            user_id=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

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
                    # The format is "event: complete\ndata: {...}\n\n"
                    lines = sse_event.strip().split("\n")
                    for line in lines:
                        if line.startswith("data: "):
                            data = json.loads(line[6:])
                            full_response_content = data.get("content", "")
                            break

                    # Save assistant message to database
                    try:
                        assistant_msg = save_assistant_message(
                            session_id=context.session_id,
                            content=full_response_content,
                            citations=context.citations,
                            db=db,
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
                            user_id=current_user.id,
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
