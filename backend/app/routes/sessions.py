import os
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Document, ResearchSession, ScheduledTask, User
from app.schemas.sessions import (
    SessionCreate,
    SessionUpdate,
    SessionResponse,
    ModelUpdate,
    SystemPromptUpdate,
    SystemPromptResponse,
    SessionModelResponse,
    ThunderAIExport,
    ImportResult,
)
from app.services.chromadb_client import delete_chunks, delete_collection
from app.config import settings
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf
from app.services.llm_providers import get_provider
from app.services.settings_service import get_memory_enabled
from app.services.memory_service import decay_memories

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

logger = logging.getLogger(__name__)


# ── Patchable wrapper for model validation ─────────────────
def fetch_available_chat_models(db: Session = None, user_id: int = None):
    """Return available chat models from ALL of the user's configured providers.

    Falls back to the globally configured provider (e.g. the deterministic
    LocalProvider when LLM_PROVIDER=local) when the user has no saved
    provider rows, so model validation still works for fresh accounts.

    Note: ``db``/``user_id`` are optional so tests (and older call sites)
    can monkeypatch this function with zero-argument stubs, e.g.
    ``lambda: None`` or ``lambda: ["llama3.2:3b"]``.
    """
    from app.services.settings_service import list_user_providers
    from app.services.llm_providers import get_provider

    # If a test stub replaced this function, honor the zero-arg contract.
    all_models: List[str] = []

    if db is not None and user_id is not None:
        rows = list_user_providers(db, user_id)
        for row in rows:
            try:
                prov = get_provider(
                    config={
                        "provider": row.provider_name,
                        "api_key": row.api_key or None,
                        "model": row.default_model or None,
                    }
                )
                names = prov.fetch_available_chat_models() or []
                all_models.extend(names)
            except Exception:
                continue

        if all_models:
            return all_models

    # No user providers configured (or none reachable) — fall back to the
    # globally configured provider's discovery.
    try:
        prov = get_provider()
        return prov.fetch_available_chat_models() or []
    except Exception:
        return []


