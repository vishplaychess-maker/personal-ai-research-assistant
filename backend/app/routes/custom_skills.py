"""Custom skill marketplace execution (dynamic plugin loading).

POST /api/skills/custom/{skill_id} — run a user-defined skill loaded from
``<data_dir>/custom_skills/``.

Rules:
* The skill must exist in the loader cache (unknown -> 404, indistinguishable
  from a mistyped id).
* The CURRENT user must have enabled this skill in their ``SkillPreference``
  (403 otherwise) — enablement is strictly per user.
* ``inputs`` are validated against the manifest's ``inputs_schema`` (required
  fields present) before the handler runs.
* The execution context is built FRESH per request and contains only this
  user's data (``user_id``, ``inputs``, their own stored config). Nothing is
  cached between users or requests, so no data crosses the ``user_id``
  boundary.
* Handler exceptions and hangs are contained (500 / 504) — a broken custom
  skill can never take the app down.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import app.services.custom_skill_loader as custom_skill_loader
from app.database import get_db
from app.models.models import SkillPreference, User
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skills/custom", tags=["custom-skills"])


class CustomSkillExecuteRequest(BaseModel):
    inputs: Dict[str, Any] = Field(default_factory=dict)


class CustomSkillExecuteResponse(BaseModel):
    skill_id: str
    result: Dict[str, Any]


def _missing_required_inputs(
    schema: Dict[str, Dict[str, Any]], inputs: Dict[str, Any]
) -> List[str]:
    """Names of required ``inputs_schema`` fields that are absent/blank."""
    missing: List[str] = []
    for key, spec in (schema or {}).items():
        if not isinstance(spec, dict) or not spec.get("required"):
            continue
        value = inputs.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(str(spec.get("label") or key))
    return missing


def _stored_config(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@router.post("/{skill_id}", response_model=CustomSkillExecuteResponse)
async def execute_custom_skill(
    skill_id: str,
    payload: CustomSkillExecuteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Run a user-defined custom skill on behalf of the current user."""
    skill = custom_skill_loader.get_custom_skill(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Unknown custom skill")

    pref = (
        db.query(SkillPreference)
        .filter(
            SkillPreference.user_id == current_user.id,
            SkillPreference.skill_id == skill_id,
        )
        .first()
    )
    if pref is None or not pref.enabled:
        raise HTTPException(
            status_code=403,
            detail="Enable this custom skill in Settings > Skills before running it.",
        )

    missing = _missing_required_inputs(skill.inputs_schema, payload.inputs)
    if missing:
        raise HTTPException(
            status_code=422,
            detail="Missing required input(s): " + ", ".join(missing),
        )

    # Fresh, strictly user-scoped context — the only config passed to the
    # handler is the CURRENT user's own stored preference.
    context: Dict[str, Any] = {
        "skill_id": skill_id,
        "user_id": current_user.id,
        "inputs": dict(payload.inputs),
        "config": _stored_config(pref.config_json),
    }

    try:
        result = await custom_skill_loader.run_custom_skill(skill, context)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"The custom skill did not finish within "
            f"{custom_skill_loader.EXECUTION_TIMEOUT_S:g}s.",
        )
    except Exception:
        # Never leak handler tracebacks to the client; log server-side only.
        logger.exception(
            "custom skill '%s' failed for user %s", skill_id, current_user.id
        )
        raise HTTPException(status_code=500, detail="Custom skill execution failed.")

    if not isinstance(result, dict):
        result = {"output": result}
    return CustomSkillExecuteResponse(skill_id=skill_id, result=result)
