from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy import inspect, text as sa_text

from app.config import settings
from app.database import init_db, engine, SessionLocal
from app.models.models import User
from app.routes.sessions import router as sessions_router
from app.routes.messages import router as messages_router
from app.routes.documents import router as documents_router
from app.routes.memories import router as memories_router
from app.routes.settings import router as settings_router


def _migrate_database():
    """Add new columns to existing tables without losing data."""
    inspector = inspect(engine)
    with engine.connect() as conn:
        # Add columns to documents table if missing
        existing_doc_cols = {c["name"] for c in inspector.get_columns("documents")}
        if "file_size" not in existing_doc_cols:
            conn.execute(sa_text("ALTER TABLE documents ADD COLUMN file_size INTEGER"))
        if "status" not in existing_doc_cols:
            conn.execute(sa_text("ALTER TABLE documents ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'processing'"))
        if "chunk_count" not in existing_doc_cols:
            conn.execute(sa_text("ALTER TABLE documents ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 0"))
        if "error_message" not in existing_doc_cols:
            conn.execute(sa_text("ALTER TABLE documents ADD COLUMN error_message TEXT"))

        # Add columns to document_chunks table if missing
        existing_chunk_cols = {c["name"] for c in inspector.get_columns("document_chunks")}
        if "page_number" not in existing_chunk_cols:
            conn.execute(sa_text("ALTER TABLE document_chunks ADD COLUMN page_number INTEGER"))
        if "chroma_id" not in existing_chunk_cols:
            conn.execute(sa_text("ALTER TABLE document_chunks ADD COLUMN chroma_id VARCHAR(100)"))

        # Add column to messages table if missing
        existing_msg_cols = {c["name"] for c in inspector.get_columns("messages")}
        if "citations" not in existing_msg_cols:
            conn.execute(sa_text("ALTER TABLE messages ADD COLUMN citations TEXT"))

        # Migrate memories table columns
        existing_mem_cols = {c["name"] for c in inspector.get_columns("memories")}
        if "session_id" not in existing_mem_cols:
            conn.execute(sa_text("ALTER TABLE memories ADD COLUMN session_id INTEGER REFERENCES research_sessions(id)"))
        if "category" not in existing_mem_cols:
            conn.execute(sa_text("ALTER TABLE memories ADD COLUMN category VARCHAR(30) NOT NULL DEFAULT 'fact'"))
        if "last_used_at" not in existing_mem_cols:
            conn.execute(sa_text("ALTER TABLE memories ADD COLUMN last_used_at TIMESTAMP"))
        # Update existing memories' last_used_at
        conn.execute(sa_text("UPDATE memories SET last_used_at = created_at WHERE last_used_at IS NULL"))
        # Refresh column info after ALTER TABLE ADD COLUMN
        existing_mem_cols = {c["name"] for c in inspector.get_columns("memories")}
        # Migrate old 'type' column → new 'category' column
        if "type" in existing_mem_cols and "category" in existing_mem_cols:
            # Copy existing type values to category where possible
            try:
                conn.execute(sa_text(
                    "UPDATE memories SET category = 'fact' WHERE type IN ('fact', 'note', 'tag')"
                ))
            except Exception:
                pass
            # Drop the old type column (SQLite 3.35.0+ supports this)
            try:
                conn.execute(sa_text("ALTER TABLE memories DROP COLUMN type"))
            except Exception:
                pass  # If drop fails, column remains but won't be used

        conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables and default user on startup."""
    # Ensure the SQLite parent directory exists
    Path("/data").mkdir(parents=True, exist_ok=True)
    Path("/data/uploads").mkdir(parents=True, exist_ok=True)

    init_db()
    _migrate_database()
    _create_default_user()
    yield


def _create_default_user():
    """Create a default user if one does not exist."""
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == "default").first()
        if not existing:
            user = User(username="default", email="default@example.com")
            db.add(user)
            db.commit()
            print("✓ Created default user")
        else:
            print("✓ Default user already exists")
    except Exception as exc:
        db.rollback()
        print(f"✗ Error creating default user: {exc}")
    finally:
        db.close()


app = FastAPI(
    title="Personal AI Research Assistant",
    version="0.3.0",
    lifespan=lifespan,
)

# ── Register routers ─────────────────────────────────────
app.include_router(sessions_router)
app.include_router(messages_router)
app.include_router(documents_router)
app.include_router(memories_router)
app.include_router(settings_router)


@app.get("/api/health")
async def health_check():
    """Check whether Backend, ChromaDB, and Ollama are reachable."""
    backend_status = "ok"
    chromadb_status = "unavailable"
    ollama_status = "unavailable"

    # Check ChromaDB ──────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.chromadb_url}/api/v2/heartbeat")
            if resp.status_code == 200:
                chromadb_status = "ok"
    except Exception:
        pass

    # Check Ollama ────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.ollama_url}/")
            if resp.status_code == 200 and "Ollama" in resp.text:
                ollama_status = "ok"
    except Exception:
        pass

    return {
        "backend": backend_status,
        "chromadb": chromadb_status,
        "ollama": ollama_status,
    }
