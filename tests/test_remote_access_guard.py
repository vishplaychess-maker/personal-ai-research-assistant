"""Remote/tunnel API guard: no anonymous /api access, no default-user bypass.

Simulates the two ways a remote caller reaches a loopback-bound app:
a direct non-loopback peer, and a tunnel/proxy that connects from 127.0.0.1
while adding a forwarding header.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models.models import RefreshSession, User
from app.services.auth_service import create_access_token
from app.services.refresh_token_service import create_refresh_session

REMOTE_HOST = "203.0.113.9"


def _request(
    host: str,
    method: str,
    path: str,
    *,
    headers: dict | None = None,
    cookies: dict | None = None,
    json: dict | None = None,
) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, client=(host, 51234))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.request(
                method, path, headers=headers, cookies=cookies, json=json
            )

    return asyncio.run(_run())


@pytest.fixture
def remote_user():
    """A real user + access token + refresh cookie, cleaned up afterwards."""
    db = SessionLocal()
    name = f"remote_guard_{uuid.uuid4().hex[:10]}"
    user = User(username=name, email=f"{name}@test.com")
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token({"sub": str(user.id)})
    refresh_raw = create_refresh_session(db, user)
    user_id = user.id
    db.close()
    try:
        yield {"id": user_id, "token": token, "refresh": refresh_raw}
    finally:
        db = SessionLocal()
        db.query(RefreshSession).filter(RefreshSession.user_id == user_id).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.commit()
        db.close()


def test_anonymous_non_loopback_api_is_rejected():
    response = _request(REMOTE_HOST, "GET", "/api/sessions")
    assert response.status_code == 401
    assert response.json()["detail"] == "authentication required for remote access"
    assert response.headers.get("www-authenticate") == "Bearer"


def test_anonymous_tunnel_request_is_rejected():
    # Tunnel connects from loopback; the forwarding header is the only signal.
    response = _request(
        "127.0.0.1",
        "GET",
        "/api/sessions",
        headers={"X-Forwarded-For": REMOTE_HOST},
    )
    assert response.status_code == 401


def test_anonymous_rfc7239_forwarded_request_is_rejected():
    response = _request(
        "127.0.0.1",
        "GET",
        "/api/sessions",
        headers={"Forwarded": f"for={REMOTE_HOST};proto=https"},
    )
    assert response.status_code == 401


def test_default_user_fallback_is_not_reachable_remotely():
    # Memories use get_optional_user (default user id=1) on loopback; the
    # remote guard must stop that path before the route runs.
    response = _request(REMOTE_HOST, "GET", "/api/memories")
    assert response.status_code == 401


def test_public_paths_remain_reachable_remotely():
    assert _request(REMOTE_HOST, "GET", "/health").status_code == 200
    assert _request(REMOTE_HOST, "GET", "/api/setup/status").status_code == 200


def test_remote_login_route_is_reachable():
    # Login is exempt from the guard; the route's own generic 401 proves the
    # request reached it instead of being stopped by the remote guard.
    response = _request(
        REMOTE_HOST,
        "POST",
        "/api/auth/login",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    response = _request(
        REMOTE_HOST,
        "POST",
        "/api/auth/login",
        json={"username": "nobody", "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid credentials"


def test_valid_access_token_passes_from_non_loopback(remote_user):
    response = _request(
        REMOTE_HOST,
        "GET",
        "/api/sessions",
        headers={"Authorization": f"Bearer {remote_user['token']}"},
    )
    assert response.status_code == 200


def test_valid_access_token_passes_through_tunnel(remote_user):
    response = _request(
        "127.0.0.1",
        "GET",
        "/api/sessions",
        headers={
            "Authorization": f"Bearer {remote_user['token']}",
            "X-Forwarded-For": REMOTE_HOST,
        },
    )
    assert response.status_code == 200


def test_valid_session_cookie_passes_without_bearer(remote_user):
    response = _request(
        REMOTE_HOST,
        "GET",
        "/api/health/worker",
        cookies={"research_assistant_refresh_token": remote_user["refresh"]},
    )
    assert response.status_code == 200


def test_unknown_session_cookie_is_rejected():
    response = _request(
        REMOTE_HOST,
        "GET",
        "/api/health/worker",
        cookies={"research_assistant_refresh_token": "not-a-real-session"},
    )
    assert response.status_code == 401


def test_invalid_bearer_does_not_fall_back_to_cookie(remote_user):
    response = _request(
        REMOTE_HOST,
        "GET",
        "/api/health/worker",
        headers={"Authorization": "Bearer not-a-real-token"},
        cookies={"research_assistant_refresh_token": remote_user["refresh"]},
    )
    assert response.status_code == 401


def test_remote_access_mode_requires_auth_even_from_loopback(monkeypatch):
    # Guards against a tunnel that strips forwarding headers: once remote
    # access is enabled, loopback peers are subject to the guard too.
    monkeypatch.setattr(settings, "remote_access_enabled", True)
    response = _request("127.0.0.1", "GET", "/api/sessions")
    assert response.status_code == 401


def test_loopback_requests_are_unchanged(remote_user):
    # No proxy header and a loopback peer: the remote guard stays out of the way.
    response = _request(
        "127.0.0.1",
        "GET",
        "/api/sessions",
        headers={"Authorization": f"Bearer {remote_user['token']}"},
    )
    assert response.status_code == 200
