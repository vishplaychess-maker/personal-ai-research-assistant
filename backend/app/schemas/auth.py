"""
Phase 6A / 7B — Auth request/response schemas.

Phase 7B additions:
- LoginResponse: now includes refresh_token
- RefreshRequest / RefreshResponse: token rotation
- LogoutRequest / LogoutResponse: session invalidation
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
    """Response body for successful login (Phase 6A, backward compat)."""

    access_token: str
    token_type: str = "bearer"


class LoginResponse(BaseModel):
    """Response body for successful login (Phase 7B, includes refresh token).

    When the frontend expects both access and refresh tokens, use this.
    For backward compatibility, TokenResponse is still accepted by existing
    frontend code until Phase 7B frontend update is deployed.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    """Request body for POST /api/auth/refresh."""

    refresh_token: str = Field(..., description="The refresh token to exchange")


class RefreshResponse(BaseModel):
    """Response body for successful token refresh."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LogoutResponse(BaseModel):
    """Response body for logout."""

    detail: str = "Logged out successfully"


class SessionInfo(BaseModel):
    """
    Safe session metadata returned to the client.

    Never returns token_hash or any raw token data.
    `last_used_at` is a proxy for the session creation time;
    a full implementation would update it on each token refresh.
    """

    id: int
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    revoked_at: Optional[datetime] = None
    device_info: Optional[str] = None
    is_current: bool = False

    model_config = {"from_attributes": True}


class SessionsListResponse(BaseModel):
    """Response containing the list of active sessions for the user."""

    sessions: list[SessionInfo]
    total: int
    active_count: int


class UserResponse(BaseModel):
    """Public user information (no password or sensitive data)."""

    id: int
    username: str
    email: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}
