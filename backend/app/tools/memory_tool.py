"""
save_memory / use_memory tools — let the LLM persist and recall durable
user preferences/facts across sessions.

Both use the same marker protocol as the terminal and python-sandbox tools:
the LLM emits  [SAVE_MEMORY: <content>]  or  [USE_MEMORY: <query>]  inside
its response, the workflow detects the markers, persists (save) or retrieves
(use) memories via memory_service, and strips the markers from the
user-visible response.
"""

import logging
import re
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session as DBSession

from app.services.memory_service import save_memory

logger = logging.getLogger(__name__)

SAVE_MEMORY_PATTERN = re.compile(r"\[SAVE_MEMORY:\s*(.+?)\]", re.DOTALL)

#: Bounded, user-visible notice emitted when a model marker wanted a durable
#: write that now requires an approved, owner-bound proposal. Never contains the
#: requested content.
MEMORY_WRITE_REQUIRES_APPROVAL_NOTICE = (
    "Memory was not saved: saving persistent memory requires an approved "
    "proposal.")

# Phase 4: explicit on-demand recall marker — [USE_MEMORY: <query>].
USE_MEMORY_PATTERN = re.compile(r"\[USE_MEMORY:\s*(.+?)\]", re.DOTALL)


def extract_memory_markers(text: str) -> List[str]:
    """Extract all [SAVE_MEMORY: ...] contents from a response."""
    if not text:
        return []
    return [m.strip() for m in SAVE_MEMORY_PATTERN.findall(text) if m.strip()]


def memory_write_requires_approval() -> bool:
    """Phase 1B.2R MP-4C — a model-emitted [SAVE_MEMORY: ...] is a PROPOSAL,
    never a write. ALWAYS True: no flag combination permits a model-directed
    durable-memory mutation before a server-owned approval has been consumed.
    Named (rather than a bare literal at the call site) so the invariant is
    testable and cannot drift per caller."""
    return True


def process_memory_markers(
    text: str,
    db: DBSession,
    user_id: int,
    session_id: Optional[int] = None,
) -> Tuple[str, int]:
    """Strip every [SAVE_MEMORY: ...] marker and persist NOTHING.

    Phase 1B.2R MP-4C: this legacy helper used to write each marker straight to
    the user's durable memory with NO approval. It now fails closed — markers
    are removed from the user-visible text (so raw marker text can never render
    or be persisted) and ZERO memories are written. The approved durable write
    path lives in the Run-bound engine (an owner-bound PendingApproval consumed
    exactly once); this legacy helper never writes in ANY configuration.
    Always returns (cleaned_text, 0).

    Owner decision 2: a model-initiated persistent write requires an approved,
    owner-bound PendingApproval. No canonical memory-proposal path exists yet,
    so this fails closed AND tells the user with a bounded notice.
    """
    had_marker = bool(SAVE_MEMORY_PATTERN.search(text or ""))
    cleaned = SAVE_MEMORY_PATTERN.sub("", text or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if had_marker:
        cleaned = (cleaned + chr(10) + chr(10)
                   + MEMORY_WRITE_REQUIRES_APPROVAL_NOTICE).strip()
    return cleaned, 0


def _retrieve_memories_for_query(db: DBSession, user_id: int, query: str) -> str:
    """Render the memories matching *query* as a prompt-style block.

    Uses the existing memory_service retrieval (recency-ordered) and ranks
    the results by keyword overlap with the query text. Returns "" when
    nothing matches — the caller strips the marker without injecting.
    """
    from app.services.memory_service import (
        format_memories_for_prompt,
        retrieve_relevant_memories,
    )

    memories = retrieve_relevant_memories(db, user_id=user_id)
    if not memories:
        return ""

    q_words = {w for w in re.split(r"\W+", (query or "").lower()) if len(w) > 3}
    if q_words:
        scored = [
            m for m in memories
            if any(w in (m.content or "").lower() for w in q_words)
        ]
    else:
        scored = []
    if not scored:
        return ""
    return format_memories_for_prompt(scored[:5])


def process_use_memory_markers(
    text: str,
    db: DBSession,
    user_id: int,
    session_id: Optional[int] = None,
) -> Tuple[str, int]:
    """Resolve every [USE_MEMORY: <query>] marker and return (cleaned_text, used_count).

    For each marker, memory_service retrieval is queried with the marker's
    query text; matching memories are injected inline where the marker
    appeared. The marker itself is ALWAYS stripped so the user never sees
    raw tool markers (same lifecycle as [SAVE_MEMORY: ...]).
    """
    if not text or not USE_MEMORY_PATTERN.search(text):
        return text, 0

    used = 0

    def _replace(match: "re.Match[str]") -> str:
        nonlocal used
        query = (match.group(1) or "").strip()
        if not query:
            return ""
        try:
            block = _retrieve_memories_for_query(db, user_id, query)
        except Exception as exc:  # noqa: BLE001 — recall must never break chat
            logger.warning("use_memory retrieval failed (non-fatal): %s", exc)
            return ""
        if block:
            used += 1
            logger.info(
                "use_memory tool injected %d-char memory block for query %.60s",
                len(block), query,
            )
        return block

    cleaned = USE_MEMORY_PATTERN.sub(_replace, text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, used
