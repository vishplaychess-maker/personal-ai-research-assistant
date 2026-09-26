"""Phase 1B.2R MP-3C3D4C — durable Deep Research worker: submission, claim
isolation, acquisition, cited synthesis, cancellation, and atomic
finalization. Fake search/fetch/provider transports only — no live
DuckDuckGo, no live HTTP, no live LLM. Reuses the exact hermetic fixture
patterns MP-3C1's own test file already established
(`_submission`/`_seed`/`_worker`) so the run-creation and worker-injection
plumbing is proven-safe, not reinvented.
"""
from __future__ import annotations

import json
import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import Message, MessageRole, Run, RunEvent, RunStep
from app.run_engine import chat_stream
from app.run_engine import submission as sub
from app.run_engine.errors import IdempotencyKeyConflict
from app.run_engine.research_sources import RawPassageCandidate, RawSourceCandidate
from app.run_engine.worker import RunWorker
from app.tools.research_fetch import FetchedPage, FetchError
from app.tools.research_search import SearchError, SearchResultCandidate
from tests.run_engine_helpers import ensure_user, make_ctx, mk_engine, mk_factory, mk_session

_HMAC = "unit-test-hmac-key-deep-research-0123"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_worker_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_deep_research_enabled", True)
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 30)
    monkeypatch.setattr(settings, "run_worker_handler_timeout_s", 300)
    monkeypatch.setattr(settings, "research_total_deadline_s", 30.0)
    monkeypatch.setattr(settings, "research_fetch_timeout_s", 5.0)


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


class FakeChatProvider:
    """Mirrors MP-3C1's own FakeProvider exactly."""

    def __init__(self, tokens, *, response=None):
        self.tokens = list(tokens)
        self.response = response
        self.calls = 0
        self.seen = None

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        self.calls += 1
        self.seen = {"messages": messages, "system_prompt": system_prompt, "model": model_name}
        for tok in self.tokens:
            yield {"type": "token", "token": tok}
        yield {"type": "done", "response": self.response if self.response is not None else "".join(self.tokens)}


class FakeSearchAdapter:
    def __init__(self, results=None, error=False):
        # NOTE: `results if results is not None else [...]`, NOT `results or
        # [...]` — an explicitly empty list is a legitimate, falsy-but-valid
        # input (zero search results) that must NOT silently fall back to
        # the default candidate.
        self._results = results if results is not None else [
            SearchResultCandidate(url="https://a.example/1", title="Source One", search_rank=1),
        ]
        self._error = error
        self.calls = 0

    def search(self, query, *, max_results):
        self.calls += 1
        if self._error:
            raise SearchError("search_failed")
        return self._results[:max_results]


class FakeFetchAdapter:
    def __init__(self, pages=None, fail_urls=None):
        self._pages = pages or {
            "https://a.example/1": FetchedPage(
                final_url="https://a.example/1",
                passages=["The sky appears blue due to Rayleigh scattering."],
            ),
        }
        self._fail_urls = fail_urls or set()
        self.calls = []

    def fetch(self, url, **kwargs):
        self.calls.append(url)
        if url in self._fail_urls:
            raise FetchError("fetch_failed")
        return self._pages[url]


def _submission(*, message="why is the sky blue", mode="deep_research", session_id=1):
    return sub.build_chat_submission(
        session_id=session_id, message=message, image_url=None,
        provider="snap-prov", model="snap-model", mode=mode,
        system_prompt="SP", options={},
    )


def _seed(eng, *, submission=None, uid=7, sid=1, idem=None):
    db = mk_session(eng)
    try:
        ensure_user(db, uid)
        from app.models.models import ResearchSession
        if db.query(ResearchSession).filter(ResearchSession.id == sid).first() is None:
            db.add(ResearchSession(id=sid, user_id=uid, title="t"))
            db.commit()
        ctx = make_ctx(db, user_id=uid, sid="fam-alpha")
        run, created = sub.create_chat_run(
            db, ctx, submission=submission or _submission(),
            idempotency_key=idem or uuid.uuid4().hex,
        )
        return run, created
    finally:
        db.close()


def _worker(eng, *, chat_provider=None, search_adapter=None, fetch_adapter=None, **kw):
    def factory(ci, **_k):
        return chat_provider

    return RunWorker(
        mk_factory(eng), worker_id=kw.pop("worker_id", "w-1"),
        chat_provider=(factory if chat_provider is not None else None),
        research_search_adapter=search_adapter, research_fetch_adapter=fetch_adapter,
        **kw,
    )


def _fresh(eng, run_id):
    db = mk_session(eng)
    try:
        return db.query(Run).filter(Run.id == run_id).first()
    finally:
        db.close()


