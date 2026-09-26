"""Phase 1B.2R MP-4C-R2A-2 — the server-owned Python approval identity.

ONE canonical approval-`TrustedTool` identity for the EXISTING python_sandbox
tool, created through the project's production registry helpers
(`compute_identity` / `register_or_refresh`). No new registry, no gateway
bypass, no generic "unlock any tool" helper.

SEPARATION OF MEANINGS (mandatory):

* `locked=False` means ONLY "this exact server-owned tool identity is eligible
  to create an approval PROPOSAL". It never means "execute without approval".
  Every proposal still requires an explicit, server-owned approval that is
  consumed exactly once by the gateway.
* A freshly registered identity is created `locked=True` ("newly discovered")
  by the registry. This module may bootstrap ONLY that pristine first-party
  case through the registry's own `unlock_if_unchanged` opt-in.
* An operator lock is NEVER cleared here (its lock_reason is not
  "newly discovered"), and a drifted identity (schema/fingerprint changed) is
  NEVER auto-unlocked — the registry re-locks it on every refresh.

No execution, no subprocess and no code persistence happens in this module.
"""

from __future__ import annotations

from typing import Optional

from app.approval import registry

#: The ONE stable tool id. Matches the application tool name registry entry so
#: the proposed action and the eventual executor share a single identity.
PYTHON_TOOL_ID = "python_sandbox"

#: Stable built-in owner/server identity for a first-party (non-MCP) tool.
PYTHON_TOOL_NAME = "python_sandbox"
PYTHON_SERVER_MANIFEST_ID = "builtin:python_sandbox"
PYTHON_EXECUTABLE_IDENTITY = "builtin:app.tools.python_sandbox@1"
PYTHON_CONFIG_REF_ID = "builtin:python_sandbox:v1"
PYTHON_TOOL_VERSION = "1"
PYTHON_POLICY_VERSION = "1b2r-mp4c-r2a-2"

#: Deterministic, STRICT input schema. Exactly one required string field; no
#: additional properties. NOTE: minLength is a CHARACTER check and is NOT a
#: substitute for the UTF-8 byte ceiling, which application code enforces
#: separately via sandbox_policy.max_code_bytes().
PYTHON_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"code": {"type": "string", "minLength": 1}},
    "required": ["code"],
    "additionalProperties": False,
}

#: Registry lock_reason written when a row is first created.
NEWLY_DISCOVERED_REASON = "newly discovered"


def _register(db, owner_user_id: int, *, unlock_if_unchanged: bool, clock=None):
    return registry.register_or_refresh(
        db, owner_user_id=int(owner_user_id), tool_id=PYTHON_TOOL_ID,
        tool_name=PYTHON_TOOL_NAME,
        mcp_server_name=PYTHON_SERVER_MANIFEST_ID,
        executable_identity=PYTHON_EXECUTABLE_IDENTITY,
        non_secret_launch_config={},
        config_ref_id=PYTHON_CONFIG_REF_ID,
        input_schema=dict(PYTHON_INPUT_SCHEMA),
        tool_version=PYTHON_TOOL_VERSION,
        risk_metadata={"risk": "code_execution", "requires_approval": True},
        policy_version=PYTHON_POLICY_VERSION,
        unlock_if_unchanged=unlock_if_unchanged,
        clock=clock,
    )


def ensure_python_approval_identity(db, owner_user_id: int, *, clock=None):
    """Idempotently ensure the exact first-party Python identity exists and is
    eligible to create a PROPOSAL.

    Behaviour, in order:

    1. Register/refresh. A NEW row is created `locked=True` ("newly discovered");
       an existing row with a CHANGED schema hash or fingerprint is re-locked
       with a "changed" reason; an unchanged row is left exactly as it was.
    2. Bootstrap ONLY the pristine newly-created case, by re-registering with
       the registry's own `unlock_if_unchanged=True` opt-in. This runs only when
       the row is still locked with reason exactly "newly discovered", so:

       * an operator/manual lock is preserved (different reason -> skipped);
       * a drifted identity is preserved locked (reason contains "changed");
       * another tool's row is never touched (filtered by owner + tool_id).

    Returns the resolved `TrustedTool` row (which the caller may still find
    locked, in which case the gateway fails closed — that is the correct,
    safe outcome).
    """
    row = _register(db, owner_user_id, unlock_if_unchanged=False, clock=clock)
    reason = (getattr(row, "lock_reason", None) or "").strip().lower()
    if getattr(row, "locked", False) and reason == NEWLY_DISCOVERED_REASON:
        row = _register(db, owner_user_id, unlock_if_unchanged=True, clock=clock)
    return row


def resolve_python_identity(db, owner_user_id: int, tenant_id: Optional[str] = None):
    """The gateway's own resolution — fail closed (None) when unknown, disabled,
    locked, drifted or tenant-mismatched."""
    return registry.resolve_trusted(
        db, owner_user_id=int(owner_user_id), tool_id=PYTHON_TOOL_ID,
        tenant_id=tenant_id,
    )


__all__ = [
    "PYTHON_TOOL_ID", "PYTHON_TOOL_NAME", "PYTHON_SERVER_MANIFEST_ID",
    "PYTHON_EXECUTABLE_IDENTITY", "PYTHON_CONFIG_REF_ID", "PYTHON_TOOL_VERSION",
    "PYTHON_POLICY_VERSION", "PYTHON_INPUT_SCHEMA", "NEWLY_DISCOVERED_REASON",
    "ensure_python_approval_identity", "resolve_python_identity",
]
