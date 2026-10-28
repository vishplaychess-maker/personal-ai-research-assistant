"""User-defined skills (custom skill creator) management routes.

GET    /api/skills              — List the current user's skills (no body)
POST   /api/skills              — Create a skill
GET    /api/skills/{id}         — Fetch one skill (includes the L2 body)
PUT    /api/skills/{id}         — Update a skill (owner-scoped)
DELETE /api/skills/{id}         — Delete a skill (owner-scoped)
POST   /api/skills/{id}/toggle  — Flip the enabled flag, return the new state
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import User, UserSkill
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf
from app.services import user_skill_service
from app.services.user_skill_service import (
    SkillConflictError,
    SkillValidationError,
)

router = APIRouter(prefix="/api/skills", tags=["skills"])


class UserSkillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    trigger_keywords: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class UserSkillDetailResponse(UserSkillResponse):
    body: str


class UserSkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)
    trigger_keywords: Optional[str] = ""


class UserSkillUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, min_length=1, max_length=500)
    body: Optional[str] = Field(default=None, max_length=20000)
    trigger_keywords: Optional[str] = None


class ToggleResponse(BaseModel):
    id: int
    enabled: bool


def _get_owned_skill(db: Session, user_id: int, skill_id: int) -> UserSkill:
    skill = user_skill_service.get_user_skill(db, user_id, skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@router.get("", response_model=List[UserSkillResponse])
def list_skills(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List the current user's skills (newest first; the L2 body is excluded)."""
    return user_skill_service.list_skills_for_user(db, current_user.id)


class BuiltinSkillResponse(BaseModel):
    name: str
    description: str
    pinned: bool = False


class BuiltinSkillCatalogResponse(BaseModel):
    enabled: bool
    skills: List[BuiltinSkillResponse]


# NOTE: declared BEFORE the dynamic "/{skill_id}" route on purpose - FastAPI
# matches in declaration order, so a later "/catalog" would be parsed as an
# int path param and answered with 422.
@router.get("/catalog", response_model=BuiltinSkillCatalogResponse)
def list_builtin_skills(
    current_user: User = Depends(get_current_user),
):
    """List the BUILT-IN (filesystem) skill catalog - read-only, for the UI.

    The Settings > Skills tab used to render ONLY user-created skills
    (GET /api/skills), so the bundled SKILL.md playbooks were invisible in the
    UI even though they are injected into agent prompts. This exposes name +
    description (+ pinned) only: never the L2 body and never a filesystem
    path. When skills are disabled by configuration it answers
    ``enabled=false`` with an empty list, so the UI can show a clear disabled
    explanation instead of silently rendering nothing.
    """
    from app.config import settings as _settings
    from app.skills.loader import discover_skills

    if not _settings.thunder_skills_enabled:
        return BuiltinSkillCatalogResponse(enabled=False, skills=[])
    return BuiltinSkillCatalogResponse(
        enabled=True,
        skills=[
            BuiltinSkillResponse(name=s.name, description=s.description or "",
                                 pinned=bool(getattr(s, "pinned", False)))
            for s in discover_skills()
        ],
    )


@router.get("/{skill_id}", response_model=UserSkillDetailResponse)
def get_skill(
    skill_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch one owned skill, including the full L2 body."""
    return _get_owned_skill(db, current_user.id, skill_id)


@router.post("", response_model=UserSkillDetailResponse, status_code=201)
def create_skill(
    payload: UserSkillCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Create a skill for the current user (422 invalid input, 409 duplicate)."""
    try:
        return user_skill_service.create_skill(
            db,
            current_user.id,
            name=payload.name,
            description=payload.description,
            body=payload.body,
            trigger_keywords=payload.trigger_keywords or "",
        )
    except SkillConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SkillValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.put("/{skill_id}", response_model=UserSkillDetailResponse)
def update_skill(
    skill_id: int,
    payload: UserSkillUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Update an owned skill (422 invalid input, 409 duplicate name)."""
    _get_owned_skill(db, current_user.id, skill_id)
    try:
        return user_skill_service.update_skill(
            db,
            current_user.id,
            skill_id,
            name=payload.name,
            description=payload.description,
            body=payload.body,
            trigger_keywords=payload.trigger_keywords,
        )
    except SkillConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SkillValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.delete("/{skill_id}", status_code=204)
def delete_skill(
    skill_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Delete an owned skill."""
    try:
        user_skill_service.delete_skill(db, current_user.id, skill_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Skill not found")
    return None


@router.post("/{skill_id}/toggle", response_model=ToggleResponse)
def toggle_skill(
    skill_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Flip the enabled flag; disabled skills are excluded from prompts."""
    try:
        skill = user_skill_service.toggle_skill(db, current_user.id, skill_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Skill not found")
    return ToggleResponse(id=skill.id, enabled=skill.enabled)
