"""Built-in browser support routes (Phase 1 / V1).

GET /api/browser/embeddable?url=...  — header-only probe for framability.

Why this exists: when a site refuses embedding (``X-Frame-Options`` or CSP
``frame-ancestors``) the browser renders an opaque cross-origin error page
inside the iframe that JavaScript cannot observe, so the frontend cannot
distinguish "blocked" from "loading". Asking the backend to fetch only the
response HEADERS (no body) settles the question and lets the panel show its
friendly fallback screen instead of a silent blank frame.

Security: the same strict scheme allow-list as the panel's URL validator
(http/https only — the redirect chain is followed, but httpx never executes
or evaluates URLs), plus a hard timeout. No body is ever downloaded.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.models.models import User
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/api/browser", tags=["browser"])

_PROBE_TIMEOUT_S = 6.0


class EmbeddabilityResponse(BaseModel):
    embeddable: bool
    reason: Optional[str] = None


def _classify_headers(headers: httpx.Headers) -> Optional[str]:
    """Return a refusal reason string, or ``None`` when framing is allowed.

    Clickjacking defences that forbid embedding for us:
    * ``X-Frame-Options: DENY`` (no framing at all)
    * ``X-Frame-Options: SAMEORIGIN`` (only their own origin frames them;
      our app origin always differs from the target site)
    * CSP ``frame-ancestors 'none'`` or ``'self'`` (same reasoning)

    Any other/absent value (including explicit multi-origin allow lists or a
    missing header) is treated as allowed — the panel still shows a hint for
    rare mismatches.
    """
    xfo = headers.get("x-frame-options", "").strip().lower()
    if xfo in ("deny", "sameorigin"):
        return (
            "The site sends X-Frame-Options: "
            + headers.get("x-frame-options", "").strip()
            + "."
        )
    csp = headers.get("content-security-policy", "")
    for directive in csp.split(";"):
        directive = directive.strip()
        if directive.lower().startswith("frame-ancestors"):
            values = directive.split(None, 1)[1] if len(directive.split(None, 1)) > 1 else ""
            tokens = values.lower().split()
            if "'none'" in tokens or "'self'" in tokens:
                return "The site's CSP frame-ancestors rule forbids embedding."
            if not tokens:  # frame-ancestors with no value = fall back to XFO; none present
                pass
            break
    return None


@router.get("/embeddable", response_model=EmbeddabilityResponse)
def check_embeddable(
    url: str,
    current_user: User = Depends(get_current_user),
) -> EmbeddabilityResponse:
    """Header-only framability probe for the built-in browser panel."""
    trimmed = (url or "").strip()
    parts = urlsplit(trimmed)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        # Same verdict contract as the panel: non-http(s) = unusable.
        return EmbeddabilityResponse(
            embeddable=False,
            reason="Only http:// and https:// websites can be opened here.",
        )

    try:
        # Read the status/headers, then close WITHOUT pulling the body.
        with httpx.Client(follow_redirects=True, timeout=_PROBE_TIMEOUT_S) as client:
            with client.stream("GET", trimmed) as response:
                status = response.status_code
                headers = response.headers
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="The website took too long to respond.")
    except httpx.HTTPError:
        # Unreachable target → the panel's fallback screen explains it.
        return EmbeddabilityResponse(
            embeddable=False, reason="The website could not be reached."
        )

    refusal = _classify_headers(headers)
    if refusal:
        return EmbeddabilityResponse(embeddable=False, reason=refusal)

    content_type = headers.get("content-type", "").lower()
    if status >= 400:
        return EmbeddabilityResponse(
            embeddable=False, reason=f"The website answered with HTTP {status}."
        )
    if content_type and "text/html" not in content_type and "application/xhtml" not in content_type:
        return EmbeddabilityResponse(
            embeddable=False, reason="That address is not a web page (it looks like a file)."
        )
    return EmbeddabilityResponse(embeddable=True)
