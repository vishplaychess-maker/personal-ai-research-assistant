"""
Client for generating text embeddings using Ollama's nomic-embed-text model.
"""

import hashlib

import httpx

from app.config import settings

OLLAMA_EMBED_URL = f"{settings.ollama_url}/api/embeddings"
EMBED_MODEL = "nomic-embed-text"
EMBED_TIMEOUT = 30.0


def _local_embedding(text: str) -> list[float]:
    """Deterministic local embedding for tests/offline (LLM_PROVIDER=local).

    Returns a 384-dim vector derived from SHA-256 of the text, so
    ChromaDB collections have consistent dimensions and queries are
    deterministic without needing Ollama.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    vec: list[float] = []
    counter = 0
    while len(vec) < 384:
        h = hashlib.sha256(f"{digest.hex()}-{counter}".encode()).digest()
        vec.extend(b / 255.0 for b in h)
        counter += 1
    # Center around 0 for better cosine similarity distribution
    return [v - 0.5 for v in vec[:384]]


def generate_embedding(text: str) -> list[float]:
    """
    Generate a vector embedding for a text string using Ollama.

    Args:
        text: The text to embed.

    Returns:
        A list of float values representing the embedding vector.

    Raises:
        ConnectionError: If Ollama is unreachable.
        RuntimeError: If the API returns an error.
    """
    # Local test mode: return deterministic embeddings without network
    if settings.llm_provider == "local":
        return _local_embedding(text)

    try:
        with httpx.Client(timeout=EMBED_TIMEOUT) as client:
            resp = client.post(OLLAMA_EMBED_URL, json={
                "model": EMBED_MODEL,
                "prompt": text,
            })
    except httpx.ConnectError:
        raise ConnectionError(
            "Cannot connect to Ollama for embedding generation. "
            "Make sure Ollama is running and nomic-embed-text is installed "
            "(`ollama pull nomic-embed-text`)."
        )
    except httpx.TimeoutException:
        raise TimeoutError(
            "Ollama embedding request timed out."
        )

    if resp.status_code != 200:
        detail = resp.text[:200]
        raise RuntimeError(f"Ollama embeddings returned HTTP {resp.status_code}: {detail}")

    data = resp.json()
    embedding = data.get("embedding")
    if not embedding:
        raise RuntimeError("Ollama returned empty embedding")
    return embedding


def generate_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """
    Generate embeddings for multiple texts.

    Args:
        texts: List of text strings to embed.

    Returns:
        List of embedding vectors.
    """
    return [generate_embedding(t) for t in texts]
