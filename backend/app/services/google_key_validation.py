"""Real validation for Google AI (Generative Language) API keys.

Google API keys are issued in several formats: AI Studio keys start with
"AIza", Firebase / Google Cloud keys start with "AQ.", and other valid
formats exist. A prefix check therefore cannot decide whether a key works,
and the previous Google entry in the provider PREFIXES map ("AIza") wrongly
rejected working "AQ." keys.

The only reliable check is a live call to the Generative Language models
list endpoint:

    GET https://generativelanguage.googleapis.com/v1beta/models

The key is sent in the x-goog-api-key header rather than as a ?key= query
parameter: httpx logs full request URLs at INFO level, so a query-string key
would leak into application logs. The header is Google documented equivalent
for the same endpoint, and keeps the key out of every URL/log line.

Privacy (HARD invariant): the key is never logged, and never appears in a
returned message. Fail-closed: every failure maps to a bounded message.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

GOOGLE_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
VERIFY_TIMEOUT_S = 5.0

INVALID_KEY_MESSAGE = "Invalid Google AI API key."
UNVERIFIED_KEY_MESSAGE = (
    "Could not verify API key. Please check your connection and try again."
)


def _request_models(api_key: str) -> httpx.Response:
    """One bounded GET to the models list. The key is a header, never a URL."""
    return httpx.get(
        GOOGLE_MODELS_URL,
        headers={"x-goog-api-key": api_key},
        timeout=VERIFY_TIMEOUT_S,
    )


def verify_google_api_key(api_key: str) -> Tuple[bool, Optional[str]]:
    """Validate a Google AI key against the live models endpoint.

    Returns ``(True, None)`` when the key is accepted and at least one model
    is listed, otherwise ``(False, bounded_message)``. An empty key means "no
    key supplied" and passes through unchanged (existing behaviour).

    Fail-closed: a timeout, transport error or any unexpected failure returns
    the connection message rather than raising.
    """
    key = (api_key or "").strip()
    if not key:
        return True, None

    try:
        response = _request_models(key)
    except httpx.TimeoutException:
        logger.warning("google key verification timed out")
        return False, UNVERIFIED_KEY_MESSAGE
    except httpx.TransportError:
        logger.warning("google key verification transport error")
        return False, UNVERIFIED_KEY_MESSAGE
    except Exception as exc:  # noqa: BLE001 - fail closed, never leak the key
        logger.warning("google key verification failed (%s)", type(exc).__name__)
        return False, UNVERIFIED_KEY_MESSAGE

    if response.status_code == 200:
        try:
            data: Any = response.json()
        except ValueError:
            return False, INVALID_KEY_MESSAGE
        models = data.get("models") if isinstance(data, dict) else None
        if isinstance(models, list) and models:
            return True, None
        return False, INVALID_KEY_MESSAGE

    if response.status_code in (400, 401, 403):
        return False, INVALID_KEY_MESSAGE

    logger.warning(
        "google key verification returned unexpected HTTP %s",
        response.status_code,
    )
    return False, UNVERIFIED_KEY_MESSAGE


__all__ = [
    "GOOGLE_MODELS_URL",
    "VERIFY_TIMEOUT_S",
    "INVALID_KEY_MESSAGE",
    "UNVERIFIED_KEY_MESSAGE",
    "verify_google_api_key",
]
