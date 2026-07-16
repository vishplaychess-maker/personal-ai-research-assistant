from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Message, ResearchSession, MessageRole
from app.schemas.documents import (
    MessageResponse,
    ChatRequest,
    ChatResponse,
    MemoryExtractionStatus,
)
from app.services.langgraph_workflow import run_research_workflow

router = APIRouter(tags=["messages"])


def _get_session_or_404(db: Session, session_id: int) -> ResearchSession:
    session = db.query(ResearchSession).filter(ResearchSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return session


@router.get(
    "/api/sessions/{session_id}/messages",
    response_model=List[MessageResponse],
)
def list_messages(session_id: int, db: Session = Depends(get_db)):
    """Get all messages for a session, oldest first."""
    _get_session_or_404(db, session_id)
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
):
    """
    Send a user message and get an AI response via the LangGraph workflow.

    1. Saves the user message
    2. Runs the research workflow (load_context → retrieve_context → generate_answer → save_output)
    3. Returns both messages with citations if documents were used
    """
    session = _get_session_or_404(db, session_id)

    # 1. Save user message
    user_msg = Message(
        session_id=session.id,
        role=MessageRole.user,
        content=payload.message,
    )
    db.add(user_msg)
    db.commit()
    db.refresh(user_msg)

    # 2. Run the LangGraph workflow
    result = run_research_workflow(
        session_id=session.id,
        user_input=payload.message,
        db=db,
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
