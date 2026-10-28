"""
Phase 5C tests — Conversation search endpoint.

Tests the search endpoint using FastAPI's TestClient and mocks
for external services. Creates test data (sessions + messages)
and verifies the search returns correct results.

Run with:
    pytest tests/test_search.py -v
"""

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal, engine, Base
from app.models.models import ResearchSession, Message

from tests.auth_helpers import register_and_login, auth_headers


# ── Fixtures ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _setup_db():
    """Create tables before each test."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        # Clean up all test data (sessions and messages).
        db.query(Message).delete()
        db.query(ResearchSession).delete()
        db.commit()
        db.close()


def _register_auth(client):
    """Register + login a fresh test user; returns (user_id, headers).

    Uses a unique auto-generated username so repeated calls never collide
    with an existing user. (Phase 7C: registrations count toward the shared
    in-process IP rate limiter, and only a subsequent successful login
    resets it — a fixed username caused 400s here that throttled later files.)
    """
    user_id, token = register_and_login(client)
    return user_id, auth_headers(token)


def _create_test_session(db, title: str, user_id: int) -> ResearchSession:
    """Helper to create a test session owned by the given user."""
    session = ResearchSession(title=title, user_id=user_id)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _create_test_message(
    db, session_id: int, role: str, content: str
) -> Message:
    """Helper to create a test message."""
    msg = Message(
        session_id=session_id,
        role=role,
        content=content,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


# ── Tests ──────────────────────────────────────────────────


class TestSearchEndpoint:
    """Tests for the GET /api/search endpoint."""

    def test_search_empty_query_rejected(self):
        """Empty query string is rejected (400 or 422)."""
        with TestClient(app) as client:
            _, headers = _register_auth(client)
            resp = client.get("/api/search", params={"q": ""}, headers=headers)
        # FastAPI's built-in validation returns 422 for min_length=1;
        # fallback code path would return 400. Accept either.
        assert resp.status_code in (400, 422)
        detail = resp.json().get("detail", "")
        if isinstance(detail, str):
            assert "required" in detail.lower() or len(detail) > 0

    def test_search_requires_auth(self):
        """Unauthenticated search requests are rejected with 401."""
        with TestClient(app) as client:
            resp = client.get("/api/search", params={"q": "anything"})
        assert resp.status_code == 401

    def test_search_no_results(self):
        """Non-matching query returns empty list."""
        with TestClient(app) as client:
            _, headers = _register_auth(client)
            resp = client.get("/api/search", params={"q": "xyznonexistent12345"}, headers=headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_search_query_too_long_rejected(self):
        """Query exceeding 200 characters is rejected."""
        long_q = "a" * 201
        with TestClient(app) as client:
            _, headers = _register_auth(client)
            resp = client.get("/api/search", params={"q": long_q}, headers=headers)
        assert resp.status_code in (400, 422)

    def test_search_returns_matching_user_message(self):
        """Search finds a matching user message."""
        with TestClient(app) as client:
            user_id, headers = _register_auth(client)
            db = SessionLocal()
            session = _create_test_session(db, "Test Chat", user_id)
            _create_test_message(db, session.id, "user", "Tell me about artificial intelligence")
            _create_test_message(db, session.id, "assistant", "AI is a fascinating field.")
            db.close()

            resp = client.get("/api/search", params={"q": "artificial"}, headers=headers)

        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1
        # The matching message should be first (most recent)
        assert "artificial" in results[0]["content"].lower()

    def test_search_returns_matching_assistant_message(self):
        """Search finds a matching assistant message."""
        with TestClient(app) as client:
            user_id, headers = _register_auth(client)
            db = SessionLocal()
            session = _create_test_session(db, "Tech Talk", user_id)
            _create_test_message(db, session.id, "user", "What is Python?")
            _create_test_message(db, session.id, "assistant", "Python is a programming language used for AI and web development.")
            db.close()

            resp = client.get("/api/search", params={"q": "programming"}, headers=headers)

        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1
        assert "programming" in results[0]["content"].lower()

    def test_search_result_has_all_fields(self):
        """Search result contains all expected fields."""
        with TestClient(app) as client:
            user_id, headers = _register_auth(client)
            db = SessionLocal()
            session = _create_test_session(db, "Research Session", user_id)
            _create_test_message(db, session.id, "user", "Tell me about machine learning.")
            db.close()

            resp = client.get("/api/search", params={"q": "machine"}, headers=headers)

        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1
        result = results[0]
        assert "session_id" in result
        assert "session_title" in result
        assert "message_id" in result
        assert "role" in result
        assert "content" in result
        assert "snippet" in result
        assert "created_at" in result
        assert result["session_title"] == "Research Session"
        assert result["role"] == "user"

    def test_search_result_snippet_truncated(self):
        """Snippet field is truncated to 150 characters."""
        with TestClient(app) as client:
            user_id, headers = _register_auth(client)
            db = SessionLocal()
            session = _create_test_session(db, "Long Content", user_id)
            long_content = "word " * 100  # ~500 chars
            _create_test_message(db, session.id, "user", long_content)
            db.close()

            resp = client.get("/api/search", params={"q": "word"}, headers=headers)

        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1
        assert len(results[0]["snippet"]) <= 150

    def test_search_matches_across_multiple_sessions(self):
        """Search returns results from different sessions."""
        with TestClient(app) as client:
            user_id, headers = _register_auth(client)
            db = SessionLocal()
            s1 = _create_test_session(db, "Session One", user_id)
            s2 = _create_test_session(db, "Session Two", user_id)
            _create_test_message(db, s1.id, "user", "I like artificial intelligence")
            _create_test_message(db, s2.id, "user", "AI research is interesting")
            db.close()

            resp = client.get("/api/search", params={"q": "intelligence"}, headers=headers)

        assert resp.status_code == 200
        results = resp.json()
        # At least one result should match (Session One has "intelligence")
        matches = [r for r in results if "intelligence" in r["content"].lower()]
        assert len(matches) >= 1

    def test_search_excludes_non_matching_messages(self):
        """Messages without the search term are not returned."""
        with TestClient(app) as client:
            user_id, headers = _register_auth(client)
            db = SessionLocal()
            session = _create_test_session(db, "Filter Test", user_id)
            _create_test_message(db, session.id, "user", "This is about cats")
            _create_test_message(db, session.id, "assistant", "Cats are wonderful pets")
            db.close()

            resp = client.get("/api/search", params={"q": "dogs"}, headers=headers)

        assert resp.status_code == 200
        results = resp.json()
        assert len(results) == 0

    def test_search_does_not_leak_other_users_sessions(self):
        """Search results are scoped to the authenticated user only."""
        with TestClient(app) as client:
            user_a, headers_a = _register_auth(client)
            # Another user with matching content in their session
            user_b, headers_b = _register_auth(client)

            db = SessionLocal()
            s_a = _create_test_session(db, "User A Chat", user_a)
            s_a_id = s_a.id
            _create_test_message(db, s_a_id, "user", "secret project plan for alpha")
            s_b = _create_test_session(db, "User B Chat", user_b)
            s_b_id = s_b.id
            _create_test_message(db, s_b_id, "user", "secret project plan for beta")
            db.close()

            resp = client.get("/api/search", params={"q": "secret project"}, headers=headers_a)

        assert resp.status_code == 200
        results = resp.json()
        # User A must only see their own session's match
        assert len(results) == 1
        # NOTE: capture s_a.id before db.close() — later commits expire the
        # attribute and reading it after close raises DetachedInstanceError.
        assert results[0]["session_id"] == s_a_id
        assert "alpha" in results[0]["content"]
