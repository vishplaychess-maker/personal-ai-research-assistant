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
SKILL_TAG_PATTERN = re.compile(r"<skill>\s*([a-z0-9-]+)\s*</skill>", re.IGNORECASE)

# Back-compat alias: older [USE_SKILL: <name>] markers are still recognised.
USE_SKILL_PATTERN = re.compile(r"\[USE_SKILL:\s*([a-z0-9-]+)\s*\]", re.IGNORECASE)

# Plain-text free-model fallback marker: "USE SKILL: name" (line-orientated).
PLAIN_SKILL_PATTERN = re.compile(r"(?im)^\s*USE\s+SKILL:\s*([a-z0-9-]+)")

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

    Returns "" when the skill is unknown so callers can no-op safely.
    """
    skill = SKILL_MANAGER.get_skill_body(name)
    if skill is None:
        logger.info("Skill '%s' not found; L2 load skipped", name)
        return ""
    return (
        f"=== Skill Loaded: {skill.name} ===\n"
        f"Description: {skill.description}\n\n"
        f"{skill.body}\n"
        f"=== End of Skill ==="
    )


def extract_skill_calls(text: Optional[str]) -> List[str]:
    """Extract all skill markers from text.

    Recognises the canonical ``<skill>name</skill>`` tag plus the legacy
    ``[USE_SKILL: <name>]`` and plain ``USE SKILL: name`` forms (free-model
    fallback). Returns a de-duplicated list of requested skill names in
    first-seen order.
    """
    if not text:
        return []
    seen: List[str] = []
    for pattern in (SKILL_TAG_PATTERN, USE_SKILL_PATTERN, PLAIN_SKILL_PATTERN):
        for m in pattern.finditer(text):
            name = m.group(1).strip().lower()
            if name and name not in seen:
                seen.append(name)
    return seen


def _strip_skill_markers(text: str) -> str:
    """Remove all recognised skill markers from text (user never sees them)."""
    cleaned = SKILL_TAG_PATTERN.sub("", text)
    cleaned = USE_SKILL_PATTERN.sub("", cleaned)
    cleaned = PLAIN_SKILL_PATTERN.sub("", cleaned)
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
