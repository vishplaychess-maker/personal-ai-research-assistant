"""Mem0 semantic index for the existing long-term memory system.

Design (owner-approved): the SQLite ``Memory`` table stays the source of truth
and all writes still pass through the approval-gated memory paths. Mem0 is a
local, user-scoped *mirror* used only to rank memories semantically before a
chat turn. If Mem0, Qdrant, or Ollama is unavailable, callers fall back to the
existing recency retrieval.

Isolation:
* Every Mem0 call passes ``user_id=str(user_id)``.
* Delete/list helpers additionally verify the returned row's ``user_id``.

Privacy:
* Qdrant runs embedded on a local path; embeddings come from local Ollama.
* ``MEM0_TELEMETRY`` is forced off before mem0 is imported.
* No memory text or credential is logged; only exception class names.

Blocking:
* All Mem0 work runs on one dedicated worker thread with a hard timeout, so a
  cold Ollama or a locked Qdrant can never wedge the API/SSE event loop. On
  timeout the caller gets the fallback value ([] / False).
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, TypeVar

# Must be set before `import mem0` (its telemetry module reads it once).
os.environ["MEM0_TELEMETRY"] = "False"

from app.config import settings
from app.paths import get_data_dir

logger = logging.getLogger(__name__)

VAULT_DIR_NAME = "mem0"
COLLECTION_NAME = "thunder_memories"
RETRY_COOLDOWN_S = 60.0

T = TypeVar("T")

_lock = threading.RLock()
_client: Any = None
_next_retry_at = 0.0

# One worker serializes Qdrant-local access (it is not safe across threads) and
# makes every operation abandonable via future.result(timeout=...).
_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="mem0"
)


def _run(fn: Callable[[], T], timeout: float, default: T) -> T:
    try:
        return _executor.submit(fn).result(timeout=max(0.1, float(timeout)))
    except concurrent.futures.TimeoutError:
        logger.warning(
            "Mem0 operation timed out after %.1fs; using fallback", float(timeout)
        )
    except Exception as exc:  # noqa: BLE001 - memory must never break chat
        logger.warning("Mem0 operation failed (%s); using fallback", type(exc).__name__)
    return default


def _build_config() -> Dict[str, Any]:
    base = get_data_dir() / VAULT_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    ollama = settings.ollama_url
    return {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": COLLECTION_NAME,
                "path": str(base / "qdrant"),
                "embedding_model_dims": int(settings.mem0_embedding_dims),
            },
        },
        "embedder": {
            "provider": "ollama",
            "config": {
                "model": settings.mem0_embedder_model,
                "ollama_base_url": ollama,
                "embedding_dims": int(settings.mem0_embedding_dims),
            },
        },
        "llm": {
            "provider": "ollama",
            "config": {
                "model": settings.mem0_llm_model,
                "ollama_base_url": ollama,
            },
        },
        "history_db_path": str(base / "history.db"),
    }


def _get_client() -> Any:
    """Return the lazily constructed Mem0 client, or None when unavailable."""
    global _client, _next_retry_at

    if not settings.mem0_enabled:
        return None
    with _lock:
        if _client is not None:
            return _client
        now = time.monotonic()
        if now < _next_retry_at:
            return None
        try:
            from mem0 import Memory

            _client = Memory.from_config(_build_config())
            logger.info("Mem0 semantic index ready (local Qdrant + Ollama embeddings)")
        except Exception as exc:  # noqa: BLE001 - memory is an enhancement, not core
            _client = None
            _next_retry_at = now + RETRY_COOLDOWN_S
            logger.warning(
                "Mem0 unavailable (%s); memory retrieval falls back to recency.",
                type(exc).__name__,
            )
        return _client


def reset_client() -> None:
    """Testing helper: drop the singleton so a fake client can be installed."""
    global _client, _next_retry_at
    with _lock:
        _client = None
        _next_retry_at = 0.0


def warm_up(timeout: float = 30.0) -> None:
    """Initialize Qdrant + the Ollama embedder in the background at startup.

    Cold init can take several seconds; doing it here keeps the first chat
    search inside its short budget. Never blocks app startup and never raises.
    """
    if not settings.mem0_enabled or not settings.mem0_warmup_enabled:
        return

    def _warm() -> None:
        _run(_get_client, timeout, None)

    threading.Thread(target=_warm, name="mem0-warmup", daemon=True).start()


def _results(payload: Any) -> List[Dict[str, Any]]:
    items = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _all(client: Any, user_id: int) -> List[Dict[str, Any]]:
    return _results(client.get_all(user_id=str(user_id)))


def _delete_matching(client: Any, user_id: int, predicate) -> int:
    removed = 0
    for item in _all(client, user_id):
        if item.get("user_id") not in (None, str(user_id)):
            continue
        if predicate(item):
            try:
                client.delete(item["id"])
                removed += 1
            except Exception:  # noqa: BLE001 - keep deleting what we can
                pass
    return removed


def _index_impl(user_id: int, db_id: int, text: str, category: str) -> bool:
    client = _get_client()
    if client is None:
        return False
    _delete_matching(
        client,
        user_id,
        lambda item: (item.get("metadata") or {}).get("db_id") == db_id,
    )
    client.add(
        text,
        user_id=str(user_id),
        infer=False,  # text is already an approved fact
        metadata={"db_id": int(db_id), "category": category},
    )
    return True


def index_memory(
    user_id: int, db_id: int, content: str, category: str = "fact"
) -> bool:
    """Mirror one approved memory into Mem0 (no LLM inference, no API key)."""
    text = (content or "").strip()
    if not text or not settings.mem0_enabled:
        return False
    return _run(
        lambda: _index_impl(user_id, db_id, text, category),
        settings.mem0_index_timeout_s,
        False,
    )


def _search_impl(user_id: int, text: str, limit: int) -> List[int]:
    client = _get_client()
    if client is None:
        return []
    payload = client.search(text, user_id=str(user_id), limit=limit)
    ids: List[int] = []
    for item in _results(payload):
        if item.get("user_id") not in (None, str(user_id)):
            continue
        db_id = (item.get("metadata") or {}).get("db_id")
        try:
            ids.append(int(db_id))
        except (TypeError, ValueError):
            continue
    return ids


def search_memory_ids(user_id: int, query: str, limit: int = 3) -> List[int]:
    """Return matching DB memory ids in semantic-relevance order."""
    text = (query or "").strip()
    if not text or not settings.mem0_enabled:
        return []
    return _run(
        lambda: _search_impl(user_id, text, max(1, int(limit))),
        settings.mem0_timeout_s,
        [],
    )


def _get_all_impl(user_id: int) -> List[Dict[str, Any]]:
    client = _get_client()
    if client is None:
        return []
    return [
        {
            "id": item.get("id"),
            "memory": item.get("memory"),
            "metadata": item.get("metadata") or {},
            "created_at": item.get("created_at"),
        }
        for item in _all(client, user_id)
        if item.get("user_id") in (None, str(user_id))
    ]


def get_all_memories(user_id: int) -> List[Dict[str, Any]]:
    """Public, user-scoped mirror rows (no credentials involved)."""
    if not settings.mem0_enabled:
        return []
    return _run(lambda: _get_all_impl(user_id), settings.mem0_timeout_s, [])


def _delete_impl(user_id: int, memory_id: str) -> bool:
    client = _get_client()
    if client is None:
        return False
    for item in _all(client, user_id):
        if item.get("id") != memory_id:
            continue
        if item.get("user_id") not in (None, str(user_id)):
            return False
        client.delete(memory_id)
        return True
    return False


def delete_memory(user_id: int, memory_id: str) -> bool:
    """Delete one mirror row only if it belongs to ``user_id``."""
    if not memory_id or not settings.mem0_enabled:
        return False
    return _run(lambda: _delete_impl(user_id, memory_id), settings.mem0_timeout_s, False)


def _remove_db_id_impl(user_id: int, db_id: int) -> int:
    client = _get_client()
    if client is None:
        return 0
    return _delete_matching(
        client,
        user_id,
        lambda item: (item.get("metadata") or {}).get("db_id") == db_id,
    )


def remove_memory_for_db_id(user_id: int, db_id: int) -> int:
    if not settings.mem0_enabled:
        return 0
    return _run(
        lambda: _remove_db_id_impl(user_id, db_id), settings.mem0_timeout_s, 0
    )


def _clear_impl(user_id: int) -> int:
    client = _get_client()
    if client is None:
        return 0
    return _delete_matching(client, user_id, lambda _item: True)


def clear_user_memories(user_id: int) -> int:
    if not settings.mem0_enabled:
        return 0
    return _run(lambda: _clear_impl(user_id), settings.mem0_timeout_s, 0)
