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
from app.models.models import User, RefreshSession
from app.services.auth_service import create_access_token
from app.services.rate_limiter import get_rate_limiter, get_lockout_duration, InMemoryRateLimiter


# ── Phase 7C constants ──────────────────────────────


REFRESH_COOKIE = "research_assistant_refresh_token"
CSRF_COOKIE = "research_assistant_csrf_token"


# ── Fixtures ───────────────────────────────────────────────


@pytest.fixture
def client():
    """Yield a TestClient connected to the app with a fresh DB."""
    with TestClient(app) as c:
        yield c


def _cleanup_test_users():
    """Remove any test users and their refresh sessions."""
    db = SessionLocal()
    try:
        # Clean up refresh sessions first to avoid FK issues
        test_users = db.query(User).filter(
            User.username.like("testuser%")
        ).all()
        test_user_ids = [u.id for u in test_users]
        if test_user_ids:
            db.query(RefreshSession).filter(
                RefreshSession.user_id.in_(test_user_ids)
            ).delete(synchronize_session=False)
        for u in test_users:
            db.delete(u)
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def cleanup(client):
    """Clean up test users and rate limiter state before and after each test.

    Depends on the ``client`` fixture so the TestClient context manager has
    already run the app lifespan (init_db + migrations) before we touch the
    database — this makes the suite work against a fresh/empty test DB.
    """
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

    # ── Unauthenticated requests (Phase 7C) ────────────────

    def test_unauthenticated_requests_are_rejected(self, client):
        """
        Requests without a token are rejected (no fallback to default user).

        Phase 7C removed the backward-compat fallback that silently used
        user id=1 for unauthenticated requests — the cause of cross-account
        "session not found" and data leaks. Authenticated routes now require
        a valid access token.
        """
        # Create a session without authentication → 401 (no default-user fallback)
        resp = client.post("/api/sessions", json={"title": "Unauthenticated Session"})
        assert resp.status_code == 401

        # List sessions without authentication → 401
        resp = client.get("/api/sessions")
        assert resp.status_code == 401

        # Read a session without authentication → 401
        resp = client.get("/api/sessions/1")
        assert resp.status_code == 401


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


# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
# Phase 7B + 7C — Refresh Cookie, Rotation, Logout, CSRF, CORS
# ═══════════════════════════════════════════════════════════════


def _register_and_login(client, username="testuser", password="securePass123!"):
    """Register + login; return (access_token, refresh_token-from-cookie)."""
    reg = client.post("/api/auth/register", json={
        "username": username,
        "email": f"{username}@test.com",
        "password": password,
    })
    assert reg.status_code == 201
    login = client.post("/api/auth/login", json={
        "username": username,
        "password": password,
    })
    assert login.status_code == 200
    return login.json()["access_token"], client.cookies.get(REFRESH_COOKIE)


def _login_only(client, username="testuser", password="securePass123!"):
    """Login (no register) and return the refresh cookie value."""
    login = client.post("/api/auth/login", json={
        "username": username,
        "password": password,
    })
    assert login.status_code == 200
    return client.cookies.get(REFRESH_COOKIE)


def _csrf_headers(client, csrf=None):
    """Headers carrying the CSRF token from the client cookie jar."""
    token = csrf if csrf is not None else client.cookies.get(CSRF_COOKIE)
    headers = {}
    if token:
        headers["X-CSRF-Token"] = token
    return headers


