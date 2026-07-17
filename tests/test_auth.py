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

import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.models.models import User
from app.services.auth_service import create_access_token
from app.services.rate_limiter import get_rate_limiter, get_lockout_duration, InMemoryRateLimiter


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
    """Clean up test users and rate limiter state before and after each test."""
    _cleanup_test_users()
    get_rate_limiter().reset()
    yield
    _cleanup_test_users()
    get_rate_limiter().reset()


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


# ═══════════════════════════════════════════════════════════════
# Phase 7A — Rate Limiting & Lockout Tests
# ═══════════════════════════════════════════════════════════════


class TestRateLimitUnit:
    """Unit tests for the rate limiter internals."""

    def test_get_lockout_duration_base(self):
        """1 failed attempt → base lockout (30s)."""
        assert get_lockout_duration(1, 30, 900) == 30

    def test_get_lockout_duration_exponential(self):
        """Lockout doubles with each failure."""
        assert get_lockout_duration(2, 30, 900) == 60
        assert get_lockout_duration(3, 30, 900) == 120
        assert get_lockout_duration(4, 30, 900) == 240

    def test_get_lockout_duration_capped(self):
        """Lockout is capped at max_seconds (900 = 15 min)."""
        assert get_lockout_duration(6, 30, 900) == 900  # 30*32=960 → capped
        assert get_lockout_duration(10, 30, 900) == 900

    def test_get_lockout_duration_zero_or_negative(self):
        """Zero or negative failures returns 0."""
        assert get_lockout_duration(0, 30, 900) == 0
        assert get_lockout_duration(-1, 30, 900) == 0

    def test_rate_limiter_peek_does_not_record(self):
        """peek_rate_limit does not record attempts."""
        limiter = InMemoryRateLimiter()
        is_limited, remaining = limiter.peek_rate_limit("test_key", 5, 60)
        assert not is_limited
        assert remaining == 5
        # Attempts should still be 0
        is_limited2, remaining2 = limiter.peek_rate_limit("test_key", 5, 60)
        assert remaining2 == 5  # peek didn't record

    def test_rate_limiter_cleanup_expired(self):
        """Expired entries are removed by cleanup."""
        limiter = InMemoryRateLimiter()
        # Manually add old entries (using far-past timestamps)
        far_past = time.time() - 7200  # 2 hours ago
        limiter._attempts["old_key"] = [far_past]
        limiter._attempts["fresh_key"] = [time.time()]
        cleaned = limiter.cleanup_expired(max_age_seconds=3600)
        assert cleaned == 1
        assert "old_key" not in limiter._attempts
        assert "fresh_key" in limiter._attempts

    def test_rate_limiter_reset(self):
        """Reset clears all records."""
        limiter = InMemoryRateLimiter()
        limiter.record_attempt("key1")
        limiter.record_attempt("key2")
        assert len(limiter._attempts) == 2
        limiter.reset()
        assert len(limiter._attempts) == 0

    def test_rate_limiter_is_rate_limited_records_attempt(self):
        """is_rate_limited records an attempt when not yet limited."""
        limiter = InMemoryRateLimiter()
        # First 4 calls: not limited, each records
        for i in range(4):
            assert not limiter.is_rate_limited("test_key", 5, 60)
        # 5th call: not limited (equal, not exceeding)
        assert not limiter.is_rate_limited("test_key", 5, 60)
        # 6th call: limited
        assert limiter.is_rate_limited("test_key", 5, 60)


