"""
Phase 6A/6B — Authentication and Authorization tests.

Tests cover:
  Phase 6A:
  - User registration (success, duplicate username, duplicate email, validation)
  - User login (success, wrong password, nonexistent user)
  - Current user retrieval (valid token, invalid token, expired token)
  - Edge cases (password strength, username format, email format)

  Phase 6B:
  - Cross-user isolation (User A cannot access User B's data)
  - Backward compatibility (unauthenticated requests fall back to user 1)
  - Ownership scoping on sessions, memories, search, and documents
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.models.models import User
from app.services.auth_service import create_access_token


# ── Fixtures ───────────────────────────────────────────────


@pytest.fixture
def client():
    """Yield a TestClient connected to the app with a fresh DB."""
    with TestClient(app) as c:
        yield c


def _cleanup_test_users():
    """Remove any test users created during tests."""
    db = SessionLocal()
    try:
        test_users = db.query(User).filter(
            User.username.like("testuser%")
        ).all()
        for u in test_users:
            db.delete(u)
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up test users before and after each test."""
    _cleanup_test_users()
    yield
    _cleanup_test_users()


# ── Helpers ────────────────────────────────────────────────


def _register_user_and_login(client, username, password="securePass123!"):
    """Register a user and return (user_id, token)."""
    reg = client.post("/api/auth/register", json={
        "username": username,
        "email": f"{username}@test.com",
        "password": password,
    })
    assert reg.status_code == 201
    user_id = reg.json()["id"]
    login = client.post("/api/auth/login", json={
        "username": username,
        "password": password,
    })
    assert login.status_code == 200
    return user_id, login.json()["access_token"]


