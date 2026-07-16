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


class ModelInfo(BaseModel):
    """Represents an Ollama model from GET /api/tags."""
    name: str
    size: Optional[str] = None
    modified_at: Optional[str] = None


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
