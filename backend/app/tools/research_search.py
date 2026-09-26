"""Durable Deep Research — search adapter (Phase 1B.2R MP-3C3D4B).

Wraps the existing free, local, no-key DuckDuckGo search
(:func:`app.tools.web_search.search_web`, unchanged) behind an injectable
adapter so the acquisition orchestrator never depends on a concrete search
implementation directly (tests use a fake). Adds deterministic URL
normalization/deduplication and rank preservation — legacy `search_web`
does neither. Legacy behavior/signature is NOT modified; this module only
wraps it.

No paid search API, no hidden key, ever.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol
from urllib.parse import urlsplit, urlunsplit

from app.run_engine.errors import RunEngineError

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class SearchError(RunEngineError):
    """Bounded, non-secret search failure — never a raw exception string."""


@dataclass(frozen=True)
class SearchResultCandidate:
    url: str          # normalized: lowercase scheme+host, no fragment, no default port
    title: str
    search_rank: int  # 1-based, preserved from the underlying result order


class SearchAdapter(Protocol):
    def search(self, query: str, *, max_results: int) -> List[SearchResultCandidate]:
        """Return up to *max_results* deduplicated candidates, or an empty
        list — never raises for "no results", only for a genuine adapter
        failure (raises :class:`SearchError`, bounded, no raw exception
        text)."""
        ...


_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_result_url(raw_url: str) -> Optional[str]:
    """Reject unsupported schemes; strip the fragment; lowercase
    scheme+host; drop an explicit default port. Returns ``None`` for
    anything unusable — callers must skip it, never fetch it."""
    try:
        parts = urlsplit((raw_url or "").strip())
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return None
    host = parts.hostname
    if not host:
        return None
    netloc = host.lower()
    if parts.port and parts.port != _DEFAULT_PORTS.get(scheme):
        netloc += f":{parts.port}"
    path = parts.path or "/"
    # query is kept (distinguishes real content); fragment is always dropped
    # (client-side only, never sent to the server, never meaningful for
    # server-side fetch/dedup).
    return urlunsplit((scheme, netloc, path, parts.query, ""))


class DuckDuckGoSearchAdapter:
    """Default adapter — reuses the existing, unmodified, free DuckDuckGo
    helper. Never logs the raw query or a raw exception."""

    def search(self, query: str, *, max_results: int) -> List[SearchResultCandidate]:
        from app.tools.web_search import search_web  # local import: mirrors legacy's own lazy-import style

        try:
            raw_results = search_web(query, max_results=max_results)
        except Exception as exc:  # noqa: BLE001 — never propagate raw exception text
            raise SearchError("search_failed") from exc

        seen: set[str] = set()
        candidates: List[SearchResultCandidate] = []
        rank = 0
        for r in raw_results or []:
            if not isinstance(r, dict):
                continue
            url = normalize_result_url(r.get("url", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            rank += 1
            candidates.append(SearchResultCandidate(
                url=url,
                title=(r.get("title") or "").strip(),
                search_rank=rank,
            ))
            if len(candidates) >= max_results:
                break
        return candidates
