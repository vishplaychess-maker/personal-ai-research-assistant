"""
Web Scraper Tool for the AI Research Agent.

Uses Playwright to launch a headless Chromium browser, navigate to a URL,
wait for the page to load, and extract the main text content.

This tool is designed to be invoked by the LLM agent when a user asks
about the content of a specific web page. It can also be called directly.

Usage (as a LangChain-style tool):
    from app.tools.web_scraper import web_scraper

    result = web_scraper.invoke({"url": "https://docs.pydantic.dev/latest/"})

Usage (standalone):
    from app.tools.web_scraper import scrape_url
    content = scrape_url("https://news.ycombinator.com")
"""

import asyncio
import concurrent.futures
import logging
import re
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────

# Approximate token limit: ~4 chars per token → 16 000 chars ≈ 4 000 tokens
MAX_CONTENT_CHARS = 16_000

NAVIGATION_TIMEOUT_MS = 30_000
WAIT_TIMEOUT_MS = 10_000

_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ── Core scraping logic (async) ───────────────────────────


async def _scrape_async(url: str) -> str:
    """Launch headless Chromium, navigate to *url*, and return visible text."""
    from playwright.async_api import async_playwright

    logger.info("Web scraper: navigating to %s", url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        context = await browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()

        try:
            response = await page.goto(
                url,
                wait_until="networkidle",
                timeout=NAVIGATION_TIMEOUT_MS,
            )

            if response and response.status >= 400:
                return (
                    f"[Web Scraper] HTTP {response.status} {response.status_text} "
                    f"when fetching {url}"
                )

            # Give JS-rendered pages a moment to stabilise
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=WAIT_TIMEOUT_MS
                )
            except Exception:
                logger.debug(
                    "networkidle timeout for %s — using current content", url
                )

            content = await page.inner_text("body")

            # Collapse whitespace
            content = "\n".join(
                line.strip() for line in content.splitlines() if line.strip()
            )

            # Truncate to ~4 000 tokens
            if len(content) > MAX_CONTENT_CHARS:
                content = (
                    content[:MAX_CONTENT_CHARS]
                    + f"\n\n[Content truncated at {MAX_CONTENT_CHARS} chars]"
                )

            logger.info(
                "Web scraper: extracted %d chars from %s", len(content), url
            )
            return content

        finally:
            await context.close()
            await browser.close()


def _scrape_sync(url: str) -> str:
    """Run the async scraper from either a sync OR an already-async context.

    ``asyncio.run()`` raises if a loop is already running (e.g. the async SSE
    streaming route calls ``prepare_chat_context`` synchronously on the loop
    thread). Detect that case and run the coroutine to completion in a
    one-shot worker thread that owns its own event loop.
    ``Future.result()`` re-raises any exception from the thread, so callers'
    existing try/except still works.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_scrape_async(url))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_scrape_async(url))).result()


# ── LangChain tool definition ─────────────────────────────


@tool
def web_scraper(url: str) -> str:
    """Scrape the text content of a web page using a headless browser.

    Use this tool when the user asks you to read, summarise, or extract
    information from a specific URL.  The tool launches Chromium, navigates
    to the page, waits for it to load, and returns the visible text content.

    ADVISOR BEHAVIOUR:
    - Before scraping, explain what you will extract and warn if the page
      may be very large (content is truncated to ~4000 tokens).
    - After scraping, suggest next steps: save as note, search related
      articles, extract specific sections, etc.
    - If the page fails to load, suggest alternatives (cached version,
      different URL, manual check).

    Args:
        url: The full HTTP(S) URL to scrape (e.g. "https://example.com").

    Returns:
        The visible text content of the page, or an error message.
    """
    if not url or not url.startswith(("http://", "https://")):
        return (
            f"[Web Scraper] Error: invalid URL '{url}'. "
            "Must start with http:// or https://."
        )

    try:
        return _scrape_sync(url)
    except Exception as exc:
        error_msg = f"[Web Scraper] Error scraping {url}: {type(exc).__name__}: {exc}"
        logger.error(error_msg, exc_info=True)
        return error_msg


# ── Public helper (non-tool, for direct calls) ────────────


def scrape_url(url: str) -> str:
    """Scrape a URL and return the text content.

    Convenience wrapper that can be called directly without going through
    the LangChain tool interface.
    """
    return web_scraper.invoke({"url": url})


# ── URL detection helper ──────────────────────────────────

URL_PATTERN = re.compile(r"https?://[\w\-]+(?:\.[\w\-]+)+[/\w\-.,;:!?~#@\$&()*+=%\[\]'""]*")


def extract_urls(text: str) -> list[str]:
    """Find and deduplicate URLs in *text*, stripping trailing punctuation."""
    raw = URL_PATTERN.findall(text)
    seen: set[str] = set()
    unique: list[str] = []
    for match in raw:
        # findall returns strings when there are no capturing groups
        url = match if isinstance(match, str) else match[0]
        # Strip trailing punctuation that the regex may have grabbed
        url_clean = url.rstrip(".,;:!?)\"'")
        if url_clean not in seen:
            seen.add(url_clean)
            unique.append(url_clean)
    return unique


if __name__ == "__main__":
    # Runnable check for the sync/async bridge — stubs the real scraper so it
    # needs no browser or network. Run: `python -m app.tools.web_scraper`
    async def _fake(url: str) -> str:
        await asyncio.sleep(0)
        if url == "boom":
            raise ValueError("scrape failed")
        return f"scraped:{url}"

    _scrape_async = _fake  # _scrape_sync reads this from module globals

    # 1. no loop running → asyncio.run branch
    assert _scrape_sync("https://a") == "scraped:https://a"

    # 2. called from inside a running loop → worker-thread branch
    async def _from_loop(arg: str) -> str:
        return await asyncio.get_running_loop().run_in_executor(
            None, _scrape_sync, arg
        )

    assert asyncio.run(_from_loop("https://b")) == "scraped:https://b"

    # 3. errors propagate out of the worker thread
    try:
        asyncio.run(_from_loop("boom"))
        raise AssertionError("expected ValueError to propagate")
    except ValueError as exc:
        assert str(exc) == "scrape failed"

    print("web_scraper sync/async bridge self-check OK")
