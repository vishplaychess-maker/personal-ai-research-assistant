"""Mem0 semantic layer over the existing approval-gated memory system.

All Mem0 calls are faked; the DB tests run against the suite's isolated SQLite
database. No Ollama/Qdrant call is made here.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import Memory, User
from app.services import mem0_service, memory_service


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "memory-service-hmac-01")
    for flag in (
        "run_messages_stream_durable_enabled",
        "run_engine_enabled",
        "run_worker_enabled",
        "run_chat_stream_enabled",
        "run_public_api_enabled",
        "run_deep_research_enabled",
        "run_multi_agent_enabled",
    ):
        monkeypatch.setattr(settings, flag, False)
    from app.services.rate_limiter import reset_rate_limiter

    reset_rate_limiter()
    yield


@pytest.fixture
def db():
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _user(db, prefix="mem") -> User:
    user = User(
        username=f"{prefix}_{uuid.uuid4().hex[:10]}",
        email=f"{prefix}-{uuid.uuid4().hex[:8]}@example.com",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _memory(db, user, content, *, age_seconds=0, category="fact") -> Memory:
    row = Memory(
        user_id=user.id,
        content=content,
        category=category,
        last_used_at=datetime.utcnow() - timedelta(seconds=age_seconds),
        last_accessed_at=datetime.utcnow() - timedelta(seconds=age_seconds),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ── retrieval ──────────────────────────────────────────────


def test_semantic_ids_drive_retrieval_and_stay_user_scoped(db, monkeypatch):
    alice = _user(db, "alice")
    bob = _user(db, "bob")
    older = _memory(db, alice, "My project is HireShade.", age_seconds=100,
                    category="project_context")
    newer = _memory(db, alice, "My favorite language is Rust.", age_seconds=10,
                    category="preference")
    bob_row = _memory(db, bob, "Bob's project is Secret.", age_seconds=5,
                      category="project_context")
    calls = []

    monkeypatch.setattr(memory_service, "get_memory_enabled", lambda _db: True)

    def fake_search(user_id, query, limit=3):
        calls.append((user_id, query, limit))
        return [older.id, newer.id]

    monkeypatch.setattr(mem0_service, "search_memory_ids", fake_search)

    rows = memory_service.retrieve_relevant_memories(
        db, user_id=alice.id, query="What is my project name?", max_results=3
    )

    assert [row.id for row in rows] == [older.id, newer.id]
    assert bob_row.id not in {row.id for row in rows}
    assert calls == [(alice.id, "What is my project name?", 3)]

    # The other user's semantic hits can never pull Alice's rows.
    monkeypatch.setattr(
        mem0_service, "search_memory_ids", lambda uid, q, limit=3: [bob_row.id]
    )
    bob_rows = memory_service.retrieve_relevant_memories(
        db, user_id=bob.id, query="project", max_results=3
    )
    assert [row.id for row in bob_rows] == [bob_row.id]


def test_semantic_miss_falls_back_to_recency(db, monkeypatch):
    user = _user(db)
    old = _memory(db, user, "old fact", age_seconds=100)
    new = _memory(db, user, "new fact", age_seconds=1)

    monkeypatch.setattr(memory_service, "get_memory_enabled", lambda _db: True)
    monkeypatch.setattr(
        mem0_service, "search_memory_ids", lambda uid, q, limit=3: []
    )

    rows = memory_service.retrieve_relevant_memories(
        db, user_id=user.id, query="anything", max_results=3
    )

    assert [row.id for row in rows] == [new.id, old.id]


def test_memory_disabled_skips_both_search_and_rows(db, monkeypatch):
    user = _user(db)
    _memory(db, user, "a fact")
    called = {"search": False}

    def fake_search(*_args, **_kwargs):
        called["search"] = True
        return []

    monkeypatch.setattr(memory_service, "get_memory_enabled", lambda _db: False)
    monkeypatch.setattr(mem0_service, "search_memory_ids", fake_search)

    assert memory_service.retrieve_relevant_memories(
        db, user_id=user.id, query="x"
    ) == []
    assert called["search"] is False


# ── Mem0 client behavior ───────────────────────────────────


class _FakeClient:
    def __init__(self, *, search_exc=None, rows=None):
        self.search_exc = search_exc
        self.rows = rows or []
        self.search_calls = []
        self.add_calls = []
        self.deleted = []

    def search(self, query, **kwargs):
        self.search_calls.append((query, kwargs))
        if self.search_exc:
            raise self.search_exc
        return {"results": self.rows}

    def get_all(self, **kwargs):
        return {"results": list(self.rows)}

    def add(self, text, **kwargs):
        self.add_calls.append((text, kwargs))
        return {"results": [{"id": "new"}]}

    def delete(self, memory_id):
        self.deleted.append(memory_id)


def test_search_failure_returns_empty(monkeypatch):
    fake = _FakeClient(search_exc=RuntimeError("down"))
    monkeypatch.setattr(mem0_service, "_get_client", lambda: fake)

    assert mem0_service.search_memory_ids(1, "query") == []


def test_delete_is_owner_scoped(monkeypatch):
    fake = _FakeClient(
        rows=[
            {"id": "mine", "user_id": "1", "metadata": {"db_id": 1}},
            {"id": "theirs", "user_id": "2", "metadata": {"db_id": 2}},
        ]
    )
    monkeypatch.setattr(mem0_service, "_get_client", lambda: fake)

    assert mem0_service.delete_memory(1, "mine") is True
    assert fake.deleted == ["mine"]
    assert mem0_service.delete_memory(1, "theirs") is False
    assert fake.deleted == ["mine"]


def test_index_removes_stale_mirror_and_skips_inference(monkeypatch):
    fake = _FakeClient(
        rows=[{"id": "old", "user_id": "7", "metadata": {"db_id": 42}}]
    )
    monkeypatch.setattr(mem0_service, "_get_client", lambda: fake)

    assert mem0_service.index_memory(7, 42, "My project is HireShade", "project_context")

    assert fake.deleted == ["old"]
    text, kwargs = fake.add_calls[-1]
    assert text == "My project is HireShade"
    assert kwargs["infer"] is False
    assert kwargs["user_id"] == "7"
    assert kwargs["metadata"]["db_id"] == 42


def test_index_failure_is_swallowed(monkeypatch):
    class _Broken(_FakeClient):
        def add(self, *_args, **_kwargs):
            raise RuntimeError("qdrant down")

    monkeypatch.setattr(mem0_service, "_get_client", lambda: _Broken())
    assert mem0_service.index_memory(1, 2, "text") is False


def test_telemetry_is_disabled_before_mem0_import():
    assert os.environ.get("MEM0_TELEMETRY", "").lower() in ("false", "0", "no")


# ── write mirroring ────────────────────────────────────────


def test_save_memory_mirrors_the_approved_row(db, monkeypatch):
    user = _user(db)
    calls = []
    monkeypatch.setattr(
        mem0_service,
        "index_memory",
        lambda user_id, db_id, content, category: calls.append(
            (user_id, db_id, content, category)
        )
        or True,
    )

    row = memory_service.save_memory(
        db, user.id, "My project is HireShade", "project_context"
    )

    assert row is not None
    assert calls == [(user.id, row.id, "My project is HireShade", "project_context")]


def test_save_memory_does_not_mirror_sensitive_content(db, monkeypatch):
    user = _user(db)
    calls = []
    monkeypatch.setattr(
        mem0_service,
        "index_memory",
        lambda *args: calls.append(args) or True,
    )

    assert memory_service.save_memory(db, user.id, "my password is hunter2") is None
    assert calls == []


# ── HTTP routes keep the mirror in sync ────────────────────


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client):
    from tests.auth_helpers import auth_headers, register_and_login

    _uid, token = register_and_login(client, username="memroute_" + uuid.uuid4().hex[:8])
    return client, auth_headers(token)


def test_memory_routes_mirror_create_and_delete(signed_in, monkeypatch):
    client, headers = signed_in
    indexed = []
    removed = []

    monkeypatch.setattr(
        mem0_service,
        "index_memory",
        lambda user_id, db_id, content, category: indexed.append(
            (user_id, db_id, content, category)
        )
        or True,
    )
    monkeypatch.setattr(
        mem0_service,
        "remove_memory_for_db_id",
        lambda user_id, db_id: removed.append((user_id, db_id)) or 1,
    )

    created = client.post(
        "/api/memories",
        headers=headers,
        json={"content": "My project is HireShade", "category": "project_context"},
    )
    assert created.status_code == 201, created.text[:300]
    memory_id = created.json()["id"]
    assert indexed and indexed[0][1] == memory_id

    deleted = client.delete(f"/api/memories/{memory_id}", headers=headers)
    assert deleted.status_code == 204
    assert removed == [(indexed[0][0], memory_id)]