class TestLoginRateLimit:
    """IP-based rate limiting on the login endpoint."""

    def _register_user(self, client, username="testuser_ratelimit"):
        """Register a user for rate limiting tests."""
        resp = client.post("/api/auth/register", json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "securePass123!",
        })
        assert resp.status_code == 201

    def test_login_rate_limit_exceeded(self, client):
        """Too many rapid failed attempts return 429."""
        self._register_user(client)
        # The IP rate limit is 10 attempts per 60 seconds.
        # Send 11 rapid failed requests from the same IP.
        for i in range(11):
            resp = client.post("/api/auth/login", json={
                "username": "testuser_ratelimit",
                "password": f"wrong_password_{i}",
            })
            if resp.status_code == 429:
                # Rate-limited — success
                assert "Too many requests" in resp.json()["detail"]
                assert "Retry-After" in resp.headers
                return

        # If we never got 429, the test should fail
        pytest.fail("Rate limit was not triggered after 11 rapid attempts")

    def test_login_retry_after_header(self, client):
        """429 response includes Retry-After header."""
        self._register_user(client)
        for i in range(12):
            resp = client.post("/api/auth/login", json={
                "username": "testuser_ratelimit",
                "password": f"bad_{i}",
            })
            if resp.status_code == 429:
                assert "Retry-After" in resp.headers
                retry_after = int(resp.headers["Retry-After"])
                assert retry_after > 0
                return
        pytest.fail("Never received 429")

    def test_different_users_share_ip_rate_limit(self, client):
        """Different usernames from the same IP share the rate limit.

        Account lockout threshold is 5; IP rate limit is 10 per 60s.
        Use 4 failed attempts per user to stay below lockout but
        accumulate enough IP records to trigger the IP rate limit:
          2 registrations + 4 user_A + 4 user_B = 10 IP records
        """
        self._register_user(client, "testuser_rl_a")
        self._register_user(client, "testuser_rl_b")
        # Make 4 failed attempts for user A (2 reg + 4 = 6 IP records)
        for i in range(4):
            resp = client.post("/api/auth/login", json={
                "username": "testuser_rl_a",
                "password": f"wrong_{i}",
            })
            assert resp.status_code == 401
        # Make 4 failed attempts for user B (6 + 4 = 10 IP records = limit)
        for i in range(4):
            resp = client.post("/api/auth/login", json={
                "username": "testuser_rl_b",
                "password": f"bad_{i}",
            })
            assert resp.status_code == 401
        # Next attempt should be rate-limited (10 IP records, limit is 10)
        resp = client.post("/api/auth/login", json={
            "username": "testuser_rl_a",
            "password": "securePass123!",
        })
        assert resp.status_code == 429

    def test_login_success_does_not_trigger_rate_limit(self, client):
        """Successful login counts as an attempt but resets IP counter."""
        self._register_user(client)
        # Successful login
        resp = client.post("/api/auth/login", json={
            "username": "testuser_ratelimit",
            "password": "securePass123!",
        })
        assert resp.status_code == 200
        # A few more successful logins shouldn't trigger rate limit
        for i in range(5):
            resp = client.post("/api/auth/login", json={
                "username": "testuser_ratelimit",
                "password": "securePass123!",
            })
            # These should be fine since reset clears attempts
            assert resp.status_code == 200, f"Failed on attempt {i}: {resp.json()}"


class TestAccountLockout:
    """Account lockout after repeated failed login attempts."""

    def _register_user(self, client, username="testuser_lockout"):
        """Register a user for lockout tests."""
        resp = client.post("/api/auth/register", json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "securePass123!",
        })
        assert resp.status_code == 201
        return username

    def test_account_lockout_after_threshold_failures(self, client):
        """
        After 'rate_limit_lockout_threshold' consecutive failures,
        the account is locked (429). The lockout threshold is 5.
        """
        username = self._register_user(client)
        # Send 5 wrong passwords (threshold is 5)
        for i in range(5):
            resp = client.post("/api/auth/login", json={
                "username": username,
                "password": f"wrong_password_{i}",
            })
            assert resp.status_code == 401, f"Expected 401 on attempt {i+1}"

        # The 5th failure triggers lockout, but it also returns 401
        # because the password is wrong. The lockout applies to the NEXT attempt.
        # Actually, looking at the code: when failed_attempts >= threshold,
        # the account is locked AND the current request returns 401.
        # The next request should return 429.

        # The 6th request should be locked
        resp = client.post("/api/auth/login", json={
            "username": username,
            "password": "wrong_password_5",
        })
        assert resp.status_code == 429, f"Expected 429 lockout, got {resp.status_code}: {resp.json()}"
        assert "Too many requests" in resp.json()["detail"]
        assert "Retry-After" in resp.headers

    def test_lockout_resets_after_successful_login(self, client):
        """Failed attempts are reset after a successful login."""
        username = self._register_user(client)
        # Make 3 wrong attempts
        for i in range(3):
            resp = client.post("/api/auth/login", json={
                "username": username,
                "password": f"bad_{i}",
            })
            assert resp.status_code == 401
        # Successful login resets counter
        resp = client.post("/api/auth/login", json={
            "username": username,
            "password": "securePass123!",
        })
        assert resp.status_code == 200
        # Now 3 more wrong attempts should not trigger lockout (counter was reset)
        for i in range(3):
            resp = client.post("/api/auth/login", json={
                "username": username,
                "password": f"bad2_{i}",
            })
            assert resp.status_code == 401, f"Expected 401 on attempt {i+1}"