def _steps(eng, run_id):
    db = mk_session(eng)
    try:
        return (db.query(RunStep).filter(RunStep.run_id == run_id)
                .order_by(RunStep.seq.asc()).all())
    finally:
        db.close()


def _progress_codes(eng, run_id):
    db = mk_session(eng)
    try:
        rows = (db.query(RunEvent).filter(RunEvent.run_id == run_id,
                                          RunEvent.type == "research.progress")
                .order_by(RunEvent.run_seq.asc()).all())
        return [json.loads(r.data)["code"] for r in rows]
    finally:
        db.close()


def _assistant_messages(eng, sid=1):
    db = mk_session(eng)
    try:
        return db.query(Message).filter(Message.session_id == sid,
                                        Message.role == MessageRole.assistant).all()
    finally:
        db.close()


def _run_to_success(eng, worker, run_id):
    """Drive advance_deep_research_runs() until the run is terminal or a
    fixed iteration budget is exhausted (defends against an infinite loop
    if a test's expectations are wrong)."""
    for _ in range(6):
        outcomes = worker.advance_deep_research_runs()
        run = _fresh(eng, run_id)
        if run.status in ("succeeded", "failed", "cancelled", "outcome_unknown"):
            return run, outcomes
    return _fresh(eng, run_id), []


# ── B. Submission / idempotency ───────────────────────────────


def test_submission_creates_a_deep_research_run(eng):
    run, created = _seed(eng)
    assert created is True
    assert run.kind == "deep_research"
    assert run.status == "queued"


def test_replay_with_same_key_creates_zero_duplicates(eng):
    key = uuid.uuid4().hex
    run1, created1 = _seed(eng, idem=key)
    run2, created2 = _seed(eng, idem=key)
    assert created1 is True
    assert created2 is False
    assert run1.id == run2.id
    db = mk_session(eng)
    try:
        assert db.query(Run).filter(Run.kind == "deep_research").count() == 1
    finally:
        db.close()


def test_same_key_different_prompt_conflicts(eng):
    key = uuid.uuid4().hex
    _seed(eng, submission=_submission(message="question A"), idem=key)
    db = mk_session(eng)
    try:
        ctx = make_ctx(db, user_id=7, sid="fam-alpha")
        with pytest.raises(IdempotencyKeyConflict):
            sub.create_chat_run(
                db, ctx, submission=_submission(message="question B"),
                idempotency_key=key,
            )
    finally:
        db.close()


