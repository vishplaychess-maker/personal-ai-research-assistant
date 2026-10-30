"""Fail-open contract for the OpenViking sidecar service and its orchestrator gate.

The sidecar is an enhancement, never a dependency: every transport failure,
timeout, or server error envelope must degrade to a safe default ({} / False)
with a warning log and no exception crossing the service boundary.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

import app.services.openviking_service as ov
from app.config import settings
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client

MOCK_HITS = {
    "result": [
        {
            "uri": "viking://user/7/memories/preferences/reporting_style.md",
            "level": 2,
            "score": 0.72,
            "abstract": "Lead with a one-line verdict.",
        },
        {
            "uri": "viking://user/7/skills/summarize-services",
            "level": 0,
            "score": 0.61,
            "abstract": "Summarize Python services.",
        },
    ]
}


def _mock_client(handler):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://mock"
    )
    return client


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "openviking_enabled", True)
    monkeypatch.setattr(settings, "openviking_url", "http://mock")
    return True


@pytest.mark.parametrize(
    "handler, expected",
    [
        (lambda request: httpx.Response(200, json=MOCK_HITS), 2),
        (
            lambda request: httpx.Response(
                200, json={"result": {"resources": MOCK_HITS["result"]}}
            ),
            2,
        ),
        (lambda request: httpx.Response(200, json={"result": []}), 0),
    ],
)
def test_get_structured_context_success(enabled, monkeypatch, handler, expected):
    seen = {}

    def _handler(request):
        seen["path"] = request.url.path
        seen["target"] = httpx.Response  # placeholder, replaced below
        import json as _json

        body = _json.loads(request.content)
        seen["target_uri"] = body["target_uri"]
        seen["user_header"] = request.headers.get("X-OpenViking-User")
        return handler(request)

    monkeypatch.setattr(ov, "_client", lambda timeout_s: _mock_client(_handler))
    context = asyncio.run(ov.get_structured_context("7", "summarize services"))
    assert len(context.get("memories", [])) == expected
    if expected:
        assert seen["path"] == "/api/v1/search/find"
        assert seen["target_uri"] == "viking://user/7"
        assert seen["user_header"] == "7"


def test_empty_resources_does_not_shadow_memories(enabled, monkeypatch):
    """Live server returns {memories:[...], resources:[], skills:[]} —
    a leading empty list must not hide the populated key."""
    envelope = {
        "result": {
            "resources": [],
            "memories": MOCK_HITS["result"],
            "skills": [],
            "total": 2,
        }
    }
    monkeypatch.setattr(
        ov, "_client",
        lambda timeout_s: _mock_client(
            lambda request: httpx.Response(200, json=envelope)
        ),
    )
    context = asyncio.run(ov.get_structured_context("7", "anything"))
    assert len(context["memories"]) == 2


def test_register_skill_requests_async_compile(enabled, monkeypatch):
    """wait=False must be sent: inline VLM compile exceeds the 30 s budget."""
    captured = {}

    def _handler(request):
        import json as _json

        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"result": {"task_id": "t-1"}})

    monkeypatch.setattr(ov, "_client", lambda timeout_s: _mock_client(_handler))
    assert asyncio.run(ov.register_skill("my_skill", "desc"))
    assert captured["body"]["wait"] is False


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("sidecar down"),
        httpx.TimeoutException("budget exhausted"),
        httpx.ReadTimeout("slow compile"),
    ],
)
def test_transport_failures_degrade_to_empty(enabled, monkeypatch, exc, caplog):
    def _handler(request):
        raise exc

    monkeypatch.setattr(ov, "_client", lambda timeout_s: _mock_client(_handler))
    with caplog.at_level(logging.WARNING):
        context = asyncio.run(ov.get_structured_context("7", "anything"))
    assert context == {}
    assert ov._UNAVAILABLE_MSG in caplog.text


def test_error_envelope_degrades(enabled, monkeypatch, caplog):
    def _handler(request):
        return httpx.Response(
            200, json={"status": "error", "error": {"message": "queue wedged"}}
        )

    monkeypatch.setattr(ov, "_client", lambda timeout_s: _mock_client(_handler))
    with caplog.at_level(logging.WARNING):
        context = asyncio.run(ov.get_structured_context("7", "anything"))
    assert context == {}
    assert "queue wedged" not in context  # never partial data
    assert ov._UNAVAILABLE_MSG in caplog.text


def test_http_5xx_degrades(enabled, monkeypatch):
    monkeypatch.setattr(
        ov, "_client",
        lambda timeout_s: _mock_client(lambda request: httpx.Response(503)),
    )
    assert asyncio.run(ov.get_structured_context("7", "anything")) == {}


def test_disabled_makes_no_network_call(monkeypatch):
    monkeypatch.setattr(settings, "openviking_enabled", False)

    def _boom(timeout_s):
        raise AssertionError("must not create a client when disabled")

    monkeypatch.setattr(ov, "_client", _boom)
    assert asyncio.run(ov.get_structured_context("7", "anything")) == {}
    assert asyncio.run(ov.register_skill("my_skill", "desc")) is False


def test_register_skill_success(enabled, monkeypatch):
    captured = {}

    def _handler(request):
        import json as _json

        captured["path"] = request.url.path
        captured["md"] = _json.loads(request.content)["data"]
        return httpx.Response(200, json={"result": {"task_id": "t-1"}})

    monkeypatch.setattr(ov, "_client", lambda timeout_s: _mock_client(_handler))
    assert asyncio.run(ov.register_skill("summarize_services", "Does things"))
    assert captured["path"] == "/api/v1/skills"
    assert "name: summarize_services" in captured["md"]


@pytest.mark.parametrize("bad_id", ["Bad ID", "9lives", "a" * 60, "", "drop\nname"])
def test_register_skill_rejects_invalid_ids(enabled, monkeypatch, bad_id):
    def _boom(timeout_s):
        raise AssertionError("invalid ids must never reach the network")

    monkeypatch.setattr(ov, "_client", _boom)
    assert asyncio.run(ov.register_skill(bad_id, "desc")) is False


def test_register_skill_timeout_degrades_to_false(enabled, monkeypatch, caplog):
    def _handler(request):
        raise httpx.TimeoutException("30s budget exhausted")

    monkeypatch.setattr(
        ov, "_client",
        lambda timeout_s: httpx.AsyncClient(
            transport=httpx.MockTransport(_handler), base_url="http://mock"
        ),
    )
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(ov.register_skill("my_skill", "desc")) is False
    assert "skipping skill registration" in caplog.text


def test_register_skill_recovers_via_existence_probe(enabled, monkeypatch):
    """POST may time out while the server finishes the skill anyway —
    the GET probe turns that into a True without a warning."""
    calls = []

    def _handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            raise httpx.ReadTimeout("client gave up")
        return httpx.Response(200, json={"result": {"name": "my_skill"}})

    monkeypatch.setattr(
        ov, "_client",
        lambda timeout_s: httpx.AsyncClient(
            transport=httpx.MockTransport(_handler), base_url="http://mock"
        ),
    )
    assert asyncio.run(ov.register_skill("my_skill", "desc")) is True
    assert ("GET", "/api/v1/skills/my_skill") in calls


def test_sync_wrappers_never_raise(monkeypatch):
    monkeypatch.setattr(settings, "openviking_enabled", True)

    def _explode(*args, **kwargs):
        raise RuntimeError("bridge exploded")

    monkeypatch.setattr(ov, "run_coro_sync", _explode)
    assert ov.get_structured_context_sync(7, "q") == {}
    assert ov.register_skill_sync("my_skill", "d") is False


def test_format_context_block(enabled):
    block = ov.format_context_block({"memories": MOCK_HITS["result"]})
    assert "OpenViking structured context" in block
    assert "reporting_style.md" in block
    assert "relevance 0.72" in block
    assert ov.format_context_block({}) == ""


# ── Orchestrator gate ──────────────────────────────────────────────────────


def test_openviking_context_gate(client, monkeypatch):
    """Off by default; needs BOTH the server flag and the user capability."""
    from fastapi.testclient import TestClient  # noqa: F401 (fixture parity)

    from app.services import orchestrator
    from tests.auth_helpers import auth_headers, register_and_login

    user_id, token = register_and_login(client, username="ov_gate_user")
    headers = auth_headers(token)

    monkeypatch.setattr(settings, "openviking_enabled", True)
    # Capability ships disabled -> helper is a silent no-op.
    assert orchestrator._openviking_context("task-1", user_id, "q") == ("", [])

    response = client.patch(
        "/api/capabilities/openviking_memory",
        json={"enabled": True},
        headers=headers,
    )
    assert response.status_code == 200, response.text[:200]

    monkeypatch.setattr(
        ov,
        "get_structured_context_sync",
        lambda uid, query: {"memories": MOCK_HITS["result"]},
    )
    block, warnings = orchestrator._openviking_context("task-1", user_id, "q")
    assert warnings == []
    assert "OpenViking structured context" in block

    # Server flag off again -> no call even with the capability enabled.
    monkeypatch.setattr(settings, "openviking_enabled", False)
    assert orchestrator._openviking_context("task-1", user_id, "q") == ("", [])
