"""Approval domain (Phase 1B.1).

Persistent, tamper-evident, hash-bound approvals. Domain-only: no MCP / Python /
terminal / browser / streaming / langgraph / route / frontend wiring, no auto
mode, no carve-out enforcement, no paid calls. A later phase (1B.2) binds the
existing transient approval mechanisms to this layer.
"""

from app.approval.canonical import CanonicalizationError, canonicalize
from app.approval.clock import Clock, FixedClock, SystemClock
from app.approval.envelope import ACTION_ENVELOPE_VERSION, ActionEnvelope, build_envelope
from app.approval.errors import (
    ActiveProposalLimitError,
    ApprovalAlreadyConsumed,
    ApprovalError,
    ApprovalExpired,
    ChainIntegrityError,
    EnvelopeMismatch,
    GrantScopeError,
    InvalidTransition,
)
from app.approval.hashing import (
    GENESIS,
    GRANT_SCOPE_VERSION,
    action_hash,
    args_hash,
    grant_scope_hash,
    row_hash,
    server_fingerprint,
    tool_schema_hash,
)
from app.approval.action_log import ChainReport, append_event, chain_scope_id, verify_chain
from app.approval.dedupe import (
    PROPOSAL_DEDUPE_FIELDS,
    PROPOSAL_DEDUPE_VERSION,
    proposal_dedupe_key,
)
from app.approval.identity import open_tool_call, open_turn
from app.approval import registry
from app.approval import gateway
from app.approval.gateway import GatewayContext, GatewayResult
from app.approval.service import (
    GRANT_AUTHORIZATION_USED_EVENT,
    consume_approval,
    create_session_grant,
    dispatch_with_session_grant,
    reconstruct_envelope,
    record_decision,
    request_approval,
    resolve_session_grant,
    revoke_session_grant,
)
from app.approval.state_machine import (
    APPROVED,
    CONSUMED,
    DENIED,
    EXPIRED,
    REQUESTED,
    REVOKED,
    STATES,
    TERMINAL,
)

__all__ = [
    "canonicalize", "CanonicalizationError",
    "Clock", "SystemClock", "FixedClock",
    "ActionEnvelope", "build_envelope", "ACTION_ENVELOPE_VERSION",
    "args_hash", "action_hash", "grant_scope_hash", "GRANT_SCOPE_VERSION",
    "tool_schema_hash", "server_fingerprint", "row_hash", "GENESIS",
    "request_approval", "record_decision", "consume_approval",
    "create_session_grant", "resolve_session_grant", "dispatch_with_session_grant",
    "revoke_session_grant", "GRANT_AUTHORIZATION_USED_EVENT",
    "proposal_dedupe_key", "PROPOSAL_DEDUPE_FIELDS", "PROPOSAL_DEDUPE_VERSION",
    "open_turn", "open_tool_call", "registry", "gateway", "reconstruct_envelope",
    "GatewayContext", "GatewayResult",
    "append_event", "verify_chain", "chain_scope_id", "ChainReport",
    "REQUESTED", "APPROVED", "DENIED", "EXPIRED", "CONSUMED", "REVOKED",
    "STATES", "TERMINAL",
    "ApprovalError", "EnvelopeMismatch", "InvalidTransition", "ApprovalExpired",
    "ApprovalAlreadyConsumed", "GrantScopeError", "ChainIntegrityError",
    "ActiveProposalLimitError",
]
