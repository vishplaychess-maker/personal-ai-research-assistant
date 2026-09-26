"""Hashes that bind an approval to a complete, trusted action.

All hashes are SHA-256 hex (matches ``refresh_token_service`` /
``cache_service`` in this repo). Every pre-image starts with a **domain tag**
and uses **length-prefixed** separation between parts, so no concatenation of
different structured inputs can collide.

Layers:
  args_hash          - hash of the canonicalized tool arguments only.
  tool_schema_hash   - hash of the tool's declared input schema.
  server_fingerprint - stable, non-secret identity of the trusted server/tool.
  action_hash        - hash of the ENTIRE trusted action envelope (which
                       already contains args_hash + server_fingerprint +
                       tool_schema_hash + tenant/user/session + nonce +
                       policy_version). Approval consumption verifies THIS.
  row_hash           - one link of the tamper-evident ActionLog chain.
"""

from __future__ import annotations

import hashlib

from app.approval.canonical import canonicalize

# Deterministic genesis for an ActionLog chain (64 hex zeros).
GENESIS = "0" * 64

_ARGS_TAG = b"thunder.approval.args_hash.v1"
_SCHEMA_TAG = b"thunder.approval.tool_schema_hash.v1"
_FINGERPRINT_TAG = b"thunder.approval.server_fingerprint.v1"
_ACTION_TAG = b"thunder.approval.action_hash.v1"
_ROW_TAG = b"thunder.approval.row_hash.v1"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lp(part: bytes) -> bytes:
    """Length-prefix a part: 8-byte big-endian length || bytes."""
    return len(part).to_bytes(8, "big") + part


def _digest(tag: bytes, *parts: bytes) -> str:
    h = hashlib.sha256()
    h.update(_lp(tag))
    for p in parts:
        h.update(_lp(p))
    return h.hexdigest()


def args_hash(args: object) -> str:
    """SHA-256 of the canonicalized tool arguments."""
    return _digest(_ARGS_TAG, canonicalize(args))


def tool_schema_hash(schema: object) -> str:
    """SHA-256 of the tool's declared input schema (canonicalized).

    A changed schema changes this hash -> changes action_hash / grant match ->
    invalidates any earlier approval or grant for the tool.
    """
    return _digest(_SCHEMA_TAG, canonicalize(schema))


# A launch config carries credential VALUES we must never hash. Key matching
# reuses the Phase 1A precise policy (exact, case-insensitive, or anchored
# suffix — see app.approval.redaction) PLUS a small set of bare credential words
# that are unambiguous in a launch-config context. This preserves benign
# identity-relevant fields such as `token_budget`, `max_tokens`, `tokenizer`,
# `timeout`, `model`, `keymap` while redacting `api_key`, `authorization`,
# `password`, `secret`, `client_secret`, `*_api_key`, `*_secret`, `token`,
# `key`, `credential`, `cookie`, ...
_FINGERPRINT_BARE_SECRET_KEYS = frozenset({
    "token", "key", "secret", "auth", "cred", "credential", "credentials",
    "cookie", "passphrase", "apikey", "bearer",
})


def _is_credential_key(k: object) -> bool:
    if not isinstance(k, str):
        return False
    from app.approval.redaction import _is_sensitive_key  # local import: avoid cycle

    kl = k.strip().lower()
    return kl in _FINGERPRINT_BARE_SECRET_KEYS or _is_sensitive_key(k)


def _scrub_launch_config(cfg: object) -> object:
    """Recursively replace credential VALUES in a non-secret launch config with a
    fixed marker, so a rotated credential does not churn the fingerprint and a
    secret never lands in the hash pre-image. Benign identity fields are kept."""
    if isinstance(cfg, dict):
        out: dict = {}
        for k, v in cfg.items():
            out[str(k)] = "<redacted>" if _is_credential_key(k) else _scrub_launch_config(v)
        return out
    if isinstance(cfg, (list, tuple)):
        return [_scrub_launch_config(x) for x in cfg]
    return cfg


