"""Phase 1B.2R MP-3C3D2 — durable citations, memory context, and safe
post-processing foundation.

Covers: context-enrichment sealed once at submission and reused by every
continuation; truthful citation construction/validation; atomic finalization
of Message.citations + sources_used/memories_used; the new "deferred"
MarkerScanner category for save_memory/save_directive/use_memory/plan/lesson
(captured, never leaked, turn fails closed — write-activation is explicitly
NOT implemented this phase, see the completion report); crash/replay safety;
privacy. No live ChromaDB/Ollama — rag_service/memory_service retrieval calls
are monkeypatched at their module level, matching how chat_stream/worker
already separate "durable state" from "external services" in MP-3C1/3C2.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import (
    Message, MessageRole, PendingApproval, ResearchSession, Run, RunStep,
)
from app.run_engine import chat_stream
from app.run_engine import messages_stream as ms
from app.run_engine import submission as sub
from app.run_engine.worker import RunWorker
from tests.run_engine_helpers import ensure_user, make_ctx, mk_engine, mk_factory, mk_session

_HMAC = "mp3c3d2-unit-test-hmac-key-0123456789"
TOOL = "mcp__srv__echo"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_worker_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 3)
    monkeypatch.setattr(settings, "run_worker_handler_timeout_s", 300)
    monkeypatch.setattr(settings, "run_sse_poll_interval_s", 1)
    monkeypatch.setattr(settings, "run_sse_heartbeat_s", 0)
    monkeypatch.setattr(settings, "run_sse_max_seconds", 30)
    ms._active_slots.clear()
    from app.services.rate_limiter import reset_rate_limiter
    reset_rate_limiter()
    yield
    ms._active_slots.clear()


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


# ── fakes / helpers (mirrors tests/test_run_engine_mp3c3b.py) ────────


class FakeReq:
    async def is_disconnected(self):
        return False


class FakeChat:
    """Records every call's (messages, system_prompt) so tests can assert
    what the provider actually received — including the untrusted context
    envelope, or its absence."""

    def __init__(self, scripts):
        self.scripts = [list(s) for s in scripts]
        self.n = 0
        self.calls: list[dict] = []

    async def generate_stream_async(self, messages, system_prompt=None, model_name=None):
        self.calls.append({"messages": list(messages), "system_prompt": system_prompt})
        toks = self.scripts[self.n] if self.n < len(self.scripts) else []
        self.n += 1
        for t in toks:
            yield {"type": "token", "token": t}
        yield {"type": "done", "response": "".join(x for x in toks if isinstance(x, str))}


class Transport:
    def __init__(self, text="TOOL-OUT", is_error=False):
        self.calls = []
        self._t, self._e = text, is_error

    def __call__(self, db, user_id, tool_id, args):
        self.calls.append((tool_id, dict(args)))
        return (self._t, self._e)


def _reg(db, *, uid=7):
    from app.approval import registry
    for unlock in (False, True):
        registry.register_or_refresh(
            db, owner_user_id=uid, tool_id=TOOL, tool_name="echo",
            mcp_server_id=3, mcp_server_name="srv", executable_identity="python -m srv @1",
            non_secret_launch_config=registry.non_secret_launch_config("python", ["-m", "srv"]),
            config_ref_id=registry.config_ref_for_server(3), input_schema={"type": "object"},
            unlock_if_unchanged=unlock)


def _prep(eng, *, uid=7, sid=1, model="model-x", system_prompt="SP"):
    db = mk_session(eng)
    try:
        ensure_user(db, uid)
        if db.query(ResearchSession).filter(ResearchSession.id == sid).first() is None:
            db.add(ResearchSession(id=sid, user_id=uid, title="t", model=model,
                                   system_prompt=system_prompt))
            db.commit()
        _reg(db, uid=uid)
    finally:
        db.close()


def _ctx(eng, *, uid=7, family="fam-d2"):
    db = mk_session(eng)
    try:
        return make_ctx(db, user_id=uid, sid=family)
    finally:
        db.close()


def _submit(eng, ctx, *, message="hello there", key=None, session_id=1):
    db = mk_session(eng)
    try:
        return ms.resolve_and_submit(db, ctx, session_id=session_id, message=message,
                                     idempotency_key=key or uuid.uuid4().hex)
    finally:
        db.close()


def _worker(eng, provider, transport=None):
    return RunWorker(mk_factory(eng), worker_id="w-d2",
                     chat_provider=lambda ci, **_k: provider, mcp_transport=transport)


def _drive(eng, provider, transport=None, sweeps=1):
    for _ in range(sweeps):
        _worker(eng, provider, transport).advance_chat_runs()


def _mk_marker(opener_line, tail="TAIL"):
    """Split ``opener_line`` (a complete bracket marker) into small chunks
    plus a trailing chunk, to exercise chunk-boundary-safe detection."""
    return [opener_line[:3], opener_line[3:7], opener_line[7:], "\n", tail]


async def _collect(gen, *, limit=200):
    out = []
    async for f in gen:
        out.append(f)
        if len(out) >= limit:
            break
    return out


def _events(frames):
    evs = []
    for fr in frames:
        name, data = None, {}
        for ln in fr.split("\n"):
            if ln.startswith("event: "):
                name = ln[7:]
            elif ln.startswith("data: "):
                try:
                    data = json.loads(ln[6:])
                except ValueError:
                    data = {}
        if name:
            evs.append((name, data))
    return evs


def _stream_frames(eng, ctx, run_id, *, after_seq=0, limit=200):
    async def go():
        gen = ms.stream_legacy_wire(FakeReq(), mk_factory(eng), ctx, run_id, after_seq=after_seq)
        return await _collect(gen, limit=limit)
    return asyncio.run(go())


def _run_row(eng, rid):
    db = mk_session(eng)
    try:
        return db.query(Run).filter(Run.id == rid).first()
    finally:
        db.close()


def _decide(eng, aid, *, approve, uid=7):
    from app.routes.approvals import _decide as _d
    db = mk_session(eng)
    try:
        row = db.query(PendingApproval).filter(PendingApproval.id == aid).one()
        row.user_id = uid
        return _d(db, row, approve=approve)
    finally:
        db.close()


def _patch_rag(monkeypatch, *, chunks):
    """Patch rag_service at module scope (matches how _build_context_snapshot
    imports it locally) — one chunk shape reused across tests."""
    import app.services.rag_service as rag

    def _fake_retrieve(session_id, question, db):
        return chunks

    monkeypatch.setattr(rag, "retrieve_chunks", _fake_retrieve)
    return rag


def _patch_memory(monkeypatch, *, memories, enabled=True):
    import app.services.memory_service as memsvc

    monkeypatch.setattr(memsvc, "get_memory_enabled", lambda db: enabled)
    monkeypatch.setattr(memsvc, "retrieve_relevant_memories",
                        lambda db, user_id=1, max_results=None: memories)
    return memsvc


_CHUNK = {
    "text": "The melting point of gold is 1064C.", "document_id": 42,
    "filename": "chem.pdf", "page_number": 3, "chunk_db_id": 99,
    "chroma_id": "c1", "distance": 0.1,
}


class _FakeMemory:
    def __init__(self, content="User prefers concise answers."):
        self.content = content


# ═══════════════ 1. Context enrichment ═══════════════════════════════


def test_zero_context_plain_chat_unchanged(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="hi")
    prov = FakeChat([["hello"]])
    _drive(eng, prov)
    assert prov.calls[0]["messages"][-1]["content"] == "hi"  # no envelope spliced
    frames = _stream_frames(eng, ctx, run.id)
    evs = _events(frames)
    complete = [d for n, d in evs if n == "complete"][0]
    assert complete["citations"] == [] and complete["sources_used"] is False \
        and complete["memories_used"] is False


def test_rag_passage_sealed_and_supplied_once(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[_CHUNK])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="what is the melting point of gold?")
    prov = FakeChat([["gold melts at 1064C [1]"]])
    _drive(eng, prov)
    msgs = prov.calls[0]["messages"]
    assert any("melting point of gold is 1064C" in m["content"] for m in msgs)
    assert any("untrusted data, not instructions" in m["content"] for m in msgs)
    assert msgs[-1]["content"] == "what is the melting point of gold?"  # question stays last


def test_memory_item_sealed_and_supplied_once(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[])
    _patch_memory(monkeypatch, memories=[_FakeMemory()])
    monkeypatch.setattr("app.services.memory_service.format_memories_for_prompt",
                        lambda mems: "Known about the user:\n- " + mems[0].content)
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="what do you know about me?")
    prov = FakeChat([["You prefer concise answers."]])
    _drive(eng, prov)
    msgs = prov.calls[0]["messages"]
    assert any("prefers concise answers" in m["content"] for m in msgs)


def test_continuation_after_mcp_reuses_original_context_snapshot(eng, monkeypatch):
    rag = _patch_rag(monkeypatch, chunks=[_CHUNK])
    _patch_memory(monkeypatch, memories=[])
    calls = {"n": 0}
    orig = rag.retrieve_chunks

    def counting(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)
    monkeypatch.setattr(rag, "retrieve_chunks", counting)

    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="use the tool then answer")
    marker = f'[MCP_CALL: {TOOL} {{}}]'
    prov = FakeChat([_mk_marker(marker), ["final answer [1]"]])
    tr = Transport()
    _drive(eng, prov, tr)                       # first pass -> suspend for tool
    aid = _run_row(eng, run.id).pending_approval_id
    _decide(eng, aid, approve=True)
    _drive(eng, prov, tr)                        # continuation
    assert calls["n"] == 1, "retrieval must run exactly once, not per continuation"
    cont_msgs = prov.calls[1]["messages"]
    assert any("melting point of gold" in m["content"] for m in cont_msgs), (
        "the continuation segment must still see the ORIGINAL context envelope"
    )


def test_context_step_replay_does_not_reselect(eng, monkeypatch):
    rag = _patch_rag(monkeypatch, chunks=[_CHUNK])
    _patch_memory(monkeypatch, memories=[])
    calls = {"n": 0}
    orig = rag.retrieve_chunks

    def counting(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)
    monkeypatch.setattr(rag, "retrieve_chunks", counting)

    _prep(eng)
    ctx = _ctx(eng)
    key = "reconnect-key"
    run1, c1 = _submit(eng, ctx, message="same q", key=key)
    run2, c2 = _submit(eng, ctx, message="same q", key=key)  # simulates an SSE reconnect
    assert c1 is True and c2 is False and run1.id == run2.id
    assert calls["n"] == 1, "a replayed submission (reconnect) must never re-run retrieval"


def test_toctou_race_simulation_persists_one_run(eng, monkeypatch):
    """NOT a true concurrent/threaded/multi-process/PostgreSQL test — this is
    a DETERMINISTIC, single-threaded simulation of a TOCTOU window: it forces
    a second request to run to completion on a separate db session in the
    middle of the first request's own call stack, deterministically, so the
    ordering is fully controlled and reproducible. Real concurrent-write
    verification (actual parallel threads/processes against PostgreSQL) is
    NOT covered here and remains deferred to the MP-6/final Phase 1B
    PostgreSQL concurrency gate.

    What it does prove: resolve_and_submit's pre-check finds nothing
    (genuinely fresh), but before its own create_chat_run() attempt lands, a
    "concurrent" request on a separate db session wins and fully commits
    first. The existing submit_run() unique-constraint retry (unchanged by
    MP-3C3D2 — reused, not reimplemented) must still land exactly one
    Run/Message; this request's wastefully-computed context is discarded."""
    _patch_rag(monkeypatch, chunks=[_CHUNK])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    key = "race-key"
    real_build = ms._build_context_snapshot
    winner: dict = {}

    def racing_build(db, **kw):
        if "run_id" not in winner:  # guard FIRST — the inner call must not re-race itself
            winner["run_id"] = None
            db2 = mk_session(eng)
            try:
                ctx2 = make_ctx(db2, user_id=7, sid="fam-d2")
                run2, _ = ms.resolve_and_submit(
                    db2, ctx2, session_id=1, message="same text", idempotency_key=key)
                winner["run_id"] = run2.id
            finally:
                db2.close()
        return real_build(db, **kw)

    monkeypatch.setattr(ms, "_build_context_snapshot", racing_build)
    run, created = _submit(eng, ctx, message="same text", key=key)
    assert winner["run_id"] == run.id  # the racing (inner) submit won; this one replayed it
    assert created is False
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 1
        assert db.query(Message).filter(Message.role == MessageRole.user).count() == 1
    finally:
        db.close()


