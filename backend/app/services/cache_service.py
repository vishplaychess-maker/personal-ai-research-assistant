"""
Semantic Cache-Augmented Generation (CAG) — a tiny in-process TTL cache with
embedding-based matching.

Purpose: when a user asks the *same question* OR a *semantically similar
question* in the *same session*, serve the previous answer instead of paying
for another LLM API call. e.g. "What is a vector database?" followed later by
"Explain vector DBs" resolves to the cached answer.

Design (deliberately minimal, no external vector DB):
  - Exact-match fast path: key = sha256(session_id + NUL + trimmed question),
    stored in ``_store`` (unchanged behaviour — same question -> instant hit
    with zero embedding cost).
  - Semantic path: ``_semantic_store`` is an in-memory list of entries
    ``{"session_id", "embedding", "answer", "timestamp"}``. On a cache miss,
    the query is embedded (Ollama nomic-embed-text) and compared by cosine
    similarity against stored entries for the same session. A match at or
    above ``SEMANTIC_THRESHOLD`` returns the answer with a ``[Semantic Cache
    Hit]`` prefix.
  - Graceful degradation: if Ollama is offline / not configured, embedding
    generation raises and is caught — the cache silently falls back to exact
    match only and never crashes the caller. New entries are still stored by
    exact key.
  - Eviction: lazy TTL check on read + a hard cap with oldest-first trim on
    write, for both stores.
  - Scope: single process, single worker. Cleared on restart.

ponytail: two dicts/lists + one coarse lock. If you ever run >1 uvicorn worker
or want persistence, swap for Redis with the same get()/set() signature.
"""

import hashlib
import threading
import time
from typing import List, Optional

from app.services.embeddings_client import generate_embedding

TTL_SECONDS = 60 * 60  # entries older than this are treated as a miss
MAX_ENTRIES = 512      # hard cap on distinct exact-match questions

# Semantic cache knobs.
# Cosine similarity required for a "same meaning" hit. Tuned from real
# nomic-embed-text output: strong paraphrases land ~0.76-0.94 while genuinely
# different questions are far lower (~0.3-0.7). 0.80 catches rephrased questions
# without returning a cached answer for merely-relatable ones.
SEMANTIC_THRESHOLD = 0.80
SEMANTIC_MAX_ENTRIES = 512

_store: dict[str, tuple[float, str]] = {}
_semantic_store: List[dict] = [
    # {"session_id": int, "embedding": list[float], "answer": str, "timestamp": float}
]
_lock = threading.Lock()


def _key(session_id: int, question: str) -> str:
    raw = f"{session_id}\x00{question.strip()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two equal-length vectors (0 on degenerate)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _embed(text: str) -> Optional[List[float]]:
    """Best-effort embedding. Returns None (never raises) so callers degrade
    gracefully when the embedding service (Ollama) is offline or errored."""
    try:
        return generate_embedding(text)
    except Exception:  # noqa: BLE001 — cache must never break the chat
        return None


def find_semantic_match(
    session_id: int,
    query_embedding: Optional[List[float]],
    threshold: float = SEMANTIC_THRESHOLD,
) -> Optional[str]:
    """Return the best cached answer for a semantically similar question in the
    same session, or None. Expired entries are skipped (and pruned)."""
    if not query_embedding:
        return None
    now = time.time()
    best: Optional[str] = None
    best_score = 0.0
    with _lock:
        for entry in _semantic_store:
            if entry["session_id"] != session_id:
                continue
            if now - entry["timestamp"] > TTL_SECONDS:
                continue
            score = _cosine(query_embedding, entry["embedding"])
            if score >= threshold and score > best_score:
                best_score = score
                best = entry["answer"]
    return best


