"""
Skill discovery + progressive disclosure for the AI Research Agent.

Thin, backward-compatible facade over :class:`app.skills.manager.SkillManager`,
the canonical discovery engine. The manager searches bundled, user-global,
project-local, and configured ``extra_paths`` locations with "later wins"
precedence, and guards against symlink escapes.

Progressive disclosure works in two layers:

L1 — Progressive disclosure (cheap):
    ``skills_catalog()`` (→ :meth:`SkillManager.get_skill_index`) renders ONLY
    the ``name`` + ``description`` of each skill into a compact prompt block,
    keeping the system prompt small for limited-context models.

L2 — On-demand loading (expensive, only when needed):
    ``load_skill_body(name)`` returns the full ``SKILL.md`` body for a single
    named skill. The model requests a skill by emitting the marker::

        <skill>skill-name</skill>

    ``extract_skill_calls`` detects these tags and the caller injects the body.

Everything is defensive: missing directories, unparseable files, or unknown
skill names all degrade to no-ops so the chat is never broken.
"""

import logging
import re
from typing import List, Optional

from app.config import settings
from app.skills.manager import SkillManager
from app.skills.parser import Skill

logger = logging.getLogger(__name__)

# Marker the model emits to request a skill's full body (L2).
# [^<]+ captures the name even when the model writes spaces/dashes, e.g.
# <skill>commit - message</skill>. Sanitised before lookup.
SKILL_TAG_PATTERN = re.compile(r"<skill>([^<]+)</skill>", re.IGNORECASE)

# Back-compat alias: older [USE_SKILL: <name>] markers are still recognised.
USE_SKILL_PATTERN = re.compile(r"\[USE_SKILL:\s*([^\]]+)\]", re.IGNORECASE)

# Plain-text free-model fallback marker: "USE SKILL: name" (line-orientated).
PLAIN_SKILL_PATTERN = re.compile(r"(?im)^\s*USE\s+SKILL:\s*([^\n]+)")

# Canonical manager instance. Uses app settings so env-configured
# ``EXTRA_PATHS`` are honoured on top of the bundled skills directory.
SKILL_MANAGER = SkillManager(settings)


def discover_skills() -> List[Skill]:
    """Return all discovered skills (canonical discovery with precedence)."""
    return SKILL_MANAGER.discover_skills()


def skills_catalog() -> str:
    """L1 — render a compact catalog of available skills (name + description)."""
    return SKILL_MANAGER.get_skill_index()


def load_skill_body(name: str) -> str:
    """L2 — return the full body of a single skill as a formatted block.

    Returns "" when the skill is unknown so callers can no-op safely. The
    name is sanitised (spaces -> hyphens) before lookup.
    """
    skill = SKILL_MANAGER.get_skill_body(sanitize_skill_name(name))
    if skill is None:
        logger.info("Skill '%s' not found; L2 load skipped", name)
        return ""
    return (
        f"=== Skill Loaded: {skill.name} ===\n"
        f"Description: {skill.description}\n\n"
        f"{skill.body}\n"
        f"=== End of Skill ==="
    )


def sanitize_skill_name(name: str) -> str:
    """Normalise a possibly messy skill name into a lookup-safe key.

    The model may output e.g. ``commit - message`` (spaces/dashes) for the
    skill ``commit-message``. We lowercase, trim, collapse internal whitespace,
    and replace whitespace runs with a single hyphen so the SkillManager lookup
    succeeds. Returns "" when nothing usable remains.
    """
    if not name:
        return ""
    s = name.strip().lower()
    # Collapse whitespace (spaces/tabs/newlines) into a single hyphen.
    s = re.sub(r"\s+", "-", s)
    # Collapse repeated hyphens/dashes that the model adds for readability.
    s = re.sub(r"-+", "-", s)
    s = s.strip("-")
    return s


