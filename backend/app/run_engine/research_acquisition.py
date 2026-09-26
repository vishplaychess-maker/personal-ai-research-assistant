"""Durable Deep Research — acquisition orchestrator (Phase 1B.2R MP-3C3D4B).

Wires the search adapter (:mod:`app.tools.research_search`), the guarded
fetch adapter (:mod:`app.tools.research_fetch`), and the pure
normalization/schema module (:mod:`app.run_engine.research_sources`)
together with injectable cancellation/session/deadline/heartbeat/progress
seams. **This module performs zero DB commit, zero LLM call, zero MCP/tool/
action execution, and zero knowledge-graph/memory/directive write** — it
returns a validated, in-memory structured snapshot; persisting it is the
caller's job (:mod:`app.run_engine.research_snapshot`).

Not a worker, not a route, not registered anywhere. D4C's future worker
step calls this function directly, inside its own transaction, then seals
the result via :func:`app.run_engine.research_snapshot.seal_research_snapshot`.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Callable, List
from urllib.parse import urlsplit

from app.config import settings
from app.run_engine.errors import RunEngineError
from app.run_engine.research_sources import (
    RESEARCH_MAX_PASSAGE_CHARS,
    RESEARCH_MAX_PASSAGES_PER_PAGE,
    RESEARCH_PROGRESS_CODES,
    RawPassageCandidate,
    RawSourceCandidate,
    build_research_snapshot,
)
from app.tools.research_fetch import FetchAdapter, FetchError
from app.tools.research_search import SearchAdapter, SearchError


class ResearchAcquisitionCancelled(RunEngineError):
    """Raised by an injected ``check_cancelled``/``check_session_binding``/
    ``check_deadline`` callback to stop acquisition. Never wraps/propagates
    the callback's own exception text — callers raise this directly with a
    bounded reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AcquisitionSeams:
    """Every injectable seam the orchestrator calls. All are REQUIRED
    (no silent no-op defaults) so a caller can never accidentally run
    acquisition without cancellation/session/deadline coverage.

    ``check_cancelled`` / ``check_session_binding`` / ``check_deadline``:
    call and raise :class:`ResearchAcquisitionCancelled` themselves if
    tripped (this lets each check carry its own bounded reason code).
    ``heartbeat``: renew the lease; takes no arguments, returns nothing.
    ``progress``: emit one bounded progress code + bounded counts; MUST
    perform no DB commit (persisted, if at all, by the caller's own
    control-event append, matching every other durable progress signal).
    ``now``: injectable clock (tests never depend on real wall-clock time).
    """

    check_cancelled: Callable[[], None]
    check_session_binding: Callable[[], None]
    check_deadline: Callable[[], None]
    heartbeat: Callable[[], None]
    progress: Callable[[str, dict], None]
    now: Callable[[], _dt.datetime]


@dataclass(frozen=True)
class AcquisitionResult:
    snapshot: dict
    query_count: int


def _domain_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _emit(seams: "AcquisitionSeams", code: str, data: dict) -> None:
    """The only call site that invokes ``seams.progress`` — asserts *code*
    is one of the closed, server-owned set (defense in depth: a future edit
    that accidentally introduces a 4th/stray code, e.g. a "research_planning"
    stage per D4-DECISION-1, fails loudly here rather than silently emitting
    an unbounded value)."""
    assert code in RESEARCH_PROGRESS_CODES, f"not a closed progress code: {code!r}"
    seams.progress(code, data)