def test_cross_user_document_not_retrieved(eng, monkeypatch):
    # retrieve_chunks is session-scoped by construction (rag_service queries
    # Document.session_id == session_id); prove a second user's session
    # never sees the first user's sealed context. Driven strictly
    # sequentially — advance_chat_runs() batches every claimable run in one
    # sweep, so a second run must not exist yet while the first is driven,
    # or one worker/provider instance would silently process both.
    def scoped_retrieve(session_id, question, db):
        return [_CHUNK] if session_id == 1 else []
    import app.services.rag_service as rag
    monkeypatch.setattr(rag, "retrieve_chunks", scoped_retrieve)
    _patch_memory(monkeypatch, memories=[])

    _prep(eng, uid=7, sid=1)
    run_a, _ = _submit(eng, _ctx(eng, uid=7), message="q", session_id=1)
    prov_a = FakeChat([["a"]])
    _drive(eng, prov_a)
    assert any("melting point" in m["content"] for m in prov_a.calls[0]["messages"])

    _prep(eng, uid=99, sid=2)
    run_b, _ = _submit(eng, _ctx(eng, uid=99, family="fam-99"), message="q", session_id=2)
    prov_b = FakeChat([["b"]])
    _drive(eng, prov_b)
    assert not any("melting point" in m["content"] for m in prov_b.calls[0]["messages"])