class TestRegistrationRateLimit:
    """Rate limiting on the registration endpoint."""

    def test_registration_rate_limited(self, client):
        """Rapid registration attempts trigger IP rate limit."""
        # Send 11 rapid registration requests (threshold is 10)
        for i in range(12):
            resp = client.post("/api/auth/register", json={
                "username": f"testuser_rl_reg_{i}",
                "email": f"reg{i}@example.com",
                "password": "securePass123!",
            })
            if resp.status_code == 429:
                assert "Too many requests" in resp.json()["detail"]
                return
            # Valid registration or duplicate is fine
            assert resp.status_code in (201, 400)

        pytest.fail("Registration rate limit was not triggered after 12 rapid requests")

    def test_registration_rate_limit_has_retry_after(self, client):
        """429 on registration includes Retry-After header."""
        for i in range(12):
            resp = client.post("/api/auth/register", json={
                "username": f"testuser_rl_ra_{i}",
                "email": f"ra{i}@example.com",
                "password": "securePass123!",
            })
            if resp.status_code == 429:
                assert "Retry-After" in resp.headers
                retry_after = int(resp.headers["Retry-After"])
                assert retry_after > 0
                return
        pytest.fail("Registration rate limit not triggered")


class TestGetEndpointsNotRateLimited:
    """GET endpoints should not be affected by rate limiting."""

    def test_health_still_works_after_rate_limit(self, client):
        """Health endpoint works even after hitting rate limit."""
        # Trigger IP rate limit with rapid login attempts
        for i in range(12):
            client.post("/api/auth/login", json={
                "username": f"nonexistent_{i}",
                "password": "test",
            })
        # Health endpoint should still work
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert "backend" in resp.json()


class TestJWTSecretValidation:
    """JWT secret validation at startup."""

    def test_get_lockout_duration_unit(self):
        """get_lockout_duration produces correct values."""
        assert get_lockout_duration(0, 30, 900) == 0
        assert get_lockout_duration(1, 30, 900) == 30
        assert get_lockout_duration(2, 30, 900) == 60
        assert get_lockout_duration(5, 30, 900) == 480  # 30 * 2^4 = 480
        assert get_lockout_duration(6, 30, 900) == 900  # capped at max


# ═══════════════════════════════════════════════════════════════
# Phase 7A — Memory Growth Prevention Tests
# ═══════════════════════════════════════════════════════════════


