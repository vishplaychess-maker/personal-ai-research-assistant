"""In-memory event bus + cancellation flags for OpenBot tasks.

Events are ephemeral progress frames (the DB row remains the source of truth).
The SSE route tails this bus; a process restart simply ends the live view while
the task status/result stays queryable.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List

MAX_EVENTS_PER_TASK = 300

_lock = threading.RLock()
_events: Dict[str, Deque[Dict[str, Any]]] = {}
_seq: Dict[str, int] = {}
_cancelled: set[str] = set()


def emit(
    task_id: str,
    event_type: str,
    *,
    agent: str = "orchestrator",
    message: str = "",
    tool: str | None = None,
    output: str | None = None,
) -> Dict[str, Any]:
    with _lock:
        seq = _seq.get(task_id, 0) + 1
        _seq[task_id] = seq
        event = {
            "seq": seq,
            "type": event_type,
            "agent": agent,
            "message": message,
            "tool": tool,
            "output": output,
            "at": time.time(),
        }
        bucket = _events.setdefault(task_id, deque(maxlen=MAX_EVENTS_PER_TASK))
        bucket.append(event)
        return event


def events_since(task_id: str, since: int = 0) -> List[Dict[str, Any]]:
    with _lock:
        return [e for e in _events.get(task_id, ()) if int(e["seq"]) > int(since)]


def emit_wrapper(task_id: str):
    """Return an ``emit`` bound to one task (avoids repeating the id)."""

    def _emit(event_type: str, *, agent: str = "orchestrator", message: str = "",
              tool: str | None = None, output: str | None = None):
        return emit(task_id, event_type, agent=agent, message=message,
                    tool=tool, output=output)

    return _emit


def latest_seq(task_id: str) -> int:
    with _lock:
        return _seq.get(task_id, 0)


def request_cancel(task_id: str) -> None:
    with _lock:
        _cancelled.add(task_id)


def is_cancelled(task_id: str) -> bool:
    with _lock:
        return task_id in _cancelled


def clear(task_id: str) -> None:
    with _lock:
        _events.pop(task_id, None)
        _seq.pop(task_id, None)
        _cancelled.discard(task_id)
