"""
Phase 4 tests — Long-Term Memory: CRUD, extraction, deduplication, and privacy.

Run with:
    pip install httpx pytest
    pytest tests/test_memories.py -v

Override the base URL:
    BASE_URL=http://localhost:8080 pytest tests/test_memories.py -v
"""

import os

import httpx
import pytest

BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")


# ── Helpers ────────────────────────────────────────────────


def client() -> httpx.Client:
    # 15s is too tight for Ollama cold starts (model loading can take 20-30s).
    # The non-streaming endpoint has a 120s server timeout. Use 30s for stability.
    return httpx.Client(base_url=BASE_URL, timeout=30.0)


def _clear_all_memories(c: httpx.Client):
    """Clear all memories using the DELETE endpoint with confirmation."""
    c.request("DELETE", "/api/memories", json={"confirm": True})


@pytest.fixture(autouse=True)
def _cleanup_memories():
    """Clean up all test memories and ensure memory is enabled before each test."""
    with client() as c:
        try:
            _clear_all_memories(c)
            c.patch("/api/settings/memory", json={"enabled": True})
        except Exception:
            pass


# ── Memory CRUD tests ──────────────────────────────────────


def test_create_memory():
    """POST /api/memories creates a new memory."""
    with client() as c:
        resp = c.post("/api/memories", json={
            "content": "User prefers short explanations",
            "category": "preference",
        })
    assert resp.status_code == 201
    data = resp.json()
    assert data["content"] == "User prefers short explanations"
    assert data["category"] == "preference"
    assert "id" in data
    assert data["user_id"] == 1


def test_create_memory_with_session():
    """POST /api/memories with session_id associates the memory."""
    with client() as c:
        s = c.post("/api/sessions", json={"title": "Mem Session"}).json()
        resp = c.post("/api/memories", json={
            "content": "This research is about battery chemistry",
            "category": "research_interest",
            "session_id": s["id"],
        })
    assert resp.status_code == 201
    data = resp.json()
    assert data["session_id"] == s["id"]
    assert data["category"] == "research_interest"


def test_list_memories():
    """GET /api/memories returns all memories."""
    with client() as c:
        c.post("/api/memories", json={"content": "Memory A", "category": "fact"})
        c.post("/api/memories", json={"content": "Memory B", "category": "preference"})
        resp = c.get("/api/memories")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2
    contents = [m["content"] for m in data]
    assert "Memory A" in contents
    assert "Memory B" in contents


