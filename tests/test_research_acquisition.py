"""Phase 1B.2R MP-3C3D4B — focused tests for
``app.run_engine.research_acquisition`` (the orchestrator). All adapters are
fakes; no network, no DB, no LLM/MCP/tool/KG/memory/directive call of any
kind exists in this module to even invoke."""
from __future__ import annotations

import datetime as _dt
from typing import List

import pytest

from app.run_engine.research_acquisition import (
    AcquisitionSeams,
    ResearchAcquisitionCancelled,
    run_research_acquisition,
)
from app.run_engine.research_sources import RESEARCH_PROGRESS_CODES
from app.tools.research_fetch import FetchedPage, FetchError
from app.tools.research_search import SearchError, SearchResultCandidate


class _FakeSearch:
    def __init__(self, results=None, error=False):
        self._results = results or []
        self._error = error
        self.calls = 0

    def search(self, query, *, max_results):
        self.calls += 1
        if self._error:
            raise SearchError("search_failed")
        return self._results[:max_results]


class _FakeFetch:
    def __init__(self, pages=None, fail_urls=None):
        self._pages = pages or {}
        self._fail_urls = fail_urls or set()
        self.calls: List[str] = []

    def fetch(self, url, **kwargs):
        self.calls.append(url)
        if url in self._fail_urls:
            raise FetchError("fetch_failed")
        return self._pages[url]


class _Recorder:
    """Collects every seam invocation in order, for assert-on-sequence
    tests, while behaving as a normal no-op/pass otherwise."""

    def __init__(self, cancel_after: int = 10_000, deny_session_after: int = 10_000,
                 deadline_after: int = 10_000):
        self.calls: List[str] = []
        self._cancel_after = cancel_after
        self._deny_session_after = deny_session_after
        self._deadline_after = deadline_after
        self._n_cancel = 0
        self._n_session = 0
        self._n_deadline = 0
        self.heartbeats = 0
        self.progress_events: List[tuple] = []

    def check_cancelled(self):
        self.calls.append("cancel")
        self._n_cancel += 1
        if self._n_cancel >= self._cancel_after:
            raise ResearchAcquisitionCancelled("cancelled_before_call")

    def check_session_binding(self):
        self.calls.append("session")
        self._n_session += 1
        if self._n_session >= self._deny_session_after:
            raise ResearchAcquisitionCancelled("session_revoked")

    def check_deadline(self):
        self.calls.append("deadline")
        self._n_deadline += 1
        if self._n_deadline >= self._deadline_after:
            raise ResearchAcquisitionCancelled("deadline_exceeded")

    def heartbeat(self):
        self.calls.append("heartbeat")
        self.heartbeats += 1

    def progress(self, code, data):
        self.calls.append(f"progress:{code}")
        self.progress_events.append((code, data))

    def seams(self) -> AcquisitionSeams:
        return AcquisitionSeams(
            check_cancelled=self.check_cancelled,
            check_session_binding=self.check_session_binding,
            check_deadline=self.check_deadline,
            heartbeat=self.heartbeat,
            progress=self.progress,
            now=lambda: _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
        )


# ── F35-F36: callback/progress order, no research_planning ───


def test_progress_events_are_only_the_closed_set_in_order():
    """MP-3C3D4C correction: "research_sources_ready" is deliberately NOT
    emitted by the orchestrator itself (it has no DB access and cannot know
    whether the caller's subsequent seal will succeed — that event is the
    CALLER's exclusive responsibility, emitted only after persistence
    commits). This orchestrator emits only "research_searching" and
    "research_fetching"."""
    rec = _Recorder()
    search = _FakeSearch(results=[SearchResultCandidate(url="https://a.example/1", title="A", search_rank=1)])
    fetch = _FakeFetch(pages={"https://a.example/1": FetchedPage(final_url="https://a.example/1", passages=["Hello."])})

    run_research_acquisition("q", seams=rec.seams(), search_adapter=search, fetch_adapter=fetch)

    codes = [c for c, _ in rec.progress_events]
    assert codes == ["research_searching", "research_fetching"]
    for c in codes:
        assert c in RESEARCH_PROGRESS_CODES
    assert "research_planning" not in codes
    assert "research_sources_ready" not in codes


def test_no_research_planning_progress_code_exists_anywhere():
    assert "research_planning" not in RESEARCH_PROGRESS_CODES


# ── F37: cancel before search → zero calls ───────────────────


def test_cancel_before_search_yields_zero_search_and_fetch_calls():
    rec = _Recorder(cancel_after=1)  # trips on the very first check_cancelled()
    search = _FakeSearch(results=[SearchResultCandidate(url="https://a.example/1", title="A", search_rank=1)])
    fetch = _FakeFetch()

    with pytest.raises(ResearchAcquisitionCancelled):
        run_research_acquisition("q", seams=rec.seams(), search_adapter=search, fetch_adapter=fetch)

    assert search.calls == 0
    assert fetch.calls == []


# ── F38: cancel between fetches prevents later calls ─────────


