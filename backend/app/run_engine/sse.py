"""Authenticated SSE replay / reconnect for a durable run (Phase 1B.2R MP-3A).

The database ``run_events`` ledger is the source of truth. The stream:

* replays persisted events strictly by ``run_seq`` (control + text, ordered);
* accepts ``Last-Event-ID`` (or a repo-consistent ``after_seq`` query param) and
  never re-sends an event ≤ that cursor — a reconnect resumes cleanly;
* emits ``id: <run_seq>`` so the client tracks its own cursor;
* revalidates the run's refresh-token family (STRICT origin binding) on **every**
  poll — a revoked / expired / mismatched family closes the stream safely;
* sends ``: heartbeat`` comment lines (never a fake persisted event);
* reads bounded pages (``run_event_read_max_page``) — a slow consumer only makes
  the generator block on its own socket write; memory stays at one page;
* on client disconnect closes the connection ONLY — it never cancels or mutates
  the run;
* redacts every internal error to a generic ``event: error``.

No LLM, no worker, no MCP is touched here — it is a windowed reader.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
from typing import AsyncIterator, Callable, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import Run
from app.run_engine.context import AuthContext
from app.run_engine.errors import (
    RunNotFound,
    SessionBindingMismatch,
    SessionBindingRequired,
)
from app.run_engine.events import read_run_events
from app.run_engine.session_binding import validate_session_binding
from app.run_engine.state_machine import RUN_STATES, RUN_TERMINAL

logger = logging.getLogger(__name__)

_TERMINAL_TO_STATUSES = frozenset(RUN_TERMINAL)


# ── canonical SSE wire serialization (MP-4B) ─────────────────────────────
#
# The ONE SSE serializer for the whole backend. It lives here, in the neutral
# durable module, so durable code never has to import the legacy
# services/streaming_service module (that reverse dependency was removed in
# MP-4B). The body is byte-for-byte identical to the helper it replaces - same
# event/data framing, same default json.dumps encoding, same trailing blank
# line - so every existing client, test and golden string is unaffected.
# Pure string formatting: no database, provider, worker or tool logic.


def format_sse(event: str, data: dict) -> str:
    """Format a Server-Sent Event string."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def parse_last_event_id(raw: Optional[str]) -> int:
    try:
        v = int((raw or "0").strip())
        return v if v > 0 else 0
    except (TypeError, ValueError):
        return 0


def _sse(*, event: Optional[str], data: str, seq: Optional[int]) -> str:
    lines = []
    if seq is not None:
        lines.append(f"id: {seq}")
    if event:
        lines.append(f"event: {event}")
    lines.append(f"data: {data}")
    return "\n".join(lines) + "\n\n"


def _is_terminal(run: Optional[Run]) -> bool:
    return run is not None and run.status in _TERMINAL_TO_STATUSES


async def stream_run_events(
    request,
    session_factory: Callable[[], Session],
    ctx: AuthContext,
    run_id: str,
    *,
    after_seq: int = 0,
    now_fn: Optional[Callable[[], _dt.datetime]] = None,
) -> AsyncIterator[str]:
    """Yield SSE frames for ``run_id`` starting strictly after ``after_seq``.
    Terminates on: run terminal (after draining), client disconnect, family
    revocation, the ``run_sse_max_seconds`` cap, or an internal error."""
    now = now_fn or (lambda: _dt.datetime.now(_dt.timezone.utc))
    cursor = max(0, int(after_seq or 0))
    page = int(settings.run_event_read_max_page)
    started = now()
    last_beat = started
    poll = max(1, int(settings.run_sse_poll_interval_s))
    beat_every = max(0, int(settings.run_sse_heartbeat_s))  # 0 ⇒ heartbeat every idle poll
    max_secs = int(settings.run_sse_max_seconds)

    try:
        while True:
            if await request.is_disconnected():
                return  # connection closed — never a cancel / mutation

            if (now() - started).total_seconds() > max_secs:
                yield ": stream-max-duration\n\n"
                return

            with _session(session_factory) as db:
                run = db.query(Run).filter(Run.id == run_id).first()
                try:
                    validate_session_binding(db, ctx, run=run)
                except (SessionBindingRequired, SessionBindingMismatch, RunNotFound):
                    yield _sse(event="error", data=json.dumps({"detail": "stream closed"}),
                               seq=None)
                    return
                rows = read_run_events(db, ctx, run_id, after_seq=cursor, limit=page)
                for ev in rows:
                    yield _sse(event=(ev.type or ev.category),
                               data=ev.data or "{}", seq=ev.run_seq)
                    cursor = ev.run_seq
                caught_up = len(rows) < page
                terminal = _is_terminal(run)

            if terminal and caught_up:
                return

            if not rows:
                if (now() - last_beat).total_seconds() >= beat_every:
                    yield ": heartbeat\n\n"
                    last_beat = now()
                await asyncio.sleep(poll)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — redact; never leak internals
        logger.error("run SSE stream failed: %s", "internal")
        try:
            yield _sse(event="error", data=json.dumps({"detail": "internal stream error"}),
                       seq=None)
        except Exception:  # pragma: no cover
            pass


class _session:
    """tiny sync context manager (the generator can't use `with` on a bare
    callable that returns a Session and must be closed)."""

    def __init__(self, factory: Callable[[], Session]):
        self._factory = factory
        self._db: Optional[Session] = None

    def __enter__(self) -> Session:
        self._db = self._factory()
        return self._db

    def __exit__(self, *exc) -> None:
        if self._db is not None:
            self._db.close()


# keep a stable name for callers/tests
__all__ = ["stream_run_events", "parse_last_event_id", "RUN_STATES"]
