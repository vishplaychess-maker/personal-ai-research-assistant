"""
ChromaDB client wrapper.

Manages per-session collections named "session_{session_id}".
Each collection stores document chunks with metadata.
"""

from typing import Dict, List, Optional, Any

import chromadb
from chromadb.api.types import (
    Documents,
    Embeddings,
    IDs,
    Metadatas,
)
from chromadb.errors import NotFoundError

from app.config import settings


def get_client() -> chromadb.HttpClient:
    """Get a ChromaDB HTTP client."""
    return chromadb.HttpClient(
        host=settings.chromadb_host,
        port=settings.chromadb_port,
    )


def get_or_create_collection(session_id: int) -> chromadb.Collection:
    """
    Get an existing collection for a session, or create a new one.
    """
    client = get_client()
    collection_name = f"session_{session_id}"
    try:
        return client.get_collection(collection_name)
    except NotFoundError:
        return client.create_collection(collection_name)


def delete_collection(session_id: int) -> None:
    """Delete the ChromaDB collection for a session."""
    client = get_client()
    collection_name = f"session_{session_id}"
    try:
        client.delete_collection(collection_name)
    except (NotFoundError, ValueError):
        pass  # Collection doesn't exist


def add_chunks(
    session_id: int,
    chunk_ids: IDs,
    embeddings: Embeddings,
    documents: Documents,
    metadatas: Metadatas,
) -> None:
    """
    Add chunks to a session's ChromaDB collection.

    Args:
        session_id: The research session ID.
        chunk_ids: List of unique string IDs for each chunk.
        embeddings: List of embedding vectors.
        documents: List of text content strings.
        metadatas: List of metadata dicts.
    """
    collection = get_or_create_collection(session_id)
    collection.add(
        ids=chunk_ids,
        embeddings=embeddings,
        documents=documents,
        metadatas=metadatas,
    )


def delete_chunks(session_id: int, chunk_ids: IDs) -> None:
    """
    Delete specific chunks from a session's ChromaDB collection.

    Args:
        session_id: The research session ID.
        chunk_ids: List of ChromaDB IDs to delete.
    """
    collection = get_or_create_collection(session_id)
    collection.delete(ids=chunk_ids)


def query_similar_chunks(
    session_id: int,
    query_embedding: List[float],
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """
    Query the session's ChromaDB collection for similar chunks.

    Args:
        session_id: The research session ID.
        query_embedding: The embedding vector of the user question.
        top_k: Number of results to return.

    Returns:
        List of dicts with 'id', 'text', 'metadata' keys.
    """
    collection = get_or_create_collection(session_id)
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(top_k, 50),  # cap at 50 for safety
    )

    chunks: List[Dict[str, Any]] = []
    if not results["ids"]:
        return chunks

    for i in range(len(results["ids"][0])):
        chunks.append({
            "id": results["ids"][0][i],
            "text": results["documents"][0][i] if results["documents"] else "",
            "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
            "distance": results["distances"][0][i] if results.get("distances") else None,
        })

    return chunks


def count_chunks_in_collection(session_id: int) -> int:
    """Return the number of chunks in a session's collection."""
    try:
        collection = get_or_create_collection(session_id)
        return collection.count()
    except Exception:
        return 0


def collection_exists(session_id: int) -> bool:
    """Check if a session's ChromaDB collection exists."""
    client = get_client()
    try:
        client.get_collection(f"session_{session_id}")
        return True
    except (NotFoundError, ValueError):
        return False
