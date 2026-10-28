"""Phase 1B.2R MP-5 - the ONE synchronous wait-for-terminal adapter.

The non-streaming `/messages` route and the scheduler both need to turn a durable
submission into a terminal `Run` WITHOUT a browser: submit, then wait for the Run
to reach a terminal state, then read the persisted row.

This module is the single place that wait lives. It is deliberately tiny and
strictly read-only with respect to run state:

  * it NEVER creates a Run, never advances a Run and never invokes a worker - the
    embedded worker owns execution, and this adapter only OBSERVES committed rows;
  * it reuses the ONE terminal vocabulary (`state_machine.is_run_terminal`); there
    is no second status list here to drift;
  * it FAILS CLOSED on its deadline by raising, so a caller can never mistake a
    timeout for success and report a non-terminal Run as an answer;
  * it does not hold a transaction open across the wait - the session is expired
    on every poll so committed worker state is observed.

Nothing here is enabled by a default: the flag-gated `/messages` cutover is a
later MP-5 slice (it must first retire the legacy terminal-approval encoding,
which is gated on MP-4C/MP-4D). This adapter is the prerequisite primitive.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.models.models import Run
from app.run_engine.state_machine import is_run_terminal

#: Bounded, machine-readable reason reported when the deadline expires first.
SYNC_WAIT_TIMEOUT = "sync_wait_timeout"

#: Default poll cadence while waiting (seconds).
DEFAULT_POLL_INTERVAL_S = 0.25


class SyncWaitError(Exception):
    """The Run did not reach a terminal state inside the deadline.

    `code` is a bounded slug and `status` is the last OBSERVED state - never a
    claim that the Run succeeded.
    """

    def __init__(self, code: str, run_id: str, status: Optional[str] = None) -> None:
        super().__init__(code)
        self.code = code
        self.run_id = run_id
        self.status = status


def wait_for_terminal(
    db: Session,
    run_id: str,
    *,
    deadline_s: float,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    now_fn: Optional[Callable[[], float]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> Run:
    """Block until `run_id` is terminal, or raise `SyncWaitError`.

    Returns the terminal `Run` mapped by THIS session. Raises with
    `status=None` when the Run does not exist, and with the last observed
    non-terminal `status` when the deadline expires. Never returns a
    non-terminal Run, so a timeout can never be read as an answer.
    """
    now = now_fn or time.monotonic
    sleep = sleep_fn or time.sleep
    started = now()
    while True:
        db.expire_all()
        run = db.query(Run).filter(Run.id == run_id).one_or_none()
        if run is None:
            raise SyncWaitError(SYNC_WAIT_TIMEOUT, run_id, None)
        if is_run_terminal(run.status):
            return run
        if now() - started >= float(deadline_s):
            raise SyncWaitError(SYNC_WAIT_TIMEOUT, run_id, run.status)
        sleep(float(poll_interval_s))


__all__ = ["SyncWaitError", "SYNC_WAIT_TIMEOUT", "DEFAULT_POLL_INTERVAL_S",
           "wait_for_terminal"]