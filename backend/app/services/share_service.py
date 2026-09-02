"""Pure helpers for the Shareable Agent Card feature.

These functions are side-effect-light (no networking, no scheduler, no
sessions outside the caller-provided DB) so they can be unit-tested in
isolation and reasoned about independently of FastAPI routing concerns.

Data model contract (shared_agents table):
  share_id        - unique public key (8-char nanoid)
  title           - session title snapshot
  model           - session model snapshot (may be NULL = default)
  system_prompt   - session system prompt snapshot (may be NULL = default)
  preview_message - first user message in the session ("preview" of the chat)
  tool_count      - number of tools baked into the share (0 until tools are
                    tracked per-session; kept for future growth)
  has_schedule    - whether the session had an active scheduled task at share time
  view_count      - incremented on every public page view
"""
import secrets
import string
from typing import Optional

from sqlalchemy.orm import Session

from app.models.models import Message, MessageRole, ResearchSession, SharedAgent, ScheduledTask

# Nanoid alphabet (forgiving: no lookalikes like 0/O/1/l/I).
_NANOID_ALPHABET = string.ascii_letters + string.digits


def generate_share_id(length: int = 8) -> str:
    """Generate a URL-safe nanoid of the given length.

    Uses ``secrets`` (cryptographically random), so IDs are unguessable and
    un-enumerable — important because the share page is public and unauthed.
    """
    return "".join(secrets.choice(_NANOID_ALPHABET) for _ in range(length))


def _first_user_message(db: Session, session_id: int) -> Optional[str]:
    """Return the text of the session's first user message, if any."""
    message = (
        db.query(Message)
        .filter(
            Message.session_id == session_id,
            Message.role == MessageRole.user,
        )
        .order_by(Message.id.asc())
        .first()
    )
    if message and message.content:
        return message.content.strip() or None
    return None


def _has_active_schedule(db: Session, session_id: int, user_id: int) -> bool:
    """Return True if the session has at least one active scheduled task."""
    return (
        db.query(ScheduledTask.id)
        .filter(
            ScheduledTask.session_id == session_id,
            ScheduledTask.user_id == user_id,
            ScheduledTask.is_active.is_(True),
        )
        .first()
        is not None
    )


def create_share_record(
    db: Session,
    session: ResearchSession,
    user_id: int,
) -> SharedAgent:
    """Snapshot an agent's public share record from a session.

    Copies only the small, public-safe fields. Never copies the session's
    messages, documents, or memories (privacy: the share page is public).
    """
    share_id = generate_share_id()

    share = SharedAgent(
        share_id=share_id,
        user_id=user_id,
        session_id=session.id,
        title=session.title,
        model=session.model,
        system_prompt=session.system_prompt,
        preview_message=_first_user_message(db, session.id),
        tool_count=0,  # per-session tool tracking is not yet implemented
        has_schedule=_has_active_schedule(db, session.id, user_id),
        cover_image_url=None,
        view_count=0,
    )
    db.add(share)
    db.commit()
    db.refresh(share)
    return share
