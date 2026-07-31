import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy import inspect, text as sa_text

from app.config import settings
from app.database import init_db, engine, SessionLocal
from app.models.models import User

logger = logging.getLogger(__name__)
from app.routes.sessions import router as sessions_router
from app.routes.messages import router as messages_router
from app.routes.documents import router as documents_router
from app.routes.memories import router as memories_router
from app.routes.settings import router as settings_router
from app.routes.models import router as models_router
from app.routes.search import router as search_router
from app.routes.auth import router as auth_router


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

        # Add Phase 5B columns: model and system_prompt
        existing_sess_cols = {c["name"] for c in inspector.get_columns("research_sessions")}
        if "model" not in existing_sess_cols:
            conn.execute(sa_text("ALTER TABLE research_sessions ADD COLUMN model VARCHAR(100)"))
        if "system_prompt" not in existing_sess_cols:
            conn.execute(sa_text("ALTER TABLE research_sessions ADD COLUMN system_prompt TEXT"))

        # Add Phase 6A column: hashed_password on users
        existing_user_cols = {c["name"] for c in inspector.get_columns("users")}
        if "hashed_password" not in existing_user_cols:
            conn.execute(sa_text("ALTER TABLE users ADD COLUMN hashed_password VARCHAR(255)"))

        # Add Phase 7A columns: failed_login_attempts, locked_until
        if "failed_login_attempts" not in existing_user_cols:
            conn.execute(sa_text("ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER NOT NULL DEFAULT 0"))
        if "locked_until" not in existing_user_cols:
            conn.execute(sa_text("ALTER TABLE users ADD COLUMN locked_until TIMESTAMP"))

        # Phase 7B hardening: last_used_at on refresh_sessions (nullable so
        # existing rows keep working without a backfill default)
        existing_refresh_cols = {c["name"] for c in inspector.get_columns("refresh_sessions")}
        if "last_used_at" not in existing_refresh_cols:
            conn.execute(sa_text("ALTER TABLE refresh_sessions ADD COLUMN last_used_at TIMESTAMP"))
            # Backfill new column with created_at for existing rows
            conn.execute(sa_text(
                "UPDATE refresh_sessions SET last_used_at = created_at "
                "WHERE last_used_at IS NULL"
            ))

        conn.commit()


def _validate_jwt_secret():
    """
    Validate JWT secret at startup.

    In production mode (PRODUCTION_MODE=true), refuse to start with
    the default secret. In development/test mode, allow with a warning.
    """
    default_secret = "change-me-in-production"
    if settings.jwt_secret == default_secret:
        if settings.production_mode:
            logger.critical(
                "Default JWT secret detected in production mode! "
                "Set JWT_SECRET environment variable to a secure random key.\n"
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
            sys.exit(1)
        else:
            logger.warning(
                "Using default JWT secret '%s'. "
                "Set JWT_SECRET environment variable for production.\n"
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\"",
                default_secret,
            )
    else:
        logger.info("JWT secret configured (custom value, not default)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables and default user on startup."""
    # Ensure the SQLite parent directory exists
    Path("/data").mkdir(parents=True, exist_ok=True)
    Path("/data/uploads").mkdir(parents=True, exist_ok=True)

    _validate_jwt_secret()
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
app.include_router(models_router)
app.include_router(search_router)
app.include_router(auth_router)


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
