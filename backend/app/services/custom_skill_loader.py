"""Custom skill marketplace loader (dynamic plugin loading).

Users drop self-contained Python skills into ``<data_dir>/custom_skills/``;
each skill is a folder::

    data/custom_skills/<folder>/
        manifest.json   # id, name, description, icon, inputs_schema
        handler.py      # async def execute(context: dict) -> dict

``scan_and_load_custom_skills()`` is called once at startup (and by tests):
it validates each manifest, imports ``handler.py`` with ``importlib.util``
and caches the result. HARD INVARIANT: a broken skill (bad JSON, missing
fields, syntax errors, a crashing import) is logged and skipped — it can
NEVER crash the app or the scan. That is why every per-skill step runs
inside its own ``try/except Exception``.

Execution is user-scoped: the caller (``routes/custom_skills.py``) builds a
FRESH context per request containing only that user's ``user_id``, ``inputs``
and their own stored ``SkillPreference.config_json`` — nothing is shared or
cached between users, so no data can leak across ``user_id`` boundaries.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

#: Directory name inside the writable data dir.
CUSTOM_SKILLS_DIRNAME = "custom_skills"

#: Skill ids become URL segments, ``skill_preferences.skill_id`` (String(50))
#: and Python module name suffixes — keep them strictly constrained.
_SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")

#: Fallback lucide icon when a manifest omits/invalidates ``icon``.
DEFAULT_ICON = "puzzle"

#: Hard cap for one execution; hung handlers must not wedge a request forever.
EXECUTION_TIMEOUT_S = 30.0

#: Types accepted for ``inputs_schema`` fields (informational, kept loose so
#: user-defined schemas never break the scan).
_ALLOWED_INPUT_TYPES = {"string", "text", "number", "boolean", "password"}


class CustomSkillError(Exception):
    """Raised when a loaded skill cannot actually be executed."""


@dataclass
class LoadedCustomSkill:
    """A successfully validated and imported custom skill."""

    id: str
    name: str
    description: str
    icon: str
    inputs_schema: Dict[str, Dict[str, Any]]
    directory: Path
    module: object
    manifest: Dict[str, Any] = field(default_factory=dict, repr=False)

    def execute(self, context: dict) -> Any:
        handler = getattr(self.module, "execute", None)
        if not callable(handler):
            raise CustomSkillError(
                "handler.py must define an async function execute(context)."
            )
        return handler(context)


# id -> LoadedCustomSkill. Rebuilt by every scan; read paths never raise.
_LOADED: Dict[str, LoadedCustomSkill] = {}
_SCAN_RAN = False


def get_custom_skills_dir() -> Path:
    """Writable custom-skills directory (recomputed per call; tests patch
    ``settings.data_dir``)."""
    return Path(settings.data_dir) / CUSTOM_SKILLS_DIRNAME


def ensure_custom_skills_dir() -> Path:
    """Create the directory on demand. Never raises."""
    path = get_custom_skills_dir()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "custom_skills: could not create %s (%s)", path, type(exc).__name__
        )
    return path


# ── Manifest validation ────────────────────────────────────────────────────


def _validate_inputs_schema(raw: Any) -> tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    """Return (schema, error). Lenient by design: unknown shapes are dropped
    field-by-field rather than rejecting the whole skill."""
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, "inputs_schema must be a JSON object"
    schema: Dict[str, Dict[str, Any]] = {}
    for key, spec in raw.items():
        if not isinstance(key, str) or not _SKILL_ID_RE.match(key):
            continue  # keys become config keys; keep the same strict shape
        if not isinstance(spec, dict):
            continue
        entry: Dict[str, Any] = {}
        label = spec.get("label")
        if isinstance(label, str) and label.strip():
            entry["label"] = label.strip()
        spec_type = spec.get("type")
        if isinstance(spec_type, str) and spec_type in _ALLOWED_INPUT_TYPES:
            entry["type"] = spec_type
        if spec.get("required"):
            entry["required"] = True
        placeholder = spec.get("placeholder")
        if isinstance(placeholder, str) and placeholder.strip():
            entry["placeholder"] = placeholder.strip()
        schema[key] = entry
    return schema, None


def _validate_manifest(raw: Any, folder_name: str) -> tuple[Optional[dict], str]:
    """Return (manifest, error). ``manifest`` is None when the skill must be
    skipped; ``error`` explains why (for the log, never shown raw to users)."""
    if not isinstance(raw, dict):
        return None, "manifest.json must contain a JSON object"
    skill_id = raw.get("id")
    if not isinstance(skill_id, str) or not _SKILL_ID_RE.match(skill_id):
        return None, (
            "manifest id must be 1-50 chars of a-z, 0-9 and _ starting with a letter"
        )
    if skill_id != folder_name:
        return None, f"manifest id '{skill_id}' must match the folder name '{folder_name}'"
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, "manifest.name must be a non-empty string"
    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        return None, "manifest.description must be a non-empty string"
    icon = raw.get("icon")
    icon_value = icon.strip() if isinstance(icon, str) and icon.strip() else DEFAULT_ICON
    schema, schema_error = _validate_inputs_schema(raw.get("inputs_schema"))
    if schema_error:
        return None, schema_error
    manifest = {
        "id": skill_id,
        "name": name.strip(),
        "description": description.strip(),
        "icon": icon_value,
        "inputs_schema": schema,
    }
    return manifest, ""


# ── Safe import ────────────────────────────────────────────────────────────


def _import_handler(directory: Path, skill_id: str):
    """Import ``handler.py`` via importlib. Any failure (missing file, syntax
    error, import-time crash) propagates to the caller's except block."""
    handler_path = directory / "handler.py"
    if not handler_path.is_file():
        raise FileNotFoundError("handler.py not found in the skill folder")
    module_name = f"thunder_custom_skill_{skill_id}"
    spec = importlib.util.spec_from_file_location(module_name, handler_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not create an import spec for {handler_path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        # Do not leave a half-initialized module in sys.modules.
        sys.modules.pop(module_name, None)
        raise
    return module


# ── Scan / lookup ──────────────────────────────────────────────────────────


def scan_and_load_custom_skills() -> List[Dict[str, str]]:
    """Rescan the custom skills directory from scratch.

    Returns a per-folder status report (``loaded`` / ``skipped`` + reason).
    Never raises: the whole scan is wrapped so even an unreadable directory
    only logs a warning. Existing entries are cleared first so removed or
    fixed skills are reflected on the next scan (app restart).
    """
    global _LOADED, _SCAN_RAN
    _LOADED = {}
    _SCAN_RAN = True
    report: List[Dict[str, str]] = []
    try:
        root = get_custom_skills_dir()
        folders = sorted(
            entry
            for entry in root.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        )
    except OSError as exc:
        logger.warning(
            "custom_skills: scan skipped (%s)", type(exc).__name__
        )
        return report

    for directory in folders:
        folder_name = directory.name
        try:
            manifest_path = directory / "manifest.json"
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest, error = _validate_manifest(raw, folder_name)
            if manifest is None:
                raise ValueError(error)
            module = _import_handler(directory, manifest["id"])
            if not callable(getattr(module, "execute", None)):
                raise CustomSkillError(
                    "handler.py must define an async function execute(context)."
                )
            skill = LoadedCustomSkill(
                id=manifest["id"],
                name=manifest["name"],
                description=manifest["description"],
                icon=manifest["icon"],
                inputs_schema=manifest["inputs_schema"],
                directory=directory,
                module=module,
                manifest=manifest,
            )
            _LOADED[skill.id] = skill
            report.append({"folder": folder_name, "status": "loaded", "id": skill.id})
            logger.info("custom_skills: loaded '%s'", skill.id)
        except Exception as exc:  # noqa: BLE001 — a broken skill must never crash the scan
            report.append(
                {
                    "folder": folder_name,
                    "status": "skipped",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            logger.warning(
                "custom_skills: skipping '%s' (%s: %s)",
                folder_name,
                type(exc).__name__,
                exc,
            )
    return report


def get_custom_skill(skill_id: str) -> Optional[LoadedCustomSkill]:
    """Return a loaded skill, or ``None`` when unknown/not loaded."""
    return _LOADED.get(skill_id)


def list_custom_skills() -> List[LoadedCustomSkill]:
    """All currently loaded custom skills, in id order."""
    return [_LOADED[key] for key in sorted(_LOADED)]


# ── Registry-facing views (kind="custom") ──────────────────────────────────


def _capability_view(skill: LoadedCustomSkill) -> Dict[str, Any]:
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "icon": skill.icon,
        "enabled": False,  # custom skills are opt-in per user
        "config": {},
        "kind": "custom",
        "required_config_keys": [],
        "inputs_schema": {k: dict(v) for k, v in skill.inputs_schema.items()},
    }


def list_custom_capabilities() -> List[Dict[str, Any]]:
    """Catalog entries for the loaded custom skills (``kind="custom"``)."""
    return [_capability_view(skill) for skill in list_custom_skills()]


def get_custom_capability(skill_id: str) -> Optional[Dict[str, Any]]:
    skill = get_custom_skill(skill_id)
    return _capability_view(skill) if skill is not None else None


async def run_custom_skill(skill: LoadedCustomSkill, context: Dict[str, Any]) -> Any:
    """Execute a skill's handler under the hard timeout."""
    handler_result = skill.execute(context)
    return await asyncio.wait_for(handler_result, timeout=EXECUTION_TIMEOUT_S)