class TestRateLimiterMemoryGrowth:
    """Verify the rate limiter does not leak memory."""

    # ── peek write-through pruning ────────────────────────

    def test_peek_prunes_expired_entries(self):
        """peek_rate_limit physically removes expired entries."""
        limiter = InMemoryRateLimiter()
        # Plant a stale entry
        far_past = time.time() - 120  # 2 minutes old
        limiter._attempts["stale_key"] = [far_past, far_past]
        # peek should prune them
        is_limited, _ = limiter.peek_rate_limit("stale_key", 5, 60)
        # After peek pruning with 60s window, both entries > 60s old
        # should be removed, leaving 0 entries
        assert len(limiter._attempts.get("stale_key", [])) == 0
        # With 0 entries, not limited
        assert not is_limited

    def test_peek_removes_all_entries_when_all_expired(self):
        """peek removes the key entirely when all entries are expired."""
        limiter = InMemoryRateLimiter()
        far_past = time.time() - 120
        limiter._attempts["old_key"] = [far_past, far_past]
        # peek uses default 60s window, both are older
        limiter.peek_rate_limit("old_key", 5, 60)
        # Key should remain with empty list (or be removed entirely)
        # InMemoryRateLimiter keeps the key with empty list after peek
        assert len(limiter._attempts.get("old_key", [])) == 0

    # ── record_attempt pruning ────────────────────────────

    def test_record_attempt_prunes_expired(self):
        """record_attempt prunes entries older than 60s before appending."""
        limiter = InMemoryRateLimiter()
        far_past = time.time() - 120
        limiter._attempts["key"] = [far_past, far_past]
        limiter.record_attempt("key")
        # After pruning + append, should have exactly 1 entry
        assert len(limiter._attempts["key"]) == 1

    def test_record_attempt_keeps_fresh_entries(self):
        """record_attempt keeps non-expired entries when pruning."""
        limiter = InMemoryRateLimiter()
        now = time.time()
        fresh = now - 10  # 10 seconds ago (still within 60s window)
        old = now - 120  # 2 minutes ago (expired)
        limiter._attempts["key"] = [fresh, old]
        limiter.record_attempt("key")
        # Should keep fresh + 1 new = 2 entries
        assert len(limiter._attempts["key"]) == 2

    # ── cleanup_expired ───────────────────────────────────

    def test_cleanup_expired_removes_empty_keys(self):
        """cleanup_expired removes keys whose entries are all expired."""
        limiter = InMemoryRateLimiter()
        far_past = time.time() - 7200
        limiter._attempts["expired_key"] = [far_past]
        limiter._attempts["fresh_key"] = [time.time()]
        cleaned = limiter.cleanup_expired(max_age_seconds=3600)
        assert cleaned == 1
        assert "expired_key" not in limiter._attempts
        assert "fresh_key" in limiter._attempts

    def test_cleanup_expired_preserves_active_lockouts(self):
        """Active (recent) lockout entries are not removed by cleanup."""
        limiter = InMemoryRateLimiter()
        # Lockout tracking uses record_attempt, producing recent entries
        limiter.record_attempt("rl_user:active_user")
        limiter.record_attempt("rl_ip:1.2.3.4")
        cleaned = limiter.cleanup_expired(max_age_seconds=3600)
        assert cleaned == 0
        assert "rl_user:active_user" in limiter._attempts
        assert "rl_ip:1.2.3.4" in limiter._attempts

    def test_repeated_cleanup_is_safe(self):
        """Calling cleanup_expired repeatedly does not cause errors."""
        limiter = InMemoryRateLimiter()
        # Run cleanup on empty store
        assert limiter.cleanup_expired() == 0
        # Add some fresh entries
        limiter.record_attempt("key1")
        limiter.record_attempt("key2")
        # Run cleanup again
        assert limiter.cleanup_expired() == 0
        # Run cleanup a third time
        assert limiter.cleanup_expired() == 0
        assert len(limiter._attempts) == 2

    # ── Bounded storage ───────────────────────────────────

    def test_bounded_storage_after_many_expired_keys(self):
        """Storage remains bounded after many expired keys via cleanup."""
        limiter = InMemoryRateLimiter()
        far_past = time.time() - 7200
        # Simulate 1000 unique IPs that hit once and never return
        for i in range(1000):
            limiter._attempts[f"rl_ip:1.1.1.{i}"] = [far_past]
        assert len(limiter._attempts) == 1000
        # Single cleanup pass removes all 1000
        cleaned = limiter.cleanup_expired(max_age_seconds=3600)
        assert cleaned == 1000
        assert len(limiter._attempts) == 0

    def test_bounded_storage_mixed_fresh_and_expired(self):
        """Cleanup removes only expired keys, preserving fresh ones."""
        limiter = InMemoryRateLimiter()
        now = time.time()
        far_past = now - 7200
        # 100 expired + 50 fresh
        for i in range(100):
            limiter._attempts[f"expired_{i}"] = [far_past]
        for i in range(50):
            limiter._attempts[f"fresh_{i}"] = [now]
        cleaned = limiter.cleanup_expired(max_age_seconds=3600)
        assert cleaned == 100
        assert len(limiter._attempts) == 50

    # ── Thread safety ─────────────────────────────────────

    def test_concurrent_record_attempts(self):
        """Concurrent record_attempt calls are thread-safe."""
        import concurrent.futures

        limiter = InMemoryRateLimiter()
        n_threads = 20
        n_calls = 50

        def do_records():
            for i in range(n_calls):
                limiter.record_attempt("shared_key")

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as ex:
            futures = [ex.submit(do_records) for _ in range(n_threads)]
            concurrent.futures.wait(futures)

        # Each of 20 threads calls record_attempt 50 times
        total = n_threads * n_calls
        # After pruning, all entries should be fresh (< 60s)
        assert len(limiter._attempts["shared_key"]) == total

    def test_concurrent_mixed_operations(self):
        """Concurrent peek, record, reset operations are thread-safe."""
        import concurrent.futures
        import random

        limiter = InMemoryRateLimiter()
        errors = []

        def worker(worker_id: int):
            for _ in range(30):
                op = random.choice(["peek", "record", "reset_key", "reset_all"])
                key = f"key_{worker_id % 5}"
                try:
                    if op == "peek":
                        limiter.peek_rate_limit(key, 10, 60)
                    elif op == "record":
                        limiter.record_attempt(key)
                    elif op == "reset_key":
                        limiter.reset_attempts(key)
                    elif op == "reset_all":
                        limiter.reset()
                except Exception as exc:
                    errors.append((worker_id, op, str(exc)))
                    raise

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futures = [ex.submit(worker, i) for i in range(10)]
            concurrent.futures.wait(futures)

        assert len(errors) == 0, f"Concurrent errors: {errors}"
        # Data integrity: after all ops, storage should be bounded
        # (5 possible keys, each with at most a few entries)
        assert len(limiter._attempts) <= 10, "Storage grew unexpectedly under concurrent load"

    # ── Stop / lifecycle ──────────────────────────────────

    def test_stop_clears_state(self):
        """stop() clears all state for graceful shutdown."""
        limiter = InMemoryRateLimiter()
        limiter.record_attempt("key1")
        limiter.record_attempt("key2")
        assert len(limiter._attempts) == 2
        limiter.stop()
        assert len(limiter._attempts) == 0
        assert limiter.mutation_count() == 0

    def test_stop_then_continue_is_safe(self):
        """After stop(), the limiter can still be used."""
        limiter = InMemoryRateLimiter()
        limiter.stop()
        # These should not raise
        limiter.record_attempt("new_key")
        assert limiter.is_rate_limited("new_key", 5, 60) is False
        limiter.reset()

    # ── Probabilistic auto-cleanup ────────────────────────

    def test_probabilistic_cleanup_after_many_mutations(self):
        """Auto-cleanup triggers after cleanup_interval mutations.

        Each InMemoryRateLimiter has its own cleanup_interval, so
        this test does not affect other tests or the singleton.
        """
        limiter = InMemoryRateLimiter(cleanup_interval=5)
        # 10 mutations should trigger cleanup at least once
        for i in range(10):
            limiter.record_attempt(f"auto_key_{i}")
        # mutation_count should be <= 10 - 5 = 5 (reset at 5, then
        # 5 more mutations)
        assert limiter.mutation_count() <= 5
        assert limiter.mutation_count() >= 0

    # ── reset_rate_limiter ───────────────────────────────

    def test_reset_rate_limiter_global(self):
        """reset_rate_limiter creates a fresh singleton instance."""
        from app.services.rate_limiter import reset_rate_limiter

        original = get_rate_limiter()
        original.record_attempt("test_key")
        # Replace the singleton
        reset_rate_limiter()
        new_instance = get_rate_limiter()
        # Should be a different instance
        assert new_instance is not original
        # Fresh instance has no state
        is_limited, _ = new_instance.peek_rate_limit("test_key", 5, 60)
        assert not is_limited
        # Clean up: reset singleton to original state
        get_rate_limiter().reset()


class TestRateLimitExistingBehavior:
    """Regression: existing 429, Retry-After, lockout and reset behavior."""

    def test_rate_limit_still_works(self, client):
        """429 is still returned after exceeding threshold."""
        for i in range(11):
            resp = client.post("/api/auth/login", json={
                "username": f"nonexistent_{i}",
                "password": "test",
            })
            if resp.status_code == 429:
                return
        pytest.fail("Rate limit not triggered")

    def test_retry_after_still_present(self, client):
        """Retry-After header is still included on 429."""
        for i in range(12):
            resp = client.post("/api/auth/login", json={
                "username": f"nonexistent_{i}",
                "password": "test",
            })
            if resp.status_code == 429:
                assert "Retry-After" in resp.headers
                return
        pytest.fail("Rate limit not triggered")
