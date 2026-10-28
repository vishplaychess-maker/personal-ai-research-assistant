"""Phase 1B.2R MP-3C3D5B — focused tests for the pure, DB-free durable
multi-agent role graph (``app.run_engine.multi_agent_orchestration``).

Hermetic: no DB, no network, no LLM — pure function calls only.
"""
from __future__ import annotations

import pytest

from app.run_engine.multi_agent_orchestration import (
    AGENT_PROGRESS_CODES,
    MAX_PROVIDER_CALLS,
    MULTI_AGENT_RUN_KIND,
    ROLE_ORDER,
    ROLE_REVIEWER,
    ROLE_RESEARCHER,
    ROLE_SOLUTION_SPECIALIST,
    STEP_KIND_FOR_ROLE,
    InvalidRole,
    IllegalRoleTransition,
    ProviderCallBudgetExceeded,
    assert_call_budget,
    expected_next_role,
    expected_parent_role,
    is_known_role,
    is_terminal_role,
    role_for_sequence,
    sequence_for_role,
    validate_role_transition,
)


# ── canonical role graph shape ─────────────────────────────────


def test_role_order_is_fixed_three_role_single_pass():
    assert ROLE_ORDER == (ROLE_RESEARCHER, ROLE_SOLUTION_SPECIALIST, ROLE_REVIEWER)
    assert MAX_PROVIDER_CALLS == 3


def test_second_role_is_solution_specialist_not_coder():
    assert ROLE_SOLUTION_SPECIALIST == "solution_specialist"
    assert "coder" not in ROLE_ORDER
    assert STEP_KIND_FOR_ROLE[ROLE_SOLUTION_SPECIALIST] == "agent_solution"


def test_step_kind_mapping_matches_expected_future_order():
    assert STEP_KIND_FOR_ROLE == {
        ROLE_RESEARCHER: "agent_research",
        ROLE_SOLUTION_SPECIALIST: "agent_solution",
        ROLE_REVIEWER: "agent_review",
    }


def test_multi_agent_run_kind_constant():
    assert MULTI_AGENT_RUN_KIND == "multi_agent"


def test_agent_progress_codes_closed_set():
    assert AGENT_PROGRESS_CODES == frozenset({
        "agent_researching", "agent_research_complete",
        "agent_solving", "agent_solution_complete",
        "agent_reviewing", "agent_finalizing",
    })


# ── sequence / role lookup ──────────────────────────────────────


def test_sequence_for_role_is_1_indexed_by_order():
    assert sequence_for_role(ROLE_RESEARCHER) == 1
    assert sequence_for_role(ROLE_SOLUTION_SPECIALIST) == 2
    assert sequence_for_role(ROLE_REVIEWER) == 3


def test_role_for_sequence_is_the_inverse():
    for role in ROLE_ORDER:
        assert role_for_sequence(sequence_for_role(role)) == role


@pytest.mark.parametrize("bad_seq", [0, 4, -1, 100])
def test_role_for_sequence_rejects_out_of_range(bad_seq):
    with pytest.raises(InvalidRole):
        role_for_sequence(bad_seq)


@pytest.mark.parametrize("bad_role", ["coder", "planner", "", "RESEARCHER", None, 123])
def test_unknown_role_rejected_everywhere(bad_role):
    assert is_known_role(bad_role) is False
    with pytest.raises(InvalidRole):
        sequence_for_role(bad_role)


# ── lineage: depth-1 parent contract ─────────────────────────────


def test_researcher_has_no_parent_role():
    assert expected_parent_role(ROLE_RESEARCHER) is None


def test_solution_specialist_parent_is_researcher_only():
    assert expected_parent_role(ROLE_SOLUTION_SPECIALIST) == ROLE_RESEARCHER


def test_reviewer_parent_is_solution_specialist_not_researcher():
    """Depth is exactly 1 — the Reviewer's parent is its immediate
    predecessor, never a grandparent."""
    assert expected_parent_role(ROLE_REVIEWER) == ROLE_SOLUTION_SPECIALIST


# ── role transition: skip/repeat/reversal structurally impossible ──


def test_expected_next_role_walks_the_fixed_sequence():
    assert expected_next_role(None) == ROLE_RESEARCHER
    assert expected_next_role(ROLE_RESEARCHER) == ROLE_SOLUTION_SPECIALIST
    assert expected_next_role(ROLE_SOLUTION_SPECIALIST) == ROLE_REVIEWER
    assert expected_next_role(ROLE_REVIEWER) is None  # terminal — no revision loop


def test_validate_role_transition_accepts_the_one_true_sequence():
    validate_role_transition(previous_role=None, next_role=ROLE_RESEARCHER)
    validate_role_transition(previous_role=ROLE_RESEARCHER, next_role=ROLE_SOLUTION_SPECIALIST)
    validate_role_transition(previous_role=ROLE_SOLUTION_SPECIALIST, next_role=ROLE_REVIEWER)


@pytest.mark.parametrize("previous,attempted", [
    (None, ROLE_SOLUTION_SPECIALIST),        # skip researcher
    (None, ROLE_REVIEWER),                   # skip both
    (ROLE_RESEARCHER, ROLE_REVIEWER),        # skip solution_specialist
    (ROLE_RESEARCHER, ROLE_RESEARCHER),      # repeat
    (ROLE_SOLUTION_SPECIALIST, ROLE_RESEARCHER),  # reversal
    (ROLE_REVIEWER, ROLE_RESEARCHER),        # reversal after terminal
])
def test_validate_role_transition_rejects_skip_repeat_and_reversal(previous, attempted):
    with pytest.raises(IllegalRoleTransition):
        validate_role_transition(previous_role=previous, next_role=attempted)


def test_validate_role_transition_rejects_unknown_next_role():
    with pytest.raises(InvalidRole):
        validate_role_transition(previous_role=None, next_role="coder")


# ── terminal role ─────────────────────────────────────────────


def test_only_reviewer_is_terminal():
    assert is_terminal_role(ROLE_REVIEWER) is True
    assert is_terminal_role(ROLE_RESEARCHER) is False
    assert is_terminal_role(ROLE_SOLUTION_SPECIALIST) is False
    assert is_terminal_role("coder") is False


# ── provider call budget ─────────────────────────────────────


@pytest.mark.parametrize("calls_made", [0, 1, 2])
def test_assert_call_budget_allows_under_ceiling(calls_made):
    assert_call_budget(calls_made)  # must not raise


@pytest.mark.parametrize("calls_made", [3, 4, 100])
def test_assert_call_budget_rejects_at_or_over_ceiling(calls_made):
    with pytest.raises(ProviderCallBudgetExceeded):
        assert_call_budget(calls_made)


@pytest.mark.parametrize("bad_value", [-1, True, False, "3", 3.0, None])
def test_assert_call_budget_rejects_non_nonnegative_int(bad_value):
    with pytest.raises(ValueError):
        assert_call_budget(bad_value)


# ── purity: no side-effecting imports leak into this module ────


def test_orchestration_module_is_db_network_and_llm_free():
    import app.run_engine.multi_agent_orchestration as mod

    src = open(mod.__file__, encoding="utf-8").read()
    for forbidden in ("sqlalchemy", "requests", "httpx", "openai", "anthropic"):
        assert forbidden not in src.lower()
