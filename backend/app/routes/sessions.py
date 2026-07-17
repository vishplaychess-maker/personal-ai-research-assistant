import os
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Document, ResearchSession, User
from app.schemas.sessions import (
    SessionCreate,
    SessionUpdate,
    SessionResponse,
    ModelUpdate,
    SystemPromptUpdate,
    SystemPromptResponse,
    SessionModelResponse,
)
from app.services.chromadb_client import delete_chunks, delete_collection
from app.config import settings
from app.services.auth_service import get_optional_user

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _get_session_or_404(
    db: Session,
    session_id: int,
    user_id: int | None = None,
) -> ResearchSession:
    """
    Get a session by ID, optionally scoped to a specific user.

    If user_id is provided, the session must belong to that user;
    otherwise returns 404 to prevent user enumeration.
    """
    query = db.query(ResearchSession).filter(ResearchSession.id == session_id)
    if user_id is not None:
        query = query.filter(ResearchSession.user_id == user_id)
    session = query.first()
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return session


def _cleanup_session_artifacts(db: Session, session_id: int):
    """
    Clean up uploaded files and ChromaDB vectors for a session's documents.
    Called before deleting the session itself.
    """
    docs = db.query(Document).filter(Document.session_id == session_id).all()
    all_chroma_ids = []
    for doc in docs:
        # Delete uploaded file
        if doc.file_path and os.path.exists(doc.file_path):
            try:
                os.remove(doc.file_path)
            except OSError:
                pass
        # Collect ChromaDB IDs
        for chunk in doc.chunks:
            if chunk.chroma_id:
                all_chroma_ids.append(chunk.chroma_id)

    # Delete ChromaDB vectors
    if all_chroma_ids:
        try:
            delete_chunks(session_id, all_chroma_ids)
        except Exception:
            pass

    # Delete the entire collection
    try:
        delete_collection(session_id)
    except Exception:
        pass


@router.post("", response_model=SessionResponse, status_code=201)
def create_session(
    payload: SessionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user),
):
    """Create a new research session."""
    session = ResearchSession(title=payload.title, user_id=current_user.id)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.get("", response_model=List[SessionResponse])
def list_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user),
):
    """List all sessions for the current user, newest first."""
    sessions = (
        db.query(ResearchSession)
        .filter(ResearchSession.user_id == current_user.id)
        .order_by(ResearchSession.updated_at.desc())
        .all()
    )
    return sessions


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user),
):
    """Get a single session by ID (scoped to current user)."""
    return _get_session_or_404(db, session_id, user_id=current_user.id)


@router.patch("/{session_id}", response_model=SessionResponse)
def update_session(
    session_id: int,
    payload: SessionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user),
):
    """Rename a session (scoped to current user)."""
    session = _get_session_or_404(db, session_id, user_id=current_user.id)
    session.title = payload.title
    db.commit()
    db.refresh(session)
    return session


@router.delete("/{session_id}", status_code=204)
def delete_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user),
):
    """Delete a session and all its messages/documents (scoped to current user)."""
    session = _get_session_or_404(db, session_id, user_id=current_user.id)
    # Clean up uploaded files and ChromaDB vectors before deleting
    _cleanup_session_artifacts(db, session_id)
    db.delete(session)
    db.commit()
    return None


# ── Model selection ────────────────────────────────────────


@router.patch("/{session_id}/model", response_model=SessionModelResponse)
def update_session_model(
    session_id: int,
    payload: ModelUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user),
):
    """Set the model for a session (or null to use default), scoped to current user."""
    session = _get_session_or_404(db, session_id, user_id=current_user.id)
    session.model = payload.model
    db.commit()
    db.refresh(session)
    return SessionModelResponse(id=session.id, model=session.model)


# ── System prompt ──────────────────────────────────────────


DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful research assistant. Answer the user's questions "
    "clearly and concisely."
)


@router.get("/{session_id}/system-prompt", response_model=SystemPromptResponse)
def get_system_prompt(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user),
):
    """Get the system prompt for a session (scoped to current user)."""
    session = _get_session_or_404(db, session_id, user_id=current_user.id)
    if session.system_prompt:
        return SystemPromptResponse(
            system_prompt=session.system_prompt,
            using_default=False,
        )
    return SystemPromptResponse(
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        using_default=True,
    )


@router.patch("/{session_id}/system-prompt", response_model=SystemPromptResponse)
def update_system_prompt(
    session_id: int,
    payload: SystemPromptUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user),
):
    """Update the system prompt for a session (or null to reset to default)."""
    session = _get_session_or_404(db, session_id, user_id=current_user.id)
    session.system_prompt = payload.system_prompt
    db.commit()
    db.refresh(session)

    if session.system_prompt:
        return SystemPromptResponse(
            system_prompt=session.system_prompt,
            using_default=False,
        )
    return SystemPromptResponse(
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        using_default=True,
    )


# ── Model selection ────────────────────────────────────────


@router.patch("/{session_id}/model", response_model=SessionModelResponse)
def update_session_model(
    session_id: int,
    payload: ModelUpdate,
    db: Session = Depends(get_db),
):
    """Set the model for a session (or null to use default)."""
    session = _get_session_or_404(db, session_id)
    session.model = payload.model
    db.commit()
    db.refresh(session)
    return SessionModelResponse(id=session.id, model=session.model)


# ── System prompt ──────────────────────────────────────────


DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful research assistant. Answer the user's questions "
    "clearly and concisely."
)


@router.get("/{session_id}/system-prompt", response_model=SystemPromptResponse)
def get_system_prompt(
    session_id: int,
    db: Session = Depends(get_db),
):
    """Get the system prompt for a session.

    Returns the custom prompt if set, or indicates the default is being used.
    """
    session = _get_session_or_404(db, session_id)
    if session.system_prompt:
        return SystemPromptResponse(
            system_prompt=session.system_prompt,
            using_default=False,
        )
    return SystemPromptResponse(
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        using_default=True,
    )


@router.patch("/{session_id}/system-prompt", response_model=SystemPromptResponse)
def update_system_prompt(
    session_id: int,
    payload: SystemPromptUpdate,
    db: Session = Depends(get_db),
):
    """Update the system prompt for a session (or null to reset to default)."""
    session = _get_session_or_404(db, session_id)
    session.system_prompt = payload.system_prompt
    db.commit()
    db.refresh(session)

    if session.system_prompt:
        return SystemPromptResponse(
            system_prompt=session.system_prompt,
            using_default=False,
        )
    return SystemPromptResponse(
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        using_default=True,
    )
