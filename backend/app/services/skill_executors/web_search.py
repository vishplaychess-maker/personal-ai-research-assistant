"""Web Search executor (Tavily).

Independently callable: the Telegram bot's intent router uses it today, and any
other service can import :func:`execute_web_search` without duplicating the
provider details.

The API key comes from ``settings.tavily_api_key`` (env ``TAVILY_API_KEY``).
It is never logged, and provider errors are re-raised as typed exceptions whose
messages are safe to show to end users.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 3

# Tavily raises provider-specific exception classes. Classifying by name keeps
# this module importable when the optional dependency is absent and avoids
# coupling to a particular tavily-python release's export list.
_RATE_LIMIT_ERRORS = {
    "usagelimitexceedederror",
    "tavilykeylesslimiterror",
    "ratelimiterror",
}
_CONFIG_ERRORS = {
    "invalidapikeyerror",
    "missingapikeyerror",
    "keylessunsupportedendpointerror",
}


class SkillExecutionError(RuntimeError):
    """Base class for executor failures with a user-safe message."""


class WebSearchNotConfigured(SkillExecutionError):
    """No usable Tavily key (or the provider rejected it)."""


class WebSearchRateLimited(SkillExecutionError):
    """The Tavily quota or per-window rate limit was exhausted."""


class WebSearchFailed(SkillExecutionError):
    """Any other provider/transport failure."""


def _configured_api_key() -> str:
    from app.config import settings

    return (getattr(settings, "tavily_api_key", "") or "").strip()


def _normalise(results: Iterable[Any], limit: int) -> list[dict[str, str]]:
    """Keep only usable results, capped at ``limit``, in a stable shape."""
    cleaned: list[dict[str, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        cleaned.append(
            {
                "title": str(item.get("title") or url).strip(),
                "url": url,
                "content": str(item.get("content") or "").strip(),
            }
        )
        if len(cleaned) >= limit:
            break
    return cleaned


def execute_web_search(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    client: Optional[Any] = None,
) -> list[dict[str, str]]:
    """Search the web and return ``[{title, url, content}]`` (at most ``limit``).

    ``client`` is an injection seam for tests: any object with
    ``.search(query=..., max_results=...)`` returning a Tavily-shaped dict.
    """
    text = (query or "").strip()
    if not text:
        return []

    capped_limit = max(1, int(limit))
    if client is None:
        api_key = _configured_api_key()
        if not api_key:
            raise WebSearchNotConfigured("TAVILY_API_KEY is not configured")
        try:
            from tavily import TavilyClient
        except ImportError as exc:  # pragma: no cover - declared dependency
            raise WebSearchNotConfigured("tavily-python is not installed") from exc
        client = TavilyClient(api_key=api_key)

    try:
        payload = client.search(
            query=text,
            max_results=capped_limit,
            search_depth="basic",
        )
    except Exception as exc:  # noqa: BLE001 - classified below
        name = type(exc).__name__.lower()
        detail = str(exc).lower()
        if name in _RATE_LIMIT_ERRORS or "429" in detail or "rate limit" in detail:
            raise WebSearchRateLimited("web search quota exhausted") from exc
        if name in _CONFIG_ERRORS:
            raise WebSearchNotConfigured("Tavily rejected the API key") from exc
        logger.warning("web search failed (%s)", type(exc).__name__)
        raise WebSearchFailed("web search failed") from exc

    results = payload.get("results") if isinstance(payload, dict) else None
    return _normalise(results or [], capped_limit)
