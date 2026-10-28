"""Typed errors for the approval domain (Phase 1B.1).

All inherit from ``ApprovalError`` so a call site can catch the family without
catching unrelated exceptions. None of these carry secret material — messages
name the *reason*, never a raw argument value / credential.
"""

from __future__ import annotations


class ApprovalError(Exception):
    """Base class for every approval-domain failure."""


class CanonicalizationError(ApprovalError):
    """A value could not be canonicalized (NaN/Infinity, bytes, a set, a custom
    object, a non-string mapping key, ...)."""


class EnvelopeError(ApprovalError):
    """The action envelope is structurally invalid (missing field, wrong type)."""


class EnvelopeMismatch(ApprovalError):
    """A resubmitted action envelope does not match the persisted action_hash
    (or args_hash). The approval cannot be consumed for this action."""


class InvalidTransition(ApprovalError):
    """A state transition was refused: the row was not in the required
    precondition state (already decided / consumed / expired / revoked), or a
    concurrent writer won the compare-and-swap."""


class ApprovalExpired(ApprovalError):
    """The approval's TTL elapsed before consumption (server clock decides)."""


class ApprovalAlreadyConsumed(ApprovalError):
    """The approval was already consumed exactly once; replay is refused."""


class GrantScopeError(ApprovalError):
    """A session grant does not cover this action — wrong tenant / user /
    session / tool identity / server fingerprint / tool-schema hash / action
    hash / policy version, or the grant is expired or revoked."""


class ChainIntegrityError(ApprovalError):
    """The tamper-evident ActionLog chain failed verification, or a concurrent
    append could not be linearized."""


class ActiveProposalLimitError(ApprovalError):
    """Creating this proposal would exceed a bounded active-proposal ceiling
    (per execution turn / session / user). Fail closed — no row is created.
    Carries only the scope name, the limit, and the observed active count —
    never any argument value."""

    def __init__(self, scope: str, limit: int, active_count: int):
        self.scope = scope
        self.limit = limit
        self.active_count = active_count
        super().__init__(
            f"active proposal limit reached for {scope}: "
            f"{active_count} active >= limit {limit}"
        )