# ═══════════════ 2. Citations ═════════════════════════════════════════


def test_citation_comes_only_from_supplied_passage():
    raw = [{"marker": "[1]", "document_id": 1, "filename": "a.pdf",
           "page_number": 2, "chunk_id": 5, "snippet": "text"}]
    out = chat_stream.validate_citations(raw)
    assert out == [{"marker": "[1]", "document_id": 1, "filename": "a.pdf",
                    "page_number": 2, "chunk_id": 5, "snippet": "text"}]


def test_model_invented_url_not_added(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="q")
    prov = FakeChat([["see https://evil.example/fake for proof"]])
    _drive(eng, prov)
    msg = None
    db = mk_session(eng)
    try:
        msg = db.query(Message).filter(Message.role == MessageRole.assistant).first()
    finally:
        db.close()
    assert msg.citations in (None, "[]", "[]")
    assert "evil.example" not in (msg.citations or "")


def test_citation_dedupe_and_order_deterministic():
    dup = {"marker": "[1]", "document_id": 1, "filename": "a.pdf",
          "page_number": 1, "chunk_id": 5, "snippet": "x"}
    other = {"marker": "[2]", "document_id": 1, "filename": "a.pdf",
            "page_number": 1, "chunk_id": 6, "snippet": "y"}
    out = chat_stream.validate_citations([dup, other, dict(dup)])
    assert len(out) == 2
    assert [c["chunk_id"] for c in out] == [5, 6]  # first-seen order preserved


