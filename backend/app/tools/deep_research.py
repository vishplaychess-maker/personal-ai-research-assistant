"""
Deep Research helper for the AI Research Agent.

Extracted from ``web_search.py`` (Phase 4, cosmetic move — behavior unchanged).

``run_deep_research`` — the autonomous "Search -> Scrape -> Synthesize"
helper. Searches the web via ``search_web`` (top-N), then scrapes the top few
most relevant URLs with the ``web_scraper`` tool, and returns a single
formatted context block ready to be injected into the system prompt. Bounded
by ``deep_research_max_scrape`` so the loop can never spin forever.

Degrades gracefully: on any import/API error it returns an empty string and
never breaks the chat.
"""

import logging
from typing import List

from app.config import settings

logger = logging.getLogger(__name__)


def run_deep_research(query: str) -> str:
    """Autonomous Search -> Scrape -> Synthesize context builder.

    Searches the web with DuckDuckGo for *query*, then scrapes the top few
    results with the web_scraper tool, and returns a single formatted context
    block the LLM can synthesize into a report with citations.

    Bounded: at most ``deep_research_max_scrape`` pages are scraped, so the
    search/scrape loop can never run away. Returns an empty string when web
    search is unavailable or nothing relevant is found (graceful no-op).
    """
    query = (query or "").strip()
    if not query or not settings.enable_deep_research:
        return ""

    from app.tools.web_search import search_web

    results = search_web(query)
    if not results:
        return ""

    from app.tools.web_scraper import web_scraper

    scrape_limit = settings.deep_research_max_scrape
    parts: List[str] = [f"=== Deep Research Context for '{query}' ==="]
    for r in results[:scrape_limit]:
        url = r["url"]
        title = r.get("title") or url
        try:
            content = web_scraper.invoke({"url": url})
            parts.append(
                f"--- Source: {title} ({url}) ---\n{content}\n--- End Source ---"
            )
        except Exception as exc:  # noqa: BLE001 — one bad page must not abort research
            logger.warning("deep_research scrape failed for %s: %s", url, exc)
            parts.append(
                f"--- Source: {title} ({url}) ---\nError scraping this page: {exc}\n"
                f"--- End Source ---"
            )
    parts.append("=== End of Deep Research Context ===")
    return "\n\n".join(parts)
