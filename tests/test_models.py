"""
Phase 5B tests — Model listing, per-session model selection, and system prompt.

Tests:
  - GET /api/models (list Ollama models)
  - PATCH /api/sessions/{id}/model (set per-session model)
  - GET /api/sessions/{id}/system-prompt (get system prompt)
  - PATCH /api/sessions/{id}/system-prompt (set system prompt)

Run with:
    pytest tests/test_models.py -v

Mock-based tests use TestClient (in-process) and do not require Docker or Ollama.
"""

import json
import os
from unittest.mock import patch

import httpx
import pytest

BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")

from tests.auth_helpers import ensure_user, register_and_login, auth_headers

# Shared test user for live-backend tests (persists across runs).
_TEST_USER = "itest_models"


def _authenticated(c: httpx.Client, username: str = _TEST_USER) -> httpx.Client:
    """Attach a valid Authorization header for the shared test user.

    The register/login lookup runs on a separate throwaway client so the
    returned client's connection pool is NOT opened before the caller
    enters it with `with client() as c:`.
    """
    with httpx.Client(base_url=str(c.base_url), timeout=15.0) as temp:
        _, token = ensure_user(temp, username)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


def _client() -> httpx.Client:
    return _authenticated(httpx.Client(base_url=BASE_URL, timeout=15.0))


def _create_session() -> int:
    """Create a test session and return its ID."""
    with _client() as c:
        resp = c.post("/api/sessions", json={"title": "Model Test Session"})
        assert resp.status_code == 201
        return resp.json()["id"]


def _delete_session(session_id: int):
    with _client() as c:
        try:
            c.delete(f"/api/sessions/{session_id}")
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _cleanup_sessions():
    """Delete any leftover test sessions before each test."""
    with _client() as c:
        sessions = c.get("/api/sessions").json()
        for s in sessions:
            if s["id"] > 10:
                try:
                    c.delete(f"/api/sessions/{s['id']}")
                except Exception:
                    pass


# ── Model listing ─────────────────────────────────────────


def test_list_models_returns_list():
    """GET /api/models returns a ModelListResponse with models or error."""
    with _client() as c:
        resp = c.get("/api/models")
    assert resp.status_code == 200
    data = resp.json()
    assert "models" in data
    assert isinstance(data["models"], list)
    # error is optional — either None or a string
    assert "error" in data
    assert data["error"] is None or isinstance(data["error"], str)


def test_list_models_model_shape():
    """Each model in the list should have 'name', 'size', and 'modified_at' fields."""
    with _client() as c:
        resp = c.get("/api/models")
    assert resp.status_code == 200
    data = resp.json()
    for model in data["models"]:
        assert "name" in model
        assert isinstance(model["name"], str)
        assert model["name"].strip() != ""
        # size and modified_at are optional
        if model.get("size") is not None:
            assert isinstance(model["size"], str)
        if model.get("modified_at") is not None:
            assert isinstance(model["modified_at"], str)


def test_list_models_ollama_unavailable():
    """When Ollama is unreachable, GET /api/models returns empty list with error field."""
    from fastapi.testclient import TestClient
    from app.main import app

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.side_effect = httpx.ConnectError("Connection refused")

        with TestClient(app) as c:
            resp = c.get("/api/models")

    assert resp.status_code == 200
    data = resp.json()
    assert data["models"] == [] or len(data["models"]) == 0
    assert data["error"] is not None
    assert "Ollama" in data["error"]


def test_list_models_ollama_timeout():
    """When Ollama times out, GET /api/models returns empty list with error field."""
    from fastapi.testclient import TestClient
    from app.main import app

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.side_effect = httpx.TimeoutException("Timed out")

        with TestClient(app) as c:
            resp = c.get("/api/models")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["models"]) == 0
    assert data["error"] is not None
    assert "Ollama" in data["error"]


# ── Per-session model selection ───────────────────────────


