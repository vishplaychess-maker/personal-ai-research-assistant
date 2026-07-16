"""
Phase 2 smoke tests — Session CRUD, message persistence, and mock Ollama.

Run with:
    pip install httpx pytest pytest-asyncio
    pytest tests/test_sessions.py -v

Override the base URL:
    BASE_URL=http://localhost:8080 pytest tests/test_sessions.py -v
"""

import os
import time  # used in session ordering test

import httpx
import pytest

BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")

# ── Helpers ────────────────────────────────────────────────


def client() -> httpx.Client:
    # 15s is too tight for Ollama cold starts (model loading can take 20-30s).
    # The non-streaming endpoint has a 120s server timeout.
    # Use 30s to match the streaming tests.
    return httpx.Client(base_url=BASE_URL, timeout=30.0)


@pytest.fixture(autouse=True)
def _cleanup_sessions():
    """Delete any leftover test sessions before each test."""
    with client() as c:
        sessions = c.get("/api/sessions").json()
        for s in sessions:
            if s["id"] > 10:  # Only clean up test-created sessions (safety)
                c.delete(f"/api/sessions/{s['id']}")


# ── Session CRUD tests ─────────────────────────────────────


def test_create_session():
    """POST /api/sessions creates a new session."""
    with client() as c:
        resp = c.post("/api/sessions", json={"title": "Test Session"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Test Session"
    assert "id" in data
    assert data["user_id"] == 1


def test_list_sessions():
    """GET /api/sessions returns a list."""
    with client() as c:
        # Create two sessions
        c.post("/api/sessions", json={"title": "Session A"})
        c.post("/api/sessions", json={"title": "Session B"})
        resp = c.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 2
    titles = [s["title"] for s in data]
    assert "Session A" in titles
    assert "Session B" in titles


def test_get_session():
    """GET /api/sessions/{id} returns the session."""
    with client() as c:
        created = c.post("/api/sessions", json={"title": "Get Me"}).json()
        resp = c.get(f"/api/sessions/{created['id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == created["id"]
    assert data["title"] == "Get Me"


def test_get_session_404():
    """GET /api/sessions/{id} returns 404 for unknown ID."""
    with client() as c:
        resp = c.get("/api/sessions/99999")
    assert resp.status_code == 404


def test_update_session():
    """PATCH /api/sessions/{id} renames the session."""
    with client() as c:
        created = c.post("/api/sessions", json={"title": "Old Title"}).json()
        resp = c.patch(
            f"/api/sessions/{created['id']}",
            json={"title": "New Title"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "New Title"


def test_update_session_404():
    """PATCH /api/sessions/{id} returns 404 for unknown ID."""
    with client() as c:
        resp = c.patch("/api/sessions/99999", json={"title": "Nope"})
    assert resp.status_code == 404


def test_delete_session():
    """DELETE /api/sessions/{id} removes the session."""
    with client() as c:
        created = c.post("/api/sessions", json={"title": "Delete Me"}).json()
        del_resp = c.delete(f"/api/sessions/{created['id']}")
        assert del_resp.status_code == 204

        # Verify it's gone
        get_resp = c.get(f"/api/sessions/{created['id']}")
    assert get_resp.status_code == 404


def test_delete_session_404():
    """DELETE /api/sessions/{id} returns 404 for unknown ID."""
    with client() as c:
        resp = c.delete("/api/sessions/99999")
    assert resp.status_code == 404


# ── Message tests ──────────────────────────────────────────


def test_send_message_creates_user_and_assistant():
    """
    POST /api/sessions/{id}/messages with a ChatRequest
    returns both user_message and assistant_message.

    The assistant_message may contain an Ollama error if Ollama is
    unavailable (which is expected in CI without Ollama).
    """
    with client() as c:
        session = c.post("/api/sessions", json={"title": "Chat Test"}).json()
        resp = c.post(
            f"/api/sessions/{session['id']}/messages",
            json={"message": "Hello, how are you?"},
        )

    # The endpoint should always return 200 even when Ollama is down
    assert resp.status_code == 200
    data = resp.json()

    # Check shape
    assert "user_message" in data
    assert "assistant_message" in data

    # User message
    um = data["user_message"]
    assert um["role"] == "user"
    assert um["content"] == "Hello, how are you?"
    assert um["session_id"] == session["id"]

    # Assistant message should exist (may be an error message if Ollama unavailable)
    am = data["assistant_message"]
    assert am["role"] == "assistant"
    assert am["session_id"] == session["id"]
    # Content is either a real response or starts with ⚠️ (Ollama unavailable)
    assert len(am["content"]) > 0


def test_list_messages():
    """GET /api/sessions/{id}/messages returns all messages in order."""
    with client() as c:
        session = c.post("/api/sessions", json={"title": "Msg List"}).json()
        # Send a message
        c.post(
            f"/api/sessions/{session['id']}/messages",
            json={"message": "First message"},
        )
        # List messages
        resp = c.get(f"/api/sessions/{session['id']}/messages")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 2  # at least user + assistant
    # First message should be the user message (ordered by created_at asc)
    assert data[0]["role"] == "user"
    assert data[0]["content"] == "First message"


def test_list_messages_404():
    """GET /api/sessions/{id}/messages returns 404 for unknown session."""
    with client() as c:
        resp = c.get("/api/sessions/99999/messages")
    assert resp.status_code == 404


def test_send_message_404():
    """POST /api/sessions/{id}/messages returns 404 for unknown session."""
    with client() as c:
        resp = c.post(
            "/api/sessions/99999/messages",
            json={"message": "Hello"},
        )
    assert resp.status_code == 404


# ── Message persistence test ───────────────────────────────


def test_messages_persist_across_requests():
    """
    Messages survive across requests (no in-memory loss).
    This tests SQLite persistence.
    """
    # Use separate clients to avoid httpx connection-pool reuse after long requests
    def _send(sid: int, msg: str):
        with httpx.Client(base_url=BASE_URL, timeout=30.0) as c:
            return c.post(f"/api/sessions/{sid}/messages", json={"message": msg})

    with httpx.Client(base_url=BASE_URL, timeout=15.0) as c:
        session = c.post("/api/sessions", json={"title": "Persist Test"}).json()
        sid = session["id"]

    # Send two messages using separate clients
    _send(sid, "Q1")
    _send(sid, "Q2")

    # Fetch all messages
    with httpx.Client(base_url=BASE_URL, timeout=15.0) as c:
        resp = c.get(f"/api/sessions/{sid}/messages")
    data = resp.json()
    assert len(data) >= 4  # 2 user + 2 assistant
    user_contents = [m["content"] for m in data if m["role"] == "user"]
    assert "Q1" in user_contents
    assert "Q2" in user_contents


# ── Session list ordering ──────────────────────────────────


def test_sessions_ordered_by_updated_at_desc():
    """Sessions should be listed with most recently updated first."""
    with client() as c:
        s1 = c.post("/api/sessions", json={"title": "First"}).json()
        time.sleep(0.05)
        s2 = c.post("/api/sessions", json={"title": "Second"}).json()

        resp = c.get("/api/sessions")
    data = resp.json()
    # Find our sessions in the list
    ids = [s["id"] for s in data if s["id"] in (s1["id"], s2["id"])]
    # Most recent should come first
    assert ids == [s2["id"], s1["id"]], f"Expected {[s2['id'], s1['id']]} got {ids}"


# ── Mock Ollama test ───────────────────────────────────────


def test_send_message_with_mocked_ollama():
    """
    Mock the Ollama generate_response function to return a deterministic
    response, verifying the full LangGraph pipeline produces correct output.

    Uses TestClient (in-process) and direct setattr (which is more reliable
    than monkeypatch.setattr when TestClient is involved in the same process).

    This test does NOT require Ollama to be running.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    import app.services.langgraph_workflow as workflow

    original = workflow.generate_response

    def mock_generate_response(messages, system_prompt=None, **kwargs):
        return "This is a mocked response from Ollama."

    try:
        workflow.generate_response = mock_generate_response

        with TestClient(app) as c:
            # Create session via TestClient
            resp = c.post("/api/sessions", json={"title": "Mock Test"})
            assert resp.status_code == 201
            session = resp.json()
            sid = session["id"]

            # Send message — the mock should intercept the Ollama call
            resp = c.post(
                f"/api/sessions/{sid}/messages",
                json={"message": "This is a test message"},
            )

        assert resp.status_code == 200
        data = resp.json()

        # User message is correct
        assert data["user_message"]["role"] == "user"
        assert data["user_message"]["content"] == "This is a test message"

        # Assistant message matches our mock
        assert data["assistant_message"]["role"] == "assistant"
        assert data["assistant_message"]["content"] == "This is a mocked response from Ollama."
    finally:
        workflow.generate_response = original
