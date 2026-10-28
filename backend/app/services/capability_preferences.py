"""Per-user capability config helpers (reads ``skill_preferences.config_json``).

Keeps JSON parsing and permission-level validation in one place so the
capabilities API and the desktop automation routes cannot disagree about what a
stored value means. Unknown/malformed values fall back to the safe default.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models.models import SkillPreference
from app.services.desktop_automation import (
    DEFAULT_PERMISSION_LEVEL,
    PermissionLevel,
)


def parse_permission_level(value: Any) -> Optional[PermissionLevel]:
    """Return a known level, or ``None`` when the value is absent/invalid."""
    if isinstance(value, PermissionLevel):
        return value
    if not isinstance(value, str):
        return None
    try:
        return PermissionLevel(value.strip())
    except ValueError:
        return None


def get_capability_config(db: Session, user_id: int, skill_id: str) -> Dict[str, Any]:
    """Return the stored config object, or ``{}`` when unset/unparseable."""
    row = (
        db.query(SkillPreference)
        .filter(
            SkillPreference.user_id == user_id,
            SkillPreference.skill_id == skill_id,
        )
        .first()
    )
    if row is None or not row.config_json:
        return {}
    try:
        data = json.loads(row.config_json)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get_permission_level(
    db: Session, user_id: int, skill_id: str = "desktop_automation"
) -> PermissionLevel:
    """Resolve the effective level; absent/invalid values become the default."""
    stored = get_capability_config(db, user_id, skill_id).get("permission_level")
    return parse_permission_level(stored) or DEFAULT_PERMISSION_LEVEL
