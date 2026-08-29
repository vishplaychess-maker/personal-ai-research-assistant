"""
Cache-Augmented Generation (CAG) — a tiny in-process TTL cache for LLM answers.

Purpose: when a user asks the *exact same question* in the *same session*, serve
the previous answer instead of paying for another LLM API call.

Design (deliberately minimal):
  - Key = sha256(session_id + NUL + trimmed question).  session_id is baked into
    the key, so answers can never leak between sessions.
  - Value = (stored_at_epoch, answer_text).
  - Eviction: lazy TTL check on read + a hard cap with oldest-first trim on write.
  - Scope: single process, single worker.  Cleared on restart, not shared across
    workers or replicas.

ponytail: one dict + one coarse lock. If you ever run >1 uvicorn worker or want
persistence, swap the dict for Redis with the same get()/set() signature.
"""

import hashlib
import threading
import time
from typing import Optional

TTL_SECONDS = 60 * 60  # entries older than this are treated as a miss
MAX_ENTRIES = 512      # hard cap on distinct cached questions

_store: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def _key(session_id: int, question: str) -> str:
    raw = f"{session_id}\x00{question.strip()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def get(session_id: int, question: str) -> Optional[str]:
    """Return the cached answer for this (session, question), or None on miss/expiry."""
    if not question or not question.strip():
        return None
    k = _key(session_id, question)
    with _lock:
        entry = _store.get(k)
        if entry is None:
            return None
        stored_at, answer = entry
        if time.time() - stored_at > TTL_SECONDS:
            _store.pop(k, None)
            return None
        return answer


def set(session_id: int, question: str, answer: str) -> None:
    """Store an answer. No-ops on empty inputs. Trims oldest entry when full."""
    if not question or not question.strip() or not answer or not answer.strip():
        return
    k = _key(session_id, question)
    with _lock:
        if len(_store) >= MAX_ENTRIES and k not in _store:
            # O(n) scan, but n <= MAX_ENTRIES and this only runs when full.
            oldest = min(_store, key=lambda kk: _store[kk][0])
            _store.pop(oldest, None)
        _store[k] = (time.time(), answer)


def clear() -> None:
    """Drop everything (used by tests)."""
    with _lock:
        _store.clear()


def _self_check() -> None:
    """Runnable check: `python -c "from app.services.cache_service import _self_check as f; f()"`"""
    import app.services.cache_service as m

    m.clear()

    # basic hit + whitespace-insensitive key
    m.set(1, "hi", "hello there")
    assert m.get(1, "hi") == "hello there"
    assert m.get(1, "  hi  ") == "hello there"

    # session isolation — same question, different session = miss
    assert m.get(2, "hi") is None
    # miss for unknown question
    assert m.get(1, "bye") is None

    # TTL expiry
    m.TTL_SECONDS = 0
    time.sleep(0.01)
    assert m.get(1, "hi") is None, "expired entry should be a miss"
    m.TTL_SECONDS = 3600

    # max-entries trim keeps newest, drops oldest
    m.clear()
    m.MAX_ENTRIES = 3
    for i in range(3):
        m.set(9, f"q{i}", f"a{i}")
        time.sleep(0.001)
    m.set(9, "q3", "a3")  # forces eviction of q0
    assert m.get(9, "q0") is None, "oldest entry should have been evicted"
    assert m.get(9, "q3") == "a3"
    assert m.get(9, "q2") == "a2"
    m.MAX_ENTRIES = 512
    m.clear()

    print("cache_service self-check OK")


if __name__ == "__main__":
    _self_check()