def extract_skill_calls(text: Optional[str]) -> List[str]:
    """Extract all skill markers from text.

    Recognises the canonical ``<skill>name</skill>`` tag plus the legacy
    ``[USE_SKILL: <name>]`` and plain ``USE SKILL: name`` forms (free-model
    fallback). Names are sanitised (spaces -> hyphens) so e.g.
    ``<skill>commit - message</skill>`` resolves to ``commit-message``.
    Returns a de-duplicated list of requested skill names in first-seen order.
    """
    if not text:
        return []
    seen: List[str] = []
    for pattern in (SKILL_TAG_PATTERN, USE_SKILL_PATTERN, PLAIN_SKILL_PATTERN):
        for m in pattern.finditer(text):
            name = sanitize_skill_name(m.group(1))
            if name and name not in seen:
                seen.append(name)
    return seen


def _strip_skill_markers(text: str) -> str:
    """Remove all recognised skill activation markers from text.

    Covers the canonical ``<skill>`` tag, legacy ``[USE_SKILL:]`` and plain
    ``USE SKILL:`` forms. Persistence markers ([SAVE_MEMORY], [SAVE_DIRECTIVE])
    are NOT stripped here — they must survive into the complete handler so they
    can be persisted before being removed from the saved response.
    """
    cleaned = text
    for pat in _STRIP_PATTERNS:
        cleaned = pat.sub("", cleaned)
    return cleaned.strip()


def process_skill_markers(text: str) -> str:
    """Detect skill markers in assistant output and strip them from the
    user-visible response.

    This is the free-model text fallback (STEP 4): small/free models that
    cannot do native tool-calling emit a plain marker (e.g. ``<skill>name</skill>``
    or ``USE SKILL: name``) inside their text. ``extract_skill_calls`` returns
    the requested skill names so the caller can load their bodies (L2) and
    inject them into the next iteration; this function removes the markers so
    they never leak into the chat.

    Returns the cleaned text (no markers).
    """
    if not text:
        return text
    return _strip_skill_markers(text)


# Marker opening sequences. Used by SkillStreamFilter to recognise when the
# tail of a buffered stream could be the start of a marker that spans tokens.
# Includes the persistence tool markers ([SAVE_MEMORY], [SAVE_DIRECTIVE],
# [USE_MEMORY]) and the code/MCP markers so they are buffered out of the
# incremental UI stream.
# Only markers from the ASSISTANT's own output are ever processed — the stream
# filter operates solely on the model's streamed text, never on user input or
# scraped web content.
_MARKER_OPENS = (
    "<skill",
    "[use_skill",
    "use skill:",
    "[use_memory",
    "[plan",
    "[lesson",
    "[save_memory",
    "[save_directive",
    "[python_code",
    "[mcp_call",
    "[proposed_command",
)

# Strip targets: only skill activation markers. Persistence markers
# ([SAVE_MEMORY], [SAVE_DIRECTIVE]) are stripped later in the "complete"
# handler after persistence runs, so they must NOT be stripped here —
# doing so would silently drop them before they can be persisted.
_STRIP_PATTERNS = (
    SKILL_TAG_PATTERN,
    USE_SKILL_PATTERN,
    PLAIN_SKILL_PATTERN,
)

# Phase 4 SSE hardening: these markers are ONLY stripped in the streaming
# filter path (SkillStreamFilter.push/flush), never in the full-response
# process_skill_markers path — so persistence handlers in messages.py /
# the workflows still see the raw text containing them and can persist
# before stripping. [USE_MEMORY] is additionally resolved (not just stripped)
# by the complete-handler; stripping it here only guards the live stream.
_SSE_STRIP_PATTERNS = (
    re.compile(r"\[SAVE_MEMORY:\s*.*?\]", re.DOTALL | re.IGNORECASE),
    re.compile(r"\[USE_MEMORY:\s*.*?\]", re.DOTALL | re.IGNORECASE),
    re.compile(r"\[SAVE_DIRECTIVE:\s*.*?\]", re.DOTALL | re.IGNORECASE),
    re.compile(r"\[PLAN:\s*.*?\]", re.DOTALL | re.IGNORECASE),
    re.compile(r"\[LESSON:\s*.*?\]", re.DOTALL | re.IGNORECASE),
)