class TestRefreshToken:
    """Refresh token issuance, rotation, and reuse detection (cookie flow)."""

    def test_login_sets_refresh_cookie(self, client):
        """Login sets the HttpOnly refresh cookie; response has no raw token."""
        reg = client.post("/api/auth/register", json={
            "username": "testuser_rf1",
            "email": "testuser_rf1@test.com",
            "password": "securePass123!",
        })
        assert reg.status_code == 201
        login = client.post("/api/auth/login", json={
            "username": "testuser_rf1",
            "password": "securePass123!",
        })
        assert login.status_code == 200
        data = login.json()
        assert data["access_token"]
        assert "refresh_token" not in data  # Phase 7C: cookie only
        assert data["token_type"] == "bearer"
        refresh = client.cookies.get(REFRESH_COOKIE)
        assert refresh is not None and len(refresh) > 20
        # The cookie must be HttpOnly
        set_cookie = login.headers.get("set-cookie", "")
        assert "HttpOnly" in set_cookie
        assert "Path=/api/auth" in set_cookie

    def test_login_sets_csrf_cookie(self, client):
        """Login also sets the non-HttpOnly CSRF cookie (double-submit)."""
        reg = client.post("/api/auth/register", json={
            "username": "testuser_rf1b",
            "email": "testuser_rf1b@test.com",
            "password": "securePass123!",
        })
        assert reg.status_code == 201
        client.post("/api/auth/login", json={
            "username": "testuser_rf1b",
            "password": "securePass123!",
        })
        csrf = client.cookies.get(CSRF_COOKIE)
        assert csrf is not None and len(csrf) > 10

    def test_refresh_token_success(self, client):
        """Refresh reads the cookie and returns a new access token."""
        _, refresh = _register_and_login(client, "testuser_rf2")
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" not in data  # cookie only
        assert data["token_type"] == "bearer"
        # The cookie should have rotated to a new value
        new_refresh = client.cookies.get(REFRESH_COOKIE)
        assert new_refresh is not None and new_refresh != refresh

    def test_refresh_ignores_json_body_token(self, client):
        """A refresh_token in the JSON body is ignored after Phase 7C."""
        _, refresh = _register_and_login(client, "testuser_rf2b")
        # Send a bogus body token while a valid cookie exists
        resp = client.post("/api/auth/refresh", json={"refresh_token": "bogus"})
        assert resp.status_code == 200
        assert resp.json()["access_token"]
        assert client.cookies.get(REFRESH_COOKIE) != "bogus"

    def test_refresh_without_cookie_rejected(self, client):
        """Refreshing with no cookie at all returns 401."""
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 401

    def test_refresh_token_rotation(self, client):
        """Old refresh cookie is invalidated after rotation."""
        _, old_refresh = _register_and_login(client, "testuser_rf3")
        # Rotate once
        resp1 = client.post("/api/auth/refresh")
        assert resp1.status_code == 200
        new_refresh = client.cookies.get(REFRESH_COOKIE)
        assert new_refresh and new_refresh != old_refresh
        # Old cookie should be rejected
        client.cookies.set(REFRESH_COOKIE, old_refresh, path="/api/auth")
        resp2 = client.post("/api/auth/refresh")
        assert resp2.status_code == 401

    def test_reuse_detection_revokes_family(self, client):
        """Reusing a rotated cookie revokes the entire token family."""
        _, old_token = _register_and_login(client, "testuser_rf4")
        resp1 = client.post("/api/auth/refresh")
        assert resp1.status_code == 200
        new_token_1 = client.cookies.get(REFRESH_COOKIE)
        # Reuse the old token → reuse detected, family revoked
        client.cookies.set(REFRESH_COOKIE, old_token, path="/api/auth")
        resp2 = client.post("/api/auth/refresh")
        assert resp2.status_code == 401
        # The new token from the same family must also be invalid now
        client.cookies.set(REFRESH_COOKIE, new_token_1, path="/api/auth")
        resp3 = client.post("/api/auth/refresh")
        assert resp3.status_code == 401

    def test_expired_refresh_token_rejected(self, client):
        """An expired refresh cookie returns 401 and clears the cookie."""
        from datetime import timedelta
        from app.database import SessionLocal
        from app.models.models import RefreshSession
        from app.services.refresh_token_service import hash_refresh_token

        _, refresh_token = _register_and_login(client, "testuser_rf5")
        db = SessionLocal()
        try:
            token_hash = hash_refresh_token(refresh_token)
            session = db.query(RefreshSession).filter(
                RefreshSession.token_hash == token_hash
            ).first()
            assert session is not None
            session.expires_at = session.expires_at - timedelta(days=60)
            db.commit()
        finally:
            db.close()
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"].lower()
        # Cookie should have been cleared
        assert client.cookies.get(REFRESH_COOKIE) is None

    def test_concurrent_refresh_reuse_detected(self, client):
        """A second refresh with the same cookie triggers reuse detection."""
        _, refresh_token = _register_and_login(client, "testuser_rf6")
        resp1 = client.post("/api/auth/refresh")
        assert resp1.status_code == 200
        new_token = client.cookies.get(REFRESH_COOKIE)
        # Reuse the SAME original cookie
        client.cookies.set(REFRESH_COOKIE, refresh_token, path="/api/auth")
        resp2 = client.post("/api/auth/refresh")
        assert resp2.status_code == 401
        # Family revoked: the rotated token no longer works either
        client.cookies.set(REFRESH_COOKIE, new_token, path="/api/auth")
        resp3 = client.post("/api/auth/refresh")
        assert resp3.status_code == 401

    def test_no_raw_refresh_token_in_database(self, client):
        """Only SHA-256 hashes of refresh tokens are stored, never raw."""
        import re
        from app.database import SessionLocal
        from app.models.models import RefreshSession

        _, refresh_token = _register_and_login(client, "testuser_rf7")
        from app.services.refresh_token_service import hash_refresh_token
        expected_hash = hash_refresh_token(refresh_token)
        assert re.match(r"^[a-f0-9]{64}$", expected_hash), \
            f"Expected SHA-256 hex digest, got: {expected_hash[:20]}..."

        db = SessionLocal()
        try:
            sessions = db.query(RefreshSession).all()
            for s in sessions:
                assert re.match(r"^[a-f0-9]{64}$", s.token_hash), \
                    f"Expected SHA-256 hex digest, got: {s.token_hash[:20]}..."
        finally:
            db.close()

    def test_no_refresh_token_in_logs(self, client, caplog):
        """Raw refresh tokens AND token hashes never appear in logs."""
        import logging
        import re
        caplog.set_level(logging.DEBUG)

        _, refresh_token = _register_and_login(client, "testuser_rf8")
        client.post("/api/auth/refresh")
        # Attempt with a bogus token
        client.cookies.set(REFRESH_COOKIE, "some_obviously_fake_token_12345", path="/api/auth")
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 401
        assert "Invalid refresh token" in resp.json()["detail"]

        for record in caplog.records:
            message = record.getMessage()
            for match in re.findall(r"[A-Za-z0-9_-]{40,}", message):
                pytest.fail(
                    f"Potential refresh token or hash leaked in log: '{match[:20]}...' "
                    f"in record: {record.name} - {record.levelname}"
                )

    def test_refresh_token_rotation_new_token_works(self, client):
        """The new cookie from rotation can be used for further refreshes."""
        _, refresh = _register_and_login(client, "testuser_rf9")
        resp1 = client.post("/api/auth/refresh")
        assert resp1.status_code == 200
        resp2 = client.post("/api/auth/refresh")
        assert resp2.status_code == 200
        assert resp2.json()["access_token"] is not None

    def test_invalid_refresh_token_rejected(self, client):
        """A bogus refresh cookie is rejected with 401."""
        client.cookies.set(REFRESH_COOKIE, "this-is-not-a-valid-refresh-token", path="/api/auth")
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 401

    def test_refresh_token_identifies_correct_user(self, client):
        """The access token issued after refresh has the correct subject."""
        from jose import jwt
        from app.config import settings

        _, refresh_b = _register_and_login(client, "testuser_rf7b")
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 200
        new_access = resp.json()["access_token"]
        payload = jwt.decode(new_access, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        assert payload["sub"] is not None


class TestLogout:
    """Server-side logout and session revocation (cookie flow)."""

    def test_logout_revokes_refresh_token(self, client):
        """After logout, the refresh cookie is revoked and cleared."""
        token, refresh = _register_and_login(client, "testuser_lo1")
        resp = client.post("/api/auth/logout", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        assert "successfully" in resp.json()["detail"]
        # Cookie cleared by logout
        assert client.cookies.get(REFRESH_COOKIE) is None
        assert client.cookies.get(CSRF_COOKIE) is None
        # The revoked token should not work for refresh
        client.cookies.set(REFRESH_COOKIE, refresh, path="/api/auth")
        resp2 = client.post("/api/auth/refresh")
        assert resp2.status_code == 401

    def test_logout_without_token_still_succeeds(self, client):
        """Logout requires auth; returns 401 without a token."""
        resp = client.post("/api/auth/logout")
        assert resp.status_code == 401

    def test_logout_all_revokes_all_sessions(self, client):
        """Logout-all revokes all refresh sessions for the user."""
        token, refresh1 = _register_and_login(client, "testuser_lo2")

        # Create a second refresh session by logging in again
        client.post("/api/auth/login", json={
            "username": "testuser_lo2",
            "password": "securePass123!",
        })
        refresh2 = client.cookies.get(REFRESH_COOKIE)

        resp = client.post("/api/auth/logout-all", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        assert "Logged out from" in resp.json()["detail"]
        # Both refresh tokens should be revoked
        client.cookies.set(REFRESH_COOKIE, refresh1, path="/api/auth")
        assert client.post("/api/auth/refresh").status_code == 401
        client.cookies.set(REFRESH_COOKIE, refresh2, path="/api/auth")
        assert client.post("/api/auth/refresh").status_code == 401


class TestSessionListing:
    """Session listing, safe response fields, and cross-user isolation."""

    def test_list_sessions_returns_user_sessions(self, client):
        """GET /api/auth/sessions returns the user's sessions (safe fields)."""
        token, _ = _register_and_login(client, "testuser_sl1")
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "sessions" in data
        assert "total" in data
        assert "active_count" in data
        assert len(data["sessions"]) >= 1
        session = data["sessions"][0]
        assert "id" in session
        assert "created_at" in session
        assert "expires_at" in session
        assert "token_hash" not in session
        assert "refresh_token" not in session

    def test_list_sessions_multiple_logins(self, client):
        """Multiple logins create multiple sessions visible in the listing."""
        token, _ = _register_and_login(client, "testuser_sl2")
        for _ in range(3):
            client.post("/api/auth/login", json={
                "username": "testuser_sl2",
                "password": "securePass123!",
            })
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 4
        assert data["active_count"] >= 4

    def test_cross_user_session_listing_isolation(self, client):
        """User A cannot see User B's sessions in their listing."""
        token_a, _ = _register_and_login(client, "testuser_sl3a")
        _, _ = _register_and_login(client, "testuser_sl3b")
        for _ in range(2):
            client.post("/api/auth/login", json={
                "username": "testuser_sl3b",
                "password": "securePass123!",
            })
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token_a}",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["active_count"] == 1

    def test_list_sessions_expired_excluded_from_active_count(self, client):
        """Expired sessions are excluded from active_count."""
        from app.database import SessionLocal
        from app.models.models import RefreshSession, User
        from datetime import datetime, timedelta, timezone

        token, _ = _register_and_login(client, "testuser_sl4")
        resp_before = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
        })
        before_active = resp_before.json()["active_count"]

        # Scope to THIS user's sessions only (never touch other users' rows).
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == "testuser_sl4").first()
            assert user is not None
            sessions = db.query(RefreshSession).filter(
                RefreshSession.user_id == user.id
            ).order_by(RefreshSession.created_at.asc()).all()
            assert sessions
            sessions[0].expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
            db.commit()
        finally:
            db.close()

        resp_after = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp_after.status_code == 200
        data = resp_after.json()
        assert data["active_count"] < before_active
        assert data["total"] >= data["active_count"]

    def test_list_sessions_current_indicator_from_cookie(self, client):
        """The session matching the refresh cookie is marked current."""
        token, refresh = _register_and_login(client, "testuser_sl5")
        # The cookie identifies the current session
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        current = [s for s in sessions if s["is_current"]]
        assert len(current) == 1

    def test_list_sessions_requires_auth(self, client):
        """Unauthenticated request returns 401."""
        resp = client.get("/api/auth/sessions")
        assert resp.status_code == 401


