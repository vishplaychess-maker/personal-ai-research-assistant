"""Web Scraper executor (crawl4ai) — the "Deep Dive" capability.

Independently callable: any HTTP route or service can import
:func:`scrape_url` without knowing crawl4ai's configuration surface.

crawl4ai renders the page in a headless Chromium (so JS-built pages work),
strips nav/footer/script/aside debris through ``excluded_tags``, and returns
the page as clean Markdown. Errors are never raised: the result dict carries
``success``/``error`` so callers can pass them straight to the client.

The timeout comes from ``settings.crawl4ai_timeout`` (env
``CRAWL4AI_TIMEOUT``, default 30s) and is enforced twice:

1. as crawl4ai's own ``page_timeout`` (ms), covering page operations, and
2. as an ``asyncio.wait_for`` guard around the whole ``arun`` call, so even a
   wedge in crawl4ai/browser startup cannot hang the process.

``crawler_factory`` is an injection seam for tests (mirrors Tavily's
``client=...`` seam in ``web_search.py``): any async context manager returning
an object with ``await arun(url=..., config=...)``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30

#: HTTP statuses the route-level error classifies by name for readability.
_HTTP_REASONS = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    410: "Gone",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}

#: Tags dropped before Markdown generation so the LLM sees body prose only.
_EXCLUDED_TAGS = ["nav", "footer", "header", "aside", "script", "style", "noscript"]


def _validate_url(url: str) -> Optional[str]:
    """Return a user-safe reason string when *url* is unusable, else ``None``."""
    if not url:
        return "A URL is required"
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"invalid URL {url!r}"
    if parts.scheme not in ("http", "https"):
        return f"invalid URL {url!r}: must be an http:// or https:// URL"
    if not parts.netloc:
        return f"invalid URL {url!r}: must include a hostname (e.g. https://example.com)"
    return None


def _classify_error(exc: BaseException) -> str:
    """Map a provider/transport exception to a short, user-safe message."""
    name = type(exc).__name__
    text = str(exc)
    if isinstance(exc, asyncio.TimeoutError) or "timed out" in text.lower():
        return "the page took too long to load"
    if "net::ERR_HTTP_403" in text or "403" in name or "403 " in text[:4]:
        return "HTTP 403 Forbidden: the server refused access"
    if "net::ERR_NAME_NOT_RESOLVED" in text:
        return "hostname could not be resolved"
    if "net::ERR_CONNECTION_REFUSED" in text:
        return "connection refused by the target server"
    if "net::ERR_HTTP_RESPONSE_CODE_FAILURE" in text:
        # Playwright aborts the navigation outright on a 4xx/5xx body (e.g.
        # 403 Forbidden), so no status code ever reaches CrawlResult.
        return "the server responded with an HTTP error status (e.g. 403 Forbidden)"
    logger.warning("web scrape failed (%s)", name)
    return "the page could not be fetched"


def _extract_markdown(result: Any) -> str:
    """Normalise crawl4ai's result into raw Markdown text.

    ``CrawlResult.markdown`` is a plain string; future versions may hand back
    a ``MarkdownGenerationResult`` whose ``raw_markdown`` is the same content.
    """
    markdown = getattr(result, "markdown", None)
    if isinstance(markdown, str):
        return markdown
    raw = getattr(markdown, "raw_markdown", None)
    if isinstance(raw, str):
        return raw
    return ""


def _http_reason(status: int) -> str:
    return _HTTP_REASONS.get(status, f"HTTP {status} error")


async def scrape_url(
    url: str,
    *,
    timeout_s: Optional[int] = None,
    crawler_factory: Optional[Callable[[], Any]] = None,
) -> dict:
    """Fetch *url* with crawl4ai and wrap the outcome for API consumers.

    Returns ``{"success": bool, "url": str, "markdown": str,
    "error": str | None}``. A non-2xx HTTP response (e.g. 403 Forbidden) is a
    ``success=False`` result, never a raised exception.
    """
    url = (url or "").strip()
    validation_error = _validate_url(url)
    if validation_error is not None:
        return {"success": False, "url": url, "markdown": "", "error": validation_error}

    if timeout_s is None:
        from app.config import settings

        timeout_s = int(
            getattr(settings, "crawl4ai_timeout", DEFAULT_TIMEOUT_S)
            or DEFAULT_TIMEOUT_S
        )

    try:
        # Imported inside the call so the web app runs without crawl4ai unless
        # a scrape is actually executed (same pattern as tavily in web_search).
        from crawl4ai import BrowserConfig, CacheMode, CrawlerRunConfig
    except ImportError:
        logger.error("crawl4ai is not installed; web_scraper executor disabled")
        return {
            "success": False,
            "url": url,
            "markdown": "",
            "error": "crawl4ai is not installed",
        }

    # ``verbose=False`` keeps crawl4ai's colourful banner prints out of the
    # server console: they crash under a non-UTF8 Windows console (cp1252
    # cannot encode the box-drawing glyphs) and would otherwise fail every
    # scrape on such a deployment.
    browser_config = BrowserConfig(headless=True, verbose=False)
    run_config = CrawlerRunConfig(
        # ≥1 guard mirrors the Settings validator; never a 0ms page timeout.
        page_timeout=max(1, timeout_s) * 1000,
        cache_mode=CacheMode.BYPASS,  # always fetch fresh page content
        excluded_tags=_EXCLUDED_TAGS,
    )

    if crawler_factory is None:

        def crawler_factory() -> Any:  # noqa: F811 - test seam replacement
            from crawl4ai import AsyncWebCrawler

            return AsyncWebCrawler(config=browser_config)

    try:
        async with crawler_factory() as crawler:
            result = await asyncio.wait_for(
                crawler.arun(url=url, config=run_config),
                timeout=max(1, timeout_s),
            )
    except asyncio.TimeoutError:
        logger.warning("web scrape timed out after %ss: %s", timeout_s, url)
        return {
            "success": False,
            "url": url,
            "markdown": "",
            "error": f"the page took longer than {timeout_s} seconds to load",
        }
    except Exception as exc:  # noqa: BLE001 - classified into a user-safe message
        return {"success": False, "url": url, "markdown": "", "error": _classify_error(exc)}

    if not getattr(result, "success", False):
        detail = (getattr(result, "error_message", "") or "").strip()
        logger.warning("web scrape failed for %s: %s", url, detail or "no error detail")
        return {
            "success": False,
            "url": url,
            "markdown": "",
            "error": _classify_error(RuntimeError(detail or "crawl failed")),
        }

    # Playwright records navigation responses without raising: a 403 lands in
    # status_code while success stays True. Classify it here, fail closed.
    status = getattr(result, "status_code", None) or 0
    if status >= 400:
        return {
            "success": False,
            "url": url,
            "markdown": "",
            "error": f"HTTP {status} {_http_reason(status)}",
        }

    markdown = _extract_markdown(result).strip()
    if not markdown:
        return {
            "success": False,
            "url": url,
            "markdown": "",
            "error": "the page loaded but no readable content was extracted",
        }

    logger.info("web scrape extracted %d chars from %s", len(markdown), url)
    return {"success": True, "url": url, "markdown": markdown, "error": None}
