"""Durable Multi-Agent — the ONE canonical role graph and pure orchestration
contract (Phase 1B.2R MP-3C3D5B).

Pure, DB-free module: no LLM call, no network, no ORM session, no tool
execution, no event emission, no Run mutation. Every function here is a
plain validation/lookup over server-owned strings and ints — the same
"pure schema/normalization" shape as ``app.run_engine.research_sources``
(D4B), just for role/sequence/lineage/budget instead of source snapshots.

Locked user decision (D5B, supersedes the D5A plan's role names): the durable
v1 role graph is a FIXED, single-pass, depth-1 sequence of exactly three
server-owned roles — ``researcher`` -> ``solution_specialist`` -> ``reviewer``.
No revision loop, no retry loop, no role recursion, no parallel execution, no
dynamic role names, and no client-supplied role graph. This is the ONE
canonical ordered tuple (:data:`ROLE_ORDER`) — no other module defines a
second, potentially-drifting copy of the role set or its order.

Why "Solution Specialist", not "Coder"
---------------------------------------
The legacy live multi-agent team's second role is literally named "Coder"
(``app.services.agent_personas.CODER_SYSTEM_PROMPT``) and is code/build-
shaped by construction. The locked D5B decision is that durable v1's second
role must be a GENERAL-PURPOSE solution role: for a coding task it may
produce code or technical implementation guidance, but for a non-coding
task it produces analysis, a structured solution, or an execution
recommendation instead — without any LLM task-router call and without
dynamically creating roles. This module therefore names the role
``"solution_specialist"`` everywhere it is durably persisted or exposed
(``ROLE_SOLUTION_SPECIALIST``, ``RunStep.kind == "agent_solution"``). No
internal compatibility adapter to the legacy "coder" name is implemented —
the D5A plan's own "coder"-shaped ``agent_code``/``agent_artifact_v1``
naming is intentionally NOT reused verbatim (see the D5B completion report
for the artifact-version discussion); nothing here reads or writes the
legacy ``AGENT_PERSONAS["coder"]`` prompt, so no dual-name ambiguity exists
to document an adapter for.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from app.run_engine.errors import RunEngineError


class MultiAgentOrchestrationError(RunEngineError):
    """Base class for multi-agent orchestration-contract violations."""


class InvalidRole(MultiAgentOrchestrationError):
    """A role name outside the closed :data:`ROLE_ORDER` set."""


class IllegalRoleTransition(MultiAgentOrchestrationError):
    """A role was reached out of order (skipped, repeated, or reversed)."""


class ProviderCallBudgetExceeded(MultiAgentOrchestrationError):
    """A caller tried to account for more provider calls than the run may
    ever make."""


# The ONE ``Run.kind`` value for a durable multi-agent run (mirrors
# ``deep_research_readiness``'s sibling constant, ``submission._DEEP_RESEARCH_RUN_KIND``
# — reused by ``engine._RUN_KINDS``/``events._RUN_KINDS`` (already includes it, as
# a plain literal, matching how every other run kind is declared there) and, as
# of this phase, by ``RunWorker.claim_next_queued``/``recover_expired`` to keep
# the generic worker from ever claiming a queued multi-agent run before D5C's
# own dedicated sweep exists).
MULTI_AGENT_RUN_KIND = "multi_agent"

# Server-owned closed progress-code set for the future ``agent.progress``
# control event (``app.run_engine.events.CONTROL_EVENT_SCHEMAS``) and for
# ``multi_agent_artifacts``'s own validation — the ONE definition, imported by
# both rather than re-declared as a second literal. No worker exists yet to
# emit any of these (D5B is foundation-only).
AGENT_PROGRESS_CODES = frozenset({
    "agent_researching", "agent_research_complete",
    "agent_solving", "agent_solution_complete",
    "agent_reviewing", "agent_finalizing",
})

# ── the ONE canonical role graph ────────────────────────────────────

ROLE_RESEARCHER = "researcher"
ROLE_SOLUTION_SPECIALIST = "solution_specialist"
ROLE_REVIEWER = "reviewer"

# Ordered, closed, depth-1 — index+1 IS the role's authoritative sequence
# number. No dynamic role may ever be appended to this tuple at runtime;
# it is a plain module-level constant, never mutated.
ROLE_ORDER: Tuple[str, ...] = (ROLE_RESEARCHER, ROLE_SOLUTION_SPECIALIST, ROLE_REVIEWER)
ROLE_SET = frozenset(ROLE_ORDER)

ROLE_SEQUENCE: Dict[str, int] = {role: i + 1 for i, role in enumerate(ROLE_ORDER)}

# The RunStep.kind each role's artifact is durably sealed under (fits
# RunStep.kind's String(32) column with room to spare). "load_context" is
# NOT listed here — it is the pre-existing seq-1 submission step every
# mode already has (app.run_engine.chat_stream.CHAT_STEP_KIND), reused
# unchanged; the multi-agent role graph starts at seq 2.
STEP_KIND_FOR_ROLE: Dict[str, str] = {
    ROLE_RESEARCHER: "agent_research",
    ROLE_SOLUTION_SPECIALIST: "agent_solution",
    ROLE_REVIEWER: "agent_review",
}
ROLE_FOR_STEP_KIND: Dict[str, str] = {v: k for k, v in STEP_KIND_FOR_ROLE.items()}

# Exactly 3 roles => exactly 3 provider calls in v1 (no revision loop, no
# re-research branch — locked decision 5). This is a hard ceiling, not a
# default: a future phase that wants more calls must change this constant
# deliberately, not raise a config value that silently multiplies cost.
MAX_PROVIDER_CALLS = len(ROLE_ORDER)


def is_known_role(role: object) -> bool:
    """True iff *role* is one of the exactly-3 closed, server-owned role
    names. Never true for a client- or model-supplied string that merely
    happens to match by accident of spelling — callers must compare
    against this closed set, never invent a new one."""
    return isinstance(role, str) and role in ROLE_SET


def sequence_for_role(role: str) -> int:
    """The ONE authoritative sequence number for *role* — 1/2/3, derived
    from :data:`ROLE_ORDER`, never accepted as an independently-supplied
    value from any caller (eliminates an entire class of role/sequence
    mismatch bugs by construction)."""
    if not is_known_role(role):
        raise InvalidRole(f"unknown role: {role!r}")
    return ROLE_SEQUENCE[role]


def role_for_sequence(sequence: int) -> str:
    """Inverse of :func:`sequence_for_role`. Raises for any sequence
    outside 1..3 — there is no fourth role and no role 0."""
    for role, seq in ROLE_SEQUENCE.items():
        if seq == sequence:
            return role
    raise InvalidRole(f"no role at sequence {sequence!r}")


def expected_parent_role(role: str) -> Optional[str]:
    """The role whose artifact must be *role*'s ``parent_artifact_id`` —
    ``None`` for the Researcher (v1's first role, whose parent is the
    sealed run context itself, not another role's artifact). Depth is
    exactly 1: every role's parent is its own immediate, single
    predecessor in :data:`ROLE_ORDER` — never a grandparent, never a
    self-reference, never a role from a different run (cross-run linkage
    is separately prevented by the sealed payload envelope's own
    tenant/user/run binding — see ``multi_agent_artifacts.seal_agent_artifact``)."""
    seq = sequence_for_role(role)
    if seq == 1:
        return None
    return role_for_sequence(seq - 1)


def expected_next_role(current_role: Optional[str]) -> Optional[str]:
    """The role that must run immediately after *current_role* —
    ``ROLE_RESEARCHER`` when nothing has run yet (``current_role is
    None``), and ``None`` once the Reviewer (the terminal role) has run.
    No revision loop: there is no path back to an earlier role."""
    if current_role is None:
        return ROLE_RESEARCHER
    seq = sequence_for_role(current_role)
    if seq >= len(ROLE_ORDER):
        return None
    return role_for_sequence(seq + 1)


def validate_role_transition(*, previous_role: Optional[str], next_role: str) -> None:
    """Raise :class:`IllegalRoleTransition` unless *next_role* is exactly
    the role :func:`expected_next_role` says must run after *previous_role*.
    This is what makes role-skipping, repeats, reversal (Reviewer before
    Researcher), and any client-supplied role graph structurally
    impossible to accept — not merely discouraged by convention."""
    if not is_known_role(next_role):
        raise InvalidRole(f"unknown role: {next_role!r}")
    expected = expected_next_role(previous_role)
    if next_role != expected:
        raise IllegalRoleTransition(
            f"expected role {expected!r} after {previous_role!r}, got {next_role!r}"
        )


def is_terminal_role(role: str) -> bool:
    """True only for the Reviewer — the one role whose output becomes the
    final user-visible answer. Used by the future D5C finalizer to decide
    whether a step's success also triggers Message creation + Run
    completion, never guessed from step count."""
    return is_known_role(role) and sequence_for_role(role) == len(ROLE_ORDER)


def assert_call_budget(calls_made: int) -> None:
    """Raise :class:`ProviderCallBudgetExceeded` if *calls_made* has
    already reached :data:`MAX_PROVIDER_CALLS` — the hard ceiling a future
    D5C worker must check BEFORE making one more provider call. D5B makes
    zero provider calls itself; this is the accounting contract that
    calls-count enforcement will use."""
    if not isinstance(calls_made, int) or isinstance(calls_made, bool) or calls_made < 0:
        raise ValueError("calls_made must be a non-negative int")
    if calls_made >= MAX_PROVIDER_CALLS:
        raise ProviderCallBudgetExceeded(
            f"provider call budget ({MAX_PROVIDER_CALLS}) already reached"
        )
