"""
Memory schemas for Phase 4 — Long-Term Memory.

Schemas:
  MemoryCreate   — Create a memory manually
  MemoryUpdate   — Edit an existing memory
  MemoryResponse — Full memory representation
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


VALID_CATEGORIES = {"fact", "preference", "research_interest", "project_context"}


class MemoryCreate(BaseModel):
    """Create a new memory manually."""

    content: str = Field(..., min_length=1, max_length=500, description="Memory content")
    category: str = Field(default="fact", description="Memory category")
    session_id: Optional[int] = Field(default=None, description="Optional session association")


class MemoryUpdate(BaseModel):
    """Edit an existing memory."""

    content: str = Field(..., min_length=1, max_length=500, description="Updated memory content")
    category: str = Field(default="fact", description="Memory category")


class MemoryResponse(BaseModel):
    """Full memory representation."""

    id: int
    user_id: int
    session_id: Optional[int] = None
    content: str
    category: str
    created_at: datetime
    last_used_at: datetime

    model_config = {"from_attributes": True}
