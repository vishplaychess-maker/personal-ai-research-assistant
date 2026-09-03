"""
save_directive tool — lets the agent persist durable behavioural lessons
("lessons learned") that improve its future behaviour.

Uses the same marker protocol as the memory, terminal and python-sandbox
tools: the LLM emits  [SAVE_DIRECTIVE: <content>]  inside its response, the
workflow detects it, saves a persistent AgentDirective row, and strips the
marker from the user-visible response. Active directives are injected into
every future system prompt (see system_prompts.directives_context).
"""

import logging
import re
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session as DBSession

from app.models.models import AgentDirective

logger = logging.getLogger(__name__)

SAVE_DIRECTIVE_PATTERN = re.compile(r"\[SAVE_DIRECTIVE:\s*(.+?)\]", re.DOTALL)

# Directives shorter than this are too trivial to be worth persisting.
_MIN_DIRECTIVE_LENGTH = 8


def extract_directive_markers(text: str) -> List[str]:
    """Extract all [SAVE_DIRECTIVE: ...] contents from a response."""
    if not text:
        return []
    return [m.strip() for m in SAVE_DIRECTIVE_PATTERN.findall(text) if m.strip()]


def list_active_directives(
    db: DBSession,
    user_id: int,
    limit: int = 20,
) -> List[str]:
    """Return the text of active directives for a user, newest first, capped.

    Never raises — returns [] on any failure so directive injection can
    never break the chat.
    """
    try:
        rows = (
            db.query(AgentDirective)
            .filter(AgentDirective.user_id == user_id, AgentDirective.is_active.is_(True))
            .order_by(AgentDirective.created_at.desc())
            .limit(limit)
            .all()
        )
    except Exception as exc:  # noqa: BLE001 — directive lookup must never break chat
        logger.warning("list_active_directives failed (non-fatal): %s", exc)
        return []
    return [row.content.strip() for row in rows if row.content and row.content.strip()]


def process_directive_markers(
    text: str,
    db: DBSession,
    user_id: int,
) -> Tuple[str, int]:
    """Save every [SAVE_DIRECTIVE: ...] marker and return (cleaned_text, saved_count).

    The cleaned text has all markers removed so the user never sees raw
    tool markers in the response.
    """
    markers = extract_directive_markers(text)
    saved = 0
    for content in markers:
        if len(content) < _MIN_DIRECTIVE_LENGTH:
            logger.info("save_directive skipped trivial directive: %.60s", content)
            continue
        try:
            directive = AgentDirective(
                user_id=user_id,
                content=content,
                is_active=True,
            )
            db.add(directive)
            db.commit()
            db.refresh(directive)
            saved += 1
            logger.info(
                "save_directive saved directive id=%d: %.60s",
                directive.id, directive.content,
            )
        except Exception as exc:  # noqa: BLE001 — persistence must never break chat
            db.rollback()
            logger.warning("save_directive failed (non-fatal): %s", exc)

    cleaned = SAVE_DIRECTIVE_PATTERN.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, saved