def server_fingerprint(
    *,
    source_type: str,
    server_manifest_id: str | None,
    executable_identity: str | None,
    launch_config: object,
    tool_name: str,
    tool_schema_hash: str,
    config_ref_id: str | None,
) -> str:
    """Stable, non-secret identity of a trusted server + one of its tools.

    INCLUDED (stable, non-secret): source_type, normalized server manifest id,
    executable/package identity + version, normalized non-secret launch config,
    discovered tool name, tool-schema hash, and a configuration/credential
    *reference* id (`config_ref_id`).

    EXCLUDED: raw credentials, secret env-var values, tokens, PIDs, ports,
    timestamps, absolute paths, any per-launch volatile data.

    INPUT CONTRACT for ``launch_config`` (enforced by the callers that build it,
    not guessed here):
      * raw credentials MUST NOT be placed in ``launch_config`` at all;
      * credentials are represented via a server-owned credential/config
        reference (``config_ref_id``), never the value;
      * an unrecognised raw-credential field in ``launch_config`` is an
        operator/configuration violation, not something this function is
        expected to launder;
      * raw secrets must never be logged or persisted as fingerprint inputs.
    As defence-in-depth, ``_scrub_launch_config`` replaces values under
    precisely-matched sensitive keys (exact / anchored-suffix policy shared
    with the Phase 1A redactor, plus a small bare-credential-word set) with a
    fixed marker so a rotated credential never churns the fingerprint. It does
    NOT use a broad substring heuristic — benign identity-relevant fields such
    as ``token_budget``, ``max_tokens``, ``tokenizer``, ``sort_key``,
    ``idempotency_key``, ``keyboard_layout``, ``tokenization_mode`` are kept.

    A changed tool schema or a changed trusted server identity changes this
    value and therefore invalidates an earlier approval/grant.
    """
    payload = {
        "source_type": source_type,
        "server_manifest_id": server_manifest_id,
        "executable_identity": executable_identity,
        "launch_config": _scrub_launch_config(launch_config),
        "tool_name": tool_name,
        "tool_schema_hash": tool_schema_hash,
        "config_ref_id": config_ref_id,
    }
    return _digest(_FINGERPRINT_TAG, canonicalize(payload))


# The exact set of keys the action envelope contributes to action_hash.
ACTION_HASH_FIELDS = (
    "action_envelope_version",
    "tool_id",
    "tool_version",
    "source_type",
    "mcp_server_id",
    "server_fingerprint",
    "tool_schema_hash",
    "args_hash",
    "affected_resources",
    "network_scope",
    "filesystem_scope",
    "credential_scope",
    "risk_level",
    "risk_scope_flags",
    "tenant_id",
    "user_id",
    "session_id",
    "run_id",
    "action_nonce",
    "policy_version",
)


def _require_exact(name: str, fields: dict, allowed: tuple[str, ...]) -> dict:
    missing = [k for k in allowed if k not in fields]
    if missing:
        raise KeyError(f"{name}: missing fields {missing}")
    extra = [k for k in fields if k not in allowed]
    if extra:
        raise KeyError(f"{name}: unexpected fields {extra}")
    return {k: fields[k] for k in allowed}


def action_hash(envelope_fields: dict) -> str:
    """SHA-256 of the complete ONE-TIME action instance.

    Includes the per-attempt ``action_nonce`` and ``run_id`` — so two otherwise
    identical attempts have DIFFERENT action_hash values. This is the identity a
    single ``PendingApproval`` is consumed against, never the identity of a
    reusable grant (see ``grant_scope_hash``).

    ``envelope_fields`` must contain exactly ``ACTION_HASH_FIELDS`` (missing keys
    are an error — callers pass ``None`` explicitly for absent values so "field
    absent" and "field present and null" share one deterministic pre-image).
    ``purpose`` is intentionally NOT a field here.
    """
    ordered = _require_exact("action_hash", envelope_fields, ACTION_HASH_FIELDS)
    return _digest(_ACTION_TAG, canonicalize(ordered))


# Bump when the grant-scope field set / meaning changes. Distinct from the
# one-time envelope version.
GRANT_SCOPE_VERSION = "1"

_GRANT_SCOPE_TAG = b"thunder.approval.grant_scope_hash.v1"

# The STABLE authorization template for permitted repetition within one
# authenticated session. Everything per-attempt is excluded: action_nonce,
# run_id, approval id, timestamps, and the untrusted purpose text.
GRANT_SCOPE_HASH_FIELDS = (
    "grant_scope_version",
    "tenant_id",
    "user_id",
    "session_id",
    "tool_id",
    "tool_version",
    "source_type",
    "mcp_server_id",
    "server_fingerprint",
    "tool_schema_hash",
    "args_hash",
    "affected_resources",
    "network_scope",
    "filesystem_scope",
    "credential_scope",
    "risk_level",
    "risk_scope_flags",
    "policy_version",
)


def grant_scope_hash(scope_fields: dict) -> str:
    """SHA-256 of the stable session-grant authorization template.

    Unchanged by a new ``action_nonce`` or ``run_id``; changed by any of tenant
    / user / session / tool identity+version / source / MCP server / fingerprint
    / schema hash / args / resources / network-fs-credential scope / risk / policy
    version. A grant match authorises ONLY its exact grant_scope_hash.
    """
    ordered = _require_exact("grant_scope_hash", scope_fields, GRANT_SCOPE_HASH_FIELDS)
    return _digest(_GRANT_SCOPE_TAG, canonicalize(ordered))


def row_hash(prev_row_hash: str, event_payload: dict) -> str:
    """One ActionLog chain link:

        row_hash = SHA-256( tag || prev_row_hash || canonical(event_payload) )
    """
    return _digest(
        _ROW_TAG,
        prev_row_hash.encode("ascii"),
        canonicalize(event_payload),
    )
