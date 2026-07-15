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
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Message schemas ────────────────────────────────────────


class MessageCreate(BaseModel):
    role: str = Field(default="user", pattern="^(user|assistant|system)$")
    content: str = Field(..., min_length=1)


class MessageResponse(BaseModel):
    id: int
    session_id: int
    role: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Chat request / response ────────────────────────────────


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=10000)


class ChatResponse(BaseModel):
    user_message: MessageResponse
    assistant_message: MessageResponse