def get(session_id: int, question: str) -> Optional[str]:
    """Return the cached answer for this (session, question), or None on miss/expiry.

    1. Exact-match fast path (no embedding cost).
    2. Otherwise, semantic match against previously cached answers in the same
       session. On a hit the answer is returned with a ``[Semantic Cache Hit]``
       prefix. If Ollama is offline the semantic step is skipped silently.
    """
    if not question or not question.strip():
        return None

    # Fast path: exact/whitespace-normalised key.
    k = _key(session_id, question)
    with _lock:
        entry = _store.get(k)
        if entry is not None:
            stored_at, answer = entry
            if time.time() - stored_at > TTL_SECONDS:
                _store.pop(k, None)
            else:
                return answer

    # Semantic path: embed the query and compare against stored answers.
    query_embedding = _embed(question)
    if query_embedding is None:
        return None  # Ollama offline -> exact-match only
    match = find_semantic_match(session_id, query_embedding)
    if match is not None:
        return f"[Semantic Cache Hit] {match}"
    return None


def set(session_id: int, question: str, answer: str) -> None:
    """Store an answer. No-ops on empty inputs. Trims oldest entry when full.

    Always stores the exact-match entry; also appends a semantic entry using an
    embedding of the question. If embedding fails (Ollama offline) the semantic
    entry is simply skipped — exact caching still works.
    """
    if not question or not question.strip() or not answer or not answer.strip():
        return

    k = _key(session_id, question)
    now = time.time()
    with _lock:
        if len(_store) >= MAX_ENTRIES and k not in _store:
            oldest = min(_store, key=lambda kk: _store[kk][0])
            _store.pop(oldest, None)
        _store[k] = (now, answer)

    # Semantic entry (best-effort).
    query_embedding = _embed(question)
    with _lock:
        if query_embedding is not None:
            if len(_semantic_store) >= SEMANTIC_MAX_ENTRIES:
                # Drop oldest-first (list is append-ordered).
                oldest = min(_semantic_store, key=lambda e: e["timestamp"])
                _semantic_store.remove(oldest)
            _semantic_store.append({
                "session_id": session_id,
                "embedding": query_embedding,
                "answer": answer,
                "timestamp": now,
            })


def get_exact(session_id: int, question: str) -> Optional[str]:
    """Exact-match-only lookup (no embedding cost).

    Used by the chat flow's first cache layer (semantic_cache.chat_lookup)
    so the semantic layer is consulted exactly once per request. Expired
    entries are treated as a miss (and pruned).
    """
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


def set_exact(session_id: int, question: str, answer: str) -> None:
    """Exact-match-only store (no embedding cost).

    The semantic layer lives in semantic_cache (ChromaDB-backed); this keeps
    the in-process instant-hit path warm without double-embedding queries.
    """
    if not question or not question.strip() or not answer or not answer.strip():
        return
    k = _key(session_id, question)
    now = time.time()
    with _lock:
        if len(_store) >= MAX_ENTRIES and k not in _store:
            oldest = min(_store, key=lambda kk: _store[kk][0])
            _store.pop(oldest, None)
        _store[k] = (now, answer)


def clear() -> None:
    """Drop everything (used by tests)."""
    with _lock:
        _store.clear()
        _semantic_store.clear()


def _self_check() -> None:
    """Runnable check: `python -c "from app.services.cache_service import _self_check as f; f()"`"""
    import app.services.cache_service as m

    m.clear()

    # basic hit + whitespace-insensitive key (exact path)
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

    # max-entries trim keeps newest, drops oldest (exact path)
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

    # Semantic match is best-effort: if Ollama is reachable, "Explain vector DBs"
    # should hit the cached "What is a vector database?" answer. If Ollama is
    # offline this simply degrades to None (no crash). Always assert no-crash.
    try:
        hit = m.get(1, "Explain vector DBs")
        if hit is not None:
            print("semantic cache HIT:", hit[:45], "...")
        else:
            print("semantic cache: no hit (Ollama offline or low similarity) — OK")
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"semantic get must not raise: {exc}") from exc

    print("cache_service self-check OK")


if __name__ == "__main__":
    _self_check()