def test_submission_performs_zero_external_calls(eng, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("submission must never call search/fetch/provider")

    monkeypatch.setattr("app.tools.research_search.DuckDuckGoSearchAdapter.search", boom)
    monkeypatch.setattr("app.tools.research_fetch.GuardedHttpFetchAdapter.fetch", boom)
    run, created = _seed(eng)
    assert created is True  # reaching here at all proves no external call fired


# ── C. Claim / isolation ──────────────────────────────────────


def test_chat_and_mcp_sweeps_never_claim_a_deep_research_run(eng):
    run, _ = _seed(eng)
    w = _worker(eng, chat_provider=FakeChatProvider(["x"]))
    chat_outcomes = w.advance_chat_runs()
    mcp_outcomes = w.advance_mcp_runs()
    assert all(o.run_id != run.id for o in chat_outcomes)
    assert all(o.run_id != run.id for o in mcp_outcomes)
    assert _fresh(eng, run.id).status == "queued"  # untouched


def test_generic_claim_next_queued_excludes_deep_research(eng):
    run, _ = _seed(eng)
    w = _worker(eng, chat_provider=FakeChatProvider(["x"]))
    claimed = w.claim_next_queued()
    assert claimed is None  # the ONLY queued run is deep_research — must not be claimed here
    assert _fresh(eng, run.id).status == "queued"


def test_only_deep_research_sweep_claims_it(eng):
    run, _ = _seed(eng)
    w = _worker(
        eng, chat_provider=FakeChatProvider(["Blue skies.", " [W1]"]),
        search_adapter=FakeSearchAdapter(), fetch_adapter=FakeFetchAdapter(),
    )
    w.advance_deep_research_runs()
    assert _fresh(eng, run.id).status != "queued"  # claimed and advanced


# ── D/E. Acquisition + synthesis + citations: full happy path ──


def test_full_happy_path_acquisition_then_synthesis_then_citations(eng):
    run, _ = _seed(eng)
    provider = FakeChatProvider(["The sky is blue due to scattering ", "[W1]", "."])
    search = FakeSearchAdapter()
    fetch = FakeFetchAdapter()
    w = _worker(eng, chat_provider=provider, search_adapter=search, fetch_adapter=fetch)

    final, _ = _run_to_success(eng, w, run.id)
    assert final.status == "succeeded"
    assert search.calls == 1
    assert fetch.calls == ["https://a.example/1"]
    assert provider.calls == 1

    steps = _steps(eng, run.id)
    kinds = [s.kind for s in steps]
    assert "research_fetch_extract" in kinds
    assert "deep_research_synthesis" in kinds

    msgs = _assistant_messages(eng)
    assert len(msgs) == 1
    citations = json.loads(msgs[0].citations)
    assert any(c["marker"] == "[W1]" for c in citations)
    assert citations[0].get("url") == "https://a.example/1"


def test_progress_events_in_order_closed_set_only(eng):
    """MP-3C3D4C closeout correction: ``research_synthesizing`` is now
    actually emitted (atomically with the synthesis RunStep's pending ->
    running transition, before the provider is invoked — see worker.py) —
    an earlier revision of this test encoded its ABSENCE as the expected
    behavior, which was itself the bug (the event existed in the schema but
    was never persisted; a reconnecting client had no way to know synthesis
    had started)."""
    run, _ = _seed(eng)
    w = _worker(
        eng, chat_provider=FakeChatProvider(["ans ", "[W1]"]),
        search_adapter=FakeSearchAdapter(), fetch_adapter=FakeFetchAdapter(),
    )
    _run_to_success(eng, w, run.id)
    codes = _progress_codes(eng, run.id)
    assert codes == ["research_searching", "research_fetching", "research_sources_ready",
                      "research_synthesizing"]
    assert "research_planning" not in codes


def test_zero_sources_terminates_without_calling_provider(eng):
    run, _ = _seed(eng)
    provider = FakeChatProvider(["should never stream"])
    w = _worker(
        eng, chat_provider=provider,
        search_adapter=FakeSearchAdapter(results=[]), fetch_adapter=FakeFetchAdapter(),
    )
    final, _ = _run_to_success(eng, w, run.id)
    assert final.status == "failed"
    assert final.status_reason == "research_no_sources"
    assert provider.calls == 0


def test_partial_page_failure_with_one_valid_source_still_proceeds(eng):
    run, _ = _seed(eng)
    results = [
        SearchResultCandidate(url="https://a.example/bad", title="Bad", search_rank=1),
        SearchResultCandidate(url="https://a.example/good", title="Good", search_rank=2),
    ]
    fetch = FakeFetchAdapter(
        pages={"https://a.example/good": FetchedPage(
            final_url="https://a.example/good", passages=["Good passage text here."])},
        fail_urls={"https://a.example/bad"},
    )
    w = _worker(
        eng, chat_provider=FakeChatProvider(["cited ", "[W1]"]),
        search_adapter=FakeSearchAdapter(results=results), fetch_adapter=fetch,
    )
    final, _ = _run_to_success(eng, w, run.id)
    assert final.status == "succeeded"


# ── restart / replay after seal: zero new network calls ──────


def test_restart_after_sealed_snapshot_performs_zero_new_network_calls(eng):
    """Simulates a genuine crash window: acquisition completed and sealed
    a snapshot (worker 1), then the process died before synthesis ever
    started — the run is left `running` with an EXPIRED lease and no
    synthesis step. A second worker's sweep must reclaim it and drive
    synthesis ONLY, never touching search/fetch again (both adapters here
    raise if invoked at all)."""
    import datetime as _dt

    from app.run_engine.research_snapshot import seal_research_snapshot
    from app.run_engine.research_sources import build_research_snapshot

    run, _ = _seed(eng)
    snapshot = build_research_snapshot(
        acquired_at="2026-01-01T00:00:00+00:00", query_count=1,
        candidates=[RawSourceCandidate(
            url="https://a.example/1", title="Source One", domain="a.example",
            search_rank=1, retrieved_at="2026-01-01T00:00:00+00:00", published_at=None,
            passages=[RawPassageCandidate(text="Sealed passage from worker 1.")],
        )],
    )
    db = mk_session(eng)
    try:
        seal_research_snapshot(
            db, tenant_id=None, user_id=7, session_id=1, run_id=run.id,
            step_id=str(uuid.uuid4()), seq=2, snapshot=snapshot, commit=False,
        )
        # simulate: worker 1 claimed the run (lease now expired) and crashed
        # right after sealing, before any synthesis step existed.
        db.query(Run).filter(Run.id == run.id).update({
            "status": "running", "lease_owner": "dead-worker/aaaa",
            "lease_expires_at": _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5),
            "version": 1,
        })
        db.commit()
    finally:
        db.close()

    class _BoomSearch:
        def search(self, *a, **k):
            raise AssertionError("must not re-search after snapshot is sealed")

    class _BoomFetch:
        def fetch(self, *a, **k):
            raise AssertionError("must not re-fetch after snapshot is sealed")

    w2 = _worker(
        eng, chat_provider=FakeChatProvider(["done ", "[W1]"]),
        search_adapter=_BoomSearch(), fetch_adapter=_BoomFetch(),
    )
    final, _ = _run_to_success(eng, w2, run.id)
    assert final.status == "succeeded"
    msgs = _assistant_messages(eng)
    citations = json.loads(msgs[0].citations)
    assert citations[0]["url"] == "https://a.example/1"


