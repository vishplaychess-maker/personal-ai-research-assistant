"""
Phase 3 tests — Document upload, chunking, RAG retrieval, and citations.

Run with:
    pip install httpx pytest pypdf
    pytest tests/test_documents.py -v

Override the base URL:
    BASE_URL=http://localhost:8080 pytest tests/test_documents.py -v
"""

import io
import json
import os

import httpx
import pytest

BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")

from tests.auth_helpers import ensure_user, register_and_login, auth_headers

# Shared test user for live-backend tests (persists across runs).
_TEST_USER = "itest_documents"

# ── PDF helpers ─────────────────────────────────────────────


def _make_pdf_with_text(text: str) -> bytes:
    """Create a simple single-page PDF with the given text."""
    from io import BytesIO
    from pypdf import PdfWriter, PdfReader

    writer = PdfWriter()
    writer.add_blank_page(612, 792)

    # Embed text using a simple approach
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    packet = BytesIO()
    c = canvas.Canvas(packet, pagesize=letter)
    y = 740
    for line in text.split("\n"):
        c.drawString(72, y, line[:100])
        y -= 14
    c.save()
    packet.seek(0)

    overlay_pdf = PdfReader(packet)
    writer.pages[0].merge_page(overlay_pdf.pages[0])

    result = BytesIO()
    writer.write(result)
    result.seek(0)
    return result.read()


# ── Helpers ────────────────────────────────────────────────


def _authenticated(c: httpx.Client, username: str = _TEST_USER) -> httpx.Client:
    """Attach a valid Authorization header for the shared test user.

    The register/login lookup runs on a separate throwaway client so the
    returned client's connection pool is NOT opened before the caller
    enters it with `with client() as c:`.
    """
    with httpx.Client(base_url=str(c.base_url), timeout=15.0) as temp:
        _, token = ensure_user(temp, username)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


def client() -> httpx.Client:
    return _authenticated(httpx.Client(base_url=BASE_URL, timeout=60.0))


def create_session(c: httpx.Client) -> dict:
    resp = c.post("/api/sessions", json={"title": "Doc Test Session"})
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture(autouse=True)
def _cleanup():
    with client() as c:
        sessions = c.get("/api/sessions").json()
        if not isinstance(sessions, list):
            return
        for s in sessions:
            if isinstance(s, dict) and s.get("id", 0) > 10:
                docs = c.get(f"/api/sessions/{s['id']}/documents").json()
                if isinstance(docs, list):
                    for d in docs:
                        if isinstance(d, dict) and "id" in d:
                            c.delete(f"/api/documents/{d['id']}")
                c.delete(f"/api/sessions/{s['id']}")


# ── TXT upload tests ───────────────────────────────────────


