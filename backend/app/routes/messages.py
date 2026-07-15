from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Message, ResearchSession, MessageRole
from app.schemas.sessions import (
    MessageCreate,
    MessageResponse,
    ChatRequest,
    ChatResponse,
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
    return messages


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
    2. Runs the research workflow (load_context → generate_answer → save_output)
    3. Returns both messages
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
        # 3. Save assistant response (saved inside the workflow)
        assistant_msg = result["assistant_message"]

    # Update session timestamp
    from datetime import datetime
    session.updated_at = datetime.utcnow()
    db.commit()

    return ChatResponse(
        user_message=MessageResponse.model_validate(user_msg),
        assistant_message=MessageResponse.model_validate(assistant_msg),
    )
