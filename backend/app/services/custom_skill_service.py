"""Shared custom-skill discovery, invocation matching, and execution.

Used by both the chat prompt builder and the OpenBot orchestrator so an enabled
custom skill is visible and executable from either surface. Execution is always
scoped to the current user and reuses the loader's hard 30s timeout.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.models import SkillPreference
from app.services import custom_skill_loader
from app.services.async_bridge import run_coro_sync
from app.services.custom_skill_loader import LoadedCustomSkill, run_custom_skill

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 2500
_VERBS = (
    "run", "use", "execute", "invoke", "call", "start", "trigger",
    "fire", "greet", "apply",
)
_NAME_RE = re.compile(
    r"(?:my name is|call me|name\s*[:=])\s+([A-Za-z][A-Za-z0-9 ._'-]{0,60})",
    re.IGNORECASE,
)
_FIELD_RE = re.compile(
    r"\b([a-z][a-z0-9_]{0,30})\s*[:=]\s*[\"']?([^,;\n\"']{1,80})",
    re.IGNORECASE,
)


@dataclass
class CustomSkillInvocation:
    skill: LoadedCustomSkill
    inputs: Dict[str, Any]
    config: Dict[str, Any]
    missing: List[str]

    @property
    def executable(self) -> bool:
        return not self.missing


def _config(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def enabled_skills(db: Session, user_id: int) -> List[Tuple[LoadedCustomSkill, Dict[str, Any]]]:
    """Loaded, user-enabled custom skills with their stored config."""
    rows = (
        db.query(SkillPreference)
        .filter(
            SkillPreference.user_id == user_id,
            SkillPreference.enabled.is_(True),
        )
        .all()
    )
    found: List[Tuple[LoadedCustomSkill, Dict[str, Any]]] = []
    for row in rows:
        skill = custom_skill_loader.get_custom_skill(row.skill_id)
        if skill is not None:
            found.append((skill, _config(row.config_json)))
    return found


def catalog_block(db: Session, user_id: int) -> str:
    """Prompt section listing enabled custom skills and how they are invoked."""
    skills = enabled_skills(db, user_id)
    if not skills:
        return ""
    lines = [
        "=== Available Custom Skills (enabled by the user) ===",
        "The runtime can execute these with the execute_custom_skill tool "
        "(skill_id + inputs). If the user asks to run one and required inputs "
        "are missing, ask for them instead of inventing values.",
    ]
    for skill, _config_value in skills:
        required = [
            str(spec.get("label") or key)
            for key, spec in (skill.inputs_schema or {}).items()
            if isinstance(spec, dict) and spec.get("required")
        ]
        suffix = f" Required inputs: {', '.join(required)}." if required else ""
        lines.append(f"- {skill.id}: {skill.name} — {skill.description}{suffix}")
    lines.append("=== End Custom Skills ===")
    return "\n".join(lines)


def _extract_inputs(skill: LoadedCustomSkill, text: str) -> Dict[str, Any]:
    inputs: Dict[str, Any] = {}
    schema = skill.inputs_schema or {}
    name_match = _NAME_RE.search(text)
    for key, spec in schema.items():
        spec = spec if isinstance(spec, dict) else {}
        value: Any = None
        if key == "name" and name_match:
            value = name_match.group(1).strip().rstrip(".")
        if value is None:
            for field_match in _FIELD_RE.finditer(text):
                if field_match.group(1).lower() == str(key).lower():
                    value = field_match.group(2).strip()
                    break
        if value is None and "default" in spec:
            value = spec.get("default")
        if value is not None:
            if spec.get("type") == "number":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    pass
            inputs[key] = value
    return inputs


def _missing_required(skill: LoadedCustomSkill, inputs: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    for key, spec in (skill.inputs_schema or {}).items():
        if not isinstance(spec, dict) or not spec.get("required"):
            continue
        value = inputs.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(str(spec.get("label") or key))
    return missing


def match_invocation(db: Session, user_id: int, text: str) -> Optional[CustomSkillInvocation]:
    """Match an explicit 'run/use <skill>' request to an enabled skill."""
    lowered = (text or "").lower()
    if not lowered or not any(re.search(rf"\b{verb}\b", lowered) for verb in _VERBS):
        return None
    for skill, config in enabled_skills(db, user_id):
        names = {skill.name.lower(), skill.id.lower(), skill.id.replace("_", " ")}
        if not any(name and name in lowered for name in names):
            continue
        inputs = _extract_inputs(skill, text)
        return CustomSkillInvocation(
            skill=skill,
            inputs=inputs,
            config=config,
            missing=_missing_required(skill, inputs),
        )
    return None


def execute(invocation: CustomSkillInvocation, user_id: int) -> Any:
    context = {
        "skill_id": invocation.skill.id,
        "user_id": user_id,
        "inputs": dict(invocation.inputs),
        "config": dict(invocation.config),
    }
    return run_coro_sync(lambda: run_custom_skill(invocation.skill, context))


def result_text(result: Any) -> str:
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, ensure_ascii=False, default=str)
    compact = " ".join(text.split())
    if len(compact) > MAX_RESULT_CHARS:
        compact = compact[: MAX_RESULT_CHARS - 1].rstrip() + "…"
    return compact


def prompt_execution_block(
    invocation: CustomSkillInvocation, user_id: int
) -> str:
    """Execute one matched skill and return the prompt block for the model."""
    if invocation.missing:
        return (
            "=== Custom Skill Needs Input ===\n"
            f"The user asked to run {invocation.skill.name}, but these required "
            f"inputs are missing: {', '.join(invocation.missing)}. Ask the user "
            "for them; do not invent values."
        )
    try:
        result = execute(invocation, user_id)
    except Exception as exc:  # noqa: BLE001 - a skill failure is not a chat failure
        logger.warning(
            "custom skill '%s' execution failed (%s)",
            invocation.skill.id,
            type(exc).__name__,
        )
        return (
            "=== Custom Skill Execution Failed ===\n"
            f"{invocation.skill.name} could not run ({type(exc).__name__}). "
            "Tell the user plainly; do not invent a result."
        )
    return (
        "=== Custom Skill Execution Result (trusted handler output) ===\n"
        f"Skill: {invocation.skill.id} ({invocation.skill.name})\n"
        f"Inputs: {json.dumps(invocation.inputs, ensure_ascii=False)}\n"
        f"Result: {result_text(result)}\n"
        "Use this result to answer the user's request."
    )
