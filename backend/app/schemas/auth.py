"""
Phase 6A — Auth request/response schemas.

Used by the register, login, and me endpoints.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    """Request body for POST /api/auth/register."""

    username: str = Field(
        ...,
        min_length=3,
        max_length=50,
        pattern=r"^[a-zA-Z0-9_]+$",
        description="Username (alphanumeric and underscores only)",
    )
    email: str = Field(..., max_length=255, description="User email address")
    password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="Password (minimum 8 characters)",
    )


class LoginRequest(BaseModel):
    """Request body for POST /api/auth/login."""

    username: str = Field(..., description="Username")
    password: str = Field(..., description="Password")


class TokenResponse(BaseModel):
    """Response body for successful login."""

    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    """Public user information (no password or sensitive data)."""

    id: int
    username: str
    email: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}
