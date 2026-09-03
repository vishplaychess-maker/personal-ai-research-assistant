"""Agent skills package — Claude-style SKILL.md files with progressive disclosure.

Skills are discovered from the ``skills/`` directory. Only each skill's
``name`` + ``description`` are injected into the system prompt (L1); the full
``SKILL.md`` body is loaded on demand (L2) when the model emits a
``[USE_SKILL: <name>]`` marker.
"""

from app.skills.parser import Skill, parse_skill_md
from app.skills.loader import (
    discover_skills,
    skills_catalog,
    load_skill_body,
    extract_skill_calls,
    USE_SKILL_PATTERN,
    SKILLS_TOOL_CONTEXT,
)

__all__ = [
    "Skill",
    "parse_skill_md",
    "discover_skills",
    "skills_catalog",
    "load_skill_body",
    "extract_skill_calls",
    "USE_SKILL_PATTERN",
    "SKILLS_TOOL_CONTEXT",
]