# ── F. Citations ───────────────────────────────────────────────


def test_hallucinated_marker_fails_closed(eng):
    run, _ = _seed(eng)
    # [W99] does not exist in the sealed 1-passage snapshot.
    provider = FakeChatProvider(["fabricated citation ", "[W99]"])
    w = _worker(
        eng, chat_provider=provider,
        search_adapter=FakeSearchAdapter(), fetch_adapter=FakeFetchAdapter(),
    )
    final, _ = _run_to_success(eng, w, run.id)
    assert final.status == "failed"
    assert final.status_reason == "citation_validation_failed"


def test_no_citation_marker_fails_research_citations_missing(eng):
    run, _ = _seed(eng)
    provider = FakeChatProvider(["an answer with no citation marker at all"])
    w = _worker(
        eng, chat_provider=provider,
        search_adapter=FakeSearchAdapter(), fetch_adapter=FakeFetchAdapter(),
    )
    final, _ = _run_to_success(eng, w, run.id)
    assert final.status == "failed"
    assert final.status_reason == "research_citations_missing"


def test_duplicate_marker_dedupes_in_final_citations(eng):
    run, _ = _seed(eng)
    provider = FakeChatProvider(["[W1] and again [W1] same source"])
    w = _worker(
        eng, chat_provider=provider,
        search_adapter=FakeSearchAdapter(), fetch_adapter=FakeFetchAdapter(),
    )
    final, _ = _run_to_success(eng, w, run.id)
    assert final.status == "succeeded"
    msgs = _assistant_messages(eng)
    citations = json.loads(msgs[0].citations)
    w1_count = sum(1 for c in citations if c["marker"] == "[W1]")
    assert w1_count == 1


def test_corrupt_local_context_snapshot_fails_closed_no_message(eng, monkeypatch):
    """MP-3C3D4C closeout correction: a present-but-unreadable sealed
    context snapshot (``ContextSnapshotError`` or a decrypt failure) must
    fail the run closed with ``context_snapshot_unreadable`` and create NO
    Message — mirroring the plain-chat path's own
    ``read_context_snapshot`` contract exactly (its docstring: "a caller
    must fail closed, not invent a fabricated-empty citation set"). An
    earlier revision of ``_run_deep_research_synthesis`` caught this
    broadly and silently continued with ``local_citations=[]``, which would
    have produced a "successful" cited answer from web sources alone while
    silently discarding a corrupt/anomalous local-citation state instead of
    surfacing it."""
    run, _ = _seed(eng)
    monkeypatch.setattr(chat_stream, "read_context_snapshot",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("corrupt")))
    provider = FakeChatProvider(["a real answer anyway [W1]"])
    w = _worker(
        eng, chat_provider=provider,
        search_adapter=FakeSearchAdapter(), fetch_adapter=FakeFetchAdapter(),
    )
    final, _ = _run_to_success(eng, w, run.id)
    assert final.status == "failed"
    assert final.status_reason == "context_snapshot_unreadable"
    assert _assistant_messages(eng) == []


# ── G. Policy: no fallback, no KG/memory/directive write ──────


def test_no_llm_mcp_kg_memory_directive_execution_paths_in_deep_research_methods():
    """Scoped to the deep-research methods' OWN source (not the whole
    worker.py file, which legitimately mentions "save_memory" etc. in
    docstrings for the UNRELATED chat/MCP drivers elsewhere in the same
    file) — proves the deep-research code path itself never calls any of
    these."""
    import inspect

    import app.run_engine.worker as worker_mod

    for name in ("_run_deep_research_acquisition", "_run_deep_research_synthesis",
                 "_finalize_deep_research_success", "_advance_deep_research_run",
                 "advance_deep_research_runs"):
        source = inspect.getsource(getattr(worker_mod.RunWorker, name))
        for forbidden in ("ingest_text_async(", "save_memory(", "save_directive(",
                          "extract_mcp_calls(", "knowledge_graph."):
            assert forbidden not in source, f"{name} unexpectedly references {forbidden!r}"


def test_flags_off_by_default_and_sweep_is_a_noop(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_deep_research_enabled", False)
    run, _ = _seed(eng)
    w = _worker(
        eng, chat_provider=FakeChatProvider(["x"]),
        search_adapter=FakeSearchAdapter(), fetch_adapter=FakeFetchAdapter(),
    )
    outcomes = w.advance_deep_research_runs()
    assert outcomes == []
    assert _fresh(eng, run.id).status == "queued"  # completely untouched
