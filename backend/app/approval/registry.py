"""Trusted tool registry (Phase 1B.2).

The server's authoritative record of which MCP tools may be dispatched. A tool
executes ONLY when a ``trusted_tools`` row exists for it with
``enabled = True`` and ``locked = False``.

Fail-closed rules:
  * unknown / unregistered tool          -> ``resolve_trusted`` returns None
  * newly discovered tool                -> row created ``locked = True``
  * materially changed tool schema       -> row set ``locked = True``
  * materially changed server fingerprint -> row set ``locked = True``
  * disabled tool                        -> not resolvable
  * DB error while resolving              -> raised, caller blocks execution

"Materially changed" is deterministic: any change to ``tool_schema_hash`` or
``server_fingerprint`` (computed by ``app.approval.hashing`` over server-owned,
NON-SECRET inputs). Existing PendingApprovals and SessionGrants bind these
hashes, so a change invalidates every prior authorization for that tool.

Credentials: only a server-owned ``config_ref_id`` (e.g. ``mcp_server:<id>``)
is stored / hashed / logged — never raw env values, never the credential.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.approval.canonical import canonicalize
from app.approval.clock import Clock, SystemClock
from app.approval.hashing import server_fingerprint, tool_schema_hash
from app.models.models import TrustedTool

DEFAULT_POLICY_VERSION = "1b1.1"


def canonical_schema(schema: object) -> dict:
    """A JSON-object view of a tool input schema, safe to canonicalize/store.
    Non-dict / missing -> ``{}``."""
    if isinstance(schema, dict):
        return schema
    if isinstance(schema, str):
        try:
            v = json.loads(schema)
            return v if isinstance(v, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def compute_identity(
    *,
    tool_id: str,
    tool_name: str,
    server_manifest_id: str | None,
    executable_identity: str | None,
    non_secret_launch_config: object,
    config_ref_id: str | None,
    input_schema: object,
) -> tuple[dict, str, str]:
    """Return ``(canonical_schema_dict, tool_schema_hash, server_fingerprint)``
    from server-owned, non-secret inputs only. ``non_secret_launch_config`` must
    NOT contain raw credentials (the caller strips ``env`` etc.)."""
    sch = canonical_schema(input_schema)
    tsh = tool_schema_hash(sch)
    fp = server_fingerprint(
        source_type="mcp",
        server_manifest_id=server_manifest_id,
        executable_identity=executable_identity,
        launch_config=non_secret_launch_config,
        tool_name=tool_name,
        tool_schema_hash=tsh,
        config_ref_id=config_ref_id,
    )
    return sch, tsh, fp


def register_or_refresh(
    db: Session,
    *,
    owner_user_id: int,
    tool_id: str,
    tool_name: str,
    tenant_id: str | None = None,
    mcp_server_id: int | None = None,
    mcp_server_name: str | None = None,
    executable_identity: str | None = None,
    non_secret_launch_config: object = None,
    config_ref_id: str | None = None,
    input_schema: object = None,
    tool_version: str = "",
    risk_metadata: dict | None = None,
    policy_version: str = DEFAULT_POLICY_VERSION,
    unlock_if_unchanged: bool = False,
    clock: Clock | None = None,
) -> TrustedTool:
    """Idempotently record a tool.

    * new tool                          -> created ``locked = True``
    * unchanged identity (same schema hash + fingerprint):
        - default: left as-is
        - ``unlock_if_unchanged`` (operator re-verification): unlocked
    * changed schema hash or fingerprint -> ``locked = True`` + ``lock_reason``
    """
    clock = clock or SystemClock()
    now = clock.now()
    sch, tsh, fp = compute_identity(
        tool_id=tool_id, tool_name=tool_name,
        server_manifest_id=mcp_server_name or (str(mcp_server_id) if mcp_server_id else None),
        executable_identity=executable_identity,
        non_secret_launch_config=non_secret_launch_config or {},
        config_ref_id=config_ref_id, input_schema=input_schema,
    )
    schema_text = canonicalize(sch).decode("utf-8")
    risk_text = json.dumps(risk_metadata or {}, sort_keys=True, ensure_ascii=False)

    row = (
        db.query(TrustedTool)
        .filter(TrustedTool.owner_user_id == owner_user_id, TrustedTool.tool_id == tool_id)
        .one_or_none()
    )
    if row is None:
        row = TrustedTool(
            id=str(uuid.uuid4()), tenant_id=tenant_id, owner_user_id=owner_user_id,
            tool_id=tool_id, tool_version=tool_version, source_type="mcp",
            mcp_server_id=mcp_server_id, mcp_server_name=mcp_server_name,
            input_schema_json=schema_text, tool_schema_hash=tsh, server_fingerprint=fp,
            config_ref_id=config_ref_id, enabled=True, locked=True,
            lock_reason="newly discovered", risk_metadata_json=risk_text,
            policy_version=policy_version, discovered_at=now, updated_at=now,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = (
                db.query(TrustedTool)
                .filter(TrustedTool.owner_user_id == owner_user_id,
                        TrustedTool.tool_id == tool_id)
                .one()
            )
        else:
            return row

    schema_changed = row.tool_schema_hash != tsh
    fp_changed = row.server_fingerprint != fp
    changed = schema_changed or fp_changed
    row.input_schema_json = schema_text
    row.tool_schema_hash = tsh
    row.server_fingerprint = fp
    row.tool_version = tool_version or row.tool_version
    row.mcp_server_id = mcp_server_id
    row.mcp_server_name = mcp_server_name
    row.config_ref_id = config_ref_id
    row.risk_metadata_json = risk_text
    row.policy_version = policy_version
    row.updated_at = now
    if changed:
        row.locked = True
        if schema_changed and fp_changed:
            row.lock_reason = "tool schema and server fingerprint changed"
        elif fp_changed:
            row.lock_reason = "server fingerprint changed"
        else:
            row.lock_reason = "tool schema changed"
    elif unlock_if_unchanged:
        row.locked = False
        row.lock_reason = None
    db.commit()
    return row


# Stable machine-readable reasons a resolve can fail closed (Phase 1B.2
# correction — feeds the gateway's sanitized `tool.execution.blocked` event).
RESOLVE_REASONS = frozenset({
    "ok", "unknown_tool", "tool_disabled", "tool_locked",
    "schema_mismatch", "fingerprint_mismatch", "tenant_mismatch",
})


def resolve_reason(
    db: Session,
    *,
    owner_user_id: int,
    tool_id: str,
    tenant_id: str | None = None,
) -> tuple[TrustedTool | None, str]:
    """Like ``resolve_trusted`` but also returns a stable reason string from
    ``RESOLVE_REASONS``. ``("ok"`` iff the row is returned; otherwise the row is
    ``None`` and the reason names *why* it failed closed — so the gateway can
    emit a precise, non-sensitive blocked-event reason code."""
    row = (
        db.query(TrustedTool)
        .filter(TrustedTool.owner_user_id == owner_user_id, TrustedTool.tool_id == tool_id)
        .one_or_none()
    )
    if row is None:
        return None, "unknown_tool"
    if not row.enabled:
        return None, "tool_disabled"
    if row.locked:
        lr = (row.lock_reason or "").lower()
        if "fingerprint" in lr and "schema" not in lr:
            return None, "fingerprint_mismatch"
        if "schema" in lr:
            return None, "schema_mismatch"
        return None, "tool_locked"          # e.g. "newly discovered"
    if (row.tenant_id or None) != (tenant_id or None):
        return None, "tenant_mismatch"
    return row, "ok"


def resolve_trusted(
    db: Session,
    *,
    owner_user_id: int,
    tool_id: str,
    tenant_id: str | None = None,
) -> TrustedTool | None:
    """Return the trusted row IFF it is registered, enabled, and unlocked, and
    (when a tenant is given) tenant-matched. Otherwise None (fail closed)."""
    return resolve_reason(
        db, owner_user_id=owner_user_id, tool_id=tool_id, tenant_id=tenant_id,
    )[0]


def lock_tool(db: Session, *, owner_user_id: int, tool_id: str, reason: str,
              clock: Clock | None = None) -> bool:
    clock = clock or SystemClock()
    row = (
        db.query(TrustedTool)
        .filter(TrustedTool.owner_user_id == owner_user_id, TrustedTool.tool_id == tool_id)
        .one_or_none()
    )
    if row is None:
        return False
    row.locked = True
    row.lock_reason = reason[:120]
    row.updated_at = clock.now()
    db.commit()
    return True


def config_ref_for_server(mcp_server_id: int | None) -> str | None:
    """Server-owned reference used in place of any credential/env value."""
    return f"mcp_server:{mcp_server_id}" if mcp_server_id is not None else None


def non_secret_launch_config(command: str | None, args) -> dict:
    """The part of an MCP launch spec that is safe to fingerprint: command +
    args ONLY. ``env`` (which can hold secrets) is deliberately excluded."""
    return {"command": command or "", "args": list(args or [])}
