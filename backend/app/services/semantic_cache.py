"""
Semantic cache (CAG layer 2) — ChromaDB-backed, provider/model-scoped.

Sits BEHIND the existing exact-match CAG in ``cache_service``:

  1. Exact match (cache_service, session-scoped, zero embedding cost)
  2. Semantic match (this module, cross-session, scoped to provider+model)
  3. LLM call

Semantics:
  - ``lookup`` embeds the query (existing ``embeddings_client`` path: Ollama
    nomic-embed-text; deterministic local vectors when LLM_PROVIDER=local) and
    queries a ChromaDB collection filtered by provider+model metadata. A cosine
    similarity >= threshold is a HIT: the cached final response is returned and
    the LLM is never called.
  - ``store`` embeds the query and upserts ``{provider, model, created_at}``
    metadata, capped at ``SEMANTIC_CACHE_MAX_ENTRIES`` (oldest evicted).
  - The collection name embeds the embedding dimension
    (``semantic_cache_{dim}``) so Ollama 768-dim and local 384-dim vectors can
    never be mixed inside one collection (Chroma rejects dim mismatches).
  - Collections are created with cosine distance, so ``similarity = 1 - distance``.

Failure policy: cache/embedding errors are logged as warnings and reported as
MISS / skipped store. They NEVER propagate into the chat flow — the exact-match
CAG in cache_service remains the fallback (it is always consulted first and
never touches the network).

Grep contract (one line per lookup):
    event=semantic_cache result=HIT  similarity=0.92 latency_ms=12 provider=glm model=glm-5.3-flash
    event=semantic_cache result=MISS reason=low_similarity latency_ms=45 provider=glm model=glm-5.3-flash
"""

import hashlib
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from chromadb.errors import NotFoundError

from app.config import settings
from app.services.chromadb_client import get_client
from app.services.embeddings_client import generate_embedding

logger = logging.getLogger(__name__)

COLLECTION_PREFIX = "semantic_cache_"


def resolve_provider_model(
    provider_config: Optional[dict],
    model_name: Optional[str],
) -> Tuple[str, str]:
    """Resolve the (provider, model) pair that scopes cache entries.

    provider_config is the dict returned by get_user_llm_config()
    ({"provider", "api_key", "model"}) or None for the global .env provider.
    model_name (session-level override) wins over provider_config["model"].
    """
    cfg = provider_config or {}
    provider = cfg.get("provider") or settings.llm_provider
    model = model_name or cfg.get("model") or settings.default_model
    return str(provider), str(model)


def _entry_id(provider_key: str, model: str, query: str) -> str:
    """Deterministic ID: re-storing the same question upserts in place."""
    raw = f"{provider_key}\x00{model}\x00{query.strip().lower()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _get_collection(dim: int):
    """Get/create the cache collection for this embedding dimension.

    Cosine space so query distance == 1 - cosine_similarity.
    """
    client = get_client()
    name = f"{COLLECTION_PREFIX}{dim}"
    try:
        return client.get_collection(name)
    except NotFoundError:
        pass
    try:
        return client.create_collection(
            name, configuration={"hnsw": {"space": "cosine"}}
        )
    except (TypeError, ValueError):
        # Older chromadb builds take the space via collection metadata.
        return client.create_collection(name, metadata={"hnsw:space": "cosine"})


def _provider_model_filter(provider_key: str, model: str) -> Dict[str, Any]:
    return {
        "$and": [
            {"provider": {"$eq": provider_key}},
            {"model": {"$eq": model}},
        ]
    }


def lookup(
    query: str,
    provider_key: str,
    model: str,
    threshold: Optional[float] = None,
) -> Optional[str]:
    """Return the cached response for a semantically similar query, or None.

    Never raises. Exactly one greppable log line per call.
    """
    if not settings.semantic_cache_enabled:
        return None
    if not query or not query.strip():
        return None

    if threshold is None:
        threshold = settings.semantic_cache_threshold

    t0 = time.perf_counter()
    try:
        embedding = generate_embedding(query)
        if not embedding:
            raise RuntimeError("empty embedding")
        collection = _get_collection(len(embedding))
        results = collection.query(
            query_embeddings=[embedding],
            n_results=1,
            where=_provider_model_filter(provider_key, model),
            include=["documents", "distances"],
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)

        ids = (results.get("ids") or [[]])[0]
        if not ids:
            logger.info(
                "event=semantic_cache result=MISS reason=empty_index "
                "latency_ms=%d provider=%s model=%s",
                latency_ms, provider_key, model,
            )
            return None

        distances = (results.get("distances") or [[]])[0]
        similarity = 1.0 - float(distances[0]) if distances else 0.0
        if similarity < threshold:
            logger.info(
                "event=semantic_cache result=MISS reason=low_similarity "
                "similarity=%.3f threshold=%.2f latency_ms=%d provider=%s model=%s",
                similarity, threshold, latency_ms, provider_key, model,
            )
            return None

        documents = (results.get("documents") or [[]])[0]
        answer = documents[0] if documents else ""
        if not answer:
            logger.info(
                "event=semantic_cache result=MISS reason=empty_document "
                "latency_ms=%d provider=%s model=%s",
                latency_ms, provider_key, model,
            )
            return None

        logger.info(
            "event=semantic_cache result=HIT similarity=%.3f latency_ms=%d "
            "provider=%s model=%s",
            similarity, latency_ms, provider_key, model,
        )
        return answer
    except Exception as exc:  # noqa: BLE001 — cache must never break the chat
        latency_ms = int((time.perf_counter() - t0) * 1000)
        logger.warning(
            "event=semantic_cache result=MISS reason=error error=%s "
            "latency_ms=%d provider=%s model=%s",
            exc, latency_ms, provider_key, model,
        )
        return None


