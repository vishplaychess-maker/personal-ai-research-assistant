from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── Session schemas ────────────────────────────────────────


class SessionCreate(BaseModel):
    title: str = Field(default="New Research Session", max_length=255)


class SessionUpdate(BaseModel):
    title: str = Field(..., max_length=255)


class SessionResponse(BaseModel):
    id: int
    title: str
    user_id: int
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Model schemas ──────────────────────────────────────────


def is_free_model(model_name: str, provider: Optional[str] = None) -> bool:
    """Determine if a model is free-tier.

    - Local / Ollama models are always free.
    - OpenRouter: all models returned by API are free (filtered by pricing 0) -> True
    - Models containing ':free' are free (e.g. meta-llama/...:free).
    """
    name_lower = model_name.lower()
    provider_lower = (provider or "").lower()
    if provider_lower in ("ollama", "local"):
        return True
    if provider_lower == "openrouter":
        return True  # we only expose free-tier via pricing filter
    if ":free" in name_lower:
        return True
    return False


class ModelInfo(BaseModel):
    """Represents an Ollama model from GET /api/tags."""
    name: str
    size: Optional[str] = None
    modified_at: Optional[str] = None
    is_free: bool = False


class ModelListResponse(BaseModel):
    models: list[ModelInfo] = []
    error: Optional[str] = None


class ModelUpdate(BaseModel):
    model: Optional[str] = None  # null = use default


# ── System prompt schemas ──────────────────────────────────


class SystemPromptUpdate(BaseModel):
    system_prompt: Optional[str] = Field(None, max_length=2000)


class SystemPromptResponse(BaseModel):
    system_prompt: Optional[str] = None
    using_default: bool = False


class SessionModelResponse(BaseModel):
    id: int
    model: Optional[str] = None


# ── Export / Import schemas ─────────────────────────────────


class ExportSessionSchedule(BaseModel):
    cron_expression: Optional[str] = None
    prompt: Optional[str] = None
    is_active: bool = False


class ExportSessionData(BaseModel):
    title: str
    model: Optional[str] = None
    system_prompt: Optional[str] = None


class ExportPayload(BaseModel):
    version: str = "1.0"
    exported_at: str
    session: ExportSessionData
    schedule: ExportSessionSchedule
    memory: dict  # {"enabled": bool}


class ThunderAIExport(BaseModel):
    thunder_ai_export: ExportPayload


class ImportResult(BaseModel):
    session_id: int
    title: str
    schedule_created: bool


# ── Shareable Agent Card (F6) ──────────────────────────────


class ShareCreateResult(BaseModel):
    """Returned to the owner when they publish a share link."""

    share_id: str
    share_url: str


class PublicSharedAgent(BaseModel):
    """Public-safe snapshot rendered on the share card page.

    Deliberately excludes session_id, user_id and view_count internals that
    could be used to enumerate/associate records. view_count is exposed (as
    views) because it's the growth metric, but nothing else about the owner.
    """

    share_id: str
    title: str
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    preview_message: Optional[str] = None
    tool_count: int = 0
    has_schedule: bool = False
    cover_image_url: Optional[str] = None
    views: int = 0

    model_config = {"from_attributes": True}
