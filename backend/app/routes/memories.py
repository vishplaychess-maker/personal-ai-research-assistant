"""
Memory management routes for Phase 4 — Transparent Long-Term Memory.

GET    /api/memories              — List all memories
POST   /api/memories              — Create a memory manually
PATCH  /api/memories/{id}         — Edit a memory
DELETE /api/memories/{id}         — Delete a single memory
DELETE /api/memories              — Clear all memories (requires confirmation)

Technical debt (multi-user):
  All endpoints hardcode user_id=1 (DEFAULT_USER_ID). This is correct for the
  current single-user prototype but MUST be made dynamic when authentication /
  multi-user support is added in a future phase.
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.models import Memory, User
from app.schemas.memories import MemoryCreate, MemoryUpdate, MemoryResponse

router = APIRouter(prefix="/api/memories", tags=["memories"])

# TODO (multi-user): Replace with dynamic user resolution from auth context
DEFAULT_USER_ID = 1


# ── Clear-all confirmation schema ──────────────────────────


class ClearMemoriesRequest(BaseModel):
    confirm: bool = Field(..., description="Must be true to clear all memories")


# ── Helpers ────────────────────────────────────────────────


def _get_memory_or_404(db: Session, memory_id: int) -> Memory:
    memory = db.query(Memory).filter(Memory.id == memory_id).first()
    if not memory:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
    return memory


# ── Endpoints ──────────────────────────────────────────────


@router.get("", response_model=List[MemoryResponse])
def list_memories(db: Session = Depends(get_db)):
    """
    List all memories for the default user, most recently used first.
    Works even when memory is disabled (view-only).
    """
    memories = (
        db.query(Memory)
        .filter(Memory.user_id == DEFAULT_USER_ID)
        .order_by(Memory.last_used_at.desc())
        .all()
    )
    return [MemoryResponse.model_validate(m) for m in memories]


@router.post("", response_model=MemoryResponse, status_code=201)
def create_memory(payload: MemoryCreate, db: Session = Depends(get_db)):
    """
    Create a new memory manually.

    Validates the category and checks for sensitive content.
    Works even when memory extraction is disabled (manual add still allowed).
    """
    # Validate category
    valid_categories = {"fact", "preference", "research_interest", "project_context"}
    if payload.category not in valid_categories:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category '{payload.category}'. Valid: {', '.join(sorted(valid_categories))}",
        )

    # Check for sensitive content
    from app.services.memory_service import _is_sensitive
    if _is_sensitive(payload.content):
        raise HTTPException(
            status_code=400,
            detail="Cannot save memories containing sensitive information (passwords, keys, tokens, etc.)",
        )

    # Ensure default user exists
    user = db.query(User).filter(User.id == DEFAULT_USER_ID).first()
    if not user:
        raise HTTPException(status_code=500, detail="Default user not found")

    memory = Memory(
        user_id=DEFAULT_USER_ID,
        session_id=payload.session_id,
        content=payload.content,
        category=payload.category,
    )
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return memory


@router.patch("/{memory_id}", response_model=MemoryResponse)
def update_memory(
    memory_id: int,
    payload: MemoryUpdate,
    db: Session = Depends(get_db),
):
    """Edit a memory's content and/or category."""
    memory = _get_memory_or_404(db, memory_id)

    # Validate category
    valid_categories = {"fact", "preference", "research_interest", "project_context"}
    if payload.category not in valid_categories:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category '{payload.category}'. Valid: {', '.join(sorted(valid_categories))}",
        )

    # Check for sensitive content
    from app.services.memory_service import _is_sensitive
    if _is_sensitive(payload.content):
        raise HTTPException(
            status_code=400,
            detail="Cannot save memories containing sensitive information",
        )

    memory.content = payload.content
    memory.category = payload.category
    db.commit()
    db.refresh(memory)
    return memory


@router.delete("/{memory_id}", status_code=204)
def delete_memory(memory_id: int, db: Session = Depends(get_db)):
    """Delete a single memory by ID."""
    memory = _get_memory_or_404(db, memory_id)
    db.delete(memory)
    db.commit()
    return None


@router.delete("", status_code=204)
def clear_all_memories(payload: ClearMemoriesRequest, db: Session = Depends(get_db)):
    """
    Delete ALL memories for the default user.

    Requires explicit confirmation via JSON body:
      { "confirm": true }
    """
    if not payload.confirm:
        raise HTTPException(
            status_code=400,
            detail="Confirmation required. Set 'confirm' to true to clear all memories.",
        )

    memories = db.query(Memory).filter(Memory.user_id == DEFAULT_USER_ID).all()
    for mem in memories:
        db.delete(mem)
    db.commit()
    return None
