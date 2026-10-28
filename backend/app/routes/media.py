"""
Media Generation routes for Thunder AI.

POST   /api/media/generate-image  — Generate an image
POST   /api/media/generate-video  — Start video generation (async)
GET    /api/media/video/{task_id} — Poll video generation status
GET    /api/media/providers       — List available media providers
"""

import datetime
import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.models import MediaVideoTask, User
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf
from app.services.media_generation import GenerationResult, get_media_service
from app.services.rate_limiter import get_rate_limiter

router = APIRouter(prefix="/api/media", tags=["media"])

logger = logging.getLogger(__name__)

# Rate limiter key prefix
MEDIA_RATE_LIMIT_PREFIX = "media_gen"


# ── Schemas ─────────────────────────────────────────────────


class ImageGenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    provider: Optional[Literal["venice", "nexa", "agnes"]] = None
    model: Optional[str] = Field(default=None, max_length=100)
    width: int = Field(default=1024, ge=256, le=2048)
    height: int = Field(default=1024, ge=256, le=2048)


class VideoGenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    provider: Optional[Literal["venice", "nexa", "agnes"]] = None
    model: Optional[str] = Field(default=None, max_length=100)
    duration: Literal["3s", "5s", "10s"] = "5s"


class MediaGenerateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    success: bool
    url: Optional[str] = None
    task_id: Optional[str] = None
    error: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None


class VideoPollResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    success: bool
    url: Optional[str] = None
    task_id: Optional[str] = None
    error: Optional[str] = None
    provider: Optional[str] = None
    status: str  # "processing" | "completed" | "failed"


class ProviderInfo(BaseModel):
    name: str
    label: str
    has_api_key: bool
    default_image_model: str
    default_video_model: str
    available_image_models: list[str]
    available_video_models: list[str]


class ProvidersResponse(BaseModel):
    providers: list[ProviderInfo]
    primary: str
    fallback_order: list[str]


# ── Provider metadata ───────────────────────────────────────

PROVIDER_LABELS = {
    "venice": "Venice AI",
    "nexa": "NexaAPI",
    "agnes": "Agnes AI",
}

PROVIDER_IMAGE_MODELS = {
    "venice": ["flux-schnell", "flux-pro", "flux-2-max"],
    "nexa": ["flux-schnell", "flux-pro"],
    "agnes": ["agnes-image-2.1-flash"],
}

PROVIDER_VIDEO_MODELS = {
    "venice": ["seedance-2-0-text-to-video-basic", "seedance-2-5-text-to-video-basic"],
    "nexa": ["kling-video", "ltx-video"],
    "agnes": ["agnes-video-2.5-flash", "agnes-video-v2.0"],
}


# ── Helpers ─────────────────────────────────────────────────


def _check_media_enabled() -> None:
    """Raise 503 if media generation is disabled."""
    if not settings.enable_media_generation:
        raise HTTPException(
            status_code=503,
            detail="Media generation is disabled. Set ENABLE_MEDIA_GENERATION=true to enable.",
        )


def _check_media_rate_limit(user: User) -> None:
    """Check rate limit for media generation endpoints.

    Keyed on the authenticated user id only — the user is the stable identity
    here, and folding in a client-supplied (X-Forwarded-For) IP would let a
    caller rotate the header to bypass their quota.
    """
    limiter = get_rate_limiter()
    key = f"{MEDIA_RATE_LIMIT_PREFIX}:{user.id}"
    max_attempts = settings.media_rate_limit_per_minute

    if limiter.is_rate_limited(key, max_attempts, 60):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Maximum {max_attempts} requests per minute.",
        )


def _record_video_task(db: Session, task_id: str, user_id: int, provider: str) -> None:
    """Persist task ownership (authorises later polling) and opportunistically
    prune rows older than ~2x the video timeout — no scheduler in this app."""
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(
        seconds=2 * settings.media_video_timeout_s
    )
    db.query(MediaVideoTask).filter(MediaVideoTask.created_at < cutoff).delete(
        synchronize_session=False
    )
    # merge, not add: a provider that re-issues a task_id must not 500 on the PK.
    db.merge(
        MediaVideoTask(
            task_id=task_id,
            user_id=user_id,
            provider=provider or "",
            created_at=datetime.datetime.utcnow(),
        )
    )
    db.commit()


