"""
Document upload and management routes.

POST   /api/sessions/{session_id}/documents  — Upload a PDF or TXT file
GET    /api/sessions/{session_id}/documents  — List documents for a session
GET    /api/documents/{document_id}          — Get single document details
DELETE /api/documents/{document_id}          — Delete a document + chunks + vectors
"""

import json
import os
import uuid
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.models import (
    Document,
    DocumentChunk,
    DocumentStatus,
    ResearchSession,
    User,
)
from app.schemas.documents import (
    DocumentResponse,
    DocumentListResponse,
    UploadResponse,
)
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf
from app.services.document_processor import extract_text, chunk_text
import app.services.embeddings_client as embeddings_client
from app.services.chromadb_client import add_chunks, delete_chunks, delete_collection

router = APIRouter(tags=["documents"])

# ── Constants ──────────────────────────────────────────────

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cpp", ".c", ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".r", ".m", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".sql", ".html", ".htm", ".css", ".scss", ".sass", ".less", ".json", ".yaml", ".yml", ".xml", ".toml", ".ini", ".cfg", ".conf", ".md", ".markdown", ".rst", ".tex", ".dockerfile", ".gitignore", ".dockerignore"}
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "text/plain",
    "text/x-python",
    "application/javascript",
    "text/javascript",
    "application/typescript",
    "text/typescript",
    "text/x-java-source",
    "text/x-c",
    "text/x-c++",
    "text/x-csharp",
    "text/x-go",
    "text/x-rust",
    "text/x-ruby",
    "application/x-php",
    "text/x-swift",
    "text/x-kotlin",
    "text/x-scala",
    "text/x-r",
    "text/x-objc",
    "application/x-sh",
    "application/x-sql",
    "text/html",
    "text/css",
    "application/json",
    "application/x-yaml",
    "application/xml",
    "application/toml",
    "text/markdown",
    "application/x-tex",
    "text/x-dockerfile",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "text/markdown",
    "application/octet-stream",
}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB


# ── Helpers ────────────────────────────────────────────────