def test_citation_urls_omitted_this_phase_no_weak_validator():
    # rag_service.build_citation_list never emits a url field — and rather
    # than ship a hand-rolled scheme/hostname blocklist, this phase omits
    # citation URLs entirely (the repo's proven SSRF guard,
    # app.services.browser_agent.assert_url_allowed, does a live DNS
    # resolution and must never run inside worker finalization's atomic
    # staging area — see chat_stream.py's citation-fields comment). Any url
    # on a sealed citation, safe-looking or not, is dropped.
    for maybe_url in ("javascript:alert(1)", "http://localhost/x", "https://example.com/doc"):
        cite = {"marker": "[1]", "document_id": 1, "filename": "a", "page_number": None,
               "chunk_id": 1, "snippet": "s", "url": maybe_url}
        out = chat_stream.validate_citations([cite])
        assert "url" not in out[0], maybe_url


def test_citations_committed_atomically_with_message(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[_CHUNK])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="q")
    prov = FakeChat([["answer [1]"]])
    _drive(eng, prov)
    db = mk_session(eng)
    try:
        msg = db.query(Message).filter(Message.role == MessageRole.assistant).one()
        cites = json.loads(msg.citations)
        assert len(cites) == 1 and cites[0]["filename"] == "chem.pdf"
    finally:
        db.close()


def test_complete_event_returns_committed_citations(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[_CHUNK])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="q")
    prov = FakeChat([["answer [1]"]])
    _drive(eng, prov)
    evs = _events(_stream_frames(eng, ctx, run.id))
    complete = [d for n, d in evs if n == "complete"][0]
    assert complete["sources_used"] is True
    assert complete["citations"][0]["filename"] == "chem.pdf"


def test_reconnect_returns_identical_completion_metadata(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[_CHUNK])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="q")
    prov = FakeChat([["answer [1]"]])
    _drive(eng, prov)
    a = [d for n, d in _events(_stream_frames(eng, ctx, run.id)) if n == "complete"][0]
    b = [d for n, d in _events(_stream_frames(eng, ctx, run.id, after_seq=0)) if n == "complete"][0]
    assert a == b


def test_rollback_creates_no_message_citations_or_complete(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[_CHUNK])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="q")
    from app.run_engine import worker as worker_mod

    class _StaleTransition(Exception):
        pass

    def boom(self, db, *, claimed, frm, to):
        raise worker_mod.RunLeaseLost("stale")

    monkeypatch.setattr(RunWorker, "_apply_transition", boom)
    prov = FakeChat([["answer [1]"]])
    _drive(eng, prov)
    db = mk_session(eng)
    try:
        assert db.query(Message).filter(Message.role == MessageRole.assistant).count() == 0
    finally:
        db.close()


# ═══════════════ 3. Memory flags ══════════════════════════════════════


def test_memories_used_true_only_when_provider_received_memory(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[])
    _patch_memory(monkeypatch, memories=[_FakeMemory()])
    monkeypatch.setattr("app.services.memory_service.format_memories_for_prompt",
                        lambda mems: "MEMBLOCK")
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="q")
    prov = FakeChat([["a"]])
    _drive(eng, prov)
    assert any("MEMBLOCK" in m["content"] for m in prov.calls[0]["messages"])
    evs = _events(_stream_frames(eng, ctx, run.id))
    complete = [d for n, d in evs if n == "complete"][0]
    assert complete["memories_used"] is True


