"""Agent skills package — Claude-style SKILL.md files with progressive disclosure.

Skills are folders containing a ``SKILL.md`` with YAML frontmatter (``name``,
``description``, optional ``pinned``). Discovery is handled by the canonical
:class:`~app.skills.manager.SkillManager` (bundled + user-global + project-local
+ extra paths), which enables progressive disclosure:

L1 — name + description only (keeps the prompt small for limited-context models)
L2 — full ``SKILL.md`` body loaded on demand via a ``<skill>name</skill>`` marker
L3 — associated resource files listed alongside the body (``skill_tool``)
"""

from app.skills.parser import Skill, parse_skill_md
from app.skills.manager import SkillManager
from app.skills.loader import (
    discover_skills,
    skills_catalog,
    load_skill_body,
    extract_skill_calls,
    USE_SKILL_PATTERN,
    SKILL_TAG_PATTERN,
    SKILLS_TOOL_CONTEXT,
    SKILL_MANAGER,
)

__all__ = [
    "Skill",
    "parse_skill_md",
    "SkillManager",
    "discover_skills",
    "skills_catalog",
    "load_skill_body",
    "extract_skill_calls",
    "USE_SKILL_PATTERN",
    "SKILL_TAG_PATTERN",
    "SKILLS_TOOL_CONTEXT",
    "SKILL_MANAGER",
]
