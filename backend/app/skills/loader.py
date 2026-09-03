"""
Skill discovery + progressive disclosure for the AI Research Agent.

Claude-style skills live in ``backend/app/skills/<skill-name>/SKILL.md``.
Each file has YAML frontmatter (``name``, ``description``, optional
``pinned``). Discovery works in two "layers":

L1 — Progressive disclosure (cheap):
    ``skills_catalog()`` scans the skills directory once and renders ONLY the
    ``name`` + ``description`` of every skill into a compact prompt block. This
    keeps the system prompt small so small/free models with limited context can
    still "see" what capabilities exist without loading every skill body.

L2 — On-demand loading (expensive, only when needed):
    ``load_skill_body(name)`` returns the full ``SKILL.md`` body for a single
    named skill. The model requests a skill by emitting the marker::

        [USE_SKILL: <skill-name>]

    The backend detects this marker (``extract_skill_calls``) — e.g. in the
    user message or a generated answer — loads the skill body, and injects it
    into context so the model can follow the skill's instructions.

Everything is defensive: missing directory, unparseable files, or unknown
skill names all degrade to no-ops so the chat is never broken.
"""

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from app.config import settings
from app.skills.parser import Skill, parse_skill_md

logger = logging.getLogger(__name__)

# Marker the model emits to request a skill's full body (L2).
USE_SKILL_PATTERN = re.compile(r"\[USE_SKILL:\s*([a-z0-9-]+)\s*\]", re.IGNORECASE)


def _skills_root() -> Path:
    """Absolute path to the skills directory (defaults to this package's folder)."""
    configured = getattr(settings, "skills_dir", None)
    if configured:
        return Path(configured)
    # Default: <package>/skills
    return Path(__file__).resolve().parent


def discover_skills(dir_path: Optional[str] = None) -> List[Skill]:
    """Scan the skills directory and return all parseable skills.

    Finds every ``SKILL.md`` in a direct subdirectory (one level deep). Skips
    files that fail to parse or have an invalid name. Never raises.
    """
    root = Path(dir_path) if dir_path else _skills_root()
    skills: List[Skill] = []
    try:
        if not root.exists():
            logger.info("Skills directory not found: %s", root)
            return skills
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            skill_file = entry / "SKILL.md"
            if not skill_file.exists():
                continue
            skill = parse_skill_md(skill_file, source="local")
            if skill is not None:
                skills.append(skill)
    except Exception as exc:  # noqa: BLE001 — discovery must never break chat
        logger.warning("Skill discovery failed (non-fatal): %s", exc)
    return skills


def get_skill(name: str, dir_path: Optional[str] = None) -> Optional[Skill]:
    """Return a single skill by name, or None if it does not exist."""
    normalized = (name or "").strip().lower()
    if not normalized:
        return None
    for skill in discover_skills(dir_path):
        if skill.name == normalized:
            return skill
    return None


def skills_catalog(dir_path: Optional[str] = None) -> str:
    """L1 — render a compact catalog of available skills (name + description)."""
    skills = discover_skills(dir_path)
    if not skills:
        return ""

    # Pinned skills first, then alphabetical.
    skills = sorted(skills, key=lambda s: (not s.pinned, s.name))

    lines = ["=== Skills Available ==="]
    lines.append(
        "The assistant has the following optional skills. Only the skill's "
        "name and description are shown here. To USE one, emit the marker "
        "[USE_SKILL: <skill-name>] and the full skill instructions will be loaded."
    )
    for s in skills:
        desc = (s.description or "No description.").strip().replace("\n", " ")
        lines.append(f"- **{s.name}**: {desc}")
    lines.append("=== End of Skills ===")
    return "\n".join(lines)


def load_skill_body(name: str, dir_path: Optional[str] = None) -> str:
    """L2 — return the full body of a single skill as a formatted block.

    Returns "" when the skill is unknown so callers can no-op safely.
    """
    skill = get_skill(name, dir_path)
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
    """Extract all [USE_SKILL: <name>] marker names from text.

    Returns a de-duplicated list of requested skill names (lowercased, in
    first-seen order).
    """
    if not text:
        return []
    seen: List[str] = []
    for m in USE_SKILL_PATTERN.finditer(text):
        name = m.group(1).strip().lower()
        if name and name not in seen:
            seen.append(name)
    return seen


SKILLS_TOOL_CONTEXT = """\
## Skills Tool (Progressive Disclosure)
The assistant has optional "skills" — folders containing a SKILL.md with
step-by-step instructions for specialised tasks. Only each skill's name and
description are in your context by default (see "=== Skills Available ===").

When the user's request matches a listed skill, ACTIVATE it by emitting the
marker on its own line:

[USE_SKILL: <skill-name>]

Rules:
- Use the EXACT skill name from the "Skills Available" listing.
- Only request ONE skill at a time.
- After you emit [USE_SKILL: ...], the full skill instructions will appear in
  your context; follow them precisely.
- If no listed skill applies, ignore this tool and answer normally.
"""
