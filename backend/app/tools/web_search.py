"""
web_search tool + Deep Research helper for the AI Research Agent.

Uses Tavily (https://tavily.com/) to run real-time web searches. Lets the
agent answer questions about current events, latest research, or anything it
does not already know — without requiring the user to paste a URL.

Two pieces:

1. ``web_search`` — a LangChain-style tool. Input is a ``query`` string;
   returns the top-N results (URLs, titles, snippets) so the LLM knows what
   is available on the web.

2. ``run_deep_research`` — the autonomous "Search -> Scrape -> Synthesize"
   helper. Searches with Tavily (top-N), then scrapes the top few most
   relevant URLs with the ``web_scraper`` tool, and returns a single formatted
   context block ready to be injected into the system prompt. Bounded by
   ``deep_research_max_scrape`` so the loop can never spin forever.

Both degrade gracefully: without a TAVILY_API_KEY (or on any API error) the
search returns a clear no-op message and never breaks the chat.
"""

import logging
from typing import Dict, List, Optional

from langchain_core.tools import tool

from app.config import settings

logger = logging.getLogger(__name__)


def _get_tavily_client():
    """Return a configured TavilyClient, or None if no API key is set."""
    if not settings.tavily_api_key:
        return None
    try:
        from tavily import TavilyClient

        return TavilyClient(api_key=settings.tavily_api_key)
    except Exception as exc:  # noqa: BLE001 — import/setup must never break chat
        logger.warning("Tavily client init failed (non-fatal): %s", exc)
        return None


def search_web(
    query: str,
    max_results: Optional[int] = None,
) -> List[Dict[str, str]]:
    """Run a Tavily web search and return a list of result dicts.

    Each result dict has ``title``, ``url`` and ``snippet`` keys. Returns an
    empty list if Tavily is unconfigured or the search fails.
    """
    limit = max_results or settings.deep_research_max_results
    client = _get_tavily_client()
    if client is None:
        logger.info("web_search skipped: TAVILY_API_KEY not configured")
        return []

    try:
        response = client.search(query=query, max_results=limit)
    except Exception as exc:  # noqa: BLE001 — search must never break chat
        logger.warning("Tavily search failed (non-fatal): %s", exc)
        return []

    results = response.get("results", []) if isinstance(response, dict) else []
    parsed: List[Dict[str, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        title = (item.get("title") or "").strip()
        # Tavily returns "content" (a snippet) and/or "raw_content".
        snippet = (item.get("content") or item.get("raw_content") or "").strip()
        if url:
            parsed.append({"url": url, "title": title, "snippet": snippet[:500]})
    return parsed


@tool
def web_search(query: str) -> str:
    """Search the live web and return the top results.

    Use this tool when the user asks about current events, the latest
    research, real-time data, or anything you do not already know. Returns
    the top results (titles, URLs, and short snippets) so you can pick which
    pages to read with the web_scraper tool.

    Args:
        query: The search query to look up on the web (e.g.
            "latest advancements in RAG systems 2025").

    Returns:
        A human-readable list of the top search results, or a message that
        web search is unavailable.
    """
    results = search_web(query)
    if not results:
        return (
            "[Web Search] No results found or web search is unavailable "
            "(check that TAVILY_API_KEY is configured)."
        )

    lines = [f"=== Web Search Result for '{query}' ==="]
    for i, r in enumerate(results, start=1):
        lines.append(
            f"{i}. {r['title']}\n   URL: {r['url']}\n   Snippet: {r['snippet']}"
        )
    lines.append("=== End of Web Search Result ===")
    return "\n".join(lines)


def run_deep_research(query: str) -> str:
    """Autonomous Search -> Scrape -> Synthesize context builder.

    Searches the web with Tavily for *query*, then scrapes the top few
    results with the web_scraper tool, and returns a single formatted context
    block the LLM can synthesize into a report with citations.

    Bounded: at most ``deep_research_max_scrape`` pages are scraped, so the
    search/scrape loop can never run away. Returns an empty string when web
    search is unavailable or nothing relevant is found (graceful no-op).
    """
    query = (query or "").strip()
    if not query or not settings.enable_deep_research:
        return ""

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
