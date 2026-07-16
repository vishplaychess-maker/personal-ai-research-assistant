"""
Settings routes for Phase 4 — persistent application settings.

GET   /api/settings/memory   — Read the current memory-enabled setting
PATCH /api/settings/memory   — Update the memory-enabled setting

The setting is persisted in SQLite and survives Docker restarts.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.settings_service import get_memory_enabled, set_memory_enabled

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ── Schemas ────────────────────────────────────────────────


class MemorySettingResponse(BaseModel):
    enabled: bool


class MemorySettingUpdate(BaseModel):
    enabled: bool


# ── Endpoints ──────────────────────────────────────────────


@router.get("/memory", response_model=MemorySettingResponse)
def read_memory_setting(db: Session = Depends(get_db)):
    """
    Get the current memory-enabled setting.

    Returns the value from the database, falling back to the
    config default if no row exists yet.
    """
    return MemorySettingResponse(enabled=get_memory_enabled(db))


@router.patch("/memory", response_model=MemorySettingResponse)
def update_memory_setting(
    payload: MemorySettingUpdate,
    db: Session = Depends(get_db),
):
    """
    Update the memory-enabled setting and persist it to the database.

    Accepts and returns:
      { "enabled": true }
    """
    return MemorySettingResponse(enabled=set_memory_enabled(db, payload.enabled))