def store(
    query: str,
    response: str,
    provider_key: str,
    model: str,
) -> None:
    """Embed and upsert a final response. Never raises.

    Bounds the collection at settings.semantic_cache_max_entries by evicting
    the oldest entries (by created_at metadata) after each write.
    """
    if not settings.semantic_cache_enabled:
        return
    if not query or not query.strip() or not response or not response.strip():
        return

    try:
        embedding = generate_embedding(query)
        if not embedding:
            raise RuntimeError("empty embedding")
        collection = _get_collection(len(embedding))
        collection.upsert(
            ids=[_entry_id(provider_key, model, query)],
            embeddings=[embedding],
            documents=[response],
            metadatas=[{
                "provider": provider_key,
                "model": model,
                "created_at": time.time(),
            }],
        )

        max_entries = settings.semantic_cache_max_entries
        if max_entries > 0 and collection.count() > max_entries:
            _evict_oldest(collection, max_entries)
    except Exception as exc:  # noqa: BLE001 — cache must never break the chat
        logger.warning("semantic_cache store failed (non-fatal): %s", exc)


def _evict_oldest(collection, max_entries: int) -> None:
    """Delete the oldest entries until the collection fits the cap."""
    all_entries = collection.get(include=["metadatas"])
    metas = all_entries.get("metadatas") or []
    ids = all_entries.get("ids") or []
    paired = sorted(
        zip(ids, metas),
        key=lambda item: (item[1] or {}).get("created_at", 0.0),
    )
    overflow = len(paired) - max_entries
    if overflow <= 0:
        return
    stale_ids = [pid for pid, _ in paired[:overflow]]
    if stale_ids:
        collection.delete(ids=stale_ids)


# ── Chat-flow helpers (exact-first, then semantic) ────────────


def chat_lookup(
    session_id: int,
    query: str,
    provider_config: Optional[dict],
    model_name: Optional[str],
) -> Tuple[Optional[str], str]:
    """Two-layer cache lookup used by the chat paths.

    1. Exact match via cache_service (session-scoped, zero embedding cost).
    2. Semantic match via this module (provider+model-scoped, cross-session).

    Returns (answer, kind) where kind is "exact", "semantic" or "none".
    Never raises.
    """
    if not query or not query.strip():
        return None, "none"

    from app.services import cache_service

    try:
        exact = cache_service.get_exact(session_id, query)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Exact-cache lookup failed (non-fatal): %s", exc)
        exact = None
    if exact is not None:
        return exact, "exact"

    provider_key, model = resolve_provider_model(provider_config, model_name)
    semantic = lookup(query, provider_key, model)
    if semantic is not None:
        return semantic, "semantic"
    return None, "none"


def chat_store(
    session_id: int,
    query: str,
    response: str,
    provider_config: Optional[dict],
    model_name: Optional[str],
) -> None:
    """Two-layer store used by the chat paths. Never raises.

    Always writes the exact-match entry; additionally upserts a semantic
    entry (skipped when embedding is unavailable) so paraphrases hit later.
    """
    if not query or not query.strip() or not response or not response.strip():
        return

    from app.services import cache_service

    try:
        cache_service.set_exact(session_id, query, response)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Exact-cache store failed (non-fatal): %s", exc)

    provider_key, model = resolve_provider_model(provider_config, model_name)
    store(query, response, provider_key, model)


def clear() -> None:
    """Drop every semantic cache collection (used by tests/maintenance)."""
    try:
        client = get_client()
        for col in client.list_collections():
            name = getattr(col, "name", None) or (
                col.get("name") if isinstance(col, dict) else None
            )
            if name and str(name).startswith(COLLECTION_PREFIX):
                client.delete_collection(str(name))
    except Exception as exc:  # noqa: BLE001
        logger.warning("semantic_cache clear failed (non-fatal): %s", exc)
