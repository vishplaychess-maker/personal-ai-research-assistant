"""
Shared authentication helpers for backend integration tests.

Phase 7C bug-fix: session / message / document / search routes now require
a valid access token (the unauthenticated fallback to user 1 was removed).
These helpers let tests register + log in a test user and build
Authorization headers, working with both httpx.Client (live backend) and
FastAPI's TestClient (in-process).
"""

import uuid


def register_and_login(client, username=None, password="securePass123!"):
    """
    Register a fresh test user and log in.

    Args:
        client: httpx.Client or TestClient instance.
        username: Optional fixed username (a unique one is generated otherwise).

    Returns:
        (user_id, access_token)
    """
    name = username or f"testuser_{uuid.uuid4().hex[:8]}"
    reg = client.post(
        "/api/auth/register",
        json={
            "username": name,
            "email": f"{name}@test.com",
            "password": password,
        },
    )
    
    # User already unte direct ga login aipovali (DB pollution fix)
    if reg.status_code == 400 and "already taken" in reg.text.lower():
        login = client.post(
            "/api/auth/login",
            json={"username": name, "password": password},
        )
        assert login.status_code == 200, f"login failed: {login.status_code} {login.text[:200]}"
        # Get user id from token or a quick me call, or check how tests use it. 
        # Usually tests unpack: user_id, token = register_and_login(...)
        # Let's use an authenticated request to get the user id if needed, or query /api/auth/me
        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
        user_id = me.json().get("id", 1) if me.status_code == 200 else 1
        return user_id, login.json()["access_token"]

    assert reg.status_code == 201, f"register failed: {reg.status_code} {reg.text[:200]}"
    user_id = reg.json()["id"]
    login = client.post(
        "/api/auth/login",
        json={"username": name, "password": password},
    )
    assert login.status_code == 200, f"login failed: {login.status_code} {login.text[:200]}"
    return user_id, login.json()["access_token"]


def ensure_user(client, username, password="securePass123!"):
    """
    Register a fixed-username user if it does not already exist, then log in.

    Designed for live-backend httpx tests where the DB persists between runs:
    a second run will get a 400 on register (username exists), so we fall
    back to logging in and resolving the id via /api/auth/me.

    Returns:
        (user_id, access_token)
    """
    reg = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": password,
        },
    )
    if reg.status_code == 201:
        login = client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        assert login.status_code == 200, login.text[:200]
        return reg.json()["id"], login.json()["access_token"]

    # User already exists — log in and resolve id via /me.
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert login.status_code == 200, login.text[:200]
    token = login.json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text[:200]
    return me.json()["id"], token


def auth_headers(token):
    """Build the Authorization header dict for an access token."""
    return {"Authorization": f"Bearer {token}"}
