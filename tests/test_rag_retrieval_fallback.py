"""RAG retrieval must not silently drop a ready document.

The upload's Chroma metadata update is best-effort; when it fails the chunk
carries ``chunk_db_id: 0``. Retrieval used to skip those rows, so a document
showed "ready" but the model received no context. It now falls back to the
SQLite chunk row.
"""

from __future__ import annotations

import uuid

from app.database import SessionLocal
from app.models.models import (
    Document,
    DocumentChunk,
    DocumentStatus,
    ResearchSession,
    User,
)
from app.services import rag_service


def _seed_document() -> tuple[int, int, int]:
    db = SessionLocal()
    try:
        username = "ragfb_" + uuid.uuid4().hex[:8]
        user = User(username=username, email=f"{username}@example.com")
        db.add(user)
        db.commit()
        db.refresh(user)
        session = ResearchSession(user_id=user.id, title="RAG fallback")
        db.add(session)
        db.commit()
        db.refresh(session)
        doc = Document(
            session_id=session.id,
            filename="hello.pdf",
            content_type="application/pdf",
            file_path="unused.pdf",
            file_size=10,
            status=DocumentStatus.ready.value,
            chunk_count=1,
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        chunk = DocumentChunk(
            document_id=doc.id,
            chunk_index=0,
            content="Hello Thunder AI",
            page_number=1,
            chroma_id=f"doc_{doc.id}_chunk_0",
        )
        db.add(chunk)
        db.commit()
        db.refresh(chunk)
        return session.id, doc.id, chunk.id
    finally:
        db.close()


def test_missing_chroma_chunk_db_id_falls_back_to_sqlite(monkeypatch):
    session_id, doc_id, chunk_id = _seed_document()

    monkeypatch.setattr(rag_service, "collection_exists", lambda sid: True)
    monkeypatch.setattr(rag_service, "generate_embedding", lambda q: [0.1, 0.2])
    monkeypatch.setattr(
        rag_service,
        "query_similar_chunks",
        lambda sid, emb, top_k: [
            {
                "id": "doc_chunk_0",
                "text": "Hello Thunder AI",
                "metadata": {
                    "document_id": doc_id,
                    "chunk_index": 0,
                    "chunk_db_id": 0,
                    "page_number": 1,
                },
                "distance": 0.1,
            }
        ],
    )

    db = SessionLocal()
    try:
        chunks = rag_service.retrieve_chunks(session_id, "What does it say?", db)
    finally:
        db.close()

    assert len(chunks) == 1
    assert chunks[0]["text"] == "Hello Thunder AI"
    assert chunks[0]["chunk_db_id"] == chunk_id
    assert chunks[0]["filename"] == "hello.pdf"
