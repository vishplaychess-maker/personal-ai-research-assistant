"""User-defined skills (custom skill creator) — service layer.

CRUD for the ``UserSkill`` model plus the runtime merge layer that blends
DB-backed skills with filesystem skills:

* **L1 index** — :func:`merged_skill_catalog` renders the SAME
  "=== Available Skills ===" block as filesystem skills, with the user's
  enabled DB skills appended. Filesystem skills take precedence on name
  collisions (DB entries with a colliding name are omitted at render time;
  they are also rejected at create time).
* **L2 loading** — :func:`load_skill_body_for_user` resolves a skill name
  through the filesystem path first, then falls back to the user's enabled
  DB skills, formatting DB bodies identically to ``loader.load_skill_body``.

Sanitization (prompt-injection defense):
* names must match ``^[a-z0-9][a-z0-9-_]{1,48}$`` — lowercase, marker/URL-safe;
* descriptions are capped at 200 chars;
* bodies are capped at 8000 chars and REJECTED when they contain any skill
  activation marker (``<skill>…</skill>``, ``[USE_SKILL: …]``, ``USE SKILL: …``)
  so stored content can never spoof skill activation;
* trigger keywords are lowercased, de-duplicated, capped at 10 and 32 chars each.
"""

import logging
import re
from typing import List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.models import UserSkill

logger = logging.getLogger(__name__)

# Marker-safe, lowercase name: 2-49 chars, starts alphanumeric.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-_]{1,48}$")

MAX_DESCRIPTION_LEN = 200
MAX_BODY_LEN = 8000
MAX_KEYWORDS = 10
MAX_KEYWORD_LEN = 32

_FS_SOURCE = "filesystem"


class SkillValidationError(ValueError):
    """Raised when user input fails validation (maps to HTTP 422)."""


class SkillConflictError(ValueError):
    """Raised on duplicate skill names (maps to HTTP 409)."""


def _marker_patterns() -> Tuple[re.Pattern, ...]:
    """Skill activation marker patterns (imported lazily to avoid cycles)."""
    from app.skills.loader import (
        SKILL_TAG_PATTERN,
        USE_SKILL_PATTERN,
        PLAIN_SKILL_PATTERN,
    )

    return (SKILL_TAG_PATTERN, USE_SKILL_PATTERN, PLAIN_SKILL_PATTERN)


def validate_name(raw: str) -> str:
    name = (raw or "").strip().lower()
    if not NAME_RE.match(name):
        raise SkillValidationError(
            "Invalid skill name: use 2-49 lowercase letters, digits, hyphens "
            "or underscores, starting with a letter or digit (e.g. 'my-skill')."
        )
    return name


def validate_description(raw: str) -> str:
    description = (raw or "").strip()
    if not description:
        raise SkillValidationError("Description is required.")
    if len(description) > MAX_DESCRIPTION_LEN:
        raise SkillValidationError(
            f"Description too long ({len(description)} chars; max {MAX_DESCRIPTION_LEN})."
        )
    return description


def validate_body(raw: str) -> str:
    body = (raw or "").strip()
    if not body:
        raise SkillValidationError("Skill body is required.")
    if len(body) > MAX_BODY_LEN:
        raise SkillValidationError(
            f"Skill body too long ({len(body)} chars; max {MAX_BODY_LEN})."
        )
    for pattern in _marker_patterns():
        if pattern.search(body):
            raise SkillValidationError(
                "Skill body must not contain skill activation markers "
                "(<skill>…</skill>, [USE_SKILL: …], USE SKILL: …)."
            )
    return body


def sanitize_keywords(raw) -> str:
    """Lowercase, de-duplicate and cap a comma-separated keyword list."""
    if isinstance(raw, (list, tuple)):
        parts = [str(k) for k in raw]
    else:
        parts = (raw or "").split(",")
    seen: List[str] = []
    for part in parts:
        keyword = re.sub(r"\s+", " ", part.strip().lower())
        keyword = keyword.replace(" ", "-")
        keyword = re.sub(r"-+", "-", keyword).strip("-")
        if not keyword:
            continue
        keyword = keyword[:MAX_KEYWORD_LEN]
        if keyword not in seen:
            seen.append(keyword)
        if len(seen) >= MAX_KEYWORDS:
            break
    return ",".join(seen)


def _fs_skill_names() -> set:
    from app.skills.loader import SKILL_MANAGER

    return {s.name for s in SKILL_MANAGER.discover_skills()}


def _ensure_no_fs_collision(name: str) -> None:
    if name in _fs_skill_names():
        raise SkillConflictError(
            f"Skill name '{name}' is already used by a built-in (filesystem) "
            "skill. Built-in skills take precedence — choose another name."
        )


def get_user_skill(db: Session, user_id: int, skill_id: int) -> Optional[UserSkill]:
    return (
        db.query(UserSkill)
        .filter(UserSkill.id == skill_id, UserSkill.user_id == user_id)
        .first()
    )


