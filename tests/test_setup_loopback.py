"""Proxy-aware setup lock and post-configure auth (Phase 2 - .exe packaging).

Tunnel products connect from loopback, so a forwarding header must be treated
as a remote caller. Once configured, even a loopback request needs a session.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.main import app
from app.services import access_guard, setup_service


def _request(
    host: str,
    method: str,
    path: str,
    *,
    headers: dict | None = None,
    **kwargs,
) -> httpx.Response:
    """Call the real ASGI app with a synthetic client address."""

    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, client=(host, 51234))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.request(method, path, headers=headers, **kwargs)

    return asyncio.run(_run())


@pytest.fixture
def first_run(monkeypatch):
    """Simulate an unconfigured install and a successful (no-op) save."""
    monkeypatch.setattr(setup_service, "is_configured", lambda: False)
    monkeypatch.setattr(setup_service, "save_gemini_api_key", lambda key: True)
    return None


@pytest.fixture
def already_configured(monkeypatch):
    monkeypatch.setattr(setup_service, "is_configured", lambda: True)
    monkeypatch.setattr(setup_service, "save_gemini_api_key", lambda key: True)
    return None


def test_setup_status_allowed_from_loopback():
    assert _request("127.0.0.1", "GET", "/api/setup/status").status_code == 200


def test_setup_status_allowed_from_ipv6_loopback():
    assert _request("::1", "GET", "/api/setup/status").status_code == 200


def test_setup_status_is_public_from_non_loopback():
    # Only a boolean is exposed, so status stays public for the SPA boot flow.
    response = _request("203.0.113.9", "GET", "/api/setup/status")
    assert response.status_code == 200
    assert set(response.json().keys()) == {"configured"}


def test_setup_status_is_public_through_a_tunnel():
    response = _request(
        "127.0.0.1",
        "GET",
        "/api/setup/status",
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert response.status_code == 200


def test_setup_configure_blocked_from_non_loopback(first_run):
    response = _request(
        "203.0.113.9", "POST", "/api/setup/configure",
        json={"gemini_api_key": "x"},
    )
    assert response.status_code == 403
    assert "localhost" in response.json()["detail"]


def test_setup_configure_blocked_by_x_forwarded_for(first_run):
    response = _request(
        "127.0.0.1",
        "POST",
        "/api/setup/configure",
        headers={"X-Forwarded-For": "203.0.113.9"},
        json={"gemini_api_key": "x"},
    )
    assert response.status_code == 403
    assert "localhost" in response.json()["detail"]


def test_setup_configure_blocked_by_rfc7239_forwarded(first_run):
    response = _request(
        "127.0.0.1",
        "POST",
        "/api/setup/configure",
        headers={"Forwarded": "for=203.0.113.9;proto=https"},
        json={"gemini_api_key": "x"},
    )
    assert response.status_code == 403


def test_first_run_loopback_configure_stays_open(first_run):
    response = _request(
        "127.0.0.1", "POST", "/api/setup/configure",
        json={"gemini_api_key": "test-key"},
    )
    assert response.status_code == 200
    assert response.json() == {"configured": True}


def test_configured_loopback_without_session_is_rejected(already_configured):
    response = _request(
        "127.0.0.1", "POST", "/api/setup/configure",
        json={"gemini_api_key": "test-key"},
    )
    assert response.status_code == 401
    detail = response.json()["detail"]
    assert "authentication required" in detail
    assert "test-key" not in response.text


def test_configured_loopback_with_session_is_allowed(
    already_configured, monkeypatch
):
    monkeypatch.setattr(access_guard, "has_valid_session", lambda request, db=None: True)
    response = _request(
        "127.0.0.1", "POST", "/api/setup/configure",
        json={"gemini_api_key": "test-key"},
    )
    assert response.status_code == 200


def test_other_routes_unaffected_from_non_loopback():
    assert _request("203.0.113.9", "GET", "/health").status_code == 200