def test_memory_retrieval_failure_remains_false(eng, monkeypatch):
    import app.services.memory_service as memsvc

    def boom(db):
        raise RuntimeError("db down")
    monkeypatch.setattr(memsvc, "get_memory_enabled", boom)
    _patch_rag(monkeypatch, chunks=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="q")   # must not raise
    prov = FakeChat([["a"]])
    _drive(eng, prov)
    evs = _events(_stream_frames(eng, ctx, run.id))
    complete = [d for n, d in evs if n == "complete"][0]
    assert complete["memories_used"] is False


def test_memory_contents_absent_from_refs_and_logs(eng, monkeypatch, caplog):
    _patch_rag(monkeypatch, chunks=[_CHUNK])
    _patch_memory(monkeypatch, memories=[_FakeMemory(content="MEMORY-CANARY")])
    monkeypatch.setattr("app.services.memory_service.format_memories_for_prompt",
                        lambda mems: "Known:\nMEMORY-CANARY")
    _prep(eng)
    ctx = _ctx(eng)
    with caplog.at_level("DEBUG"):
        run, _ = _submit(eng, ctx, message="q")
        prov = FakeChat([["a"]])
        _drive(eng, prov)
    db = mk_session(eng)
    try:
        for s in db.query(RunStep).filter(RunStep.run_id == run.id).all():
            assert "MEMORY-CANARY" not in (s.input_ref or "")
            assert "MEMORY-CANARY" not in (s.output_ref or "")
            assert "MEMORY-CANARY" not in (s.error_code or "")
    finally:
        db.close()
    assert "MEMORY-CANARY" not in caplog.text


# ═══════════════ 4. Marker processing (deferred, fail-closed) ═════════


def test_deferred_marker_never_visible_and_split_across_chunks(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="q")
    marker = "[SAVE_MEMORY: the user likes tea]"
    prov = FakeChat([["clean start. ", marker[:5], marker[5:15], marker[15:], "\nMORE"]])
    _drive(eng, prov)
    db = mk_session(eng)
    try:
        run_row = db.query(Run).filter(Run.id == run.id).first()
        assert run_row.status == "failed"
        assert run_row.status_reason == "nonexec_marker_deferred"
        assert db.query(Message).filter(Message.role == MessageRole.assistant).count() == 0
    finally:
        db.close()
    evs = _events(_stream_frames(eng, ctx, run.id))
    blob = json.dumps(evs)
    assert "SAVE_MEMORY" not in blob and "likes tea" not in blob


def test_clean_prefix_before_deferred_marker_still_streamed(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="q")
    prov = FakeChat([["Here is the answer. ", "[SAVE_MEMORY: x]"]])
    _drive(eng, prov)
    evs = _events(_stream_frames(eng, ctx, run.id))
    toks = "".join(d.get("token", "") for n, d in evs if n == "token")
    assert toks == "Here is the answer. "


def test_deferred_reason_distinct_from_execution_marker_reason(eng, monkeypatch):
    # driven strictly sequentially — see test_cross_user_document_not_retrieved
    # for why (advance_chat_runs() batches every claimable run in one sweep).
    _patch_rag(monkeypatch, chunks=[])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run1, _ = _submit(eng, ctx, message="a", key="k1")
    _drive(eng, FakeChat([["x", "[SAVE_DIRECTIVE: always be terse]"]]))
    run2, _ = _submit(eng, ctx, message="b", key="k2")
    _drive(eng, FakeChat([["y", "[PYTHON_CODE: import os]"]]))
    db = mk_session(eng)
    try:
        r1 = db.query(Run).filter(Run.id == run1.id).first()
        r2 = db.query(Run).filter(Run.id == run2.id).first()
        assert r1.status_reason == "nonexec_marker_deferred"
        assert r2.status_reason == "tool_resume_required"
    finally:
        db.close()


def test_execution_markers_unchanged(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="use tool")
    marker = f'[MCP_CALL: {TOOL} {{"a": 1}}]'
    prov = FakeChat([_mk_marker(marker), ["done"]])
    _drive(eng, prov, Transport())
    aid = _run_row(eng, run.id).pending_approval_id
    assert aid is not None, "MCP capture-and-suspend must be unaffected"


# ═══════════════ 5. Crash / recovery ═══════════════════════════════════


def test_finalization_cas_still_creates_one_message(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[_CHUNK])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="q")
    prov = FakeChat([["answer [1]"]])
    _drive(eng, prov)
    _drive(eng, prov)  # a second sweep of an already-terminal run: no-op
    db = mk_session(eng)
    try:
        assert db.query(Message).filter(Message.role == MessageRole.assistant).count() == 1
        assert db.query(Run).filter(Run.id == run.id).one().status == "succeeded"
    finally:
        db.close()