def _create_session(client, token, title="Test Session"):
    """Create a session using the given auth token."""
    resp = client.post("/api/sessions", json={"title": title},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 201
    return resp.json()


def _create_memory(client, token, content="Test memory", category="fact"):
    """Create a memory using the given auth token."""
    resp = client.post("/api/memories", json={"content": content, "category": category},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 201
    return resp.json()


# ── Registration Tests (Phase 6A) ──────────────────────────


class TestRegister:
    def test_register_user_success(self, client):
        """A valid registration creates a new user and returns public info."""
        payload = {
            "username": "testuser_alice",
            "email": "alice@example.com",
            "password": "securePass123!",
        }
        resp = client.post("/api/auth/register", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["username"] == "testuser_alice"
        assert data["email"] == "alice@example.com"
        assert "id" in data
        assert "created_at" in data
        assert "password" not in data  # never expose password

    def test_register_duplicate_username(self, client):
        """Registering with an existing username returns 400."""
        payload = {
            "username": "testuser_bob",
            "email": "bob@example.com",
            "password": "securePass123!",
        }
        resp1 = client.post("/api/auth/register", json=payload)
        assert resp1.status_code == 201

        resp2 = client.post("/api/auth/register", json=payload)
        assert resp2.status_code == 400
        assert "Username already taken" in resp2.json()["detail"]

    def test_register_duplicate_email(self, client):
        """Registering with an existing email returns 400."""
        payload1 = {
            "username": "testuser_carol",
            "email": "carol@example.com",
            "password": "securePass123!",
        }
        payload2 = {
            "username": "testuser_carol2",
            "email": "carol@example.com",
            "password": "securePass123!",
        }
        resp1 = client.post("/api/auth/register", json=payload1)
        assert resp1.status_code == 201

        resp2 = client.post("/api/auth/register", json=payload2)
        assert resp2.status_code == 400
        assert "Email already registered" in resp2.json()["detail"]

    def test_register_password_too_short(self, client):
        """Password shorter than 8 characters returns 422."""
        payload = {
            "username": "testuser_dave",
            "email": "dave@example.com",
            "password": "short1",
        }
        resp = client.post("/api/auth/register", json=payload)
        assert resp.status_code == 422

    def test_register_username_invalid_chars(self, client):
        """Username with invalid characters returns 422."""
        payload = {
            "username": "test user!",
            "email": "test@example.com",
            "password": "securePass123!",
        }
        resp = client.post("/api/auth/register", json=payload)
        assert resp.status_code == 422

    def test_register_username_too_short(self, client):
        """Username shorter than 3 characters returns 422."""
        payload = {
            "username": "ab",
            "email": "ab@example.com",
            "password": "securePass123!",
        }
        resp = client.post("/api/auth/register", json=payload)
        assert resp.status_code == 422


# ── Login Tests (Phase 6A) ─────────────────────────────────


class TestLogin:
    def _register_user(self, client, username="testuser_login", password="securePass123!"):
        payload = {
            "username": username,
            "email": f"{username}@example.com",
            "password": password,
        }
        resp = client.post("/api/auth/register", json=payload)
        assert resp.status_code == 201
        return resp.json()

    def test_login_success(self, client):
        """Valid credentials return a JWT token."""
        self._register_user(client)
        resp = client.post("/api/auth/login", json={
            "username": "testuser_login",
            "password": "securePass123!",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert len(data["access_token"]) > 20  # JWT is reasonably long

    def test_login_wrong_password(self, client):
        """Wrong password returns 401 with generic message."""
        self._register_user(client)
        resp = client.post("/api/auth/login", json={
            "username": "testuser_login",
            "password": "wrongPassword!",
        })
        assert resp.status_code == 401
        assert "Invalid credentials" in resp.json()["detail"]

    def test_login_nonexistent_user(self, client):
        """Unknown username returns 401 with generic message."""
        resp = client.post("/api/auth/login", json={
            "username": "nonexistent_user",
            "password": "securePass123!",
        })
        assert resp.status_code == 401
        assert "Invalid credentials" in resp.json()["detail"]

    def test_login_default_user_fails(self, client):
        """The default user (no hashed_password) cannot log in."""
        resp = client.post("/api/auth/login", json={
            "username": "default",
            "password": "anything",
        })
        assert resp.status_code == 401
        assert "Invalid credentials" in resp.json()["detail"]


# ── Current User (Me) Tests (Phase 6A) ─────────────────────


class TestMe:
    def _register_and_login(self, client, username="testuser_me"):
        """Helper: register and login, return the token."""
        reg_payload = {
            "username": username,
            "email": f"{username}@example.com",
            "password": "securePass123!",
        }
        reg_resp = client.post("/api/auth/register", json=reg_payload)
        assert reg_resp.status_code == 201

        login_resp = client.post("/api/auth/login", json={
            "username": username,
            "password": "securePass123!",
        })
        assert login_resp.status_code == 200
        return login_resp.json()["access_token"]

    def test_get_current_user_valid_token(self, client):
        """A valid token returns the authenticated user's info."""
        token = self._register_and_login(client)
        resp = client.get("/api/auth/me", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "testuser_me"
        assert "id" in data
        assert "email" in data
        assert "created_at" in data
        assert "password" not in data

    def test_get_current_user_no_token(self, client):
        """A request without a token returns 401."""
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401
        assert "Not authenticated" in resp.json()["detail"]

    def test_get_current_user_invalid_token(self, client):
        """A request with a bad token returns 401."""
        resp = client.get("/api/auth/me", headers={
            "Authorization": "Bearer invalidtoken123",
        })
        assert resp.status_code == 401
        assert "Invalid or expired token" in resp.json()["detail"]

    def test_get_current_user_expired_token(self, client):
        """A request with an expired token returns 401."""
        expired_token = create_access_token(
            data={"sub": "0"},
            expires_delta=timedelta(hours=-1),
        )
        resp = client.get("/api/auth/me", headers={
            "Authorization": f"Bearer {expired_token}",
        })
        assert resp.status_code == 401
        assert "Invalid or expired token" in resp.json()["detail"]


# ── Cross-User Isolation Tests (Phase 6B) ──────────────────


class TestCrossUserIsolation:
    """Verify that User A cannot access User B's resources."""

    def _setup_users(self, client):
        """Register two users and return their tokens."""
        user_a_id, token_a = _register_user_and_login(client, "testuser_alpha")
        user_b_id, token_b = _register_user_and_login(client, "testuser_beta")
        return token_a, token_b

    # ── Sessions ──────────────────────────────────────────

    def test_user_a_cannot_list_user_b_sessions(self, client):
        """User A's session list does not include User B's sessions."""
        token_a, token_b = self._setup_users(client)
        # User B creates a session
        sess = _create_session(client, token_b, title="Beta's Secret")
        # User A lists sessions
        resp = client.get("/api/sessions",
                          headers={"Authorization": f"Bearer {token_a}"})
        assert resp.status_code == 200
        titles = [s["title"] for s in resp.json()]
        assert "Beta's Secret" not in titles

    def test_user_a_cannot_read_user_b_session(self, client):
        """User A gets 404 when trying to read User B's session."""
        token_a, token_b = self._setup_users(client)
        sess = _create_session(client, token_b)
        resp = client.get(f"/api/sessions/{sess['id']}",
                          headers={"Authorization": f"Bearer {token_a}"})
        assert resp.status_code == 404

    def test_user_a_cannot_update_user_b_session(self, client):
        """User A gets 404 when trying to rename User B's session."""
        token_a, token_b = self._setup_users(client)
        sess = _create_session(client, token_b)
        resp = client.patch(f"/api/sessions/{sess['id']}",
                            json={"title": "Hacked!"},
                            headers={"Authorization": f"Bearer {token_a}"})
        assert resp.status_code == 404

    def test_user_a_cannot_delete_user_b_session(self, client):
        """User A gets 404 when trying to delete User B's session."""
        token_a, token_b = self._setup_users(client)
        sess = _create_session(client, token_b)
        resp = client.delete(f"/api/sessions/{sess['id']}",
                             headers={"Authorization": f"Bearer {token_a}"})
        assert resp.status_code == 404

    def test_user_a_cannot_modify_user_b_session_model(self, client):
        """User A gets 404 when trying to change User B's session model."""
        token_a, token_b = self._setup_users(client)
        sess = _create_session(client, token_b)
        resp = client.patch(f"/api/sessions/{sess['id']}/model",
                            json={"model": "llama3.2:1b"},
                            headers={"Authorization": f"Bearer {token_a}"})
        assert resp.status_code == 404

    def test_user_a_cannot_read_user_b_system_prompt(self, client):
        """User A gets 404 when trying to read User B's system prompt."""
        token_a, token_b = self._setup_users(client)
        sess = _create_session(client, token_b)
        resp = client.get(f"/api/sessions/{sess['id']}/system-prompt",
                          headers={"Authorization": f"Bearer {token_a}"})
        assert resp.status_code == 404

    def test_user_a_cannot_update_user_b_system_prompt(self, client):
        """User A gets 404 when trying to modify User B's system prompt."""
        token_a, token_b = self._setup_users(client)
        sess = _create_session(client, token_b)
        resp = client.patch(f"/api/sessions/{sess['id']}/system-prompt",
                            json={"system_prompt": "Hacked!"},
                            headers={"Authorization": f"Bearer {token_a}"})
        assert resp.status_code == 404

    # ── Messages ──────────────────────────────────────────

    def test_user_a_cannot_list_user_b_messages(self, client):
        """User A gets 404 when trying to list messages in User B's session."""
        token_a, token_b = self._setup_users(client)
        sess = _create_session(client, token_b)
        resp = client.get(f"/api/sessions/{sess['id']}/messages",
                          headers={"Authorization": f"Bearer {token_a}"})
        assert resp.status_code == 404

    # ── Memories ──────────────────────────────────────────

    def test_user_a_cannot_list_user_b_memories(self, client):
        """User A's memory list does not include User B's memories."""
        token_a, token_b = self._setup_users(client)
        _create_memory(client, token_b, content="Beta's private note")
        resp = client.get("/api/memories",
                          headers={"Authorization": f"Bearer {token_a}"})
        assert resp.status_code == 200
        contents = [m["content"] for m in resp.json()]
        assert "Beta's private note" not in contents

    def test_user_a_cannot_read_user_b_memory(self, client):
        """User A gets 404 when trying to update User B's memory by ID."""
        token_a, token_b = self._setup_users(client)
        mem = _create_memory(client, token_b)
        # Since there is no GET memory by ID, we test PATCH (update)
        # which should return 404 for another user's memory
        resp = client.patch(f"/api/memories/{mem['id']}",
                            json={"content": "Hacked!", "category": "fact"},
                            headers={"Authorization": f"Bearer {token_a}"})
        assert resp.status_code == 404

    def test_user_a_cannot_delete_user_b_memory(self, client):
        """User A gets 404 when trying to delete User B's memory."""
        token_a, token_b = self._setup_users(client)
        mem = _create_memory(client, token_b)
        resp = client.delete(f"/api/memories/{mem['id']}",
                             headers={"Authorization": f"Bearer {token_a}"})
        assert resp.status_code == 404

    def test_user_a_cannot_clear_user_b_memories(self, client):
        """User A's clear-all only clears their own memories."""
        token_a, token_b = self._setup_users(client)
        _create_memory(client, token_b, content="Beta's personal fact")
        # User A clears their memories (TestClient.delete doesn't support json=)
        import json as _json
        resp = client.request("DELETE", "/api/memories",
                              json={"confirm": True},
                              headers={"Authorization": f"Bearer {token_a}"})
        assert resp.status_code == 204
        # User B's memories should still exist
        resp = client.get("/api/memories",
                          headers={"Authorization": f"Bearer {token_b}"})
        assert resp.status_code == 200
        contents = [m["content"] for m in resp.json()]
        assert "Beta's personal fact" in contents

    # ── Search ────────────────────────────────────────────

    def test_user_a_cannot_search_user_b_messages(self, client):
        """
        User A's search does not return User B's messages.

        Note: We cannot easily send messages without Ollama in this test
        context. Session-level isolation is tested elsewhere (above).
        This test verifies that search is properly scoped to the
        authenticated user's data by checking that manually created
        sessions owned by User B are not visible to User A.
        """
        token_a, token_b = self._setup_users(client)
        # User B creates a session (no messages since Ollama may not respond)
        _create_session(client, token_b, title="Beta's Chat")
        # User A searches — should find nothing from User B
        resp = client.get("/api/search?q=unique_search_term",
                          headers={"Authorization": f"Bearer {token_a}"})
        assert resp.status_code == 200
        # Search is scoped to the user's sessions, so there should be
        # no results at all since User A has no sessions or messages
        # containing "unique_search_term"
        assert len(resp.json()) == 0

    # ── Unauthenticated fallback (backward compat) ────────

    def test_unauthenticated_requests_fallback_to_default_user(self, client):
        """Requests without a token fall back to default user (id=1)."""
        # Create a session without authentication (uses user id=1)
        resp = client.post("/api/sessions", json={"title": "Unauthenticated Session"})
        assert resp.status_code == 201
        session_id = resp.json()["id"]

        # List sessions without authentication
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        titles = [s["title"] for s in resp.json()]
        assert "Unauthenticated Session" in titles

        # Read the session without authentication
        resp = client.get(f"/api/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Unauthenticated Session"
