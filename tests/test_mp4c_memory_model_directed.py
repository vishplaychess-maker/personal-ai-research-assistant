"""MP-4C — model-directed memory: streaming automatic-extraction path.

TEST-ONLY. Drives the real entry point streaming_service.run_memory_extraction
with three verified seams so the extraction candidate reaches the write site.
The streaming test encodes the REQUIRED security contract (zero writes); the
other two are first-run GREEN controls/leak checks.
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from app.models.models import Memory
from app.services import memory_service as ms
from app.services import settings_service, streaming_service
from tests.run_engine_helpers import mk_engine, mk_session

CANDIDATE = "The user prefers dark roast coffee in the morning"


class _Resp:
    status_code = 200
    text = "Ollama is running"


class _Client:
    """Stub for memory_service.httpx.Client — defeats the Ollama ping guard."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        return _Resp()


class _Provider:
    """Stub extraction provider — returns the exact JSON keys the call site reads
    (memory_service.py:412 should_save, :416 content, :417 category)."""

    def generate_json_response(self, messages=None, system_prompt=None):
        return json.dumps({"should_save": True, "content": CANDIDATE,
                           "category": "fact"})


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


def _seams(monkeypatch):
    calls = []

    def _seam(db, user_id, content, category, session_id=None):
        calls.append((content, category))
        return None          # never writes a row

    monkeypatch.setattr(ms.httpx, "Client", _Client)
    monkeypatch.setattr(ms, "get_provider", lambda: _Provider())
    monkeypatch.setattr(ms, "_save_memory_if_new", _seam)
    return calls


def test_streaming_run_memory_extraction_zero_model_directed_writes(eng, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    db = mk_session(eng)
    try:
        settings_service.set_memory_enabled(db, True)
        calls = _seams(monkeypatch)
        streaming_service.run_memory_extraction("I love dark roast coffee", db, 1, 7)
        assert len(calls) == 0, (
            "security contract: automatic model-directed extraction must NOT reach "
            "the memory write boundary; observed calls=%d %r" % (len(calls), calls))
    finally:
        db.close()


def test_explicit_save_memory_still_persists_once(eng, monkeypatch):
    """User-initiated writes must remain unaffected by the automatic-path gate."""
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    db = mk_session(eng)
    try:
        settings_service.set_memory_enabled(db, True)
        row = ms.save_memory(db, 7, "Explicit user preference: decaf",
                             category="preference", session_id=1)
        assert row is not None and row.id is not None, "explicit save must persist"
        assert db.query(Memory).count() == 1
    finally:
        db.close()


def test_automatic_result_carries_no_candidate_text(eng, monkeypatch):
    """The automatic result must not leak candidate text or category."""
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    db = mk_session(eng)
    try:
        settings_service.set_memory_enabled(db, True)
        calls = _seams(monkeypatch)
        res = ms.extract_memory_from_message("I love dark roast coffee", db, 7, 1)
        assert res.saved is False and res.memory_id is None
        assert res.content is None and res.category is None
        assert CANDIDATE not in (res.reason or "")
        assert calls == [], "automatic extraction must not reach the write boundary"
        assert db.query(Memory).count() == 0
    finally:
        db.close()


def test_graph_extract_memory_node_cannot_persist_a_candidate(eng, monkeypatch):
    """The REAL langgraph_workflow.extract_memory node (not a copy of its logic).

    The node imports extract_memory_from_message directly, but that function
    resolves _save_memory_if_new / get_provider / httpx from memory_service's
    module globals, so the same seams apply.
    """
    from app.services import langgraph_workflow as lw

    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    db = mk_session(eng)
    try:
        settings_service.set_memory_enabled(db, True)
        calls = _seams(monkeypatch)
        state = {"db": db, "user_input": "I love dark roast coffee",
                 "user_id": 7, "session_id": 1}
        out = lw.extract_memory(state)
        res = out["extraction_result"]

        # PROOF the fixture reached the candidate-processing path instead of
        # returning early: this exact reason is produced ONLY at the former write
        # boundary, AFTER every early-return guard (disabled / sensitive_input /
        # ollama_unavailable / invalid_json / nothing_to_save / too_short /
        # sensitive_content).
        assert res.get("reason") == "automatic_write_requires_authorization", res
        assert res.get("saved") is False and res.get("memory_id") is None
        assert res.get("category") is None
        assert calls == [], "the graph node must not reach the memory write boundary"
        assert db.query(Memory).count() == 0
        blob = json.dumps(res, default=str)
        assert CANDIDATE not in blob, blob
        assert "Traceback" not in blob and "memory_service" not in blob
    finally:
        db.close()
