"""MP-4C - model/pipeline-directed Knowledge Graph ingestion must NOT be SCHEDULED.

TEST-ONLY. Two production callers used to schedule KG ingestion automatically:

  * streaming_service.prepare_chat_context - the chat pipeline's deep-research
    step (fired on every deep-research turn).
  * langgraph_workflow.deep_research - the graph node.

Both resolved, at call time, the exact name
app.services.knowledge_graph.ingest_text_async, which spawns a daemon thread
that extracts entities and writes graph_entities / graph_relations rows.

These tests replace that exact lookup with a loud synchronous counting seam.
The replacement never spawns a thread, so a call count of zero proves ingestion
was NOT SCHEDULED before any worker thread existed. It is NOT proof that no DB
write completed (there is no background thread to observe) and it does not
replace the authenticated document-upload control below, which is the only
remaining production caller and still writes rows through the real thread path.

Fixture reachability is proven independently inside each security test (the
patched run_deep_research ran exactly once and its return value reached the
caller's output), so a passing zero-call assertion cannot be vacuous.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.models.models import GraphEntity, GraphRelation, ResearchSession
from app.services import streaming_service as ss
from tests.run_engine_helpers import ensure_user, mk_engine, mk_session

RESEARCH_MARKER = "MP4C-KG-RESEARCH-CONTEXT-MARKER"


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


# -- The seam ------------------------------------------------


def _install_kg_seam(monkeypatch):
    """Replace the EXACT module attribute both callers resolve at call time.

    Both call sites do "from app.services.knowledge_graph import
    ingest_text_async" *inside* the function, so patching the module attribute
    is the lookup they perform. The seam is synchronous and records every call;
    it never touches the DB and never starts a thread.
    """
    calls = []

    def _seam(user_id, text, source=""):
        calls.append((user_id, text, source))
        return None

    monkeypatch.setattr("app.services.knowledge_graph.ingest_text_async", _seam)
    return calls


def _mock_streaming_research(monkeypatch, context):
    """Drive prepare_chat_context's deep-research step with a real context."""
    from importlib import import_module

    dr = import_module("app.tools.deep_research")     # real module, not app.tools attr
    ws = import_module("app.tools.web_scraper")
    calls = []

    def fake_run(query):
        calls.append(query)
        return context

    monkeypatch.setattr(dr, "run_deep_research", fake_run)
    monkeypatch.setattr(ws, "extract_urls", lambda text: [])   # no URL short-circuit
    monkeypatch.setattr(settings, "enable_deep_research", True)
    return calls


def _mock_graph_research(monkeypatch, context):
    """Drive langgraph_workflow.deep_research with a real context."""
    from app.services import langgraph_workflow as lw

    calls = []

    def fake_run(query):
        calls.append(query)
        return context

    # Both are module-level names in langgraph_workflow (imported at load time).
    monkeypatch.setattr(lw, "run_deep_research", fake_run)
    monkeypatch.setattr(lw, "detect_research_task", lambda text: True)
    monkeypatch.setattr(settings, "enable_deep_research", True)
    return calls


# -- Positive control for the seam mechanism (durable) -------


def test_kg_seam_intercepts_the_exact_lookup_the_callers_resolve(monkeypatch):
    """Proves the patch target is live: the same import both call sites use."""
    calls = _install_kg_seam(monkeypatch)

    from app.services.knowledge_graph import ingest_text_async  # noqa: PLC0415

    ingest_text_async(7, "payload", "deep_research")
    assert len(calls) == 1, calls
    assert calls[0] == (7, "payload", "deep_research")


# -- Security contract: streaming pipeline -------------------


def test_streaming_deep_research_step_schedules_no_kg_ingest(eng, monkeypatch):
    calls = _install_kg_seam(monkeypatch)
    dr_calls = _mock_streaming_research(monkeypatch, RESEARCH_MARKER)

    db = mk_session(eng)
    try:
        ensure_user(db, 7)
        db.add(ResearchSession(id=1, user_id=7, title="t", model="model-x"))
        db.commit()
        ctx = ss.prepare_chat_context(
            session_id=1, user_input="hello there", db=db, user_id=7,
            mode="deep_research",
        )
    finally:
        db.close()

    # Reachability: the deep-research step really ran and its context reached
    # the caller's output (the statement immediately preceding the former KG
    # call), so the zero-call assertion below is not vacuous.
    assert dr_calls == ["hello there"], dr_calls
    assert RESEARCH_MARKER in ctx.system_prompt
    assert ctx.mode == "deep_research"

    # SECURITY CONTRACT - scheduling only.
    assert calls == [], (
        "model/pipeline-directed KG ingestion was SCHEDULED; observed=%r" % (calls,)
    )


# -- Security contract: langgraph deep_research node ---------


def test_graph_deep_research_node_schedules_no_kg_ingest(monkeypatch):
    from app.services import langgraph_workflow as lw

    calls = _install_kg_seam(monkeypatch)
    dr_calls = _mock_graph_research(monkeypatch, RESEARCH_MARKER)

    state = {"user_input": "why is the sky blue", "user_id": 7}
    out = lw.deep_research(state)

    # Reachability: the node produced the context at the exact site.
    assert dr_calls == ["why is the sky blue"], dr_calls
    assert out["deep_research_context"] == RESEARCH_MARKER
    assert out["deep_research_used"] is True

    # SECURITY CONTRACT - scheduling only.
    assert calls == [], (
        "the langgraph deep_research node SCHEDULED KG ingestion; observed=%r"
        % (calls,)
    )


# -- Explicit authenticated upload path is PRESERVED ---------


