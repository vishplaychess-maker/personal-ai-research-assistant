"""DB-authoritative in-process run-event notifier (Phase 1B.2R — MP-3C3B).

A **wake-up hint only**. The ``run_events`` table remains the sole source of
truth: every SSE consumer re-queries the DB after a wake, so a missed / duplicate
/ early notification can never create a gap or a duplicate event, and a notifier
failure degrades to bounded DB polling.

Payload carries **at most** a ``run_id`` (and, internally, nothing else) — never a
prompt, text, tool args/result, provider output, user id, session id, or secret.

Delivery is single-process only (no Redis / broker — not in scope, not claimed).
Publication is triggered by a SQLAlchemy ``after_commit`` hook so an event is
only ever signalled **after** its transaction commits; the hook is thread-safe
(the worker commits on its own owned thread) because it hands off to the bound
application event loop via ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from sqlalchemy import event as _sa_event
from sqlalchemy.orm import Session as _SASession

from app.models.models import Run, RunEvent

logger = logging.getLogger(__name__)

# bounded subscriber state — a completed run's key is dropped when its last
# subscriber unsubscribes; these caps stop a reconnect storm growing memory.
_MAX_WAITERS_PER_RUN = 64
_MAX_RUNS_TRACKED = 4096


class RunNotifier:
    """One per process. All ``_subs`` mutation happens on the bound event loop."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subs: dict[str, set[asyncio.Event]] = {}

    # ---- lifecycle -------------------------------------------------

    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Call once from the app lifespan (on the running loop)."""
        try:
            self._loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def unbind_loop(self) -> None:
        self._loop = None
        self._subs.clear()

    # ---- subscribe / unsubscribe (event-loop thread only) --------

    def subscribe(self, run_id: str) -> asyncio.Event:
        """Register a waiter for ``run_id``. Returns an :class:`asyncio.Event`
        the caller ``await``s (with its own timeout) and ``clear()``s after each
        DB re-query. If the bounded caps are hit the event is returned
        **unregistered** — the caller then relies purely on its poll timeout."""
        ev = asyncio.Event()
        waiters = self._subs.get(run_id)
        if waiters is None:
            if len(self._subs) >= _MAX_RUNS_TRACKED:
                logger.debug("RunNotifier: run cap reached; polling-only for %s", "run")
                return ev
            waiters = set()
            self._subs[run_id] = waiters
        if len(waiters) >= _MAX_WAITERS_PER_RUN:
            logger.debug("RunNotifier: waiter cap reached; polling-only")
            return ev
        waiters.add(ev)
        return ev

    def unsubscribe(self, run_id: str, ev: asyncio.Event) -> None:
        waiters = self._subs.get(run_id)
        if not waiters:
            return
        waiters.discard(ev)
        if not waiters:
            self._subs.pop(run_id, None)

    def waiter_count(self, run_id: Optional[str] = None) -> int:
        if run_id is not None:
            return len(self._subs.get(run_id, ()))
        return sum(len(w) for w in self._subs.values())

    # ---- publish (any thread) -----------------------------------

    def notify(self, run_id: str) -> None:
        """Wake every waiter for ``run_id``. Safe from any thread — hands off to
        the bound loop. A no-op when no loop is bound or nothing is waiting."""
        if not run_id:
            return
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._wake, run_id)
        except RuntimeError:
            # loop already closed — the SSE readers fall back to polling
            pass

    def _wake(self, run_id: str) -> None:
        for ev in tuple(self._subs.get(run_id, ())):
            ev.set()


run_notifier = RunNotifier()


# ── after-commit publication hook (registered once, at import) ──────
#
# `before_flush` accumulates the run_ids of RunEvent inserts (and any Run row
# touched — a status transition bumps `last_event_seq`) onto session-scoped
# state, across every flush of the transaction (MP-3A/3B/3C2 flush early and
# commit later). `after_commit` fires ONE notify per run_id — so a wake is
# emitted only AFTER the rows are durably committed. `after_rollback` clears the
# accumulator. State lives on `session.info` so the worker thread's session and
# the request threads' sessions never interfere.

_INFO_KEY = "_mp3c3b_pending_notify_run_ids"


def _collect_pending(session: _SASession, flush_context=None, instances=None) -> None:
    run_ids: set[str] = set()
    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, RunEvent) and getattr(obj, "run_id", None):
            run_ids.add(obj.run_id)
        elif isinstance(obj, Run) and getattr(obj, "id", None):
            run_ids.add(obj.id)
    if run_ids:
        session.info[_INFO_KEY] = run_ids | session.info.get(_INFO_KEY, set())


def _fire_pending(session: _SASession) -> None:
    run_ids = session.info.pop(_INFO_KEY, None)
    if not run_ids:
        return
    for rid in run_ids:
        run_notifier.notify(rid)


def _clear_pending(session: _SASession, *_a) -> None:
    session.info.pop(_INFO_KEY, None)


def install_notifier_hooks() -> None:
    """Idempotent — registers the commit hooks on the ORM ``Session`` class."""
    if getattr(install_notifier_hooks, "_installed", False):
        return
    _sa_event.listen(_SASession, "before_flush", _collect_pending)
    _sa_event.listen(_SASession, "after_commit", _fire_pending)
    _sa_event.listen(_SASession, "after_rollback", _clear_pending)
    _sa_event.listen(_SASession, "after_soft_rollback", _clear_pending)
    install_notifier_hooks._installed = True  # type: ignore[attr-defined]


install_notifier_hooks()

__all__ = ["RunNotifier", "run_notifier", "install_notifier_hooks"]