# Bracket-style marker openers (for open-marker hold detection).
_BRACKET_MARKER_OPENS = tuple(o for o in _MARKER_OPENS if o.startswith("["))


def _strip_all_stream_markers(text: str) -> str:
    """Strip skill markers AND persistence/recall markers from stream text.

    Used only by SkillStreamFilter so that NO marker — skill, memory,
    directive, plan, or lesson — can ever leak to the incremental SSE UI
    stream. The full-response text is processed separately (preserving the
    raw markers) so persistence handlers still run.
    """
    cleaned = text
    for pat in _STRIP_PATTERNS:
        cleaned = pat.sub("", cleaned)
    for pat in _SSE_STRIP_PATTERNS:
        cleaned = pat.sub("", cleaned)
    return cleaned.strip()


class SkillStreamFilter:
    """Suppress skill markers from an incremental SSE token stream in real time.

    Small/free models may emit ``<skill>name</skill>`` (or the legacy forms)
    *inside* their streamed text. Without filtering, those markers could leak
    to the UI. This filter buffers tokens and only releases text it is certain
    is marker-free: any suffix that could be (or is inside) a marker is held
    back until ``flush()`` on completion, where the remaining markers are
    stripped. Real prose still streams live with minimal latency.
    """

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = ""

    def _hold_start(self, text: str) -> int:
        """Index from which the suffix of ``text`` must be held (could be a
        marker that spans tokens, or is inside an open marker)."""
        low = text.lower()

        # 1) An open <skill>...</skill> whose closing tag has not arrived yet:
        #    hold everything from the opener.
        last_open = low.rfind("<skill")
        last_close = low.rfind("</skill>")
        if last_open != -1 and (last_close == -1 or last_close < last_open):
            return last_open

        # 2) Any open bracket marker ([USE_SKILL:, [USE_MEMORY:, [PLAN:,
        #    [LESSON:, [SAVE_MEMORY:, [SAVE_DIRECTIVE:, ...) without a
        #    closing ] yet: hold from '['.
        for opener in _BRACKET_MARKER_OPENS:
            idx_open = low.rfind(opener)
            if idx_open != -1 and low.rfind("]") < idx_open:
                return idx_open

        # 3) The tail could be a prefix of a marker opening sequence. Hold
        # from the EARLIEST such index — returning a later one would release
        # an already-open '[' while holding its continuation characters.
        for i in range(len(text)):
            tail = low[i:]
            if tail and any(op.startswith(tail) for op in _MARKER_OPENS):
                return i

        return len(text)

    def push(self, token: str) -> str:
        """Feed one streamed token. Returns the marker-free slice that may be
        sent to the UI now ('' if everything must be held)."""
        self._buf += token
        hold = self._hold_start(self._buf)
        release = self._buf[:hold]
        # Strip any markers that completed within the released portion.
        released = _strip_all_stream_markers(release)
        self._buf = self._buf[hold:]
        return released

    def flush(self) -> str:
        """Return any remaining user-visible text (markers stripped). Call at
        end of stream so nothing is left buffered and the UI can continue."""
        out = _strip_all_stream_markers(self._buf)
        self._buf = ""
        return out


SKILLS_TOOL_CONTEXT = """\
## Skills Tool (Progressive Disclosure)
The assistant has optional "skills" — folders containing a SKILL.md with
step-by-step instructions for specialised tasks. Only each skill's name and
description are in your context by default (see "=== Available Skills ===").

When the user's request matches a listed skill, ACTIVATE it by emitting the
marker on its own line:

<skill>skill-name</skill>

Rules:
- Use the EXACT skill name from the "Available Skills" listing.
- Only request ONE skill at a time.
- After you emit <skill>...</skill>, the full skill instructions will appear in
  your context; follow them precisely.
- If no listed skill applies, ignore this tool and answer normally.
"""