def test_unreadable_context_snapshot_fails_closed_no_message(eng, monkeypatch):
    # the SAME sealed payload build_chat_input already decrypted successfully
    # earlier this run (to even reach generation) — an unreadable read at
    # finalization is a genuine anomaly (corrupt encrypted state), not "no
    # context was ever sealed". Must fail closed, never silently succeed
    # with fabricated-empty citations.
    _patch_rag(monkeypatch, chunks=[])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="q")
    monkeypatch.setattr(chat_stream, "read_context_snapshot",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("corrupt")))
    prov = FakeChat([["a real answer anyway"]])
    _drive(eng, prov)
    db = mk_session(eng)
    try:
        assert db.query(Message).filter(Message.role == MessageRole.assistant).count() == 0
        run_row = db.query(Run).filter(Run.id == run.id).first()
        assert run_row.status == "failed"
        assert run_row.status_reason == "context_snapshot_unreadable"
    finally:
        db.close()
    evs = _events(_stream_frames(eng, ctx, run.id))
    errs = [d for n, d in evs if n == "error"]
    assert errs and "corrupt" not in json.dumps(errs)  # bounded code only, no raw exception text


# ═══════════════ 6. Security ═══════════════════════════════════════════


def test_revoked_session_before_provider_invocation_fails_closed(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[_CHUNK])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="q")
    from tests.run_engine_helpers import revoke_family
    db = mk_session(eng)
    try:
        revoke_family(db, "fam-d2")
    finally:
        db.close()
    prov = FakeChat([["a"]])
    _drive(eng, prov)
    assert prov.calls == [], "provider must never be invoked once the session is revoked"
    db = mk_session(eng)
    try:
        assert db.query(Run).filter(Run.id == run.id).one().status == "failed"
    finally:
        db.close()


def test_no_direct_executor_or_paid_provider_call_during_submission(eng, monkeypatch):
    calls = {"n": 0}

    def spy(*a, **k):
        calls["n"] += 1
        raise AssertionError("provider must never be invoked from resolve_and_submit")
    import app.services.llm_providers as llmp
    monkeypatch.setattr(llmp, "get_provider", spy)
    _patch_rag(monkeypatch, chunks=[_CHUNK])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    _submit(eng, _ctx(eng), message="q")   # must not touch get_provider at all
    assert calls["n"] == 0


def test_canary_absent_from_refs_events_status_logs(eng, monkeypatch, caplog):
    chunk = dict(_CHUNK, text="RAG-PASSAGE-CANARY")
    _patch_rag(monkeypatch, chunks=[chunk])
    _patch_memory(monkeypatch, memories=[_FakeMemory(content="MEM-CANARY")])
    monkeypatch.setattr("app.services.memory_service.format_memories_for_prompt",
                        lambda mems: "MEM-CANARY-BLOCK")
    _prep(eng)
    ctx = _ctx(eng)
    with caplog.at_level("DEBUG"):
        run, _ = _submit(eng, ctx, message="PROMPT-CANARY")
        prov = FakeChat([["clean answer [1]"]])
        _drive(eng, prov)
        frames = _stream_frames(eng, ctx, run.id)
    db = mk_session(eng)
    try:
        for s in db.query(RunStep).filter(RunStep.run_id == run.id).all():
            for field in (s.input_ref, s.output_ref, s.error_code):
                blob = field or ""
                assert "RAG-PASSAGE-CANARY" not in blob
                assert "MEM-CANARY" not in blob
                assert "PROMPT-CANARY" not in blob
        run_row = db.query(Run).filter(Run.id == run.id).first()
        assert "RAG-PASSAGE-CANARY" not in (run_row.status_reason or "")
    finally:
        db.close()
    # the RAG passage snippet is SUPPOSED to reach the authenticated complete
    # event (that's what a citation is) — assert it does, as the positive half
    # of this test, while memory content and the raw user prompt must not.
    evs = _events(frames)
    complete = [d for n, d in evs if n == "complete"][0]
    assert "RAG-PASSAGE-CANARY" in json.dumps(complete["citations"])
    non_citation_wire = json.dumps([d for n, d in evs if n != "complete"])
    assert "RAG-PASSAGE-CANARY" not in non_citation_wire
    assert "MEM-CANARY" not in json.dumps(evs)
    assert "PROMPT-CANARY" not in json.dumps(evs)
    log_blob = caplog.text
    assert "MEM-CANARY" not in log_blob
    assert "PROMPT-CANARY" not in log_blob


