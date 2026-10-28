"""Phase 1B.2R MP-0 — pure run/step state machine.

No database, no worker, no I/O. Proves the transition tables, terminal-state
protection, the approval/cancellation race rule, denial + timeout +
outcome_unknown behaviour, and idempotent duplicate decisions.
"""

from __future__ import annotations

import inspect

import pytest

from app.run_engine import state_machine as sm
from app.run_engine.errors import InvalidRunTransition


# ── legal transitions: every edge in the table is accepted ───────────


def test_every_declared_run_transition_is_legal():
    for frm, tos in sm.RUN_TRANSITIONS.items():
        for to in tos:
            assert sm.run_can_transition(frm, to)
            sm.assert_run_transition(frm, to)  # must not raise


def test_every_declared_step_transition_is_legal():
    for frm, tos in sm.STEP_TRANSITIONS.items():
        for to in tos:
            assert sm.step_can_transition(frm, to)
            sm.assert_step_transition(frm, to)


# ── illegal transitions: everything not in the table is rejected ─────


def test_all_non_declared_run_transitions_are_illegal():
    for frm in sm.RUN_STATES:
        legal = sm.RUN_TRANSITIONS.get(frm, frozenset())
        for to in sm.RUN_STATES:
            if to in legal:
                continue
            assert not sm.run_can_transition(frm, to), (frm, to)
            with pytest.raises(InvalidRunTransition):
                sm.assert_run_transition(frm, to)


def test_unknown_states_are_rejected():
    assert not sm.run_can_transition("bogus", sm.QUEUED)
    assert not sm.run_can_transition(sm.CREATED, "bogus")
    with pytest.raises(InvalidRunTransition):
        sm.assert_run_transition("bogus", "queued")
    with pytest.raises(InvalidRunTransition):
        sm.assert_step_transition("pending", "bogus")


# ── terminal-state protection ───────────────────────────────────────


def test_run_terminal_states_have_no_outgoing_edges():
    for term in sm.RUN_TERMINAL:
        assert sm.RUN_TRANSITIONS[term] == frozenset()
        for to in sm.RUN_STATES:
            if term == sm.OUTCOME_UNKNOWN and to in (sm.SUCCEEDED, sm.FAILED):
                continue  # only via allow_reconcile
            assert not sm.run_can_transition(term, to)
            with pytest.raises(InvalidRunTransition):
                sm.assert_run_transition(term, to)


def test_outcome_unknown_only_moves_via_reconcile():
    assert not sm.run_can_transition(sm.OUTCOME_UNKNOWN, sm.SUCCEEDED)
    assert sm.run_can_transition(sm.OUTCOME_UNKNOWN, sm.SUCCEEDED, allow_reconcile=True)
    assert sm.run_can_transition(sm.OUTCOME_UNKNOWN, sm.FAILED, allow_reconcile=True)
    # reconcile cannot resurrect it to a non-decided state
    assert not sm.run_can_transition(sm.OUTCOME_UNKNOWN, sm.RUNNING, allow_reconcile=True)
    with pytest.raises(InvalidRunTransition):
        sm.assert_run_transition(sm.OUTCOME_UNKNOWN, sm.RUNNING, allow_reconcile=True)
    sm.assert_run_transition(sm.OUTCOME_UNKNOWN, sm.FAILED, allow_reconcile=True)


def test_step_terminal_states_have_no_outgoing_edges():
    for term in sm.STEP_TERMINAL:
        assert sm.STEP_TRANSITIONS[term] == frozenset()


# ── denial + timeout behaviour ──────────────────────────────────────


def test_denied_is_terminal_and_never_reopens():
    assert sm.is_run_terminal(sm.DENIED)
    assert sm.RUN_TRANSITIONS[sm.DENIED] == frozenset()
    for to in sm.RUN_STATES:
        assert not sm.run_can_transition(sm.DENIED, to)
    with pytest.raises(InvalidRunTransition):
        sm.idempotent_decision_state(sm.DENIED, "approved")


def test_timed_out_is_terminal_and_reachable_only_from_running():
    assert sm.is_run_terminal(sm.TIMED_OUT)
    frms = {f for f, tos in sm.RUN_TRANSITIONS.items() if sm.TIMED_OUT in tos}
    assert frms == {sm.RUNNING}


# ── approval vs cancellation race ───────────────────────────────────


def test_cancel_always_wins_the_race():
    assert sm.race_winner(approval_pending=True, cancel_pending=True) == "cancel"
    assert sm.race_winner(approval_pending=False, cancel_pending=True) == "cancel"
    assert sm.race_winner(approval_pending=True, cancel_pending=False) == "approval"
    assert sm.race_winner(approval_pending=False, cancel_pending=False) == "none"


# ── idempotent duplicate decisions ──────────────────────────────────


def test_idempotent_decision_state_matrix():
    # still actionable -> caller must transition
    assert sm.idempotent_decision_state(sm.WAITING_FOR_APPROVAL, "approved") is None
    assert sm.idempotent_decision_state(sm.WAITING_FOR_APPROVAL, "denied") is None
    # already settled the same way -> deterministic no-op
    for s in (sm.APPROVED, sm.RESUMING, sm.RUNNING, sm.SUCCEEDED):
        assert sm.idempotent_decision_state(s, "approved") == s
    assert sm.idempotent_decision_state(sm.DENIED, "denied") == sm.DENIED
    # settled the other way -> conflict
    with pytest.raises(InvalidRunTransition):
        sm.idempotent_decision_state(sm.DENIED, "approved")
    with pytest.raises(InvalidRunTransition):
        sm.idempotent_decision_state(sm.APPROVED, "denied")
    # no longer decidable -> conflict
    for s in (sm.CANCELLED, sm.CANCELLING, sm.FAILED, sm.TIMED_OUT,
              sm.OUTCOME_UNKNOWN, sm.CREATED, sm.QUEUED):
        with pytest.raises(InvalidRunTransition):
            sm.idempotent_decision_state(s, "approved")


def test_idempotent_decision_rejects_bad_decision_arg():
    with pytest.raises(ValueError):
        sm.idempotent_decision_state(sm.WAITING_FOR_APPROVAL, "maybe")


# ── module purity: no I/O / DB / worker imports ─────────────────────


def test_state_machine_module_is_pure():
    src = inspect.getsource(sm)
    for forbidden in ("import sqlalchemy", "from sqlalchemy", "SessionLocal",
                      "asyncio", "requests", "httpx", "open(", "socket",
                      "app.models", "app.database"):
        assert forbidden not in src, f"state_machine.py must not reference {forbidden!r}"


def test_state_lists_match_the_approved_set():
    assert sm.RUN_STATES == frozenset({
        "created", "queued", "running", "waiting_for_approval", "approved",
        "denied", "resuming", "cancelling", "cancelled", "succeeded", "failed",
        "timed_out", "outcome_unknown",
    })
