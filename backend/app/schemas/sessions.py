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