def _user_owns_task(db: Session, task_id: str, user_id: int) -> bool:
    """True only if this user created this task. Fail closed: an unknown
    task_id (never seen, or the row was pruned) returns False."""
    return (
        db.query(MediaVideoTask.task_id)
        .filter(MediaVideoTask.task_id == task_id, MediaVideoTask.user_id == user_id)
        .first()
        is not None
    )


def _result_to_response(result: GenerationResult) -> MediaGenerateResponse:
    """Convert GenerationResult to API response."""
    return MediaGenerateResponse(
        success=result.success,
        url=result.url,
        task_id=result.task_id,
        error=result.error,
        provider=result.provider,
        model=result.model,
    )


# ── Endpoints ───────────────────────────────────────────────


@router.get("/providers", response_model=ProvidersResponse)
def list_providers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List available media providers and their capabilities."""
    _check_media_enabled()

    service = get_media_service()
    available = service.get_available_providers()

    providers = []
    for name in ["venice", "nexa", "agnes"]:
        providers.append(
            ProviderInfo(
                name=name,
                label=PROVIDER_LABELS.get(name, name),
                has_api_key=name in available,
                default_image_model=PROVIDER_IMAGE_MODELS.get(name, [""])[0],
                default_video_model=PROVIDER_VIDEO_MODELS.get(name, [""])[0],
                available_image_models=PROVIDER_IMAGE_MODELS.get(name, []),
                available_video_models=PROVIDER_VIDEO_MODELS.get(name, []),
            )
        )

    return ProvidersResponse(
        providers=providers,
        primary=settings.media_provider_primary,
        fallback_order=["venice", "nexa", "agnes"],
    )


@router.post("/generate-image", response_model=MediaGenerateResponse)
async def generate_image(
    request: ImageGenerateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Generate an image using the specified or fallback provider."""
    _check_media_enabled()
    _check_media_rate_limit(current_user)

    service = get_media_service()
    result = await service.generate_image(
        prompt=request.prompt,
        provider=request.provider,
        model=request.model,
        width=request.width,
        height=request.height,
    )

    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or "Image generation failed")

    return _result_to_response(result)


@router.post("/generate-video", response_model=MediaGenerateResponse)
async def generate_video(
    request: VideoGenerateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Start video generation (async). Returns task_id for polling."""
    _check_media_enabled()
    _check_media_rate_limit(current_user)

    service = get_media_service()
    result = await service.generate_video(
        prompt=request.prompt,
        provider=request.provider,
        model=request.model,
        duration=request.duration,
    )

    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or "Video generation failed")

    if result.task_id:
        _record_video_task(db, result.task_id, current_user.id, result.provider or "")

    return _result_to_response(result)


@router.get("/video/{task_id}", response_model=VideoPollResponse)
async def poll_video(
    task_id: str,
    provider: str = Query(..., description="Provider that created the task"),
    wait: bool = Query(False, description="Wait for completion (long-poll)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Poll video generation status. Optionally wait for completion."""
    _check_media_enabled()
    _check_media_rate_limit(current_user)

    # Authorise: a task may only be polled by the user who created it.
    # Fail CLOSED — an unknown task_id (never seen, or its row pruned) is
    # refused, not proxied. 404 (not 403) so it stays indistinguishable from
    # a task that does not exist.
    if not _user_owns_task(db, task_id, current_user.id):
        raise HTTPException(status_code=404, detail="Unknown task")

    service = get_media_service()

    if wait:
        result = await service.wait_for_video(task_id, provider)
        if result.success and result.url:
            return VideoPollResponse(
                success=True,
                url=result.url,
                task_id=task_id,
                provider=provider,
                status="completed",
            )
        elif result.success and not result.url:
            # Timeout without completion
            return VideoPollResponse(
                success=False,
                task_id=task_id,
                provider=provider,
                status="processing",
                error=result.error or "Timeout waiting for video",
            )
        else:
            return VideoPollResponse(
                success=False,
                task_id=task_id,
                provider=provider,
                status="failed",
                error=result.error or "Video generation failed",
            )
    else:
        # Single poll
        result = await service.poll_video(task_id, provider)
        if result.success and result.url:
            return VideoPollResponse(
                success=True,
                url=result.url,
                task_id=task_id,
                provider=provider,
                status="completed",
            )
        elif result.success and not result.url:
            return VideoPollResponse(
                success=True,
                task_id=task_id,
                provider=provider,
                status="processing",
            )
        else:
            return VideoPollResponse(
                success=False,
                task_id=task_id,
                provider=provider,
                status="failed",
                error=result.error or "Video generation failed",
            )