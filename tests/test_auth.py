"""
Phase 6A — Authentication endpoint tests.

Tests cover:
  - User registration (success, duplicate username, duplicate email, validation)
  - User login (success, wrong password, nonexistent user)
  - Current user retrieval (valid token, invalid token, expired token)
  - Edge cases (password strength, username format, email format)
"""

import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.models.models import User
from app.services.auth_service import create_access_token, hash_password


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


# ── Helper ─────────────────────────────────────────────────


# ── Registration Tests ─────────────────────────────────────


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


# ── Login Tests ────────────────────────────────────────────


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


# ── Current User (Me) Tests ────────────────────────────────


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
        # Create a token with sub=0 (non-existent), expired 1 hour ago
        expired_token = create_access_token(
            data={"sub": 0},
            expires_delta=timedelta(hours=-1),
        )
        resp = client.get("/api/auth/me", headers={
            "Authorization": f"Bearer {expired_token}",
        })
        assert resp.status_code == 401
        assert "Invalid or expired token" in resp.json()["detail"]
