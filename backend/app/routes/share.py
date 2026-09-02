"""Shareable Agent Card routes.

Two surfaces:
  * ``POST /api/sessions/{session_id}/share`` — authenticated (owner only).
    Snapshots the session into a ``shared_agents`` row and returns the public
    share URL.
  * ``GET /api/share/agents/{share_id}`` — PUBLIC (no auth, no CSRF). Renders
    the public snapshot used by the share card page and increments the view
    counter.

Privacy contract: the public endpoint only ever returns the immutable snapshot
fields (title / model / prompt / preview / stats). It never exposes the
session's private messages, documents, memories, or owner identity.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.models import SharedAgent, User
from app.schemas.sessions import PublicSharedAgent, ShareCreateResult
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf
from app.services.share_service import create_share_record
from app.routes.sessions import _get_session_or_404

router = APIRouter(tags=["share"])


@router.post("/api/sessions/{session_id}/share", response_model=ShareCreateResult, status_code=201)
def share_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Publish a shareable snapshot of a session you own.

    Returns the nanoid ``share_id`` and the absolute public ``share_url``.
    Calling this repeatedly creates a new shared_agents row each time (a fresh
    snapshot + fresh URL), leaving prior links intact.
    """
    session = _get_session_or_404(db, session_id, user_id=current_user.id)
    share = create_share_record(db, session, current_user.id)
    share_url = f"{settings.frontend_origin.rstrip('/')}/share/agents/{share.share_id}"
    return ShareCreateResult(share_id=share.share_id, share_url=share_url)


@router.get("/api/share/agents/{share_id}", response_model=PublicSharedAgent)
def get_shared_agent(share_id: str, db: Session = Depends(get_db)):
    """Publicly fetch a shared agent's snapshot. Increments view count."""
    share = (
        db.query(SharedAgent)
        .filter(SharedAgent.share_id == share_id)
        .first()
    )
    if not share:
        raise HTTPException(status_code=404, detail="Shared agent not found")

    share.view_count += 1
    db.commit()

    return PublicSharedAgent(
        share_id=share.share_id,
        title=share.title,
        model=share.model,
        system_prompt=share.system_prompt,
        preview_message=share.preview_message,
        tool_count=share.tool_count,
        has_schedule=share.has_schedule,
        cover_image_url=share.cover_image_url,
        views=share.view_count,
    )