def test_authenticated_document_upload_still_persists_kg_rows(monkeypatch):
    """The one remaining production KG writer keeps working.

    Deterministic and SYNCHRONIZED: knowledge_graph.threading is replaced so
    the fire-and-forget worker is DEFERRED and run explicitly after the upload
    request commits (no daemon race), and the extractor returns a fixed payload.
    Rows are then asserted in the app's own DB - this is completed-write proof,
    unlike the scheduling-only tests above.
    """
    from importlib import import_module

    from fastapi.testclient import TestClient

    from app.database import SessionLocal
    from app.main import app
    from app.models.models import (
        Document,
        DocumentChunk,
        GraphEntity,
        GraphRelation,
        User,
    )
    from app.services import entity_extractor as ee
    from app.services import knowledge_graph as kg
    from app.services.rate_limiter import get_rate_limiter
    from tests.auth_helpers import auth_headers, register_and_login

    ec = import_module("app.services.embeddings_client")
    docs_route = import_module("app.routes.documents")

    marker = "MP4C-UPLOAD-ENTITY-MARKER"
    payload = {
        "entities": [
            {"name": marker, "type": "concept"},
            {"name": marker + "-PEER", "type": "concept"},
        ],
        "relations": [
            {"source": marker, "target": marker + "-PEER", "relation": "linked to"}
        ],
    }

    monkeypatch.setattr(ee, "extract_entities", lambda text: payload)
    monkeypatch.setattr(
        ec, "generate_embeddings_batch", lambda texts: [[0.1] * 8 for _ in texts]
    )
    monkeypatch.setattr(docs_route, "add_chunks", lambda **kw: None)

    real_threading = kg.threading
    pending = []

    class _DeferredThread:
        """Record the worker; the test runs it after the request commits."""

        def __init__(self, target=None, name=None, daemon=None, **kw):
            self._target = target

        def start(self):
            if self._target is not None:
                pending.append(self._target)

    class _ThreadingShim:
        Thread = _DeferredThread

        def __getattr__(self, item):
            return getattr(real_threading, item)

    monkeypatch.setattr(kg, "threading", _ThreadingShim())

    get_rate_limiter().reset()
    uid = sid = None
    try:
        with TestClient(app) as c:
            uid, token = register_and_login(c, username="mp4c_kg_uploader")
            h = auth_headers(token)
            r = c.post("/api/sessions", json={"title": "kg"}, headers=h)
            assert r.status_code == 201, r.text
            sid = r.json()["id"]

            body = (("Alpha concept linked to Beta concept. ") * 20).encode()
            up = c.post(
                f"/api/sessions/{sid}/documents",
                files={"file": ("kg.txt", body, "text/plain")},
                headers=h,
            )
            assert up.status_code == 201, up.text

        # Synchronized completion: the upload route scheduled the KG worker
        # before committing; run it now that the request's write txn is gone.
        assert pending, "the upload route did not schedule a KG worker"
        for _worker in list(pending):
            _worker()

        db = SessionLocal()
        try:
            names = sorted(
                e.name
                for e in db.query(GraphEntity)
                .filter(GraphEntity.user_id == uid)
                .all()
            )
            assert marker in names, names
            rels = (
                db.query(GraphRelation)
                .filter(GraphRelation.user_id == uid)
                .all()
            )
            assert len(rels) >= 1, "no KG relation persisted by the upload path"
        finally:
            db.close()
    finally:
        db = SessionLocal()
        try:
            if uid is not None:
                db.query(GraphRelation).filter(
                    GraphRelation.user_id == uid
                ).delete(synchronize_session=False)
                db.query(GraphEntity).filter(
                    GraphEntity.user_id == uid
                ).delete(synchronize_session=False)
                if sid is not None:
                    doc_ids = [
                        d.id
                        for d in db.query(Document)
                        .filter(Document.session_id == sid)
                        .all()
                    ]
                    if doc_ids:
                        db.query(DocumentChunk).filter(
                            DocumentChunk.document_id.in_(doc_ids)
                        ).delete(synchronize_session=False)
                    db.query(Document).filter(
                        Document.session_id == sid
                    ).delete(synchronize_session=False)
                    db.query(ResearchSession).filter(
                        ResearchSession.id == sid
                    ).delete(synchronize_session=False)
                u = db.query(User).filter(User.id == uid).first()
                if u is not None:
                    db.delete(u)
                db.commit()
        finally:
            db.close()


# -- Read-only [KG_QUERY] path is PRESERVED ------------------


def test_kg_query_injection_reads_but_writes_no_rows(eng, monkeypatch):
    """[KG_QUERY] pulls a subgraph; it must never write entities/relations."""
    from app.services import knowledge_graph as kg

    writes = []

    def _ingest_extraction(db, user_id, data):
        writes.append(("ingest_extraction", user_id))
        return 0

    def _ingest_text_async(*a, **k):
        writes.append(("ingest_text_async", a))
        return None

    monkeypatch.setattr(kg, "ingest_extraction", _ingest_extraction)
    monkeypatch.setattr(
        "app.services.knowledge_graph.ingest_text_async", _ingest_text_async
    )

    db = mk_session(eng)
    try:
        ensure_user(db, 7)
        kg.add_entity(db, 7, "Python", "language")
        db.commit()

        blocks = kg.inject_kg_context(db, 7, "tell me about [KG_QUERY: python]")

        # The read really ran: the seeded entity came back.
        assert isinstance(blocks, list) and len(blocks) == 1, blocks
        assert any("Python" in b for b in blocks), blocks
        # ... and it wrote nothing and created no new rows.
        assert writes == [], writes
        assert db.query(GraphEntity).count() == 1
        assert db.query(GraphRelation).count() == 0
    finally:
        db.close()
