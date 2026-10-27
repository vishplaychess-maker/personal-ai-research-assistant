"""Executable capability toggles (Phase 2 of the Skills capability grid).

GET    /api/capabilities          — catalog merged with the user's overrides
PATCH  /api/capabilities/{id}     — upsert one capability's preference

Distinct from ``/api/skills`` (user-authored instruction playbooks) and from
the read-only built-in ``SKILL.md`` catalog.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import SkillPreference, User
from app.services import capability_registry
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf

router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])


class CapabilityResponse(BaseModel):
    id: str
    name: str
    description: str
    icon: str
    enabled: bool
    config: Dict[str, Any] = Field(default_factory=dict)


class CapabilityUpdate(BaseModel):
    enabled: bool
    config: Optional[Dict[str, Any]] = None


def _parse_config(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _merged(default: Dict[str, Any], pref: Optional[SkillPreference]) -> Dict[str, Any]:
    """Catalog defaults are the base; a stored preference overrides them."""
    merged = {
        "id": default["id"],
        "name": default["name"],
        "description": default["description"],
        "icon": default["icon"],
        "enabled": bool(default["enabled"]),
        "config": {},
    }
    if pref is not None:
        merged["enabled"] = bool(pref.enabled)
        merged["config"] = _parse_config(pref.config_json)
    return merged


def _get_preference(db: Session, user_id: int, skill_id: str) -> Optional[SkillPreference]:
    return (
        db.query(SkillPreference)
        .filter(
            SkillPreference.user_id == user_id,
            SkillPreference.skill_id == skill_id,
        )
        .first()
    )


@router.get("", response_model=List[CapabilityResponse])
def list_capabilities(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return every catalog capability, with this user's overrides applied."""
    preferences = {
        pref.skill_id: pref
        for pref in db.query(SkillPreference)
        .filter(SkillPreference.user_id == current_user.id)
        .all()
    }
    return [
        _merged(capability, preferences.get(capability["id"]))
        for capability in capability_registry.list_capabilities()
    ]


@router.patch("/{skill_id}", response_model=CapabilityResponse)
def update_capability(
    skill_id: str,
    payload: CapabilityUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Upsert the enabled flag (and optional config) for one capability."""
    default = capability_registry.get_capability(skill_id)
    if default is None:
        raise HTTPException(status_code=404, detail="Unknown capability")

    encoded_config = (
        json.dumps(payload.config, sort_keys=True) if payload.config is not None else None
    )
    pref = _get_preference(db, current_user.id, skill_id)
    if pref is None:
        pref = SkillPreference(
            user_id=current_user.id,
            skill_id=skill_id,
            enabled=payload.enabled,
            config_json=encoded_config,
        )
        db.add(pref)
    else:
        pref.enabled = payload.enabled
        if encoded_config is not None:
            pref.config_json = encoded_config

    db.commit()
    db.refresh(pref)
    return _merged(default, pref)
