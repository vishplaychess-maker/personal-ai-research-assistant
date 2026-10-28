"""Regression: every bundled SKILL.md must parse, be discoverable, and be
selectable by the agent.

image-generation and video-generation shipped WITHOUT YAML frontmatter, so
parse_skill_md returned None and discover_skills() silently dropped them: the
Skills UI showed 5 of 7 built-ins and the agent L1 index never mentioned them.
They now carry the same minimal frontmatter (name / description / pinned) as
the other five, and their original bodies are untouched.
"""

from __future__ import annotations

from pathlib import Path

from app.services.user_skill_service import load_skill_body_for_user
from app.skills.loader import (
    SKILL_MANAGER, discover_skills, extract_skill_calls, load_skill_body,
)
from app.skills.parser import parse_skill_md

BUNDLED_DIR = Path(__file__).resolve().parents[1] / "backend" / "skills"
BUNDLED = {"browser-automation", "code-review", "commit-message",
           "image-generation", "knowledge-graph", "video-generation",
           "web-research"}
PREVIOUSLY_DROPPED = ("image-generation", "video-generation")
FIRST_TRIGGER = {
    "image-generation": 'The user asks to "generate / create / make an image"',
    "video-generation": 'The user asks to "generate / create / make a video"',
}


def test_every_bundled_skill_directory_has_a_parsable_skill_md():
    for name in sorted(BUNDLED):
        f = BUNDLED_DIR / name / "SKILL.md"
        assert f.exists(), f
        skill = parse_skill_md(f, source=str(BUNDLED_DIR))
        assert skill is not None, name + " SKILL.md did not parse"
        assert skill.name == name, (skill.name, name)
        assert skill.description, name
        assert skill.pinned is False


def test_discovery_includes_both_previously_dropped_skills():
    names = {s.name for s in discover_skills()}
    assert BUNDLED <= names, BUNDLED - names


def test_l1_index_offers_every_bundled_skill_to_the_agent():
    index = SKILL_MANAGER.get_skill_index()
    assert "=== Available Skills ===" in index
    for name in sorted(BUNDLED):
        assert ("- " + name + ":") in index, name


def test_l2_body_loads_and_preserves_the_original_instructions():
    for name, trigger in FIRST_TRIGGER.items():
        block = load_skill_body(name)
        assert block, name
        assert block.startswith("=== Skill Loaded: " + name + " ===")
        assert "## When to use" in block
        assert trigger in block              # intended trigger preserved
        assert "**Description:**" in block   # original line still present


def test_the_marker_selection_path_resolves_both_skills():
    """Selection here is marker/tool driven: the model emits
    <skill>name</skill> and the body resolves filesystem-first."""
    for name in PREVIOUSLY_DROPPED:
        calls = extract_skill_calls("<skill>" + name + "</skill>")
        assert calls and calls[0].lower() == name, calls
        assert load_skill_body_for_user(name, None, 1)
