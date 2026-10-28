"""Server-owned directive write handler (Phase 1B.2R MP-4C).

The ONE write boundary for a directive that a user has explicitly approved
through the canonical approval gateway. The legacy model-marker processor
(app.tools.directive_tool.process_directive_markers) stays fail-closed and
never calls this. The approved durable write path funnels here; nothing else may.

Never logs, echoes or returns the directive content beyond the created row's
own fields - callers must persist only bounded references/hashes.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.models import AgentDirective
from app.services.memory_service import _is_sensitive

logger = logging.getLogger(__name__)

MIN_CONTENT_CHARS = 3


def save_directive(db: Session, user_id: int, content: str) -> Optional[AgentDirective]:
    """Persist ONE directive for user_id. Returns the created row, or None when
    the content is empty / too short / sensitive (zero writes). Content is never
    logged and never included in an exception message."""
    text = (content or "").strip()
    if len(text) < MIN_CONTENT_CHARS:
        return None
    if _is_sensitive(text):
        logger.info("save_directive refused: sensitive content")
        return None
    row = AgentDirective(user_id=int(user_id), content=text, is_active=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("save_directive persisted directive id=%d", int(row.id))
    return row


__all__ = ["save_directive", "MIN_CONTENT_CHARS"]
