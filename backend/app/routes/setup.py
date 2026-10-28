"""First-run setup endpoints (Phase 1 - .exe packaging).

GET  /api/setup/status     -> {"configured": bool}   (public: one boolean)
POST /api/setup/configure  -> {"configured": true}

The POST body is {"gemini_api_key": "..."}. The key is written to the portable
config file and is NEVER echoed back or logged.

Access rules for /configure (defense in depth; main.py enforces the same
before routing, and this route repeats it so the router is safe on its own):

* any forwarding header (X-Forwarded-For / Forwarded / ...) -> 403. A tunnel
  connects from loopback, so the peer address alone cannot prove locality.
* a non-loopback peer -> 403.
* once configured, a valid session (Bearer token or live refresh cookie) is
  required even from loopback -- first-run stays open on pure loopback only.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services import access_guard, setup_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])

#: Upper bound on an accepted key so a hostile body cannot balloon the config.
MAX_API_KEY_CHARS = 4096


class SetupStatusResponse(BaseModel):
    """Bounded response: a single boolean, never any key material."""

    configured: bool


class SetupConfigureRequest(BaseModel):
    """Request body for first-run configuration."""

    # Plain str on purpose: a pydantic max_length violation would return a 422
    # whose body echoes the submitted value. Length is checked below instead.
    gemini_api_key: str = Field(default="")


@router.get("/status", response_model=SetupStatusResponse)
async def setup_status() -> SetupStatusResponse:
    """Report whether a usable Gemini key is already available."""
    return SetupStatusResponse(configured=setup_service.is_configured())


def _guard_configure(request: Request) -> None:
    """Reject proxied/non-local callers and post-configure anonymous writes."""
    if access_guard.is_forwarded(request) or not access_guard.is_loopback_client(request):
        raise HTTPException(
            status_code=403,
            detail="first-run setup is only available from localhost",
        )
    if setup_service.is_configured() and not access_guard.has_valid_session(request):
        raise HTTPException(
            status_code=401,
            detail="authentication required to reconfigure setup",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.post("/configure", response_model=SetupStatusResponse)
async def setup_configure(
    payload: SetupConfigureRequest,
    request: Request,
) -> SetupStatusResponse:
    """Persist the user-supplied Gemini key to the portable config file.

    The key is validated as non-empty, written atomically, and never returned
    in the response or written to a log line.
    """
    _guard_configure(request)
    key = (payload.gemini_api_key or "").strip()
    if not key:
        raise HTTPException(
            status_code=400,
            detail="gemini_api_key must be a non-empty string",
        )
    if len(key) > MAX_API_KEY_CHARS:
        raise HTTPException(
            status_code=400,
            detail="gemini_api_key is too long",
        )
    if not setup_service.save_gemini_api_key(key):
        raise HTTPException(
            status_code=500,
            detail="could not persist configuration",
        )
    logger.info("setup: gemini_api_key configured (value not logged)")
    return SetupStatusResponse(configured=True)


__all__ = ["router"]