def run_research_acquisition(
    query: str,
    *,
    seams: AcquisitionSeams,
    search_adapter: SearchAdapter,
    fetch_adapter: FetchAdapter,
) -> AcquisitionResult:
    """The ONE orchestration function. Synchronous, in-process, no DB
    session — a caller-owned transaction persists the result afterward.

    Flow (matches the D4A/D4B contract exactly):
      1. validate/bound the query;
      2. check cancel/session/deadline;
      3. emit ``research_searching``;
      4. search (bounded results, D4-DECISION-1: no separate planning stage —
         this whole step IS "planning + searching" folded together);
      5. check cancel/session/deadline + heartbeat;
      6. normalize/dedupe candidate URLs (defense in depth — the search
         adapter and ``research_sources.build_research_snapshot`` also
         dedupe; enforced at every layer, not a single point of failure);
      7. emit ``research_fetching`` with a bounded candidate count only;
      8-10. per page: check cancel/session/deadline before, heartbeat +
         check cancel/session/deadline after, extract bounded passages;
      11. build the authoritative, citation-correspondent snapshot;
      12. check cancel/session/deadline once more before returning (the
         caller persists it immediately after, inside its own transaction —
         this is the last chance to abort before that happens).

    ``research_sources_ready`` is deliberately NOT emitted by this function
    (MP-3C3D4C correction) — this orchestrator has no DB access and cannot
    know whether the caller's subsequent seal will actually succeed; the
    D4A/D4B contract requires that event ONLY after authoritative
    persistence commits, which is exclusively the caller's responsibility.

    A per-page fetch failure (timeout, SSRF-rejected, disallowed content
    type, network error) is caught, bounded, and SKIPPED — it never aborts
    the whole acquisition (matches the legacy `run_deep_research`'s own
    "one bad page must not abort research" posture) and never appears in
    the snapshot, in a progress event, or in an exception raised to the
    caller. Total search failure (adapter raises `SearchError`, or returns
    zero candidates) is NOT an error — an empty, valid, `truncated=False`
    snapshot with zero sources is returned, truthfully reflecting "nothing
    found," per the D4A free/local-only failure policy (never a silent
    paid-fallback trigger).
    """
    q = (query or "").strip()[: settings.research_max_query_chars]

    seams.check_cancelled()
    seams.check_session_binding()
    seams.check_deadline()

    _emit(seams, "research_searching", {})

    try:
        candidates = search_adapter.search(q, max_results=settings.research_max_result_urls)
    except SearchError:
        candidates = []

    seams.check_cancelled()
    seams.check_session_binding()
    seams.check_deadline()
    seams.heartbeat()

    # Defense-in-depth dedupe by URL (the search adapter already dedupes;
    # this guards against a future/alternate adapter that does not).
    seen_urls: set[str] = set()
    deduped = []
    for c in candidates:
        if c.url in seen_urls:
            continue
        seen_urls.add(c.url)
        deduped.append(c)

    _emit(seams, "research_fetching", {"candidate_count": len(deduped)})

    raw_sources: List[RawSourceCandidate] = []
    # `deep_research_max_scrape` is a PRE-EXISTING legacy setting with no
    # positive-value validator of its own (out of scope to add one here —
    # D4B does not modify legacy config validation) — clamped defensively
    # at the one place THIS new orchestrator relies on it as a safety
    # bound, since a negative value would otherwise slice via Python's
    # `list[:-1]`-style semantics (drop only the last element) rather than
    # yielding an empty, safely-bounded page list.
    max_scrape = max(0, settings.deep_research_max_scrape)
    for cand in deduped[:max_scrape]:
        seams.check_cancelled()
        seams.check_session_binding()
        seams.check_deadline()
        seams.heartbeat()

        try:
            page = fetch_adapter.fetch(
                cand.url,
                timeout_s=settings.research_fetch_timeout_s,
                max_redirects=settings.research_max_redirects,
                max_response_bytes=settings.research_max_response_bytes,
                max_extracted_chars=settings.research_max_extracted_chars,
                max_passages=RESEARCH_MAX_PASSAGES_PER_PAGE,
                max_passage_chars=RESEARCH_MAX_PASSAGE_CHARS,
            )
        except FetchError:
            continue  # bounded, redacted — one bad page never aborts research

        seams.check_cancelled()
        seams.check_session_binding()
        seams.check_deadline()
        seams.heartbeat()

        raw_sources.append(RawSourceCandidate(
            url=page.final_url,
            title=cand.title or None,
            domain=_domain_of(page.final_url),
            search_rank=cand.search_rank,
            retrieved_at=seams.now().isoformat(),
            published_at=None,  # nothing in this transport currently exposes one
            passages=[RawPassageCandidate(text=t) for t in page.passages],
        ))

    seams.check_cancelled()
    seams.check_session_binding()
    seams.check_deadline()

    snapshot = build_research_snapshot(
        acquired_at=seams.now().isoformat(),
        query_count=1 if q else 0,
        candidates=raw_sources,
    )

    seams.check_cancelled()
    seams.check_session_binding()
    seams.check_deadline()

    # MP-3C3D4C correction: "research_sources_ready" is NOT emitted here.
    # This orchestrator has no DB access and cannot know whether the
    # snapshot it just built will actually be sealed successfully (the
    # caller's seal could still fail — a concurrent-seal race, a DB error).
    # The D4A/D4B contract requires the event ONLY after authoritative
    # persistence commits — that is exclusively the CALLER's (D4C's worker)
    # responsibility, once `research_snapshot.seal_research_snapshot(...)`
    # actually succeeds. Emitting it here too would be both redundant and
    # ORDER-WRONG (an optimistic pre-persistence signal).
    return AcquisitionResult(snapshot=snapshot, query_count=snapshot["query_count"])
