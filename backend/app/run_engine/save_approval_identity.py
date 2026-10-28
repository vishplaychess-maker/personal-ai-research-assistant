"""Phase 1B.2R MP-4C - the server-owned memory/directive approval identities.

ONE canonical approval-TrustedTool identity per model-saveable target, created
through the project's production registry helpers (compute_identity /
register_or_refresh). No new registry, no gateway bypass.

SEPARATION OF MEANINGS (identical to python_approval_identity):

* locked=False means ONLY "this exact server-owned tool identity is eligible to
  create an approval PROPOSAL". It never means "write without approval". Every
  proposal still requires an explicit, server-owned approval consumed exactly
  once by the gateway.
* A freshly registered identity is created locked=True ("newly discovered") by
  the registry. This module may bootstrap ONLY that pristine first-party case
  through the registry's own unlock_if_unchanged opt-in.
* An operator lock is NEVER cleared here, and a drifted identity is NEVER
  auto-unlocked - the registry re-locks it on every refresh.

No write, no memory/directive persistence and no content logging happens here.
"""

from __future__ import annotations

from typing import Optional

from app.approval import registry

#: The two stable tool ids. One per model-saveable target.
MEMORY_TOOL_ID = "memory_write"
DIRECTIVE_TOOL_ID = "directive_write"
SAVE_TOOL_IDS = (MEMORY_TOOL_ID, DIRECTIVE_TOOL_ID)

#: Stable built-in owner/server identities for first-party (non-MCP) tools.
MEMORY_TOOL_NAME = "memory_write"
MEMORY_SERVER_MANIFEST_ID = "builtin:memory_write"
MEMORY_EXECUTABLE_IDENTITY = "builtin:app.services.memory_service@1"
MEMORY_CONFIG_REF_ID = "builtin:memory_write:v1"

DIRECTIVE_TOOL_NAME = "directive_write"
DIRECTIVE_SERVER_MANIFEST_ID = "builtin:directive_write"
DIRECTIVE_EXECUTABLE_IDENTITY = "builtin:app.services.directive_service@1"
DIRECTIVE_CONFIG_REF_ID = "builtin:directive_write:v1"

TOOL_VERSION = "1"
POLICY_VERSION = "1b2r-mp4c-save-1"

#: Deterministic, STRICT input schemas. Category is a bounded enum; content is
#: a non-empty string. minLength is a CHARACTER check and is NOT a substitute
#: for the configured character ceiling, which application code enforces via
#: run_engine_memory_write_max_chars().
MEMORY_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "minLength": 3},
        "category": {"type": "string"},
    },
    "required": ["content", "category"],
    "additionalProperties": False,
}
DIRECTIVE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"content": {"type": "string", "minLength": 3}},
    "required": ["content"],
    "additionalProperties": False,
}

NEWLY_DISCOVERED_REASON = "newly discovered"

_SPEC = {
    MEMORY_TOOL_ID: dict(
        tool_name=MEMORY_TOOL_NAME,
        manifest=MEMORY_SERVER_MANIFEST_ID,
        executable=MEMORY_EXECUTABLE_IDENTITY,
        config_ref=MEMORY_CONFIG_REF_ID,
        schema=MEMORY_INPUT_SCHEMA,
        risk={"risk": "persistent_user_memory", "requires_approval": True},
    ),
    DIRECTIVE_TOOL_ID: dict(
        tool_name=DIRECTIVE_TOOL_NAME,
        manifest=DIRECTIVE_SERVER_MANIFEST_ID,
        executable=DIRECTIVE_EXECUTABLE_IDENTITY,
        config_ref=DIRECTIVE_CONFIG_REF_ID,
        schema=DIRECTIVE_INPUT_SCHEMA,
        risk={"risk": "persistent_agent_directive", "requires_approval": True},
    ),
}


def kind_for_tool_id(tool_id: str) -> Optional[str]:
    """'memory' | 'directive' | None (fail closed for anything else)."""
    if tool_id == MEMORY_TOOL_ID:
        return "memory"
    if tool_id == DIRECTIVE_TOOL_ID:
        return "directive"
    return None


def tool_id_for_kind(kind: str) -> Optional[str]:
    if kind == "memory":
        return MEMORY_TOOL_ID
    if kind == "directive":
        return DIRECTIVE_TOOL_ID
    return None


def input_schema_for_tool_id(tool_id: str) -> Optional[dict]:
    spec = _SPEC.get(tool_id)
    return dict(spec["schema"]) if spec else None


def _register(db, owner_user_id: int, tool_id: str, *, unlock_if_unchanged: bool,
              clock=None):
    spec = _SPEC[tool_id]
    return registry.register_or_refresh(
        db, owner_user_id=int(owner_user_id), tool_id=tool_id,
        tool_name=spec["tool_name"], mcp_server_name=spec["manifest"],
        executable_identity=spec["executable"], non_secret_launch_config={},
        config_ref_id=spec["config_ref"], input_schema=dict(spec["schema"]),
        tool_version=TOOL_VERSION, risk_metadata=dict(spec["risk"]),
        policy_version=POLICY_VERSION, unlock_if_unchanged=unlock_if_unchanged,
        clock=clock,
    )


def ensure_save_approval_identity(db, owner_user_id: int, tool_id: str, *, clock=None):
    """Idempotently ensure the exact first-party identity exists and is eligible
    to create a PROPOSAL. Bootstraps ONLY the pristine newly-created case; an
    operator lock or a drifted identity stays locked (gateway then blocks)."""
    if tool_id not in _SPEC:
        raise ValueError("unknown save tool id")
    row = _register(db, owner_user_id, tool_id, unlock_if_unchanged=False, clock=clock)
    reason = (getattr(row, "lock_reason", None) or "").strip().lower()
    if getattr(row, "locked", False) and reason == NEWLY_DISCOVERED_REASON:
        row = _register(db, owner_user_id, tool_id, unlock_if_unchanged=True, clock=clock)
    return row


def resolve_save_identity(db, owner_user_id: int, tool_id: str,
                          tenant_id: Optional[str] = None):
    """Gateway-side resolution - fail closed (None) when unknown, disabled,
    locked, drifted or tenant-mismatched."""
    if tool_id not in _SPEC:
        return None
    return registry.resolve_trusted(
        db, owner_user_id=int(owner_user_id), tool_id=tool_id, tenant_id=tenant_id,
    )


__all__ = [
    "MEMORY_TOOL_ID", "DIRECTIVE_TOOL_ID", "SAVE_TOOL_IDS", "TOOL_VERSION",
    "POLICY_VERSION", "MEMORY_INPUT_SCHEMA", "DIRECTIVE_INPUT_SCHEMA",
    "NEWLY_DISCOVERED_REASON", "kind_for_tool_id", "tool_id_for_kind",
    "input_schema_for_tool_id", "ensure_save_approval_identity",
    "resolve_save_identity",
]