def test_cancel_between_fetches_prevents_later_fetch_calls():
    urls = ["https://a.example/1", "https://a.example/2", "https://a.example/3"]
    results = [SearchResultCandidate(url=u, title="A", search_rank=i + 1) for i, u in enumerate(urls)]
    pages = {u: FetchedPage(final_url=u, passages=["text"]) for u in urls}
    search = _FakeSearch(results=results)
    fetch = _FakeFetch(pages=pages)

    # `check_cancelled` fires twice before the fetch loop (call #1 at
    # entry, call #2 after search) then once per loop iteration before
    # each fetch (call #3 = before page 1, call #4 = before page 2, ...).
    # Tripping on call #4 lets page 1's pre-check (call #3) pass — page 1
    # is fetched — then stops before page 2 is ever attempted.
    rec = _Recorder(cancel_after=4)

    with pytest.raises(ResearchAcquisitionCancelled):
        run_research_acquisition("q", seams=rec.seams(), search_adapter=search, fetch_adapter=fetch)

    assert fetch.calls == ["https://a.example/1"]
    assert "https://a.example/2" not in fetch.calls
    assert "https://a.example/3" not in fetch.calls


# ── F39: session revoke stops ────────────────────────────────


def test_session_revoked_stops_acquisition():
    rec = _Recorder(deny_session_after=1)
    search = _FakeSearch(results=[SearchResultCandidate(url="https://a.example/1", title="A", search_rank=1)])
    fetch = _FakeFetch()

    with pytest.raises(ResearchAcquisitionCancelled) as exc_info:
        run_research_acquisition("q", seams=rec.seams(), search_adapter=search, fetch_adapter=fetch)
    assert exc_info.value.reason == "session_revoked"
    assert search.calls == 0


# ── F40: deadline stops ───────────────────────────────────────


def test_deadline_exceeded_stops_acquisition():
    rec = _Recorder(deadline_after=1)
    search = _FakeSearch(results=[SearchResultCandidate(url="https://a.example/1", title="A", search_rank=1)])
    fetch = _FakeFetch()

    with pytest.raises(ResearchAcquisitionCancelled) as exc_info:
        run_research_acquisition("q", seams=rec.seams(), search_adapter=search, fetch_adapter=fetch)
    assert exc_info.value.reason == "deadline_exceeded"
    assert search.calls == 0


# ── F41: heartbeat invoked at bounded checkpoints ─────────────


def test_heartbeat_invoked_at_least_once_per_fetched_page():
    urls = ["https://a.example/1", "https://a.example/2"]
    results = [SearchResultCandidate(url=u, title="A", search_rank=i + 1) for i, u in enumerate(urls)]
    pages = {u: FetchedPage(final_url=u, passages=["text"]) for u in urls}
    rec = _Recorder()
    search = _FakeSearch(results=results)
    fetch = _FakeFetch(pages=pages)

    run_research_acquisition("q", seams=rec.seams(), search_adapter=search, fetch_adapter=fetch)
    # 1 after search + 2 checks (before+after) per fetched page = 5
    assert rec.heartbeats >= 1 + 2 * len(urls)


# ── F42: empty results truthful ───────────────────────────────


def test_empty_search_results_produce_a_truthful_empty_snapshot():
    rec = _Recorder()
    search = _FakeSearch(results=[])
    fetch = _FakeFetch()

    result = run_research_acquisition("q", seams=rec.seams(), search_adapter=search, fetch_adapter=fetch)
    assert result.snapshot["sources"] == []
    assert result.snapshot["truncated"] is False
    assert fetch.calls == []


def test_search_adapter_raising_search_error_is_not_fatal():
    rec = _Recorder()
    search = _FakeSearch(error=True)
    fetch = _FakeFetch()

    result = run_research_acquisition("q", seams=rec.seams(), search_adapter=search, fetch_adapter=fetch)
    assert result.snapshot["sources"] == []


# ── F43: partial page failures bounded ────────────────────────


def test_one_failed_page_does_not_abort_acquisition_of_others():
    urls = ["https://a.example/good", "https://a.example/bad", "https://a.example/good2"]
    results = [SearchResultCandidate(url=u, title="A", search_rank=i + 1) for i, u in enumerate(urls)]
    pages = {
        "https://a.example/good": FetchedPage(final_url="https://a.example/good", passages=["ok1"]),
        "https://a.example/good2": FetchedPage(final_url="https://a.example/good2", passages=["ok2"]),
    }
    rec = _Recorder()
    search = _FakeSearch(results=results)
    fetch = _FakeFetch(pages=pages, fail_urls={"https://a.example/bad"})

    result = run_research_acquisition("q", seams=rec.seams(), search_adapter=search, fetch_adapter=fetch)
    urls_in_snapshot = [s["url"] for s in result.snapshot["sources"]]
    assert "https://a.example/bad" not in urls_in_snapshot
    assert set(urls_in_snapshot) == {"https://a.example/good", "https://a.example/good2"}


# ── F44: zero LLM/tool/KG/memory/directive execution ──────────


def test_orchestrator_module_has_no_llm_mcp_kg_memory_directive_imports():
    import app.run_engine.research_acquisition as mod
    import inspect

    source = inspect.getsource(mod)
    for forbidden in (
        "generate_stream", "llm_provider", "LLMProvider",
        "mcp_tool", "extract_mcp_calls", "mcp_flow",
        "knowledge_graph", "ingest_text",
        "memory_service", "save_memory", "save_directive",
    ):
        assert forbidden not in source, f"orchestrator must never reference {forbidden!r}"
