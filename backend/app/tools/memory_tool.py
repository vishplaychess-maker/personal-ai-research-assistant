"""
save_memory tool — lets the LLM persist durable user preferences/facts
across sessions.

Uses the same marker protocol as the terminal and python-sandbox tools:
the LLM emits  [SAVE_MEMORY: <content>]  inside its response, the workflow
detects it, saves the memory via memory_service, and strips the marker from
the user-visible response.
"""

import logging
import re
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session as DBSession

from app.services.memory_service import save_memory

logger = logging.getLogger(__name__)

SAVE_MEMORY_PATTERN = re.compile(r"\[SAVE_MEMORY:\s*(.+?)\]", re.DOTALL)


def extract_memory_markers(text: str) -> List[str]:
    """Extract all [SAVE_MEMORY: ...] contents from a response."""
    if not text:
        return []
    return [m.strip() for m in SAVE_MEMORY_PATTERN.findall(text) if m.strip()]


def process_memory_markers(
    text: str,
    db: DBSession,
    user_id: int,
    session_id: Optional[int] = None,
) -> Tuple[str, int]:
    """Save every [SAVE_MEMORY: ...] marker and return (cleaned_text, saved_count).

    The cleaned text has all markers removed so the user never sees raw
    tool markers in the response.
    """
    markers = extract_memory_markers(text)
    saved = 0
    for content in markers:
        mem = save_memory(
            db, user_id, content, category="preference", session_id=session_id
        )
        if mem:
            saved += 1
            logger.info(
                "save_memory tool saved memory id=%d: %.60s", mem.id, mem.content
            )
        else:
            logger.info("save_memory tool skipped memory: %.60s", content)

    cleaned = SAVE_MEMORY_PATTERN.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, saved