def _get_session_or_404(db: Session, session_id: int, user_id: int) -> ResearchSession:
    session = db.query(ResearchSession).filter(
        ResearchSession.id == session_id,
        ResearchSession.user_id == user_id,
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return session


def _get_document_or_404(db: Session, document_id: int) -> Document:
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")
    return doc


def _validate_file(filename: str, content_type: str, file_size: int) -> None:
    """Validate file extension, MIME type, and size."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file extension '{ext}'. Allowed: .pdf, .txt",
        )

    # Check MIME type - allow if extension is valid and MIME is generic
    if content_type not in ALLOWED_MIME_TYPES:
        # Allow generic octet-stream for allowed extensions (browser may send generic MIME for .md/.docx)
        if content_type not in ("application/octet-stream", "") and not content_type.startswith("text/"):
            # Still allow if extension is explicitly allowed - be permissive for drag-drop
            if ext not in ALLOWED_EXTENSIONS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid file type '{content_type}'. Allowed: application/pdf, text/plain",
                )

    # Check file size
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large ({file_size / 1024 / 1024:.1f} MB). Maximum: 20 MB",
        )

    # Protect against path traversal in filename
    clean_name = Path(filename).name
    if clean_name != filename or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")


# ── Endpoints ──────────────────────────────────────────────


@router.post(
    "/api/sessions/{session_id}/documents",
    response_model=UploadResponse,
    status_code=201,
)
async def upload_document(
    session_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """
    Upload a PDF or TXT document (scoped to current user's session).

    The file is validated, saved to /data/uploads, then processed:
      1. Text extraction (pypdf for PDF, plain read for TXT)
      2. Chunking (800 chars, 120 overlap)
      3. Embedding generation (nomic-embed-text via Ollama)
      4. Storage in ChromaDB (session_{session_id} collection)
    """
    session = _get_session_or_404(db, session_id, current_user.id)

    # Read file content
    file_content = await file.read()
    file_size = len(file_content)

    # Validate
    _validate_file(file.filename, file.content_type or "", file_size)

    # Generate secure UUID filename
    ext = Path(file.filename).suffix
    safe_filename = f"{uuid.uuid4().hex}{ext}"
    upload_path = os.path.join(settings.upload_dir, safe_filename)

    # Create DB record
    doc = Document(
        session_id=session.id,
        filename=file.filename,  # Original name preserved
        content_type=file.content_type,
        file_path=upload_path,
        file_size=file_size,
        status=DocumentStatus.processing.value,
        chunk_count=0,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    try:
        # Save file to disk
        os.makedirs(settings.upload_dir, exist_ok=True)
        with open(upload_path, "wb") as f:
            f.write(file_content)

        # Step 1: Extract text
        pages = extract_text(upload_path, doc.content_type or "text/plain")

        # Step 2: Chunk text
        chunks = chunk_text(pages)

        # Step 3: Generate embeddings
        texts = [c["text"] for c in chunks]
        embeddings = embeddings_client.generate_embeddings_batch(texts)

        # Step 4: Store in ChromaDB
        chroma_ids = []
        chroma_embeddings = []
        chroma_documents = []
        chroma_metadatas = []

        for i, chunk in enumerate(chunks):
            chunk_id_str = f"doc_{doc.id}_chunk_{i}"
            chroma_ids.append(chunk_id_str)
            chroma_embeddings.append(embeddings[i])
            chroma_documents.append(chunk["text"])
            chroma_metadatas.append({
                "document_id": doc.id,
                "original_filename": doc.filename,
                "source": doc.filename,
                "page_number": chunk.get("page_number") or 0,
                "chunk_index": chunk["chunk_index"],
                "chunk_db_id": 0,  # Will update after DB insert
            })

        add_chunks(
            session_id=session.id,
            chunk_ids=chroma_ids,
            embeddings=chroma_embeddings,
            documents=chroma_documents,
            metadatas=chroma_metadatas,
        )

        # Step 5: Save DocumentChunk records to SQLite
        for i, chunk in enumerate(chunks):
            chunk_id_str = f"doc_{doc.id}_chunk_{i}"
            db_chunk = DocumentChunk(
                document_id=doc.id,
                chunk_index=chunk["chunk_index"],
                content=chunk["text"],
                page_number=chunk.get("page_number"),
                chroma_id=chunk_id_str,
            )
            db.add(db_chunk)
            db.flush()  # Get the DB-generated ID

            # Update metadata with the actual chunk_db_id
            chroma_metadatas[i]["chunk_db_id"] = db_chunk.id

        # Update metadata in ChromaDB with correct chunk_db_id
        try:
            from app.services.chromadb_client import get_or_create_collection
            collection = get_or_create_collection(session.id)
            for i, cid in enumerate(chroma_ids):
                collection.update(
                    ids=[cid],
                    metadatas=[chroma_metadatas[i]],
                )
        except Exception:
            pass  # Non-critical update, chunks are already indexed

        # Update document status
        doc.status = DocumentStatus.ready.value
        doc.chunk_count = len(chunks)
        db.commit()
        db.refresh(doc)

        return UploadResponse(
            document=DocumentResponse.model_validate(doc),
            message=f"Document processed: {len(chunks)} chunks created and indexed in ChromaDB",
        )

    except HTTPException:
        db.rollback()
        # Clean up file
        if os.path.exists(upload_path):
            os.remove(upload_path)
        raise

    except Exception as exc:
        db.rollback()
        # Mark document as failed
        doc.status = DocumentStatus.failed.value
        doc.error_message = str(exc)[:500]
        db.commit()
        db.refresh(doc)

        return UploadResponse(
            document=DocumentResponse.model_validate(doc),
            message=f"Document processing failed: {str(exc)[:200]}",
        )


@router.get("/api/sessions/{session_id}/documents", response_model=List[DocumentResponse])
def list_documents(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all documents for a session (scoped to current user)."""
    _get_session_or_404(db, session_id, current_user.id)
    docs = (
        db.query(Document)
        .filter(Document.session_id == session_id)
        .order_by(Document.created_at.desc())
        .all()
    )
    return [DocumentResponse.model_validate(d) for d in docs]


@router.get("/api/documents/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get details for a single document (scoped to current user)."""
    doc = _get_document_or_404(db, document_id)
    # Verify the document belongs to a session owned by the current user
    session = db.query(ResearchSession).filter(
        ResearchSession.id == doc.session_id,
        ResearchSession.user_id == current_user.id,
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")
    return DocumentResponse.model_validate(doc)


@router.delete("/api/documents/{document_id}", status_code=204)
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """
    Delete a document and all associated data (scoped to current user).
      - Uploaded file
      - SQLite Document and DocumentChunk rows
      - ChromaDB vectors
    """
    doc = _get_document_or_404(db, document_id)
    # Verify the document belongs to a session owned by the current user
    session = db.query(ResearchSession).filter(
        ResearchSession.id == doc.session_id,
        ResearchSession.user_id == current_user.id,
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")
    session_id = doc.session_id

    # Collect ChromaDB IDs to delete
    chunk_ids = [c.chroma_id for c in doc.chunks if c.chroma_id]

    # Delete uploaded file
    if doc.file_path and os.path.exists(doc.file_path):
        try:
            os.remove(doc.file_path)
        except OSError:
            pass  # Non-critical

    # Delete ChromaDB vectors
    if chunk_ids:
        try:
            delete_chunks(session_id, chunk_ids)
        except Exception:
            pass  # Non-critical

    # Delete from SQLite (cascade handles DocumentChunk)
    db.delete(doc)
    db.commit()
    db.flush()

    # If no more documents for this session, clean up the collection
    remaining = (
        db.query(Document)
        .filter(Document.session_id == session_id)
        .count()
    )
    if remaining == 0:
        try:
            delete_collection(session_id)
        except Exception:
            pass

    return None