def _get_session_or_404(
    db: Session,
    session_id: int,
    user_id: int,
) -> ResearchSession:
    """
    Get a session by ID, scoped to a specific user.

    Returns 404 if the session does not exist OR does not belong to the
    given user, preventing user/session enumeration across accounts.
    """
    session = (
        db.query(ResearchSession)
        .filter(ResearchSession.id == session_id, ResearchSession.user_id == user_id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return session


def _cleanup_session_artifacts(db: Session, session_id: int):
    """
    Clean up uploaded files and ChromaDB vectors for a session's documents.
    Called before deleting the session itself.
    """
    docs = db.query(Document).filter(Document.session_id == session_id).all()
    all_chroma_ids = []
    for doc in docs:
        # Delete uploaded file
        if doc.file_path and os.path.exists(doc.file_path):
            try:
                os.remove(doc.file_path)
            except OSError:
                pass
        # Collect ChromaDB IDs
        for chunk in doc.chunks:
            if chunk.chroma_id:
                all_chroma_ids.append(chunk.chroma_id)

    # Delete ChromaDB vectors
    if all_chroma_ids:
        try:
            delete_chunks(session_id, all_chroma_ids)
        except Exception:
            pass

    # Delete the entire collection
    try:
        delete_collection(session_id)
    except Exception:
        pass


@router.post("", response_model=SessionResponse, status_code=201)
def create_session(
    payload: SessionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Create a new research session owned by the current user."""
    session = ResearchSession(title=payload.title, user_id=current_user.id, model=None)
    db.add(session)
    db.commit()
    db.refresh(session)
    # Phase 2: forget stale memories whenever a fresh session begins.
    try:
        decay_memories(db, current_user.id)
    except Exception as exc:
        logger.warning("decay_memories on session create failed (non-fatal): %s", exc)
    return session


@router.get("", response_model=List[SessionResponse])
def list_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all sessions for the current user, newest first."""
    sessions = (
        db.query(ResearchSession)
        .filter(ResearchSession.user_id == current_user.id)
        .order_by(ResearchSession.updated_at.desc())
        .all()
    )
    return sessions


@router.post("/import", response_model=ImportResult, status_code=201)
def import_session(
    payload: ThunderAIExport,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Import a session configuration from an export payload.

    Creates a new session with the imported config. Does NOT import messages,
    documents, or memories — only the agent's configuration (prompt, model,
    schedule, memory setting).
    """
    export = payload.thunder_ai_export

    if export.version != "1.0":
        raise HTTPException(status_code=400, detail=f"Unsupported export version: {export.version}")

    # Create the session
    session = ResearchSession(
        title=export.session.title,
        user_id=current_user.id,
        model=export.session.model,
        system_prompt=export.session.system_prompt,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    # Create scheduled task if schedule data is present
    schedule_created = False
    if export.schedule.cron_expression and export.schedule.prompt:
        # Validate cron expression
        try:
            from apscheduler.triggers.cron import CronTrigger
            CronTrigger.from_crontab(export.schedule.cron_expression)
        except Exception:
            # Skip schedule creation if cron is invalid, but don't fail the import
            pass
        else:
            task = ScheduledTask(
                user_id=current_user.id,
                session_id=session.id,
                prompt=export.schedule.prompt,
                cron_expression=export.schedule.cron_expression,
                is_active=export.schedule.is_active,
            )
            db.add(task)
            db.commit()
            schedule_created = True

            # Register with scheduler if active
            if export.schedule.is_active:
                try:
                    from app.services.scheduler_service import add_task_to_scheduler
                    add_task_to_scheduler(task)
                except Exception:
                    pass  # Best-effort: don't fail import if scheduler is down

    return ImportResult(
        session_id=session.id,
        title=session.title,
        schedule_created=schedule_created,
    )


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a single session by ID (scoped to current user)."""
    return _get_session_or_404(db, session_id, user_id=current_user.id)


@router.patch("/{session_id}", response_model=SessionResponse)
def update_session(
    session_id: int,
    payload: SessionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Rename a session (scoped to current user)."""
    session = _get_session_or_404(db, session_id, user_id=current_user.id)
    session.title = payload.title
    db.commit()
    db.refresh(session)
    return session


@router.delete("/{session_id}", status_code=204)
def delete_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Delete a session and all its messages/documents (scoped to current user)."""
    session = _get_session_or_404(db, session_id, user_id=current_user.id)
    # Clean up uploaded files and ChromaDB vectors before deleting
    _cleanup_session_artifacts(db, session_id)
    db.delete(session)
    db.commit()
    return None


# ── Model selection ────────────────────────────────────────


@router.patch("/{session_id}/model", response_model=SessionModelResponse)
def update_session_model(
    session_id: int,
    payload: ModelUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """
    Set the model for a session (or null to use default), scoped to current user.

    The model is validated against the chat models available from Ollama:
      - null / empty → use the configured default chat model
      - embedding-only models (e.g. nomic-embed-text) are rejected
      - models not installed on Ollama are rejected
    """
    session = _get_session_or_404(db, session_id, user_id=current_user.id)

    model = payload.model
    if model is not None:
        model = model.strip()
        if model == "":
            model = None

    if model is not None:
        if "embed" in model.lower():
            raise HTTPException(
                status_code=400,
                detail=f"Model '{model}' is an embedding-only model and cannot be used for chat",
            )
        available = fetch_available_chat_models(db, current_user.id)
        if not available:
            raise HTTPException(
                status_code=503,
                detail="Unable to verify the model because no provider model discovery is available",
            )
        if model not in available:
            raise HTTPException(
                status_code=400,
                detail=f"Model '{model}' is not available on any of your configured providers",
            )

    session.model = model
    db.commit()
    db.refresh(session)
    return SessionModelResponse(id=session.id, model=session.model)


# ── System prompt ──────────────────────────────────────────


DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful research assistant. Answer the user's questions "
    "clearly and concisely."
)


@router.get("/{session_id}/system-prompt", response_model=SystemPromptResponse)
def get_system_prompt(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get the system prompt for a session (scoped to current user)."""
    session = _get_session_or_404(db, session_id, user_id=current_user.id)
    if session.system_prompt:
        return SystemPromptResponse(
            system_prompt=session.system_prompt,
            using_default=False,
        )
    return SystemPromptResponse(
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        using_default=True,
    )


@router.patch("/{session_id}/system-prompt", response_model=SystemPromptResponse)
def update_system_prompt(
    session_id: int,
    payload: SystemPromptUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Update the system prompt for a session (or null to reset to default)."""
    session = _get_session_or_404(db, session_id, user_id=current_user.id)
    session.system_prompt = payload.system_prompt
    db.commit()
    db.refresh(session)

    if session.system_prompt:
        return SystemPromptResponse(
            system_prompt=session.system_prompt,
            using_default=False,
        )
    return SystemPromptResponse(
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        using_default=True,
    )


# ── Export / Import (F5: Shareable Agents) ─────────────────


@router.get("/{session_id}/export")
def export_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Export a session's configuration as a shareable JSON payload."""
    session = _get_session_or_404(db, session_id, user_id=current_user.id)

    # Fetch scheduled tasks for this session (active ones)
    tasks = (
        db.query(ScheduledTask)
        .filter(
            ScheduledTask.session_id == session_id,
            ScheduledTask.user_id == current_user.id,
        )
        .order_by(ScheduledTask.created_at)
        .all()
    )

    # Use the first active task's schedule, or the most recent task overall
    schedule_cron = None
    schedule_prompt = None
    schedule_active = False
    for t in tasks:
        if t.is_active:
            schedule_cron = t.cron_expression
            schedule_prompt = t.prompt
            schedule_active = True
            break
    if tasks and not schedule_active:
        # Fall back to most recent task's config (inactive)
        latest = tasks[-1]
        schedule_cron = latest.cron_expression
        schedule_prompt = latest.prompt

    memory_enabled = get_memory_enabled(db)

    return {
        "thunder_ai_export": {
            "version": "1.0",
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "session": {
                "title": session.title,
                "model": session.model,
                "system_prompt": session.system_prompt,
            },
            "schedule": {
                "cron_expression": schedule_cron,
                "prompt": schedule_prompt,
                "is_active": schedule_active,
            },
            "memory": {
                "enabled": memory_enabled,
            },
        }
    }