def test_update_session_model():
    """PATCH /api/sessions/{id}/model sets the model for a session."""
    with _client() as c:
        sid = _create_session()
        try:
            resp = c.patch(
                f"/api/sessions/{sid}/model",
                json={"model": "llama3.2:3b"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["model"] == "llama3.2:3b"
            assert data["id"] == sid

            # Verify the model is persisted
            sess = c.get(f"/api/sessions/{sid}").json()
            assert sess["model"] == "llama3.2:3b"
        finally:
            _delete_session(sid)


def test_update_session_model_to_null():
    """Setting model to null reverts to using the config default."""
    with _client() as c:
        sid = _create_session()
        try:
            # First set a model
            c.patch(
                f"/api/sessions/{sid}/model",
                json={"model": "llama3.2:3b"},
            )

            # Then clear it
            resp = c.patch(
                f"/api/sessions/{sid}/model",
                json={"model": None},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["model"] is None
            assert data["id"] == sid

            # Verify
            sess = c.get(f"/api/sessions/{sid}").json()
            assert sess["model"] is None
        finally:
            _delete_session(sid)


def test_update_session_model_404():
    """PATCH /api/sessions/99999/model returns 404."""
    with _client() as c:
        resp = c.patch(
            "/api/sessions/99999/model",
            json={"model": "llama3.2:3b"},
        )
    assert resp.status_code == 404


def test_session_response_includes_model():
    """Session response includes 'model' field even when not set."""
    with _client() as c:
        sid = _create_session()
        try:
            resp = c.get(f"/api/sessions/{sid}")
            assert resp.status_code == 200
            data = resp.json()
            assert "model" in data
            assert data["model"] is None  # Not set yet
        finally:
            _delete_session(sid)


# ── System prompt ─────────────────────────────────────────


def test_get_default_system_prompt():
    """GET /api/sessions/{id}/system-prompt returns the default when none is set."""
    with _client() as c:
        sid = _create_session()
        try:
            resp = c.get(f"/api/sessions/{sid}/system-prompt")
            assert resp.status_code == 200
            data = resp.json()
            assert "system_prompt" in data
            assert data["system_prompt"] is not None
            assert isinstance(data["system_prompt"], str)
            assert len(data["system_prompt"]) > 0
            assert data["using_default"] is True
        finally:
            _delete_session(sid)


def test_set_system_prompt():
    """PATCH /api/sessions/{id}/system-prompt sets a custom system prompt."""
    with _client() as c:
        sid = _create_session()
        try:
            custom_prompt = "You are an expert Python programmer. Be concise."
            resp = c.patch(
                f"/api/sessions/{sid}/system-prompt",
                json={"system_prompt": custom_prompt},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["system_prompt"] == custom_prompt
            assert data["using_default"] is False

            # Verify persisted
            sess = c.get(f"/api/sessions/{sid}").json()
            assert sess["system_prompt"] == custom_prompt
        finally:
            _delete_session(sid)


def test_reset_system_prompt_to_default():
    """Setting system_prompt to null resets to the default prompt."""
    with _client() as c:
        sid = _create_session()
        try:
            # Set a custom prompt
            c.patch(
                f"/api/sessions/{sid}/system-prompt",
                json={"system_prompt": "Custom prompt here"},
            )

            # Reset to default
            resp = c.patch(
                f"/api/sessions/{sid}/system-prompt",
                json={"system_prompt": None},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["using_default"] is True
            assert data["system_prompt"] is not None  # Default prompt text

            # Verify
            sess = c.get(f"/api/sessions/{sid}").json()
            assert sess["system_prompt"] is None
        finally:
            _delete_session(sid)


def test_get_system_prompt_404():
    """GET /api/sessions/99999/system-prompt returns 404."""
    with _client() as c:
        resp = c.get("/api/sessions/99999/system-prompt")
    assert resp.status_code == 404


def test_update_system_prompt_404():
    """PATCH /api/sessions/99999/system-prompt returns 404."""
    with _client() as c:
        resp = c.patch(
            "/api/sessions/99999/system-prompt",
            json={"system_prompt": "Test"},
        )
    assert resp.status_code == 404


# ── Streaming endpoint integration with per-session model ─


def test_stream_with_custom_model():
    """
    Streaming endpoint should accept a session with a custom model set.
    Uses TestClient with mocked streaming to avoid Ollama dependency.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    import app.services.streaming_service as streaming_service

    async def mock_stream(context):
        yield streaming_service.format_sse("start", {
            "session_id": context.session_id,
            "sources_used": False,
            "memories_used": False,
        })
        yield streaming_service.format_sse("token", {"token": "Hello"})
        yield streaming_service.format_sse("complete", {
            "message_id": None,
            "citations": [],
            "sources_used": False,
            "memories_used": False,
            "content": "Hello from custom model",
        })

    with patch("app.routes.messages.stream_chat_response", mock_stream):
        with TestClient(app) as c:
            _, token = register_and_login(c, "itest_models_tc")
            headers = auth_headers(token)

            # Create session
            sess_resp = c.post("/api/sessions", json={"title": "Model Stream Test"}, headers=headers)
            sid = sess_resp.json()["id"]

            # Set custom model
            c.patch(
                f"/api/sessions/{sid}/model",
                json={"model": "llama3.2:3b"},
                headers=headers,
            )

            # Stream with custom model — should work fine
            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Test with custom model"},
                headers=headers,
            )

    assert resp.status_code == 200
    events = []
    for line in resp.text.strip().split("\n"):
        if line.startswith("event: "):
            events.append(line[7:])

    # Should have start, token, complete
    assert "start" in events
    assert "token" in events
    assert "complete" in events


# ── Session response model/system_prompt fields ───────────


def test_session_response_has_model_and_system_prompt():
    """SessionResponse must include model and system_prompt fields."""
    with _client() as c:
        sid = _create_session()
        try:
            resp = c.get(f"/api/sessions/{sid}")
            assert resp.status_code == 200
            data = resp.json()
            assert "model" in data
            assert "system_prompt" in data
        finally:
            _delete_session(sid)


def test_list_sessions_includes_model_and_system_prompt():
    """Session list must include model and system_prompt fields."""
    with _client() as c:
        sid = _create_session()
        try:
            resp = c.get("/api/sessions")
            assert resp.status_code == 200
            sessions = resp.json()
            target = [s for s in sessions if s["id"] == sid]
            assert len(target) == 1
            assert "model" in target[0]
            assert "system_prompt" in target[0]
        finally:
            _delete_session(sid)


# ── Model persistence (Phase 7C bug-fix) ───────────────────


def test_existing_session_defaults_to_null_model():
    """A freshly created session has model=None (use configured default)."""
    with _client() as c:
        sid = _create_session()
        try:
            resp = c.get(f"/api/sessions/{sid}")
            assert resp.status_code == 200
            assert resp.json()["model"] is None
        finally:
            _delete_session(sid)


def test_updated_model_survives_fresh_client():
    """A selected model persists across a fresh database session / client."""
    sid = None
    with _client() as c:
        sid = _create_session()
        resp = c.patch(
            f"/api/sessions/{sid}/model",
            json={"model": "llama3.2:3b"},
        )
        assert resp.status_code == 200

    # A brand-new client (simulating a fresh page load) must see the model
    with _client() as c:
        resp = c.get(f"/api/sessions/{sid}")
        assert resp.status_code == 200
        assert resp.json()["model"] == "llama3.2:3b"
    _delete_session(sid)


def test_embedding_only_model_cannot_be_selected():
    """Embedding-only models (e.g. nomic-embed-text) are rejected for chat."""
    from fastapi.testclient import TestClient
    from app.main import app
    import app.routes.sessions as sessions_route

    # Ensure availability lookup is deterministic (returns a known list)
    def fake_available():
        return ["llama3.2:3b", "llama3.2:1b"]

    original = sessions_route.fetch_available_chat_models
    sessions_route.fetch_available_chat_models = fake_available
    try:
        with TestClient(app) as c:
            _, token = register_and_login(c, "itest_model_reject")
            headers = auth_headers(token)
            sess_resp = c.post("/api/sessions", json={"title": "Reject Test"}, headers=headers)
            sid = sess_resp.json()["id"]

            resp = c.patch(
                f"/api/sessions/{sid}/model",
                json={"model": "nomic-embed-text"},
                headers=headers,
            )
            assert resp.status_code == 400
            assert "embedding" in resp.json()["detail"].lower()
    finally:
        sessions_route.fetch_available_chat_models = original


def test_unavailable_chat_model_is_rejected():
    """A model not installed on Ollama is rejected with 400."""
    from fastapi.testclient import TestClient
    from app.main import app
    import app.routes.sessions as sessions_route

    def fake_available():
        return ["llama3.2:3b"]

    original = sessions_route.fetch_available_chat_models
    sessions_route.fetch_available_chat_models = fake_available
    try:
        with TestClient(app) as c:
            _, token = register_and_login(c, "itest_model_unavail")
            headers = auth_headers(token)
            sess_resp = c.post("/api/sessions", json={"title": "Unavail Test"}, headers=headers)
            sid = sess_resp.json()["id"]

            resp = c.patch(
                f"/api/sessions/{sid}/model",
                json={"model": "llama3.2:1b"},
                headers=headers,
            )
            assert resp.status_code == 400
            assert "not available" in resp.json()["detail"].lower()
    finally:
        sessions_route.fetch_available_chat_models = original


def test_changing_one_session_model_does_not_change_another():
    """Per-session models are isolated from each other."""
    with _client() as c:
        sid1 = _create_session()
        sid2 = _create_session()
        try:
            c.patch(f"/api/sessions/{sid1}/model", json={"model": "llama3.2:3b"})
            s1 = c.get(f"/api/sessions/{sid1}").json()
            s2 = c.get(f"/api/sessions/{sid2}").json()
            assert s1["model"] == "llama3.2:3b"
            assert s2["model"] is None
        finally:
            _delete_session(sid1)
            _delete_session(sid2)


def test_chat_generation_receives_session_selected_model():
    """The chat workflow passes the session's selected model to Ollama."""
    from fastapi.testclient import TestClient
    from app.main import app
    import app.services.langgraph_workflow as workflow

    captured = {}
    original = workflow.generate_response

    def fake_generate_response(messages, system_prompt=None, model_name=None, **kwargs):
        captured["model_name"] = model_name
        return "mocked answer"

    workflow.generate_response = fake_generate_response
    try:
        with TestClient(app) as c:
            _, token = register_and_login(c, "itest_model_chat")
            headers = auth_headers(token)
            sess_resp = c.post("/api/sessions", json={"title": "Model Chat"}, headers=headers)
            sid = sess_resp.json()["id"]
            c.patch(f"/api/sessions/{sid}/model", json={"model": "llama3.2:3b"}, headers=headers)

            resp = c.post(
                f"/api/sessions/{sid}/messages",
                json={"message": "hello"},
                headers=headers,
            )
            assert resp.status_code == 200
            assert captured.get("model_name") == "llama3.2:3b"
    finally:
        workflow.generate_response = original


def test_default_session_uses_configured_chat_model():
    """A session with no model set passes model_name=None (config default)."""
    from fastapi.testclient import TestClient
    from app.main import app
    import app.services.langgraph_workflow as workflow
    from app.config import settings

    captured = {}
    original = workflow.generate_response

    def fake_generate_response(messages, system_prompt=None, model_name=None, **kwargs):
        captured["model_name"] = model_name
        return "mocked answer"

    workflow.generate_response = fake_generate_response
    try:
        with TestClient(app) as c:
            _, token = register_and_login(c, "itest_model_default")
            headers = auth_headers(token)
            sess_resp = c.post("/api/sessions", json={"title": "Default Model"}, headers=headers)
            sid = sess_resp.json()["id"]

            resp = c.post(
                f"/api/sessions/{sid}/messages",
                json={"message": "hello"},
                headers=headers,
            )
            assert resp.status_code == 200
            # model_name=None → the Ollama client falls back to the configured default
            assert captured.get("model_name") is None
            assert settings.default_model
    finally:
        workflow.generate_response = original


def test_rag_embedding_model_remains_embedding_specific():
    """Document embeddings still use the embedding model, not the chat model."""
    import app.services.embeddings_client as ec
    assert ec.EMBED_MODEL == "nomic-embed-text"
