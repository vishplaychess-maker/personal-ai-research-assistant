from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


# ── Chat request schema (shared across routes) ──────────────


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=10000)


# ── Citation schemas ───────────────────────────────────────


class Citation(BaseModel):
    """A single citation marker with source information."""

    marker: str  # e.g. "[1]", "[2]"
    document_id: int
    filename: str
    page_number: Optional[int] = None
    chunk_id: int
    snippet: str


# ── Document schemas ───────────────────────────────────────


class DocumentResponse(BaseModel):
    id: int
    session_id: int
    filename: str
    content_type: Optional[str] = None
    file_size: Optional[int] = None
    status: str  # processing, ready, failed
    chunk_count: int
    error_message: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentListResponse(BaseModel):
    documents: List[DocumentResponse]


class UploadResponse(BaseModel):
    document: DocumentResponse
    message: str


# ── Updated ChatResponse with citations ────────────────────


class MessageResponse(BaseModel):
    id: int
    session_id: int
    role: str
    content: str
    citations: Optional[str] = None  # JSON-serialized citation list
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatResponse(BaseModel):
    user_message: MessageResponse
    assistant_message: MessageResponse
    citations: List[Citation] = Field(default_factory=list)
    sources_used: bool = False  # Whether retrieved documents were included
