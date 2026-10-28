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
    """Response body for successful login (Phase 7C, refresh token via cookie).

    Phase 7C: the raw refresh token is delivered exclusively via the
    HttpOnly `research_assistant_refresh_token` cookie; it is never
    returned in JSON responses.
    """

    access_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    """Deprecated Phase 7B request body for POST /api/auth/refresh.

    Phase 7C: the refresh token is read from the HttpOnly cookie instead.
    A JSON body token is ignored; this model is kept only for reference.
    """

    refresh_token: str = Field(..., description="The refresh token to exchange")


class RefreshResponse(BaseModel):
    """Response body for a successful token refresh (Phase 7C).

    Only the access token and metadata are returned; the new refresh
    token is set as an HttpOnly cookie on the response.
    """

    access_token: str
    token_type: str = "bearer"


class LogoutResponse(BaseModel):
    """Response body for logout."""

    detail: str = "Logged out successfully"


class SessionInfo(BaseModel):
    """
    Safe session metadata returned to the client.

    Never returns token_hash or any raw token data.
    `last_used_at` tracks the last time the session was created or rotated;
    falls back to created_at for legacy rows migrated before the column existed.
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