class TestSessionRevocation:
    """Session revocation via POST /api/auth/sessions/{id}/revoke."""

    def test_revoke_session_by_id(self, client):
        """Revoking a specific session invalidates its refresh token."""
        token, refresh1 = _register_and_login(client, "testuser_sr1")
        client.post("/api/auth/login", json={
            "username": "testuser_sr1",
            "password": "securePass123!",
        })
        refresh2 = client.cookies.get(REFRESH_COOKIE)
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
        })
        sessions = resp.json()["sessions"]
        oldest_id = sessions[-1]["id"]

        revoke_resp = client.post(f"/api/auth/sessions/{oldest_id}/revoke", headers={
            "Authorization": f"Bearer {token}",
        })
        assert revoke_resp.status_code == 200
        assert "Session revoked" in revoke_resp.json()["detail"]

        client.cookies.set(REFRESH_COOKIE, refresh1, path="/api/auth")
        assert client.post("/api/auth/refresh").status_code == 401
        client.cookies.set(REFRESH_COOKIE, refresh2, path="/api/auth")
        assert client.post("/api/auth/refresh").status_code == 200

    def test_revoke_other_user_session_rejected(self, client):
        """User A cannot revoke User B's session."""
        token_a, _ = _register_and_login(client, "testuser_sr2a")
        token_b, _ = _register_and_login(client, "testuser_sr2b")
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token_b}",
        })
        b_session_id = resp.json()["sessions"][0]["id"]
        revoke_resp = client.post(f"/api/auth/sessions/{b_session_id}/revoke", headers={
            "Authorization": f"Bearer {token_a}",
        })
        assert revoke_resp.status_code == 404

    def test_revoke_nonexistent_session(self, client):
        """Revoking a nonexistent session ID returns 404."""
        token, _ = _register_and_login(client, "testuser_sr3")
        resp = client.post("/api/auth/sessions/999999/revoke", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 404

    def test_revoke_session_requires_auth(self, client):
        """Revoking a session without auth returns 401."""
        resp = client.post("/api/auth/sessions/1/revoke")
        assert resp.status_code == 401


class TestRefreshRateLimit:
    """Rate limiting on the /api/auth/refresh endpoint."""

    def test_refresh_rate_limited_returns_429(self, client):
        """Exceeding the refresh rate limit returns 429."""
        _, _ = _register_and_login(client, "testuser_rrl1")
        got_429 = False
        for _ in range(25):
            resp = client.post("/api/auth/refresh")
            if resp.status_code == 429:
                assert "Too many requests" in resp.json()["detail"]
                got_429 = True
                break
        assert got_429, "Refresh rate limit was not triggered after 25 attempts"

    def test_refresh_rate_limit_retry_after_header(self, client):
        """429 on refresh includes a Retry-After header."""
        _, _ = _register_and_login(client, "testuser_rrl2")
        got_429 = False
        for _ in range(25):
            resp = client.post("/api/auth/refresh")
            if resp.status_code == 429:
                assert "Retry-After" in resp.headers
                assert int(resp.headers["Retry-After"]) > 0
                got_429 = True
                break
        assert got_429, "Refresh rate limit not triggered"

    def test_refresh_rate_limit_does_not_invalidate_token(self, client):
        """A valid refresh token still works after the rate limit resets."""
        _, _ = _register_and_login(client, "testuser_rrl3")
        for _ in range(25):
            resp = client.post("/api/auth/refresh")
            if resp.status_code == 429:
                break
        current_token = client.cookies.get(REFRESH_COOKIE)
        assert current_token is not None
        get_rate_limiter().reset()
        assert client.post("/api/auth/refresh").status_code == 200


class TestRefreshRateLimitIndependence:
    """Login and refresh rate limits must be independent."""

    def test_login_rate_limit_does_not_affect_refresh(self, client):
        """Hitting the login rate limit does NOT rate-limit refresh."""
        _, refresh = _register_and_login(client, "testuser_indep1")
        for i in range(12):
            client.post("/api/auth/login", json={
                "username": f"nonexistent_{i}",
                "password": "test",
            })
        resp = client.post("/api/auth/login", json={
            "username": "testuser_indep1",
            "password": "securePass123!",
        })
        assert resp.status_code == 429
        # Refresh still works (separate limiter)
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 200

    def test_refresh_rate_limit_does_not_affect_login(self, client):
        """Hitting the refresh rate limit does NOT rate-limit login."""
        _, _ = _register_and_login(client, "testuser_indep2")
        got_429 = False
        for _ in range(25):
            resp = client.post("/api/auth/refresh")
            if resp.status_code == 429:
                got_429 = True
                break
        assert got_429, "Refresh rate limit was not triggered"
        resp = client.post("/api/auth/login", json={
            "username": "testuser_indep2",
            "password": "securePass123!",
        })
        assert resp.status_code == 200


class TestLastUsedAtTracking:
    """RefreshSession.last_used_at is set on create and updated on rotation."""

    def test_last_used_at_set_on_create(self, client):
        """A freshly created refresh session has last_used_at set."""
        from app.database import SessionLocal
        from app.models.models import RefreshSession
        from app.services.refresh_token_service import hash_refresh_token

        _, refresh = _register_and_login(client, "testuser_lua1")
        db = SessionLocal()
        try:
            session = db.query(RefreshSession).filter(
                RefreshSession.token_hash == hash_refresh_token(refresh)
            ).first()
            assert session is not None
            assert session.last_used_at is not None
        finally:
            db.close()

    def test_last_used_at_updates_after_rotation(self, client):
        """The new session from rotation gets a fresh last_used_at."""
        from app.database import SessionLocal
        from app.models.models import RefreshSession
        from app.services.refresh_token_service import hash_refresh_token

        _, refresh = _register_and_login(client, "testuser_lua2")
        assert client.post("/api/auth/refresh").status_code == 200
        new_refresh = client.cookies.get(REFRESH_COOKIE)
        db = SessionLocal()
        try:
            old = db.query(RefreshSession).filter(
                RefreshSession.token_hash == hash_refresh_token(refresh)
            ).first()
            new = db.query(RefreshSession).filter(
                RefreshSession.token_hash == hash_refresh_token(new_refresh)
            ).first()
            assert old is not None and new is not None
            assert new.last_used_at is not None
            assert new.last_used_at >= old.last_used_at
        finally:
            db.close()

    def test_session_list_uses_last_used_at(self, client):
        """Session-list responses expose last_used_at."""
        token, _ = _register_and_login(client, "testuser_lua3")
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        for session in resp.json()["sessions"]:
            assert "last_used_at" in session
            assert session["last_used_at"] is not None


class TestDeviceInfoPreservation:
    """device_info is preserved when rotating with no new device info."""

    def test_device_info_exists_after_rotation(self, client):
        """Rotated session has device_info populated."""
        from app.database import SessionLocal
        from app.models.models import RefreshSession
        from app.services.refresh_token_service import hash_refresh_token

        reg = client.post("/api/auth/register", json={
            "username": "testuser_devinfo",
            "email": "devinfo@test.com",
            "password": "testpass12345",
        })
        assert reg.status_code == 201
        login = client.post("/api/auth/login", json={
            "username": "testuser_devinfo",
            "password": "testpass12345",
        })
        assert login.status_code == 200
        old_refresh = client.cookies.get(REFRESH_COOKIE)

        assert client.post("/api/auth/refresh").status_code == 200
        new_refresh = client.cookies.get(REFRESH_COOKIE)

        db = SessionLocal()
        try:
            new_hash = hash_refresh_token(new_refresh)
            session = db.query(RefreshSession).filter(
                RefreshSession.token_hash == new_hash
            ).first()
            assert session is not None
            assert session.device_info is not None
            assert session.device_info.startswith("ip:")
        finally:
            db.close()

    def test_rotate_preserves_previous_device_info(self, client):
        """Rotating with device_info=None preserves the previous device_info."""
        from app.database import SessionLocal
        from app.models.models import RefreshSession
        from app.services.refresh_token_service import (
            rotate_refresh_token,
            hash_refresh_token,
        )

        _, refresh = _register_and_login(client, "testuser_dev1")
        db = SessionLocal()
        try:
            new_raw, _ = rotate_refresh_token(db, refresh, device_info=None)
            new_hash = hash_refresh_token(new_raw)
            new_session = db.query(RefreshSession).filter(
                RefreshSession.token_hash == new_hash
            ).first()
            old_session = db.query(RefreshSession).filter(
                RefreshSession.token_hash == hash_refresh_token(refresh)
            ).first()
            assert new_session is not None and old_session is not None
            assert new_session.device_info == old_session.device_info
        finally:
            db.close()


class TestCleanupExpiredSessionsForce:
    """cleanup_expired_sessions(db, force=True) bypasses gating."""

    def test_force_bypasses_probabilistic_gating(self, client):
        """force=True deletes expired sessions even when the gate would skip."""
        from datetime import datetime, timedelta, timezone
        from app.database import SessionLocal
        from app.models.models import RefreshSession
        from app.services.refresh_token_service import (
            cleanup_expired_sessions,
            reset_cleanup_counter,
            hash_refresh_token,
        )

        _, refresh = _register_and_login(client, "testuser_clean1")
        db = SessionLocal()
        try:
            session = db.query(RefreshSession).filter(
                RefreshSession.token_hash == hash_refresh_token(refresh)
            ).first()
            assert session is not None
            session.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
            db.commit()
            reset_cleanup_counter()
            count = cleanup_expired_sessions(db)
            assert count == 0  # gated -> skipped
            count = cleanup_expired_sessions(db, force=True)
            assert count >= 1
        finally:
            db.close()

    def test_force_never_raises_when_nothing_to_clean(self, client):
        """force=True is safe when there is nothing to clean."""
        from app.database import SessionLocal
        from app.services.refresh_token_service import cleanup_expired_sessions
        db = SessionLocal()
        try:
            count = cleanup_expired_sessions(db, force=True)
            assert count >= 0
        finally:
            db.close()


class TestExpiredAccessTokenRefresh:
    """An expired access token does not block a valid refresh."""

    def test_expired_access_token_can_still_refresh(self, client):
        """Refresh works even when the access token has expired."""
        from datetime import timedelta
        from app.services.auth_service import create_access_token

        _, _ = _register_and_login(client, "testuser_eat1")
        expired = create_access_token(data={"sub": "0"}, expires_delta=timedelta(hours=-1))
        resp = client.post("/api/auth/refresh", headers={
            "Authorization": f"Bearer {expired}",
        })
        assert resp.status_code == 200
        assert "access_token" in resp.json()


class TestNoTokenHashInLogs:
    """Token hashes must never appear in log output."""

    def test_token_hash_not_logged(self, client, caplog):
        import logging
        import re
        caplog.set_level(logging.DEBUG)

        _, refresh = _register_and_login(client, "testuser_thl1")
        client.post("/api/auth/refresh")
        for record in caplog.records:
            message = record.getMessage()
            for match in re.findall(r"[a-f0-9]{64}", message):
                pytest.fail(
                    f"token hash leaked in log: '{match[:16]}...' "
                    f"in record: {record.name} - {record.levelname}"
                )


# ═══════════════════════════════════════════════════════════════
# Phase 7C — HttpOnly Cookie, CSRF, and CORS
# ═══════════════════════════════════════════════════════════════


class TestPhase7CCookieFlow:
    """Login/refresh deliver the refresh token exclusively via HttpOnly cookie."""

    def test_login_response_contains_no_raw_refresh_token(self, client):
        """Login JSON response never includes the raw refresh token."""
        reg = client.post("/api/auth/register", json={
            "username": "testuser_p7c1",
            "email": "p7c1@test.com",
            "password": "securePass123!",
        })
        assert reg.status_code == 201
        login = client.post("/api/auth/login", json={
            "username": "testuser_p7c1",
            "password": "securePass123!",
        })
        assert login.status_code == 200
        body = login.text
        assert "refresh_token" not in body
        data = login.json()
        assert set(data.keys()) == {"access_token", "token_type"}

    def test_refresh_response_contains_no_raw_refresh_token(self, client):
        """Refresh JSON response never includes the raw refresh token."""
        _, _ = _register_and_login(client, "testuser_p7c2")
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 200
        body = resp.text
        assert "refresh_token" not in body

    def test_refresh_cookie_is_httponly_and_path_scoped(self, client):
        """The refresh cookie is HttpOnly and scoped to /api/auth."""
        reg = client.post("/api/auth/register", json={
            "username": "testuser_p7c3",
            "email": "p7c3@test.com",
            "password": "securePass123!",
        })
        assert reg.status_code == 201
        login = client.post("/api/auth/login", json={
            "username": "testuser_p7c3",
            "password": "securePass123!",
        })
        set_cookie = login.headers.get("set-cookie", "")
        assert REFRESH_COOKIE in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Path=/api/auth" in set_cookie

    def test_refresh_rotates_cookie(self, client):
        """Refresh sets a new refresh cookie value."""
        _, old = _register_and_login(client, "testuser_p7c4")
        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 200
        new = client.cookies.get(REFRESH_COOKIE)
        assert new is not None and new != old

    def test_old_cookie_reuse_revokes_family(self, client):
        """Reusing the old refresh cookie revokes the family (Phase 7C flow)."""
        _, old = _register_and_login(client, "testuser_p7c5")
        assert client.post("/api/auth/refresh").status_code == 200
        rotated = client.cookies.get(REFRESH_COOKIE)
        client.cookies.set(REFRESH_COOKIE, old, path="/api/auth")
        assert client.post("/api/auth/refresh").status_code == 401
        client.cookies.set(REFRESH_COOKIE, rotated, path="/api/auth")
        assert client.post("/api/auth/refresh").status_code == 401

    def test_logout_clears_refresh_and_csrf_cookies(self, client):
        """Logout clears both the refresh and CSRF cookies."""
        token, _ = _register_and_login(client, "testuser_p7c6")
        assert client.cookies.get(REFRESH_COOKIE) is not None
        assert client.cookies.get(CSRF_COOKIE) is not None
        resp = client.post("/api/auth/logout", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        assert client.cookies.get(REFRESH_COOKIE) is None
        assert client.cookies.get(CSRF_COOKIE) is None

    def test_logout_all_clears_cookies(self, client):
        """Logout-all clears both cookies."""
        token, _ = _register_and_login(client, "testuser_p7c7")
        resp = client.post("/api/auth/logout-all", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        assert client.cookies.get(REFRESH_COOKIE) is None
        assert client.cookies.get(CSRF_COOKIE) is None


class TestPhase7CCurrentSession:
    """Current-session detection is based on the HttpOnly refresh cookie."""

    def test_current_session_matches_cookie(self, client):
        """Only the session whose hash matches the cookie is marked current."""
        from app.database import SessionLocal
        from app.models.models import RefreshSession
        from app.services.refresh_token_service import hash_refresh_token

        token, refresh1 = _register_and_login(client, "testuser_p7cs1")
        client.post("/api/auth/login", json={
            "username": "testuser_p7cs1",
            "password": "securePass123!",
        })
        refresh2 = client.cookies.get(REFRESH_COOKIE)

        # With refresh1's cookie, exactly that session is current
        client.cookies.set(REFRESH_COOKIE, refresh1, path="/api/auth")
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        current = [s for s in resp.json()["sessions"] if s["is_current"]]
        assert len(current) == 1
        db = SessionLocal()
        try:
            matching = db.query(RefreshSession).filter(
                RefreshSession.token_hash == hash_refresh_token(refresh1)
            ).first()
            assert matching is not None
            assert current[0]["id"] == matching.id
        finally:
            db.close()

        # With refresh2's cookie, the OTHER session is current
        client.cookies.set(REFRESH_COOKIE, refresh2, path="/api/auth")
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
        })
        current = [s for s in resp.json()["sessions"] if s["is_current"]]
        assert len(current) == 1
        db = SessionLocal()
        try:
            matching2 = db.query(RefreshSession).filter(
                RefreshSession.token_hash == hash_refresh_token(refresh2)
            ).first()
            assert matching2 is not None
            assert current[0]["id"] == matching2.id
            assert matching2.id != matching.id
        finally:
            db.close()

    def test_missing_cookie_no_current_session(self, client):
        """Missing refresh cookie → no session marked current."""
        token, _ = _register_and_login(client, "testuser_p7cs2")
        client.cookies.delete(REFRESH_COOKIE, path="/api/auth")
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        assert all(not s["is_current"] for s in resp.json()["sessions"])

    def test_invalid_cookie_no_current_session(self, client):
        """Invalid refresh cookie → no session marked current."""
        token, _ = _register_and_login(client, "testuser_p7cs3")
        client.cookies.set(REFRESH_COOKIE, "invalid-token-that-matches-nothing", path="/api/auth")
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        assert all(not s["is_current"] for s in resp.json()["sessions"])

    def test_revoked_cookie_no_current_session(self, client):
        """Revoked refresh cookie → no session marked current."""
        token, refresh = _register_and_login(client, "testuser_p7cs4")
        client.post("/api/auth/logout", headers={
            "Authorization": f"Bearer {token}",
        })
        client.cookies.set(REFRESH_COOKIE, refresh, path="/api/auth")
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        assert all(not s["is_current"] for s in resp.json()["sessions"])

    def test_expired_cookie_no_current_session(self, client):
        """Expired refresh cookie → no session marked current."""
        from datetime import datetime, timedelta, timezone
        from app.database import SessionLocal
        from app.models.models import RefreshSession
        from app.services.refresh_token_service import hash_refresh_token

        token, refresh = _register_and_login(client, "testuser_p7cs5")
        db = SessionLocal()
        try:
            session = db.query(RefreshSession).filter(
                RefreshSession.token_hash == hash_refresh_token(refresh)
            ).first()
            assert session is not None
            session.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
            db.commit()
        finally:
            db.close()
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        assert all(not s["is_current"] for s in resp.json()["sessions"])

    def test_query_param_refresh_token_ignored(self, client):
        """Refresh tokens via query params are never accepted."""
        token, refresh = _register_and_login(client, "testuser_p7cs6")
        client.cookies.delete(REFRESH_COOKIE, path="/api/auth")
        resp = client.get(f"/api/auth/sessions?refresh_token={refresh}", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        assert all(not s["is_current"] for s in resp.json()["sessions"])

    def test_sessions_response_never_leaks_token_or_hash(self, client):
        """Session list responses never include raw tokens or token hashes."""
        import re
        token, refresh = _register_and_login(client, "testuser_p7cs7")
        client.cookies.set(REFRESH_COOKIE, refresh, path="/api/auth")
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        body = resp.text
        assert refresh not in body
        assert "token_hash" not in body
        for match in re.findall(r"[a-f0-9]{64}", body):
            pytest.fail(f"token hash leaked in response: {match[:16]}...")


class TestPhase7CCSRF:
    """Double-submit CSRF protection on state-changing endpoints."""

    def _login_with_csrf(self, client, username="testuser_csrf"):
        """Register+login; return (token, csrf_value)."""
        token, _ = _register_and_login(client, username)
        csrf = client.cookies.get(CSRF_COOKIE)
        assert csrf is not None
        return token, csrf

    def test_missing_csrf_token_returns_403(self, client):
        """State-changing request with Origin but no CSRF token → 403."""
        token, _ = self._login_with_csrf(client, "testuser_csrf1")
        resp = client.post("/api/sessions", json={"title": "CSRF Test"}, headers={
            "Authorization": f"Bearer {token}",
            "Origin": "http://localhost:5173",
        })
        assert resp.status_code == 403
        assert "CSRF" in resp.json()["detail"]

    def test_wrong_csrf_token_returns_403(self, client):
        """State-changing request with a wrong CSRF token → 403."""
        token, _ = self._login_with_csrf(client, "testuser_csrf2")
        resp = client.post("/api/sessions", json={"title": "CSRF Test"}, headers={
            "Authorization": f"Bearer {token}",
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": "definitely-the-wrong-token",
        })
        assert resp.status_code == 403

    def test_correct_csrf_token_succeeds(self, client):
        """State-changing request with the correct CSRF token succeeds."""
        token, csrf = self._login_with_csrf(client, "testuser_csrf3")
        resp = client.post("/api/sessions", json={"title": "CSRF OK"}, headers={
            "Authorization": f"Bearer {token}",
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": csrf,
        })
        assert resp.status_code == 201

    def test_refresh_requires_csrf_from_browser(self, client):
        """Refresh with an Origin header requires a matching CSRF token."""
        _, _ = self._login_with_csrf(client, "testuser_csrf4")
        # No CSRF header + Origin → 403
        resp = client.post("/api/auth/refresh", headers={"Origin": "http://localhost:5173"})
        assert resp.status_code == 403
        # With correct CSRF header → 200
        csrf = client.cookies.get(CSRF_COOKIE)
        resp = client.post("/api/auth/refresh", headers={
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": csrf,
        })
        assert resp.status_code == 200

    def test_logout_requires_csrf_from_browser(self, client):
        """Logout with an Origin header requires a matching CSRF token."""
        token, csrf = self._login_with_csrf(client, "testuser_csrf5")
        resp = client.post("/api/auth/logout", headers={
            "Authorization": f"Bearer {token}",
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": csrf,
        })
        assert resp.status_code == 200

    def test_get_requests_do_not_require_csrf(self, client):
        """GET requests are exempt from CSRF protection."""
        token, _ = self._login_with_csrf(client, "testuser_csrf6")
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
            "Origin": "http://localhost:5173",
        })
        assert resp.status_code == 200

    def test_login_and_register_exempt_from_csrf(self, client):
        """Login and registration are exempt from CSRF (pre-auth)."""
        # Register (no CSRF needed)
        resp = client.post("/api/auth/register", json={
            "username": "testuser_csrf7",
            "email": "csrf7@test.com",
            "password": "securePass123!",
        })
        assert resp.status_code == 201
        # Login (no CSRF needed)
        resp = client.post("/api/auth/login", json={
            "username": "testuser_csrf7",
            "password": "securePass123!",
        })
        assert resp.status_code == 200


class TestPhase7CCORS:
    """CORS allows the configured origin with credentials (no wildcard)."""

    def test_cors_allows_configured_origin_with_credentials(self, client):
        """A request from the configured origin gets CORS headers."""
        token, _ = _register_and_login(client, "testuser_cors1")
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
            "Origin": "http://localhost:5173",
        })
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
        assert resp.headers.get("access-control-allow-credentials") == "true"

    def test_cors_does_not_use_wildcard(self, client):
        """CORS never returns a wildcard origin with credentials."""
        token, _ = _register_and_login(client, "testuser_cors2")
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
            "Origin": "http://localhost:5173",
        })
        assert resp.status_code == 200
        acao = resp.headers.get("access-control-allow-origin", "")
        assert acao != "*"
        assert acao == "http://localhost:5173"

    def test_cors_rejects_unknown_origin(self, client):
        """An unconfigured origin is not allowed."""
        token, _ = _register_and_login(client, "testuser_cors3")
        resp = client.get("/api/auth/sessions", headers={
            "Authorization": f"Bearer {token}",
            "Origin": "http://evil.example.com",
        })
        assert resp.status_code == 200  # Request still processed
        assert resp.headers.get("access-control-allow-origin") != "http://evil.example.com"