def list_skills_for_user(db: Session, user_id: int) -> List[UserSkill]:
    return (
        db.query(UserSkill)
        .filter(UserSkill.user_id == user_id)
        .order_by(UserSkill.created_at.desc())
        .all()
    )


def list_enabled_for_user(db: Session, user_id: int) -> List[UserSkill]:
    """Enabled DB skills only — the runtime (prompt injection) view."""
    return (
        db.query(UserSkill)
        .filter(UserSkill.user_id == user_id, UserSkill.enabled.is_(True))
        .order_by(UserSkill.created_at.desc())
        .all()
    )


def create_skill(
    db: Session,
    user_id: int,
    *,
    name: str,
    description: str,
    body: str,
    trigger_keywords: str = "",
) -> UserSkill:
    clean_name = validate_name(name)
    clean_description = validate_description(description)
    clean_body = validate_body(body)
    clean_keywords = sanitize_keywords(trigger_keywords)

    _ensure_no_fs_collision(clean_name)

    existing = (
        db.query(UserSkill)
        .filter(UserSkill.user_id == user_id, UserSkill.name == clean_name)
        .first()
    )
    if existing:
        raise SkillConflictError(f"You already have a skill named '{clean_name}'.")

    skill = UserSkill(
        user_id=user_id,
        name=clean_name,
        description=clean_description,
        body=clean_body,
        trigger_keywords=clean_keywords,
        enabled=True,
    )
    db.add(skill)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise SkillConflictError(f"You already have a skill named '{clean_name}'.")
    db.refresh(skill)
    logger.info("Created user skill '%s' for user %s", clean_name, user_id)
    return skill


def update_skill(
    db: Session,
    user_id: int,
    skill_id: int,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    body: Optional[str] = None,
    trigger_keywords: Optional[str] = None,
) -> UserSkill:
    skill = get_user_skill(db, user_id, skill_id)
    if not skill:
        raise LookupError("Skill not found")

    if name is not None:
        clean_name = validate_name(name)
        if clean_name != skill.name:
            _ensure_no_fs_collision(clean_name)
            existing = (
                db.query(UserSkill)
                .filter(
                    UserSkill.user_id == user_id,
                    UserSkill.name == clean_name,
                    UserSkill.id != skill.id,
                )
                .first()
            )
            if existing:
                raise SkillConflictError(
                    f"You already have a skill named '{clean_name}'."
                )
        skill.name = clean_name
    if description is not None:
        skill.description = validate_description(description)
    if body is not None:
        skill.body = validate_body(body)
    if trigger_keywords is not None:
        skill.trigger_keywords = sanitize_keywords(trigger_keywords)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise SkillConflictError("A skill with that name already exists.")
    db.refresh(skill)
    return skill


def delete_skill(db: Session, user_id: int, skill_id: int) -> None:
    skill = get_user_skill(db, user_id, skill_id)
    if not skill:
        raise LookupError("Skill not found")
    db.delete(skill)
    db.commit()


def toggle_skill(db: Session, user_id: int, skill_id: int) -> UserSkill:
    skill = get_user_skill(db, user_id, skill_id)
    if not skill:
        raise LookupError("Skill not found")
    skill.enabled = not skill.enabled
    db.commit()
    db.refresh(skill)
    return skill


# ── Runtime merge layer (L1 + L2) ─────────────────────────


def merged_skill_catalog(db: Session, user_id: int) -> str:
    """L1 index: filesystem skills + the user's enabled DB skills in ONE block.

    FS precedence: DB skills whose name matches a filesystem skill are
    omitted. Returns "" when there is nothing to show (same as the
    filesystem-only path).
    """
    from app.skills.loader import SKILL_MANAGER

    db_skills = list_enabled_for_user(db, user_id)
    if not db_skills:
        return SKILL_MANAGER.get_skill_index()

    fs_names = _fs_skill_names()
    extra = [
        (s.name, s.description)
        for s in db_skills
        if s.name not in fs_names
    ]
    return SKILL_MANAGER.get_skill_index(extra_skills=extra)


def load_skill_body_for_user(name: str, db: Session, user_id: int) -> str:
    """L2 body for a skill name: filesystem first, then the user's DB skills.

    DB bodies are formatted identically to ``loader.load_skill_body``. The
    name is sanitized the same way (whitespace -> hyphens, lowercase).
    Returns "" when the skill is unknown so callers can no-op safely.
    """
    from app.skills.loader import load_skill_body, sanitize_skill_name

    fs_block = load_skill_body(name)
    if fs_block:
        return fs_block
    if db is None:
        return ""

    clean = sanitize_skill_name(name)
    if not clean:
        return ""
    skill = (
        db.query(UserSkill)
        .filter(
            UserSkill.user_id == user_id,
            UserSkill.name == clean,
            UserSkill.enabled.is_(True),
        )
        .first()
    )
    if not skill:
        return ""
    return (
        f"=== Skill Loaded: {skill.name} ===\n"
        f"Description: {skill.description}\n\n"
        f"{skill.body}\n"
        f"=== End of Skill ==="
    )
