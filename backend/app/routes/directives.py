"""
Agent Directives (F6 Cap 3 "Lessons Learned") management routes.

GET    /api/directives          — List the current user's agent directives
PATCH  /api/directives/{id}     — Toggle a directive's is_active status
DELETE /api/directives/{id}     — Delete a directive
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import AgentDirective, User
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf

router = APIRouter(prefix="/api/directives", tags=["directives"])


class DirectiveResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    content: str
    is_active: bool
    created_at: datetime


class DirectiveUpdate(BaseModel):
    is_active: bool


def _get_owned_directive(db: Session, user_id: int, directive_id: int) -> AgentDirective:
    """Fetch a directive owned by the user or raise 404."""
    row = (
        db.query(AgentDirective)
        .filter(AgentDirective.id == directive_id, AgentDirective.user_id == user_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Directive not found")
    return row


@router.get("", response_model=list[DirectiveResponse])
def list_directives(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all agent directives for the current user (active first, newest first)."""
    rows = (
        db.query(AgentDirective)
        .filter(AgentDirective.user_id == current_user.id)
        .order_by(AgentDirective.is_active.desc(), AgentDirective.created_at.desc())
        .all()
    )
    return rows


@router.patch("/{directive_id}", response_model=DirectiveResponse)
def toggle_directive(
    directive_id: int,
    payload: DirectiveUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Set a directive's is_active status (owner-scoped)."""
    row = _get_owned_directive(db, current_user.id, directive_id)
    row.is_active = payload.is_active
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/{directive_id}", status_code=204)
def delete_directive(
    directive_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Delete a directive (owner-scoped)."""
    row = _get_owned_directive(db, current_user.id, directive_id)
    db.delete(row)
    db.commit()
    return None
