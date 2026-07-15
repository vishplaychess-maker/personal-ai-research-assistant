"""
Smoke tests for Phase 1 health endpoint.

Run with:
    pip install httpx pytest
    pytest tests/test_health.py -v

Or point at a specific host:
    BASE_URL=http://localhost:8080 pytest tests/test_health.py -v
"""

import os

import httpx
import pytest

BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")

# ── Helpers ────────────────────────────────────────────────


def client():
    return httpx.Client(base_url=BASE_URL, timeout=10.0)


# ── Tests ──────────────────────────────────────────────────


def test_health_endpoint_returns_200():
    """GET /api/health should return HTTP 200."""
    with client() as c:
        resp = c.get("/api/health")
    assert resp.status_code == 200


def test_health_endpoint_returns_json():
    """The response must be valid JSON."""
    with client() as c:
        resp = c.get("/api/health")
    assert resp.headers["content-type"].startswith("application/json")


def test_health_contains_required_keys():
    """All three services must be present in the response."""
    with client() as c:
        data = c.get("/api/health").json()
    assert "backend" in data
    assert "chromadb" in data
    assert "ollama" in data


def test_health_backend_is_ok():
    """Backend should always report 'ok'."""
    with client() as c:
        data = c.get("/api/health").json()
    assert data["backend"] == "ok"


def test_health_values_are_valid():
    """Each service status must be 'ok' or 'unavailable' (never anything else)."""
    with client() as c:
        data = c.get("/api/health").json()
    for service in ("backend", "chromadb", "ollama"):
        assert data[service] in ("ok", "unavailable"), (
            f"'{service}' has unexpected value: {data[service]!r}"
        )


def test_health_chromadb_ollama_optional():
    """ChromaDB and Ollama may be 'unavailable' — the endpoint must still respond 200."""
    with client() as c:
        data = c.get("/api/health").json()
    # This just confirms the assertion above — the endpoint doesn't crash.
    assert data["chromadb"] in ("ok", "unavailable")
    assert data["ollama"] in ("ok", "unavailable")