def test_upload_txt():
    """Upload a plain text file and verify processing."""
    with client() as c:
        session = create_session(c)
        content = b"Hello, world! This is a test document for the research assistant."
        resp = c.post(
            f"/api/sessions/{session['id']}/documents",
            files={"file": ("test.txt", content, "text/plain")},
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["document"]["filename"] == "test.txt"
    assert data["document"]["status"] == "ready"
    assert data["document"]["chunk_count"] >= 1
    with client() as c:
        list_resp = c.get(f"/api/sessions/{session['id']}/documents")
    assert list_resp.status_code == 200
    ids = [d["id"] for d in list_resp.json()]
    assert data["document"]["id"] in ids


def test_upload_txt_longer():
    """Upload a longer TXT that produces multiple chunks."""
    with client() as c:
        session = create_session(c)
        text = "This is paragraph one. " * 60  # ~1200 chars
        content = text.encode("utf-8")
        resp = c.post(
            f"/api/sessions/{session['id']}/documents",
            files={"file": ("research.txt", content, "text/plain")},
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["document"]["status"] == "ready"
    assert data["document"]["chunk_count"] >= 2


# ── PDF upload test ────────────────────────────────────────


def test_upload_pdf():
    """Upload a PDF file and verify text extraction."""
    pdf_bytes = _make_pdf_with_text("Research findings: The experiment confirmed the hypothesis.")
    with client() as c:
        session = create_session(c)
        resp = c.post(
            f"/api/sessions/{session['id']}/documents",
            files={"file": ("paper.pdf", pdf_bytes, "application/pdf")},
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["document"]["filename"] == "paper.pdf"
    assert data["document"]["status"] == "ready"
    assert data["document"]["chunk_count"] >= 1


# ── Invalid file tests ─────────────────────────────────────


def test_upload_invalid_extension():
    """Reject files with unsupported extensions."""
    with client() as c:
        session = create_session(c)
        resp = c.post(
            f"/api/sessions/{session['id']}/documents",
            files={"file": ("test.docx", b"content", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        )
    assert resp.status_code == 400


def test_upload_oversized():
    """Reject files larger than 20 MB."""
    with client() as c:
        session = create_session(c)
        large_content = b"x" * (21 * 1024 * 1024)  # 21 MB
        resp = c.post(
            f"/api/sessions/{session['id']}/documents",
            files={"file": ("large.txt", large_content, "text/plain")},
        )
    assert resp.status_code == 400


def test_upload_invalid_session():
    """Reject uploads for non-existent sessions."""
    with client() as c:
        resp = c.post(
            "/api/sessions/99999/documents",
            files={"file": ("test.txt", b"content", "text/plain")},
        )
    assert resp.status_code == 404


# ── Document deletion tests ────────────────────────────────


def test_delete_document():
    """Delete a document and verify it's removed from list."""
    with client() as c:
        session = create_session(c)
        up = c.post(
            f"/api/sessions/{session['id']}/documents",
            files={"file": ("delete_me.txt", b"Content to delete", "text/plain")},
        ).json()
        doc_id = up["document"]["id"]

        del_resp = c.delete(f"/api/documents/{doc_id}")
        assert del_resp.status_code == 204

        list_resp = c.get(f"/api/sessions/{session['id']}/documents")
        ids = [d["id"] for d in list_resp.json()]
        assert doc_id not in ids


def test_delete_document_404():
    """DELETE /api/documents/{id} returns 404 for unknown ID."""
    with client() as c:
        resp = c.delete("/api/documents/99999")
    assert resp.status_code == 404


# ── Document listing and detail tests ──────────────────────


def test_list_documents_empty():
    """GET /api/sessions/{id}/documents returns empty list when no docs."""
    with client() as c:
        session = create_session(c)
        resp = c.get(f"/api/sessions/{session['id']}/documents")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_document():
    """GET /api/documents/{id} returns document details."""
    with client() as c:
        session = create_session(c)
        up = c.post(
            f"/api/sessions/{session['id']}/documents",
            files={"file": ("detail.txt", b"Detail test content", "text/plain")},
        ).json()
        doc_id = up["document"]["id"]

        resp = c.get(f"/api/documents/{doc_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == doc_id
    assert data["filename"] == "detail.txt"
    assert data["status"] in ("processing", "ready")


def test_get_document_404():
    """GET /api/documents/{id} returns 404 for unknown ID."""
    with client() as c:
        resp = c.get("/api/documents/99999")
    assert resp.status_code == 404


# ── Chunking tests (pure unit tests, no API needed) ────────


def test_chunking_single_short():
    """Single short text produces one chunk."""
    from app.services.document_processor import chunk_text
    pages = [{"text": "Short text.", "page_number": 1}]
    chunks = chunk_text(pages, chunk_size=800, overlap=120)
    assert len(chunks) == 1
    assert chunks[0]["text"] == "Short text."


def test_chunking_multiple_chunks():
    """Long text is split into multiple overlapping chunks."""
    from app.services.document_processor import chunk_text
    text = "word " * 2000  # ~10000 chars
    pages = [{"text": text, "page_number": 1}]
    chunks = chunk_text(pages, chunk_size=800, overlap=120)
    assert len(chunks) >= 10


def test_chunking_page_numbers():
    """Chunks preserve page numbers from extracted pages."""
    from app.services.document_processor import chunk_text
    pages = [
        {"text": "Page one content. " * 30, "page_number": 1},
        {"text": "Page two content. " * 30, "page_number": 2},
    ]
    chunks = chunk_text(pages, chunk_size=800, overlap=120)
    page_nums = {c.get("page_number") for c in chunks}
    assert 1 in page_nums
    assert 2 in page_nums


# ── Mock Ollama embeddings test ─────────────────────────────


def test_upload_with_mocked_embeddings(monkeypatch):
    """
    Mock the embeddings client so upload works without Ollama running.
    Verifies the full upload pipeline: extract → chunk → embed → store.
    """
    import app.services.embeddings_client as ec

    def mock_embedding(text: str) -> list[float]:
        return [0.1] * 384

    def mock_batch(texts: list[str]) -> list[list[float]]:
        return [mock_embedding(t) for t in texts]

    monkeypatch.setattr(ec, "generate_embedding", mock_embedding)
    monkeypatch.setattr(ec, "generate_embeddings_batch", mock_batch)

    with client() as c:
        session = create_session(c)
        content = b"This is a test document with mocked embeddings."
        resp = c.post(
            f"/api/sessions/{session['id']}/documents",
            files={"file": ("mock_test.txt", content, "text/plain")},
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["document"]["status"] == "ready"
    assert data["document"]["chunk_count"] >= 1


# ── Mock ChromaDB test ──────────────────────────────────────


def test_chromadb_collection_isolation(monkeypatch):
    """
    Verify that ChromaDB operations use separate collections per session
    by mocking ChromaDB client and tracking calls.
    """
    called = []

    class MockCollection:
        def add(self, **kwargs):
            called.append(("add", kwargs.get("ids")))
        def delete(self, ids):
            called.append(("delete", ids))
        def query(self, **kwargs):
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        def count(self):
            return 0
        def update(self, ids, metadatas):
            called.append(("update", ids))

    class MockClient:
        def __init__(self):
            self.collections = {}
        def get_collection(self, name):
            called.append(("get", name))
            return self.collections.setdefault(name, MockCollection())
        def create_collection(self, name):
            called.append(("create", name))
            return self.collections.setdefault(name, MockCollection())
        def delete_collection(self, name):
            called.append(("delete_collection", name))

    import app.services.chromadb_client as cc
    monkeypatch.setattr(cc, "get_client", lambda: MockClient())

    # Verify collection names are session-scoped
    cc.get_or_create_collection(1)
    cc.get_or_create_collection(2)
    cc.get_or_create_collection(1)

    names = [c[1] for c in called if c[0] in ("get", "create")]
    assert "session_1" in names
    assert "session_2" in names


# ── Citation mapping tests ──────────────────────────────────


def test_citation_building():
    """Verify that citation markers are built correctly from retrieved chunks."""
    from app.services.rag_service import build_citation_list

    chunks = [
        {
            "text": "The Earth orbits the Sun at about 93 million miles.",
            "document_id": 1,
            "filename": "astronomy.txt",
            "page_number": 3,
            "chunk_db_id": 101,
            "chroma_id": "doc_1_chunk_0",
            "distance": 0.15,
        },
        {
            "text": "Jupiter is the largest planet in our solar system.",
            "document_id": 1,
            "filename": "astronomy.txt",
            "page_number": 5,
            "chunk_db_id": 102,
            "chroma_id": "doc_1_chunk_1",
            "distance": 0.22,
        },
    ]

    citations = build_citation_list(chunks)
    assert len(citations) == 2
    assert citations[0]["marker"] == "[1]"
    assert citations[0]["document_id"] == 1
    assert citations[0]["filename"] == "astronomy.txt"
    assert citations[0]["page_number"] == 3
    assert citations[0]["chunk_id"] == 101
    assert "Sun" in citations[0]["snippet"]

    assert citations[1]["marker"] == "[2]"
    assert citations[1]["chunk_id"] == 102
    assert "Jupiter" in citations[1]["snippet"]


# ── RAG context formatting test ────────────────────────────


def test_rag_context_formatting():
    """Verify the RAG context block is properly formatted for the LLM prompt."""
    from app.services.rag_service import format_rag_context

    chunks = [
        {
            "text": "Paris is the capital of France.",
            "document_id": 1,
            "filename": "geography.txt",
            "page_number": 2,
            "chunk_db_id": 5,
            "chroma_id": "doc_1_chunk_0",
            "distance": 0.1,
        },
    ]

    context = format_rag_context(chunks)
    assert "[1]" in context
    assert "geography.txt" in context
    assert "Paris" in context
    assert "untrusted data" in context.lower()
    assert "not instructions" in context.lower()
    assert "Do NOT fabricate citations" in context


def test_no_citations_for_no_docs():
    """When no documents exist, citations list should be empty."""
    from app.services.rag_service import build_citation_list
    assert build_citation_list([]) == []


def test_rag_context_empty_when_no_docs():
    """When no documents are ready, format_rag_context returns empty string."""
    from app.services.rag_service import format_rag_context
    assert format_rag_context([]) == ""
