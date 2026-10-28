"""Durable run + step state machines (Phase 1B.2R — MP-0).

PURE domain logic — no database, no worker, no I/O. MP-0 ships the transition
tables + guards + the cancellation/approval race rule; the worker that applies
them is MP-1+.

Run lifecycle
-------------

    created -> queued -> running -> {waiting_for_approval, succeeded, failed,
                                     timed_out, cancelling, outcome_unknown}
    waiting_for_approval -> {approved, denied, failed, cancelling}
        (failed here == approval expired OR a resume-time revalidation failure)
    approved -> {resuming, cancelling, failed}
    resuming -> {running, failed, cancelling, outcome_unknown}
    cancelling -> {cancelled, outcome_unknown}
    queued  -> {running, cancelling, cancelled}
    created -> {queued, cancelling}

Terminal: succeeded, failed, timed_out, cancelled, denied, outcome_unknown.
``outcome_unknown`` is terminal EXCEPT for one explicit, deliberate future
operation: reconciliation may move it to ``succeeded`` or ``failed`` (guarded by
``allow_reconcile=True``). Nothing else may leave a terminal state.

Cancellation vs approval race
-----------------------------

If a run in ``waiting_for_approval`` has BOTH an unconsumed ``cancel_requested``
signal AND an ``approval_decided`` signal pending, **cancel wins**: a user who
asked to stop the run gets it stopped; an approval that lands after a cancel is
moot. ``race_winner()`` encodes this so the worker is deterministic.
"""

from __future__ import annotations

from app.run_engine.errors import InvalidRunTransition

# ── run states ────────────────────────────────────────────────────────

CREATED = "created"
QUEUED = "queued"
RUNNING = "running"
WAITING_FOR_APPROVAL = "waiting_for_approval"
APPROVED = "approved"
DENIED = "denied"
RESUMING = "resuming"
CANCELLING = "cancelling"
CANCELLED = "cancelled"
SUCCEEDED = "succeeded"
FAILED = "failed"
TIMED_OUT = "timed_out"
OUTCOME_UNKNOWN = "outcome_unknown"

RUN_STATES = frozenset({
    CREATED, QUEUED, RUNNING, WAITING_FOR_APPROVAL, APPROVED, DENIED, RESUMING,
    CANCELLING, CANCELLED, SUCCEEDED, FAILED, TIMED_OUT, OUTCOME_UNKNOWN,
})

RUN_TERMINAL = frozenset({
    SUCCEEDED, FAILED, TIMED_OUT, CANCELLED, DENIED, OUTCOME_UNKNOWN,
})

# from -> allowed set of to (excludes reconciliation; see assert_run_transition)
RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    CREATED: frozenset({QUEUED, CANCELLING}),
    QUEUED: frozenset({RUNNING, CANCELLING, CANCELLED}),
    RUNNING: frozenset({
        WAITING_FOR_APPROVAL, SUCCEEDED, FAILED, TIMED_OUT, CANCELLING,
        OUTCOME_UNKNOWN,
    }),
    WAITING_FOR_APPROVAL: frozenset({APPROVED, DENIED, FAILED, CANCELLING}),
    APPROVED: frozenset({RESUMING, CANCELLING, FAILED}),
    RESUMING: frozenset({RUNNING, FAILED, CANCELLING, OUTCOME_UNKNOWN}),
    CANCELLING: frozenset({CANCELLED, OUTCOME_UNKNOWN}),
    # terminal
    SUCCEEDED: frozenset(),
    FAILED: frozenset(),
    TIMED_OUT: frozenset(),
    CANCELLED: frozenset(),
    DENIED: frozenset(),
    OUTCOME_UNKNOWN: frozenset(),   # + {SUCCEEDED, FAILED} via allow_reconcile
}

# ── step states ──────────────────────────────────────────────────────

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_BLOCKED = "blocked"
STEP_WAITING = "waiting"
STEP_SKIPPED = "skipped"
STEP_INCOMPLETE = "incomplete"

STEP_STATES = frozenset({
    STEP_PENDING, STEP_RUNNING, STEP_SUCCEEDED, STEP_FAILED, STEP_BLOCKED,
    STEP_WAITING, STEP_SKIPPED, STEP_INCOMPLETE,
})

STEP_TERMINAL = frozenset({
    STEP_SUCCEEDED, STEP_FAILED, STEP_BLOCKED, STEP_SKIPPED, STEP_INCOMPLETE,
})

