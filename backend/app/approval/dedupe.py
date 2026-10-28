"""``proposal_dedupe_key`` — server-owned identity of ONE logical MCP proposal.

Distinct from every 1B.1 hash:

* ``args_hash``       — arguments only.
* ``action_hash``     — the one-time execution instance (has ``action_nonce`` +
  ``run_id``); changes on every attempt, so it cannot dedupe repeated proposals
  of the *same* logical call.
* ``grant_scope_hash``— the reusable session-grant scope (excludes ``run_id`` /
  ``action_nonce`` / the per-call identity); too broad for one-time proposals.
* ``tool_call_id`` alone — not sufficient; the key also binds the full trusted
  scope so a changed schema / fingerprint / args / policy is a different
  proposal even for the same slot.

The key binds ONLY server-derived, trusted values. It is never accepted or
overridable from the client / LLM. It is used to enforce "one ACTIVE
PendingApproval per logical proposal" (partial-unique index +
``request_approval`` reuse).

KNOWN LIMITATION: the live ``/messages/stream`` path has no durable
``execution_turn_id`` / ``tool_call_id`` (Phase 1B.2 does not add one). So this
dedupe guarantee applies only where the same trusted persisted turn/call ids
are supplied through an internal path — NOT across real streaming HTTP retries.
"""

from __future__ import annotations

from app.approval.hashing import _digest, _require_exact
from app.approval.canonical import canonicalize

_DEDUPE_TAG = b"thunder.approval.proposal_dedupe_key.v1"
PROPOSAL_DEDUPE_VERSION = "1"

# Every field must be present (callers pass explicit ``None`` for absent values
# so "absent" and "present & null" share one deterministic pre-image).
PROPOSAL_DEDUPE_FIELDS = (
    "proposal_dedupe_version",
    "tenant_id",
    "user_id",
    "session_id",
    "run_id",
    "execution_turn_id",
    "tool_call_id",
    "tool_id",
    "tool_version",
    "source_type",
    "mcp_server_id",
    "server_fingerprint",
    "tool_schema_hash",
    "args_hash",
    "affected_resources",   # sorted list
    "network_scope",
    "filesystem_scope",
    "credential_scope",
    "risk_level",
    "risk_scope_flags",     # sorted list
    "policy_version",
)


def proposal_dedupe_key(fields: dict) -> str:
    """SHA-256 over the canonicalized dedupe field set. ``fields`` must contain
    exactly ``PROPOSAL_DEDUPE_FIELDS``."""
    ordered = _require_exact("proposal_dedupe_key", fields, PROPOSAL_DEDUPE_FIELDS)
    # normalise the two list fields so flag/resource ordering is not significant
    ordered = dict(ordered)
    for seqf in ("affected_resources", "risk_scope_flags"):
        v = ordered.get(seqf) or []
        ordered[seqf] = sorted(v)
    return _digest(_DEDUPE_TAG, canonicalize(ordered))
