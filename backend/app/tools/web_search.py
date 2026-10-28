"""
web_search tool for the AI Research Agent.

Uses DuckDuckGo (via the ``duckduckgo-search`` package) for real-time web
search. 100 % free — no API key required.

Two pieces:

1. ``search_web`` — plain helper returning top-N results (URLs, titles,
   snippets) as a list of dicts.

2. ``web_search`` — a LangChain-style tool wrapping it for the LLM.

The autonomous deep-research helper lives in ``app.tools.deep_research``
(extracted in Phase 4, behavior unchanged) and reuses ``search_web`` here.

Both degrade gracefully: on any import/API error the search returns an empty
list and never breaks the chat.
"""

import logging
from typing import Dict, List, Optional

from langchain_core.tools import tool

from app.config import settings

logger = logging.getLogger(__name__)

# DuckDuckGo backends to try, most reliable first. "auto" rotates through the
# native html/lite/bing endpoints; "html" and "lite" are the two primary
# DuckDuckGo pages. Trying a couple with a short backoff materially improves the
# hit rate against DuckDuckGo's aggressive rate limiting on shared/cloud IPs.
_DDG_BACKENDS = ("auto", "html", "lite")


def search_web(
    query: str,
    max_results: Optional[int] = None,
) -> List[Dict[str, str]]:
    """Run a DuckDuckGo web search and return a list of result dicts.

    Each result dict has ``title``, ``url`` and ``snippet`` keys. Returns an
    empty list if the search fails or the library is unavailable. Does a small
    bounded retry across backends so a single rate-limit blip does not kill it.
    """
    import time

    limit = max_results or settings.deep_research_max_results

    try:
        from duckduckgo_search import DDGS
    except ImportError:
        logger.warning("duckduckgo-search package not installed")
        return []

    results = None

    for backend in _DDG_BACKENDS:
        try:
            results = DDGS().text(keywords=query, max_results=limit, backend=backend)
        except Exception as exc:  # noqa: BLE001 — search must never break chat
            logger.warning("DuckDuckGo search (%s) failed (non-fatal): %s", backend, exc)
            results = None
        if results:
            break
        # Short backoff between backend attempts to appease rate limiting.
        time.sleep(0.5)

    if not results:
        logger.warning("DuckDuckGo search returned no results")
        return []

    parsed: List[Dict[str, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = (item.get("href") or "").strip()
        title = (item.get("title") or "").strip()
        snippet = (item.get("body") or "").strip()
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
            "[Web Search] No results found or web search is unavailable."
        )

    lines = [f"=== Web Search Result for '{query}' ==="]
    for i, r in enumerate(results, start=1):
        lines.append(
            f"{i}. {r['title']}\n   URL: {r['url']}\n   Snippet: {r['snippet']}"
        )
    lines.append("=== End of Web Search Result ===")
    return "\n".join(lines)

