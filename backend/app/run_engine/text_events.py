"""Durable ``run_events.category = "text"`` persistence + size/time batching
(Phase 1B.2R MP-3A).

Text events carry only **model-visible response text** — never a token id,
credential, header, cookie, raw tool argument, or ciphertext. They share the
per-run monotonic ``run_seq`` with control events (control events are always
written immediately and are NOT routed through this module).

Batching (existing settings): flush when the buffer reaches
``run_event_text_flush_bytes`` OR ``run_event_text_flush_ms`` elapse; the final
partial buffer flushes on ``close()``. The buffer accumulates a Python ``str``,
so a multi-byte codepoint is never split; a flush larger than
``run_text_event_max_bytes`` is chunked on **character** boundaries.

MP-3A wiring uses an injected in-process publisher (tests) — no LLM is invoked.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import Run, RunEvent, _utcnow_aware
from app.run_engine.events import _try_append_event
from app.run_engine.errors import ControlEventContention, ControlEventTooLarge, RunNotFound

TEXT_CATEGORY = "text"
TEXT_EVENT_TYPE = "text"

_APPEND_MAX_ATTEMPTS = 5


def _chunk_by_bytes(text: str, max_bytes: int) -> list[str]:
    """Split ``text`` into pieces each ≤ ``max_bytes`` UTF-8 bytes, never
    splitting a codepoint. A single codepoint wider than ``max_bytes`` is
    emitted whole (correctness over the soft cap)."""
    out: list[str] = []
    buf = ""
    buf_len = 0
    for ch in text:
        cl = len(ch.encode("utf-8"))
        if buf and buf_len + cl > max_bytes:
            out.append(buf)
            buf, buf_len = "", 0
        buf += ch
        buf_len += cl
    if buf:
        out.append(buf)
    return out


def append_text_event(
    db: Session,
    *,
    run_id: str,
    tenant_id: Optional[str],
    user_id: int,
    text: str,
    commit: bool = True,
) -> RunEvent:
    """Append ONE ``text`` event (``data = {"text": <chunk>}``) with the next
    ``run_seq``. ``text`` must be a non-empty ``str`` whose UTF-8 length is
    ≤ ``settings.run_text_event_max_bytes``. Bounded retry on seq contention.
    Raises :class:`ControlEventTooLarge`, :class:`RunNotFound`,
    :class:`ControlEventContention`, or ``ValueError``."""
    if not isinstance(text, str) or text == "":
        raise ValueError("text event body must be a non-empty str")
    body = text.encode("utf-8")  # also validates it is representable
    limit = int(settings.run_text_event_max_bytes)
    if len(body) > limit:
        raise ControlEventTooLarge(len(body), limit)
    data_text = json.dumps({"text": text}, ensure_ascii=False, separators=(",", ":"))

    for _ in range(_APPEND_MAX_ATTEMPTS):
        ev = _try_append_event(
            db, run_id=run_id, tenant_id=tenant_id, user_id=user_id,
            event_type=TEXT_EVENT_TYPE, data_text=data_text, category=TEXT_CATEGORY,
        )
        if ev is not None:
            if commit:
                db.commit()
            return ev
        db.rollback()
    raise ControlEventContention()


def read_persisted_text(db: Session, run_id: str) -> str:
    """Concatenate every committed `text` event payload for `run_id` in
    `run_seq` order — byte-for-byte what an SSE client replaying this run
    has already received.

    Used for crash-safe continuation (MP-3C3D5C closeout): a finalizer that
    must publish a validated burst exactly once reads this prefix first and
    publishes only the missing suffix, so a crash between two committed
    chunks can neither drop text nor duplicate it. Raises `ValueError` for a
    malformed payload so the caller fails closed instead of guessing."""
    rows = (
        db.query(RunEvent.data)
        .filter(RunEvent.run_id == run_id, RunEvent.category == TEXT_CATEGORY)
        .order_by(RunEvent.run_seq.asc())
        .all()
    )
    parts: list[str] = []
    for (data,) in rows:
        try:
            payload = json.loads(data)
        except (TypeError, ValueError) as exc:  # json.JSONDecodeError ⊂ ValueError
            raise ValueError("malformed text event payload") from exc
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            raise ValueError("malformed text event payload")
        parts.append(text)
    return "".join(parts)


class TextEventBuffer:
    """Byte/time batched writer for one run's response text. Owns short-lived
    sessions from ``session_factory`` (one per flush). Thread-affine — a single
    producer calls :meth:`publish` / :meth:`tick` / :meth:`close`."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        run_id: str,
        *,
        tenant_id: Optional[str],
        user_id: int,
        now_fn: Optional[Callable[[], _dt.datetime]] = None,
        flush_bytes: Optional[int] = None,
        flush_ms: Optional[int] = None,
        max_bytes: Optional[int] = None,
    ) -> None:
        self._sf = session_factory
        self.run_id = run_id
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._now = now_fn or _utcnow_aware
        self._flush_bytes = int(flush_bytes or settings.run_event_text_flush_bytes)
        self._flush_ms = int(flush_ms or settings.run_event_text_flush_ms)
        self._max_bytes = int(max_bytes or settings.run_text_event_max_bytes)
        self._buf = ""
        self._buf_bytes = 0
        self._last_flush = self._now()
        self.flushed_events = 0

    def publish(self, chunk: str) -> None:
        """Add response text. Flushes once the byte threshold is reached
        (possibly writing several capped events)."""
        if not chunk:
            return
        self._buf += chunk
        self._buf_bytes += len(chunk.encode("utf-8"))
        if self._buf_bytes >= self._flush_bytes:
            self._flush()

    def tick(self) -> None:
        """Time-based flush — call periodically from the producer's loop."""
        if not self._buf:
            return
        elapsed_ms = (self._now() - self._last_flush).total_seconds() * 1000.0
        if elapsed_ms >= self._flush_ms:
            self._flush()

    @property
    def flush_interval_s(self) -> float:
        """The time-based flush bound in seconds (public view of ``flush_ms``).

        D6C-J4: the producer uses this to drive :meth:`tick` from its own
        lightweight wake-up, so the documented time bound holds even while the
        provider is stalled and no further token arrives to trigger a flush.
        """
        return max(0.01, self._flush_ms / 1000.0)

    def close(self) -> None:
        """Flush the final partial buffer (completion or failure)."""
        self._flush()

    def _flush(self) -> None:
        if not self._buf:
            self._last_flush = self._now()
            return
        pieces = _chunk_by_bytes(self._buf, self._max_bytes)
        self._buf, self._buf_bytes = "", 0
        for piece in pieces:
            db = self._sf()
            try:
                run = db.query(Run).filter(Run.id == self.run_id).first()
                if run is None:
                    raise RunNotFound(self.run_id)
                append_text_event(
                    db, run_id=self.run_id, tenant_id=self._tenant_id,
                    user_id=self._user_id, text=piece, commit=True,
                )
                self.flushed_events += 1
            finally:
                db.close()
        self._last_flush = self._now()
