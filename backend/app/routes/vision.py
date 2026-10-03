"""Local Vision route (ZERO API cost) — describe an image with a LOCAL model.

POST /api/vision/describe takes a multipart image and returns the bounded
local_vision_service.VisionResult as JSON:
{"status", "description", "notice", "context"}.

The image is validated on-box (magic-byte type + size) and described entirely
locally via the Ollama model in settings.local_vision_model (default
"moondream"). NO paid API is ever called.

Fail-closed: every failure is a bounded status + notice, never a 500. Image
bytes and the description are never logged (sha256 + byte count only).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel

from app.config import settings
from app.models.models import User
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf

logger = logging.getLogger(__name__)

router = APIRouter(tags=["vision"])

#: Read at most the configured cap plus this slack before handing the bytes to
#: the service, so an oversize upload is detected without buffering it whole.
_READ_SLACK = 64


class VisionDescribeResponse(BaseModel):
    status: str
    description: Optional[str] = None
    notice: str = ""
    #: The exact untrusted line a caller may prepend to model context, or null.
    context: Optional[str] = None


@router.post("/api/vision/describe", response_model=VisionDescribeResponse)
def describe_image_route(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> VisionDescribeResponse:
    """Describe an uploaded image locally. Never raises on a vision failure."""
    from app.services.local_vision_service import (
        FAILED_NOTICE, STATUS_FAILED, context_line, describe_image,
    )

    try:
        cap = max(1, int(getattr(settings, "local_vision_max_bytes", 5242880)))
        raw = file.file.read(cap + _READ_SLACK + 1) or b""
        result = describe_image(raw)
    except Exception as exc:  # noqa: BLE001 - a vision failure is never a 500
        logger.warning("local_vision: describe route failed (%s)", type(exc).__name__)
        return VisionDescribeResponse(status=STATUS_FAILED, notice=FAILED_NOTICE)

    return VisionDescribeResponse(
        status=result.status,
        description=result.description,
        notice=result.notice,
        context=context_line(result),
    )


__all__ = ["router", "VisionDescribeResponse"]