# ═══════════════ 7. Closeout corrections ═══════════════════════════════
# Context-snapshot versioning, passage/citation one-to-one correspondence,
# and fail-closed idempotency pre-check behavior.


_BAD_CHUNK = {
    "text": "SHOULD-NEVER-REACH-PROVIDER", "document_id": 43, "filename": None,
    "page_number": None, "chunk_db_id": 100, "chroma_id": "c2", "distance": 0.2,
}


def _read_snapshot_for(eng, run_id):
    db = mk_session(eng)
    try:
        run_row = db.query(Run).filter(Run.id == run_id).first()
        seq1 = (db.query(RunStep).filter(RunStep.run_id == run_id)
                .order_by(RunStep.seq.asc()).first())
        return chat_stream.read_context_snapshot(db, run_row, seq1)
    finally:
        db.close()


def test_every_fresh_submission_seals_context_snapshot_version(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[_CHUNK])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    run, _ = _submit(eng, _ctx(eng), message="q")
    snap = _read_snapshot_for(eng, run.id)
    assert snap["context_snapshot_version"] == chat_stream.CONTEXT_SNAPSHOT_VERSION


def test_empty_retrieval_seals_valid_versioned_empty_snapshot(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    run, _ = _submit(eng, _ctx(eng), message="q")
    assert _read_snapshot_for(eng, run.id) == chat_stream.build_context_snapshot()


def test_unknown_context_version_fails_closed(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    run, _ = _submit(eng, _ctx(eng), message="q")
    # the run's context was sealed with the CURRENT version; simulate a
    # future version bump making it look unknown/stale on read.
    monkeypatch.setattr(chat_stream, "CONTEXT_SNAPSHOT_VERSION", 999)
    with pytest.raises(chat_stream.ContextSnapshotError):
        _read_snapshot_for(eng, run.id)


def test_present_malformed_context_fails_closed(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    # simulate a future regression that bypasses build_context_snapshot's own
    # safety and seals a malformed-but-versioned dict.
    monkeypatch.setattr(chat_stream, "build_context_snapshot",
                        lambda **kw: {"context_snapshot_version": chat_stream.CONTEXT_SNAPSHOT_VERSION,
                                      "citations": "not-a-list", "sources_used": False,
                                      "rag_context_text": None, "memory_context_text": None,
                                      "memories_used": False})
    run, _ = _submit(eng, _ctx(eng), message="q")
    with pytest.raises(chat_stream.ContextSnapshotError):
        _read_snapshot_for(eng, run.id)


def test_genuinely_absent_context_key_is_narrow_backward_compat(monkeypatch):
    # simulates a genuinely pre-MP-3C3D2 sealed payload: no "context" key at
    # all in the parsed body (not merely null — the key was never written).
    monkeypatch.setattr(chat_stream, "_open_submission_body",
                        lambda db, run, step: {"message": "hi", "exec": {}})
    assert chat_stream.read_context_snapshot(None, None, None) == (
        chat_stream.build_context_snapshot()
    )


def test_direct_run_api_no_retrieval_seals_valid_empty_snapshot(eng):
    # mirrors routes/runs.py's call exactly: create_chat_run with NO context
    # argument at all (the direct Run public API does no RAG/memory
    # retrieval) — must still seal a valid versioned empty snapshot, not a
    # bare None.
    _prep(eng)
    ctx = _ctx(eng)
    db = mk_session(eng)
    try:
        submission_obj = sub.build_chat_submission(
            session_id=1, message="direct api q", image_url=None, model="model-x",
            mode="chat", system_prompt="SP", provider=settings.llm_provider,
        )
        run, created = sub.create_chat_run(db, ctx, submission=submission_obj,
                                           idempotency_key="direct-api-key")
    finally:
        db.close()
    assert created is True
    assert _read_snapshot_for(eng, run.id) == chat_stream.build_context_snapshot()


def test_invalid_citation_metadata_drops_passage_before_provider(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[_CHUNK, _BAD_CHUNK])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="q")
    prov = FakeChat([["answer [1]"]])
    _drive(eng, prov)
    blob = json.dumps(prov.calls[0]["messages"])
    assert "SHOULD-NEVER-REACH-PROVIDER" not in blob
    assert "melting point of gold" in blob      # the valid chunk still reaches the provider
    snap = _read_snapshot_for(eng, run.id)
    assert len(snap["citations"]) == 1
    assert snap["citations"][0]["filename"] == "chem.pdf"


def test_duplicate_chunks_produce_deterministic_matching_context(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[_CHUNK, dict(_CHUNK)])   # same document_id+chunk_db_id
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="q")
    snap = _read_snapshot_for(eng, run.id)
    assert len(snap["citations"]) == 1
    assert snap["rag_context_text"].count("melting point of gold") == 1


def test_build_context_snapshot_enforces_truthfulness_invariant():
    claimed_but_uncited = chat_stream.build_context_snapshot(
        rag_context_text="some text", citations=[], sources_used=True)
    assert claimed_but_uncited["sources_used"] is False
    assert claimed_but_uncited["rag_context_text"] is None

    claimed_but_no_text = chat_stream.build_context_snapshot(memories_used=True)
    assert claimed_but_no_text["memories_used"] is False

    good_cite = {"marker": "[1]", "document_id": 1, "filename": "a", "page_number": None,
                "chunk_id": 1, "snippet": "s"}
    honest = chat_stream.build_context_snapshot(
        rag_context_text="text", citations=[good_cite], sources_used=True,
        memory_context_text="mem", memories_used=True)
    assert honest["sources_used"] is True and honest["memories_used"] is True
    assert honest["context_snapshot_version"] == chat_stream.CONTEXT_SNAPSHOT_VERSION


def test_final_message_citations_exactly_match_selected_context(eng, monkeypatch):
    _patch_rag(monkeypatch, chunks=[_CHUNK, _BAD_CHUNK])
    _patch_memory(monkeypatch, memories=[])
    _prep(eng)
    ctx = _ctx(eng)
    run, _ = _submit(eng, ctx, message="q")
    prov = FakeChat([["answer [1]"]])
    _drive(eng, prov)
    snap = _read_snapshot_for(eng, run.id)
    db = mk_session(eng)
    try:
        msg = db.query(Message).filter(Message.role == MessageRole.assistant).one()
        final_cites = json.loads(msg.citations)
    finally:
        db.close()
    assert final_cites == snap["citations"]


def test_precheck_db_failure_zero_retrieval_zero_rows(eng, monkeypatch):
    calls = {"rag": 0, "mem": 0}
    import app.services.rag_service as rag
    import app.services.memory_service as memsvc

    def counting_rag(*a, **k):
        calls["rag"] += 1
        return []

    def counting_mem_enabled(db):
        calls["mem"] += 1
        return False

    monkeypatch.setattr(rag, "retrieve_chunks", counting_rag)
    monkeypatch.setattr(memsvc, "get_memory_enabled", counting_mem_enabled)

    def boom(*a, **k):
        raise RuntimeError("db connection lost")

    # patch the name resolve_and_submit actually resolves — its own module's
    # imported local binding, not app.run_engine.engine's own attribute.
    monkeypatch.setattr(ms, "_select_by_idem_scope", boom)

    _prep(eng)
    ctx = _ctx(eng)
    db = mk_session(eng)
    try:
        with pytest.raises(ms.DurableStreamError) as ei:
            ms.resolve_and_submit(db, ctx, session_id=1, message="q", idempotency_key="k1")
    finally:
        db.close()
    assert ei.value.http_status == 503
    assert calls["rag"] == 0 and calls["mem"] == 0
    db = mk_session(eng)
    try:
        assert db.query(Run).count() == 0
    finally:
        db.close()


def test_same_key_conflict_zero_retrieval_and_409(eng, monkeypatch):
    rag = _patch_rag(monkeypatch, chunks=[_CHUNK])
    _patch_memory(monkeypatch, memories=[])
    calls = {"n": 0}
    orig = rag.retrieve_chunks

    def counting(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)
    monkeypatch.setattr(rag, "retrieve_chunks", counting)

    _prep(eng)
    ctx = _ctx(eng)
    key = "conflict-key"
    _submit(eng, ctx, message="first message", key=key)
    assert calls["n"] == 1
    db = mk_session(eng)
    try:
        with pytest.raises(ms.DurableStreamError) as ei:
            ms.resolve_and_submit(db, ctx, session_id=1, message="DIFFERENT message",
                                  idempotency_key=key)
    finally:
        db.close()
    assert ei.value.http_status == 409
    assert calls["n"] == 1   # the conflicting attempt never re-ran retrieval


def test_precheck_failure_exception_text_absent_from_logs(eng, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("DB-ERROR-CANARY: connection string user=admin pass=hunter2")
    monkeypatch.setattr(ms, "_select_by_idem_scope", boom)
    _prep(eng)
    ctx = _ctx(eng)
    db = mk_session(eng)
    try:
        with caplog.at_level("DEBUG"):
            with pytest.raises(ms.DurableStreamError) as ei:
                ms.resolve_and_submit(db, ctx, session_id=1, message="q",
                                      idempotency_key="k2")
    finally:
        db.close()
    assert "DB-ERROR-CANARY" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "DB-ERROR-CANARY" not in ei.value.detail
    assert ei.value.http_status == 503