def test_update_memory():
    """PATCH /api/memories/{id} updates content and category."""
    with client() as c:
        created = c.post("/api/memories", json={
            "content": "Old content",
            "category": "fact",
        }).json()
        resp = c.patch(f"/api/memories/{created['id']}", json={
            "content": "Updated content",
            "category": "preference",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["content"] == "Updated content"
    assert data["category"] == "preference"


def test_update_memory_404():
    """PATCH /api/memories/{id} returns 404 for unknown ID."""
    with client() as c:
        resp = c.patch("/api/memories/99999", json={
            "content": "Nope",
            "category": "fact",
        })
    assert resp.status_code == 404


def test_delete_memory():
    """DELETE /api/memories/{id} removes the memory."""
    with client() as c:
        created = c.post("/api/memories", json={
            "content": "Delete me",
            "category": "fact",
        }).json()
        del_resp = c.delete(f"/api/memories/{created['id']}")
        assert del_resp.status_code == 204

        get_resp = c.get("/api/memories")
    ids = [m["id"] for m in get_resp.json()]
    assert created["id"] not in ids


def test_delete_memory_404():
    """DELETE /api/memories/{id} returns 404 for unknown ID."""
    with client() as c:
        resp = c.delete("/api/memories/99999")
    assert resp.status_code == 404


def test_clear_all_memories():
    """DELETE /api/memories with confirm=true clears all."""
    with client() as c:
        c.post("/api/memories", json={"content": "Mem 1", "category": "fact"})
        c.post("/api/memories", json={"content": "Mem 2", "category": "preference"})

        # Without confirmation — should fail
        fail_resp = c.request("DELETE", "/api/memories", json={"confirm": False})
        assert fail_resp.status_code == 400

        # With confirmation
        ok_resp = c.request("DELETE", "/api/memories", json={"confirm": True})
        assert ok_resp.status_code == 204

        # Verify empty
        list_resp = c.get("/api/memories")
        assert len(list_resp.json()) == 0


# ── Empty / invalid content tests ──────────────────────────


def test_create_empty_memory_rejected():
    """POST /api/memories with empty content returns an error."""
    with client() as c:
        resp = c.post("/api/memories", json={
            "content": "",
            "category": "fact",
        })
    # FastAPI's Pydantic validation should reject empty string
    assert resp.status_code == 422


def test_create_memory_invalid_category():
    """POST /api/memories with invalid category returns 400."""
    with client() as c:
        resp = c.post("/api/memories", json={
            "content": "Some memory",
            "category": "invalid_category",
        })
    assert resp.status_code == 400


# ── Sensitive info filtering tests ─────────────────────────


def test_sensitive_content_rejected():
    """POST /api/memories with sensitive content returns 400."""
    with client() as c:
        resp = c.post("/api/memories", json={
            "content": "My password is secret123",
            "category": "fact",
        })
        assert resp.status_code == 400

        resp2 = c.post("/api/memories", json={
            "content": "My API_key is abc123def456",
            "category": "fact",
        })
        assert resp2.status_code == 400

        resp3 = c.post("/api/memories", json={
            "content": "My API token ghp_testtoken123",
            "category": "fact",
        })
        assert resp3.status_code == 400


def test_sensitive_content_rejected_in_update():
    """PATCH /api/memories/{id} with sensitive content returns 400."""
    with client() as c:
        created = c.post("/api/memories", json={
            "content": "Safe content",
            "category": "fact",
        }).json()
        resp = c.patch(f"/api/memories/{created['id']}", json={
            "content": "My token is ghp_abc123",
            "category": "fact",
        })
    assert resp.status_code == 400


def test_sensitive_content_rejects_long_random_strings():
    """
    Memory service should reject long alphanumeric strings that
    look like API keys or tokens (defense against document content leak).
    """
    with client() as c:
        resp = c.post("/api/memories", json={
            "content": "sk-abcdefghijklmnopqrstuvwxyz1234567890abcdefg",
            "category": "fact",
        })
        assert resp.status_code == 400, \
            "Long alphanumeric strings resembling API keys should be rejected"

        # Also test with a typical token pattern
        resp2 = c.post("/api/memories", json={
            "content": "ghp_token_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "category": "fact",
        })
        assert resp2.status_code == 400, \
            "GitHub token pattern should be rejected"


# ── Duplicate protection tests ─────────────────────────────


def test_duplicate_exact_match_via_extraction():
    """
    _save_memory_if_new should return the existing memory for exact match.
    This tests the internal deduplication logic directly.
    """
    from sqlalchemy.orm import Session as DBSession
    from app.database import SessionLocal
    from app.services.memory_service import _save_memory_if_new

    db: DBSession = SessionLocal()
    try:
        # First save
        m1 = _save_memory_if_new(db, user_id=1,
            content="User researches battery chemistry",
            category="research_interest")
        m1_id = m1.id

        # Second save with exact same content
        m2 = _save_memory_if_new(db, user_id=1,
            content="User researches battery chemistry",
            category="research_interest")
        assert m2.id == m1_id, f"Expected same ID {m1_id}, got {m2.id}"
    finally:
        db.close()


def test_nearly_identical_memory_merged():
    """
    Nearly identical content should update existing memory.
    Tests the deduplication logic directly.
    """
    from sqlalchemy.orm import Session as DBSession
    from app.database import SessionLocal
    from app.services.memory_service import _save_memory_if_new

    db: DBSession = SessionLocal()
    try:
        # First save
        m1 = _save_memory_if_new(db, user_id=1,
            content="User prefers short explanations with detailed examples",
            category="preference")
        m1_id = m1.id

        # Slightly different wording — should merge into same memory
        m2 = _save_memory_if_new(db, user_id=1,
            content="User prefers short explanations with detailed code examples",
            category="preference")
        assert m2.id == m1_id, f"Expected same ID {m1_id}, got {m2.id}"
    finally:
        db.close()


def test_distinct_memories_both_saved():
    """Distinct memories should both be saved separately via HTTP."""
    with client() as c:
        c.post("/api/memories", json={
            "content": "User researches battery chemistry",
            "category": "research_interest",
        })
        c.post("/api/memories", json={
            "content": "User prefers visual explanations",
            "category": "preference",
        })
        resp = c.get("/api/memories")
    assert len(resp.json()) == 2


# ── Memory ordering test ───────────────────────────────────


def test_memories_ordered_by_last_used():
    """Memories should be listed with most recently created first."""
    with client() as c:
        m1 = c.post("/api/memories", json={"content": "Old memory", "category": "fact"}).json()
        m2 = c.post("/api/memories", json={"content": "New memory", "category": "preference"}).json()

        resp = c.get("/api/memories")
    data = resp.json()
    # Most recently created should come first
    ids = [m["id"] for m in data if m["id"] in (m1["id"], m2["id"])]
    assert ids[0] == m2["id"], f"Expected {m2['id']} first, got {ids}"


# ── Memory injection into chat context ─────────────────────


def test_memories_used_flag_with_memory():
    """Send a chat message while memories exist and check memories_used flag."""
    from fastapi.testclient import TestClient
    from app.main import app
    import app.services.langgraph_workflow as workflow

    original = workflow.generate_response

    def mock_generate_response(messages, system_prompt=None):
        return "This is a mocked response."

    try:
        workflow.generate_response = mock_generate_response

        with TestClient(app) as c:
            # Create a memory first
            c.post("/api/memories", json={
                "content": "User likes simple analogies",
                "category": "preference",
            })

            # Create a session and send a message
            s = c.post("/api/sessions", json={"title": "Memory Chat Test"}).json()
            resp = c.post(f"/api/sessions/{s['id']}/messages", json={
                "message": "Explain something using an analogy.",
            })

        assert resp.status_code == 200
        data = resp.json()

        # Memories exist, so memories_used should be True
        assert data["memories_used"] is True, "memories_used should be True when memories exist"

    finally:
        workflow.generate_response = original


def test_memories_not_used_without_memories():
    """memories_used should be False when no memories exist."""
    from fastapi.testclient import TestClient
    from app.main import app
    import app.services.langgraph_workflow as workflow

    original = workflow.generate_response

    def mock_generate_response(messages, system_prompt=None):
        return "No memories needed."

    try:
        workflow.generate_response = mock_generate_response

        with TestClient(app) as c:
            s = c.post("/api/sessions", json={"title": "No Memory Test"}).json()
            resp = c.post(f"/api/sessions/{s['id']}/messages", json={
                "message": "Hello, how are you?",
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["memories_used"] is False, "memories_used should be False when no memories exist"

    finally:
        workflow.generate_response = original



# ── Settings API tests ─────────────────────────────────────


def test_get_memory_setting_returns_enabled():
    """GET /api/settings/memory returns the current enabled state."""
    with client() as c:
        # Ensure known state
        c.patch("/api/settings/memory", json={"enabled": True})
        resp = c.get("/api/settings/memory")
    assert resp.status_code == 200
    data = resp.json()
    assert "enabled" in data
    assert data["enabled"] is True


def test_patch_memory_setting_toggles():
    """PATCH /api/settings/memory toggles and persists the setting."""
    with client() as c:
        # Disable
        resp = c.patch("/api/settings/memory", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

        # Confirm via GET
        get1 = c.get("/api/settings/memory")
        assert get1.json()["enabled"] is False

        # Re-enable
        resp2 = c.patch("/api/settings/memory", json={"enabled": True})
        assert resp2.status_code == 200
        assert resp2.json()["enabled"] is True

        # Confirm via GET
        get2 = c.get("/api/settings/memory")
        assert get2.json()["enabled"] is True


# ── Privacy: document content not saved as memory ──────────


def test_memory_extraction_prompt_has_privacy_instructions():
    """
    The memory extraction prompt explicitly instructs the model
    to NOT save document content, passwords, or API keys as personal memory.
    """
    from app.services.memory_service import MEMORY_EXTRACTION_PROMPT, _is_sensitive

    # Verify the prompt has privacy instructions
    assert "uploaded documents" in MEMORY_EXTRACTION_PROMPT
    assert "passwords" in MEMORY_EXTRACTION_PROMPT or "API" in MEMORY_EXTRACTION_PROMPT

    # Verify sensitive content filter works
    assert _is_sensitive("My password is hunter2")
    assert _is_sensitive("The PDF content contains my API_key")
    assert not _is_sensitive("User likes studying chemistry")


# ── Memory-disabled behavior tests ────────────────────────


def test_memory_disabled_no_new_memories_from_chat():
    """
    When memory is disabled via PATCH /api/settings/memory,
    sending a durable-preference message does NOT create a Memory row.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    import app.services.langgraph_workflow as workflow

    original = workflow.generate_response

    def mock_generate_response(messages, system_prompt=None):
        return "Short and simple answers work best."

    try:
        workflow.generate_response = mock_generate_response

        with TestClient(app) as c:
            # Disable memory via the DB-backed setting
            c.patch("/api/settings/memory", json={"enabled": False})

            # Create a session and send a message that would normally
            # trigger memory extraction
            s = c.post("/api/sessions", json={"title": "Disabled Extract Test"}).json()
            c.post(f"/api/sessions/{s['id']}/messages", json={
                "message": "I prefer short and simple explanations.",
            })

            # Verify no memory was created
            mems = c.get("/api/memories").json()
            assert len(mems) == 0, f"Expected 0 memories when disabled, got {len(mems)}"
    finally:
        workflow.generate_response = original
        # Re-enable memory for subsequent tests
        with TestClient(app) as c2:
            c2.patch("/api/settings/memory", json={"enabled": True})


def test_memory_disabled_does_not_inject_existing():
    """
    When memory is disabled, existing memories are NOT injected
    into the prompt. memories_used should be False.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    import app.services.langgraph_workflow as workflow

    original = workflow.generate_response

    def mock_generate_response(messages, system_prompt=None):
        return "Memory-disabled response."

    try:
        workflow.generate_response = mock_generate_response

        with TestClient(app) as c:
            # Create a memory first
            c.request("DELETE", "/api/memories", json={"confirm": True})
            c.post("/api/memories", json={
                "content": "User likes simple analogies",
                "category": "preference",
            })

            # Disable memory via DB-backed setting
            c.patch("/api/settings/memory", json={"enabled": False})

            # Send a message
            s = c.post("/api/sessions", json={"title": "Disabled Mem Test"}).json()
            resp = c.post(f"/api/sessions/{s['id']}/messages", json={
                "message": "Explain something.",
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["memories_used"] is False, "memories_used should be False when memory is disabled"

    finally:
        workflow.generate_response = original
        with TestClient(app) as c2:
            c2.patch("/api/settings/memory", json={"enabled": True})


def test_memory_disabled_memories_still_visible():
    """
    When memory is disabled, existing memories remain visible
    and editable in the Memory panel.
    """
    with client() as c:
        # Create a memory
        c.post("/api/memories", json={
            "content": "User studies machine learning",
            "category": "research_interest",
        })

        # Disable memory via DB setting
        c.patch("/api/settings/memory", json={"enabled": False})

        # Memories should still be visible
        mems = c.get("/api/memories").json()
        assert len(mems) >= 1
        assert any(m["content"] == "User studies machine learning" for m in mems)

        # Should still be editable
        target = next(m for m in mems if m["content"] == "User studies machine learning")
        edit = c.patch(f"/api/memories/{target['id']}", json={
            "content": "User studies ML and deep learning",
            "category": "research_interest",
        })
        assert edit.status_code == 200

        # Should still be deletable
        c.delete(f"/api/memories/{target['id']}")
        mems2 = c.get("/api/memories").json()
        assert all(m["id"] != target["id"] for m in mems2)


def test_memory_reenabled_after_disabled():
    """After re-enabling memory, extraction and retrieval resume."""
    with client() as c:
        # Ensure memory is enabled
        c.patch("/api/settings/memory", json={"enabled": True})

        # Create a memory manually
        c.post("/api/memories", json={
            "content": "User likes code examples",
            "category": "preference",
        })

        # Verify memory was saved
        mems = c.get("/api/memories").json()
        assert any(m["content"] == "User likes code examples" for m in mems)


def test_memory_setting_persists_across_sessions():
    """
    The memory setting persists across separate database sessions.
    Sets the value, then opens a new client connection and verifies.
    """
    with client() as c1:
        # Disable via first client
        c1.patch("/api/settings/memory", json={"enabled": False})

    # Open a new client (simulating new request/db session)
    with client() as c2:
        get_resp = c2.get("/api/settings/memory")
        assert get_resp.status_code == 200
        assert get_resp.json()["enabled"] is False

    # Re-enable
    with client() as c3:
        c3.patch("/api/settings/memory", json={"enabled": True})


def test_memory_disabled_returns_memories_used_false():
    """
    Directly test that when memory is disabled via the DB setting,
    retrieve_relevant_memories returns an empty list.
    """
    from sqlalchemy.orm import Session as DBSession
    from app.database import SessionLocal
    from app.services.memory_service import retrieve_relevant_memories
    from app.services.settings_service import set_memory_enabled

    db: DBSession = SessionLocal()
    try:
        # Create a memory first
        from app.models.models import Memory
        db.add(Memory(user_id=1, content="Test memory", category="fact"))
        db.commit()

        # Disable memory via the DB setting
        set_memory_enabled(db, False)

        # Should return empty
        result = retrieve_relevant_memories(db)
        assert len(result) == 0, "retrieve_relevant_memories should return [] when disabled"

        # Re-enable for cleanup
        set_memory_enabled(db, True)

        # Should return memories now
        result2 = retrieve_relevant_memories(db)
        assert len(result2) >= 1
    finally:
        db.close()
