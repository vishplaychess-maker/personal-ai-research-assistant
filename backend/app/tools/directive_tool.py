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

#: Bounded, user-visible notice emitted when a model marker wanted a durable
#: write that now requires an approved, owner-bound proposal. Never contains the
#: requested content.
DIRECTIVE_WRITE_REQUIRES_APPROVAL_NOTICE = (
    "Directive was not saved: saving a lesson requires an approved proposal.")

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
    """Strip every [SAVE_DIRECTIVE: ...] marker and persist NOTHING.

    Owner decision 2 (MP-4C): a model-initiated persistent write requires an
    approved, owner-bound PendingApproval. The canonical proposal authority is
    Run-bound (it is built from a context carrying run_id, and the preview path
    requires run_id plus tool_call_id and loads the Run and its RunStep), so a
    legacy workflow that owns NO canonical Run cannot create or resume a
    proposal. This path therefore fails closed: markers are removed from the
    user-visible text, ZERO directives are written, the requested content is
    never logged, and a bounded notice tells the user why. Explicit
    user-initiated saves through the directives API are unaffected.
    Always returns (cleaned_text, 0).
    """
    had_marker = bool(SAVE_DIRECTIVE_PATTERN.search(text or ""))
    cleaned = SAVE_DIRECTIVE_PATTERN.sub("", text or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if had_marker:
        cleaned = (cleaned + chr(10) + chr(10)
                   + DIRECTIVE_WRITE_REQUIRES_APPROVAL_NOTICE).strip()
    return cleaned, 0
