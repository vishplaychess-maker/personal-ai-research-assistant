from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import ResearchSession, User
from app.schemas.sessions import (
    SessionCreate,
    SessionUpdate,
    SessionResponse,
)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

DEFAULT_USER_ID = 1


def _get_session_or_404(db: Session, session_id: int) -> ResearchSession:
    session = db.query(ResearchSession).filter(ResearchSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return session


@router.post("", response_model=SessionResponse, status_code=201)
def create_session(payload: SessionCreate, db: Session = Depends(get_db)):
    """Create a new research session."""
    # Ensure the default user exists
    user = db.query(User).filter(User.id == DEFAULT_USER_ID).first()
    if not user:
        raise HTTPException(status_code=500, detail="Default user not found")

    session = ResearchSession(title=payload.title, user_id=DEFAULT_USER_ID)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.get("", response_model=List[SessionResponse])
def list_sessions(db: Session = Depends(get_db)):
    """List all sessions, newest first."""
    sessions = (
        db.query(ResearchSession)
        .order_by(ResearchSession.updated_at.desc())
        .all()
    )
    return sessions


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(session_id: int, db: Session = Depends(get_db)):
    """Get a single session by ID."""
    return _get_session_or_404(db, session_id)


@router.patch("/{session_id}", response_model=SessionResponse)
def update_session(
    session_id: int,
    payload: SessionUpdate,
    db: Session = Depends(get_db),
):
    """Rename a session."""
    session = _get_session_or_404(db, session_id)
    session.title = payload.title
    db.commit()
    db.refresh(session)
    return session


@router.delete("/{session_id}", status_code=204)
def delete_session(session_id: int, db: Session = Depends(get_db)):
    """Delete a session and all its messages/documents."""
    session = _get_session_or_404(db, session_id)
    db.delete(session)
    db.commit()
    return None