STEP_TRANSITIONS: dict[str, frozenset[str]] = {
    STEP_PENDING: frozenset({STEP_RUNNING, STEP_SKIPPED}),
    STEP_RUNNING: frozenset({
        STEP_SUCCEEDED, STEP_FAILED, STEP_BLOCKED, STEP_WAITING, STEP_INCOMPLETE,
    }),
    # `waiting` is the step surfacing an approval requirement; the worker
    # re-runs it after the run leaves waiting_for_approval.
    STEP_WAITING: frozenset({STEP_RUNNING, STEP_BLOCKED, STEP_SKIPPED}),
    STEP_SUCCEEDED: frozenset(),
    STEP_FAILED: frozenset(),
    STEP_BLOCKED: frozenset(),
    STEP_SKIPPED: frozenset(),
    STEP_INCOMPLETE: frozenset(),
}

# ── guards ───────────────────────────────────────────────────────────


def is_run_terminal(state: str) -> bool:
    return state in RUN_TERMINAL


def is_step_terminal(state: str) -> bool:
    return state in STEP_TERMINAL


def run_can_transition(frm: str, to: str, *, allow_reconcile: bool = False) -> bool:
    if frm not in RUN_STATES or to not in RUN_STATES:
        return False
    if allow_reconcile and frm == OUTCOME_UNKNOWN and to in (SUCCEEDED, FAILED):
        return True
    return to in RUN_TRANSITIONS.get(frm, frozenset())


def assert_run_transition(frm: str, to: str, *, allow_reconcile: bool = False) -> None:
    """Raise :class:`InvalidRunTransition` unless ``frm -> to`` is legal.

    ``allow_reconcile`` is the ONLY way to leave ``outcome_unknown`` (an
    explicit future reconciliation operation), and only to a decided terminal.
    """
    if frm not in RUN_STATES:
        raise InvalidRunTransition(frm, to, scope="run")
    if to not in RUN_STATES:
        raise InvalidRunTransition(frm, to, scope="run")
    if not run_can_transition(frm, to, allow_reconcile=allow_reconcile):
        raise InvalidRunTransition(frm, to, scope="run")


def step_can_transition(frm: str, to: str) -> bool:
    if frm not in STEP_STATES or to not in STEP_STATES:
        return False
    return to in STEP_TRANSITIONS.get(frm, frozenset())


def assert_step_transition(frm: str, to: str) -> None:
    if not step_can_transition(frm, to):
        raise InvalidRunTransition(frm, to, scope="step")


def race_winner(*, approval_pending: bool, cancel_pending: bool) -> str:
    """Deterministic winner when both signals are pending on a waiting run.

    Returns ``"cancel"`` when a cancel is pending (it always wins), else
    ``"approval"`` when only an approval is pending, else ``"none"``.
    """
    if cancel_pending:
        return "cancel"
    if approval_pending:
        return "approval"
    return "none"


# states a run can be in once an "approved" decision has taken effect (so a
# duplicate approve is a deterministic no-op rather than an error)
_APPROVED_SETTLED = frozenset({APPROVED, RESUMING, RUNNING, SUCCEEDED})


def idempotent_decision_state(current: str, decision: str) -> str | None:
    """Resolve a duplicate/late approval-or-deny request deterministically.

    * ``current == waiting_for_approval`` -> ``None`` (the caller must perform
      the real transition).
    * the run is ALREADY settled consistently with ``decision`` -> return
      ``current`` unchanged (idempotent no-op).
    * the run is settled the OTHER way, or is no longer decidable
      (cancelled / cancelling / failed / timed_out / outcome_unknown) ->
      raise :class:`InvalidRunTransition`.
    """
    if decision not in ("approved", "denied"):
        raise ValueError(f"decision must be 'approved' or 'denied', got {decision!r}")
    if current == WAITING_FOR_APPROVAL:
        return None
    if decision == "approved":
        if current in _APPROVED_SETTLED:
            return current
        if current == DENIED:
            raise InvalidRunTransition(current, APPROVED, scope="run")
    else:  # denied
        if current == DENIED:
            return current
        if current in _APPROVED_SETTLED:
            raise InvalidRunTransition(current, DENIED, scope="run")
    # cancelled / cancelling / failed / timed_out / outcome_unknown / created / queued
    raise InvalidRunTransition(current, decision, scope="run")
