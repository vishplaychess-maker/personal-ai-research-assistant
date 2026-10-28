"""Per-user configuration for official MCP servers.

Values live in ``SkillPreference.config_json`` under a ``config`` object, with
every value encrypted via the existing Fernet helpers. The stored shape is::

    {"config": {"GITHUB_TOKEN": "gAAAA..."}, "connected": true, "checked_at": "..."}

No raw value is ever returned by the API; list endpoints expose presence only.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from sqlalchemy.orm import Session

from app.models.models import SkillPreference
from app.services import mcp_registry
from app.services.encryption_service import decrypt_key, encrypt_key


class McpServerNotFound(LookupError):
    """Unknown official server id."""


class McpConfigError(ValueError):
    """Invalid or incomplete configuration (safe to show)."""


class McpConnectionError(ValueError):
    """The credentials failed the live connection check (safe to show)."""


def _load_json(config_json: Optional[str]) -> Dict[str, Any]:
    if not config_json:
        return {}
    try:
        data = json.loads(config_json)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _stored_config(config_json: Optional[str]) -> Dict[str, str]:
    data = _load_json(config_json)
    raw = data.get("config")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v}


def decrypt_stored_config(config_json: Optional[str]) -> Dict[str, str]:
    return {key: decrypt_key(value) for key, value in _stored_config(config_json).items()}


def is_configured(config_json: Optional[str], required_keys: tuple[str, ...]) -> bool:
    stored = _stored_config(config_json)
    return bool(required_keys) and all(bool(stored.get(key)) for key in required_keys)


def get_preference(db: Session, user_id: int, server_id: str) -> Optional[SkillPreference]:
    return (
        db.query(SkillPreference)
        .filter(
            SkillPreference.user_id == user_id,
            SkillPreference.skill_id == server_id,
        )
        .first()
    )


def build_view(
    server: mcp_registry.OfficialMCPServer, pref: Optional[SkillPreference]
) -> Dict[str, Any]:
    """Public, secret-free view used by both the registry and capabilities APIs."""
    stored = _stored_config(pref.config_json if pref else None)
    configured = is_configured(pref.config_json if pref else None, server.required_config_keys)
    enabled = bool(pref.enabled) if pref else False
    connected_flag = bool(_load_json(pref.config_json if pref else None).get("connected"))
    if enabled and configured and connected_flag:
        status = "connected"
    elif configured:
        status = "disconnected"
    else:
        status = "not_configured"
    return {
        "id": server.id,
        "name": server.name,
        "description": server.description,
        "required_config_keys": list(server.required_config_keys),
        "enabled": enabled,
        "status": status,
        "configured_keys": [key for key in server.required_config_keys if stored.get(key)],
    }


def save_server_config(
    db: Session,
    user_id: int,
    server_id: str,
    *,
    enabled: bool,
    provided: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Merge, validate, encrypt, and persist one server's configuration."""
    server = mcp_registry.get_server(server_id)
    if server is None:
        raise McpServerNotFound(server_id)

    pref = get_preference(db, user_id, server_id)
    plain = decrypt_stored_config(pref.config_json if pref else None)

    for key, value in (provided or {}).items():
        if key not in server.required_config_keys:
            raise McpConfigError(f"Unknown config key '{key}'.")
        if isinstance(value, str) and value.strip():
            plain[key] = value.strip()

    if enabled:
        missing = [key for key in server.required_config_keys if not plain.get(key)]
        if missing:
            raise McpConfigError(
                "Missing required configuration: " + ", ".join(missing)
            )
        if not mcp_registry.check_connection(server_id, plain):
            raise McpConnectionError(
                "Connection check failed. Verify the credentials and try again."
            )

    # encrypt_key raises RuntimeError when no server key is configured; routes
    # turn that into a 500 rather than storing plaintext.
    encrypted = {key: encrypt_key(value) for key, value in plain.items() if value}
    payload = {
        "config": encrypted,
        "connected": bool(enabled),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    if pref is None:
        pref = SkillPreference(
            user_id=user_id,
            skill_id=server_id,
            enabled=enabled,
            config_json=json.dumps(payload, sort_keys=True),
        )
        db.add(pref)
    else:
        pref.enabled = enabled
        pref.config_json = json.dumps(payload, sort_keys=True)
    db.commit()
    db.refresh(pref)
    return build_view(server, pref)


def list_server_views(db: Session, user_id: int) -> list[Dict[str, Any]]:
    prefs = {
        row.skill_id: row
        for row in db.query(SkillPreference)
        .filter(SkillPreference.user_id == user_id)
        .all()
    }
    return [build_view(server, prefs.get(server.id)) for server in mcp_registry.list_servers()]
