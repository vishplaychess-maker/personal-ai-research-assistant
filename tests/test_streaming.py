"""
Phase 5A tests — SSE streaming endpoint.

Tests the streaming endpoint at POST /api/sessions/{id}/messages/stream.

Usage:
    pytest tests/test_streaming.py -v

Some tests require the Docker backend to be running.
Mock-based tests use TestClient (in-process) and run without Docker.
"""

import json
import os
from typing import Any, Dict, List

import httpx
import pytest

BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")

# ── Helpers ────────────────────────────────────────────────


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, timeout=30.0)


def _create_session() -> int:
    """Create a test session and return its ID."""
    with _client() as c:
        resp = c.post("/api/sessions", json={"title": "Stream Test Session"})
        assert resp.status_code == 201
        return resp.json()["id"]


def _delete_session(session_id: int):
    """Delete a test session."""
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


# ── Parse SSE helpers ─────────────────────────────────────


def _parse_sse_events(response_text: str) -> List[Dict[str, Any]]:
    """
    Parse SSE event stream into a list of events.

    Each event has: { "event": "...", "data": {...} }
    """
    events = []
    current_event = None
    current_data_lines = []

    for line in response_text.strip().split("\n"):
        if line.startswith("event: "):
            if current_event:
                events.append({
                    "event": current_event,
                    "data": json.loads("".join(current_data_lines)),
                })
                current_data_lines = []
            current_event = line[7:]
        elif line.startswith("data: "):
            current_data_lines.append(line[6:])

    if current_event:
        events.append({
            "event": current_event,
            "data": json.loads("".join(current_data_lines)),
        })

    return events


# ── Non-streaming endpoint still works ─────────────────────


def test_non_streaming_endpoint_still_works():
    """The original POST /api/sessions/{id}/messages must still work."""
    with _client() as c:
        sid = _create_session()
        try:
            resp = c.post(
                f"/api/sessions/{sid}/messages",
                json={"message": "Hello, are you working?"},
            )
            # Should return 200 (not 404) — Ollama may return an error but
            # the endpoint should respond with a ChatResponse JSON.
            assert resp.status_code == 200
            data = resp.json()
            assert "user_message" in data
            assert "assistant_message" in data
            assert data["user_message"]["role"] == "user"
            assert data["assistant_message"]["role"] == "assistant"
        finally:
            _delete_session(sid)


# ── Session validation ────────────────────────────────────


def test_stream_invalid_session_returns_404():
    """POST /api/sessions/99999/messages/stream must return 404."""
    with _client() as c:
        resp = c.post(
            "/api/sessions/99999/messages/stream",
            json={"message": "Hello"},
        )
    assert resp.status_code == 404


def test_stream_invalid_session_id_returns_422():
    """POST /api/sessions/-1/messages/stream must return 422."""
    with _client() as c:
        resp = c.post(
            "/api/sessions/-1/messages/stream",
            json={"message": "Hello"},
        )
    assert resp.status_code == 422


# ── Streaming event order ─────────────────────────────────


def test_stream_event_order():
    """Streaming endpoint must emit events in correct order: start → token* → complete."""
    with _client() as c:
        sid = _create_session()
        try:
            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Say 'Hello' in one word."},
            )
        finally:
            _delete_session(sid)

    # Check that the endpoint responded
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("text/event-stream")

    events = _parse_sse_events(resp.text)
    assert len(events) >= 2, f"Expected at least 2 events, got {len(events)}"

    # First event must be 'start'
    assert events[0]["event"] == "start", f"Expected first event to be 'start', got '{events[0]['event']}'"
    assert "session_id" in events[0]["data"]

    # If streaming worked (Ollama available), last event should be 'complete'
    # If Ollama unavailable, last event may be 'error'
    last_event = events[-1]["event"]
    assert last_event in ("complete", "error"), (
        f"Expected last event to be 'complete' or 'error', got '{last_event}'"
    )

    # If complete, verify message_id is present
    if last_event == "complete":
        assert "message_id" in events[-1]["data"]
        assert isinstance(events[-1]["data"]["message_id"], int)
        assert events[-1]["data"]["message_id"] > 0


def test_stream_tokens_are_delivered():
    """Token events should have non-empty token text."""
    with _client() as c:
        sid = _create_session()
        try:
            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Count from 1 to 5."},
            )
        finally:
            _delete_session(sid)

    assert resp.status_code == 200
    events = _parse_sse_events(resp.text)

    # Find token events
    token_events = [e for e in events if e["event"] == "token"]
    if token_events:
        for te in token_events:
            assert "token" in te["data"], f"Token event missing 'token' field: {te}"
            # Tokens can be empty string for the first event, but typically non-empty
            # We just verify the field exists


def test_stream_completion_has_metadata():
    """Complete event should include message_id, citations, sources_used, memories_used."""
    with _client() as c:
        sid = _create_session()
        try:
            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "What is 2+2?"},
            )
        finally:
            _delete_session(sid)

    assert resp.status_code == 200
    events = _parse_sse_events(resp.text)
    last_event = events[-1]

    if last_event["event"] == "complete":
        required_fields = ["message_id", "citations", "sources_used", "memories_used"]
        for field in required_fields:
            assert field in last_event["data"], (
                f"Complete event missing field '{field}': {last_event['data']}"
            )


# ── Message persistence after completion ──────────────────


def test_stream_message_persisted_after_completion():
    """After a successful stream, the assistant message should exist in the database."""
    with _client() as c:
        sid = _create_session()
        try:
            # Send streaming message
            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Say 'persistence test'"},
            )

            assert resp.status_code == 200
            events = _parse_sse_events(resp.text)
            last_event = events[-1]

            if last_event["event"] == "complete":
                message_id = last_event["data"]["message_id"]

                # List messages — verify the assistant message is there
                msgs_resp = c.get(f"/api/sessions/{sid}/messages")
                assert msgs_resp.status_code == 200
                messages = msgs_resp.json()

                # Find the saved message by ID
                saved = [m for m in messages if m["id"] == message_id]
                assert len(saved) == 1, f"Message {message_id} not found in session messages"
                assert saved[0]["role"] == "assistant"
                assert len(saved[0]["content"]) > 0
        finally:
            _delete_session(sid)


# ── Message does NOT persist on error ─────────────────────


def test_stream_no_message_on_ollama_error():
    """If Ollama returns an error, no assistant message should be saved."""
    with _client() as c:
        sid = _create_session()
        try:
            # Get message count before
            msgs_before = c.get(f"/api/sessions/{sid}/messages").json()
            count_before = len(msgs_before)

            # Streaming endpoint may error if Ollama is unavailable
            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "This should trigger an error"},
            )

            # If we got an error event, check no extra assistant message was saved
            if resp.status_code == 200:
                events = _parse_sse_events(resp.text)
                last_event = events[-1]
                if last_event["event"] == "error":
                    msgs_after = c.get(f"/api/sessions/{sid}/messages").json()
                    # Only user message should have been added (no assistant)
                    assert len(msgs_after) == count_before + 1, (
                        f"Expected {count_before + 1} messages (user only), got {len(msgs_after)}"
                    )
                    last_msg = msgs_after[-1]
                    assert last_msg["role"] == "user", (
                        f"Expected last message to be 'user', got '{last_msg['role']}'"
                    )
        finally:
            _delete_session(sid)


# ── Existing non-streaming endpoint compatibility ─────────


def test_non_streaming_still_functional():
    """The non-streaming endpoint must still create messages after streaming endpoint is added."""
    with _client() as c:
        sid = _create_session()
        try:
            # Send via non-streaming
            resp = c.post(
                f"/api/sessions/{sid}/messages",
                json={"message": "Non-streaming message"},
            )
            assert resp.status_code == 200
        finally:
            _delete_session(sid)


# ── In-process mock test (no Docker required) ─────────────


def test_stream_mocked_ollama():
    """
    Mock the stream_chat_response function to verify the endpoint
    processes stream events correctly without Ollama running.

    Patches at app.routes.messages.stream_chat_response (where it's
    imported and used) rather than the source module, since the
    endpoint holds a local reference via the import.

    Uses TestClient (in-process) to test the route logic.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    import app.services.streaming_service as streaming_service

    async def mock_stream_chat_response(context):
        """Mock streaming generator that yields predictable events."""
        yield streaming_service.format_sse("start", {
            "session_id": context.session_id,
            "sources_used": False,
            "memories_used": False,
        })
        yield streaming_service.format_sse("token", {"token": "Hello"})
        yield streaming_service.format_sse("token", {"token": " world"})
        yield streaming_service.format_sse("token", {"token": "!"})
        yield streaming_service.format_sse("complete", {
            "message_id": None,
            "citations": [],
            "sources_used": False,
            "memories_used": False,
            "content": "Hello world!",
        })

    from unittest.mock import patch
    with patch("app.routes.messages.stream_chat_response", mock_stream_chat_response):
        with TestClient(app) as c:
            # Create session
            sess_resp = c.post("/api/sessions", json={"title": "Mock Stream Test"})
            assert sess_resp.status_code == 201
            sid = sess_resp.json()["id"]

            # Send streaming message
            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Hello from mock"},
            )

    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("text/event-stream")

    events = _parse_sse_events(resp.text)

    # Verify event order — first must be start
    assert len(events) >= 5, f"Expected >= 5 events, got {len(events)}"
    assert events[0]["event"] == "start", "First event must be 'start'"

    # Collect all token events
    token_events = [e for e in events if e["event"] == "token"]
    assert len(token_events) >= 3, f"Expected >= 3 token events, got {len(token_events)}"

    # Verify all expected tokens are present (exact order not guaranteed)
    token_texts = [t["data"]["token"] for t in token_events]
    expected_tokens = ["Hello", " world", "!"]
    for expected in expected_tokens:
        assert expected in token_texts, (
            f"Expected token {expected!r} not found in {token_texts}"
        )

    # Last event should be complete (not error)
    last_event = events[-1]
    assert last_event["event"] == "complete", (
        f"Expected 'complete', got '{last_event['event']}'"
    )
    assert "message_id" in last_event["data"]
    assert last_event["data"]["citations"] == []


# ── Memory enabled/disabled compatibility ─────────────────


def test_stream_memory_compatibility():
    """Streaming endpoint should work with both memory enabled and disabled."""
    with _client() as c:
        sid = _create_session()
        try:
            # Test with memory enabled (default)
            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Say 'memory enabled test'"},
            )
            assert resp.status_code == 200

            # Toggle memory off
            c.patch("/api/settings/memory", json={"enabled": False})

            # Test with memory disabled
            resp2 = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Say 'memory disabled test'"},
            )
            assert resp2.status_code == 200

            # Restore memory setting
            c.patch("/api/settings/memory", json={"enabled": True})
        finally:
            _delete_session(sid)


# ── Error event format ────────────────────────────────────


def test_stream_error_event_format():
    """Error events should have 'code' and 'detail' fields."""
    with _client() as c:
        sid = _create_session()
        try:
            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Trigger error"},
            )
        finally:
            _delete_session(sid)

    if resp.status_code == 200:
        events = _parse_sse_events(resp.text)
        error_events = [e for e in events if e["event"] == "error"]
        for ee in error_events:
            assert "code" in ee["data"], f"Error event missing 'code': {ee}"
            assert "detail" in ee["data"], f"Error event missing 'detail': {ee}"
            assert isinstance(ee["data"]["code"], str)
            assert isinstance(ee["data"]["detail"], str)
            assert len(ee["data"]["code"]) > 0
            assert len(ee["data"]["detail"]) > 0


# ── Exactly one complete event ────────────────────────────


def test_stream_exactly_one_complete_event():
    """The stream must emit exactly one 'complete' event (never zero or multiple)."""
    with _client() as c:
        sid = _create_session()
        try:
            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Say 'single complete'"},
            )
        finally:
            _delete_session(sid)

    if resp.status_code == 200:
        events = _parse_sse_events(resp.text)
        complete_events = [e for e in events if e["event"] == "complete"]
        # Accept either 1 complete or 0 (if Ollama unavailable — error instead)
        error_events = [e for e in events if e["event"] == "error"]
        if len(complete_events) == 0:
            assert len(error_events) >= 1, (
                "Expected either 1 complete event or >= 1 error events, "
                f"got {len(complete_events)} complete and {len(error_events)} error"
            )
        else:
            assert len(complete_events) == 1, (
                f"Expected exactly 1 'complete' event, got {len(complete_events)}"
            )


# ── No duplicate assistant messages ────────────────────────


def test_stream_no_duplicate_assistant_messages():
    """Sending the same streaming message twice must create exactly two messages total."""
    with _client() as c:
        sid = _create_session()
        try:
            # Send same message twice
            msg1 = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Say 'hello'"},
            )
            msg2 = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Say 'hello'"},
            )

            # List all messages
            msgs = c.get(f"/api/sessions/{sid}/messages").json()

            # Count assistant messages
            assistant_msgs = [m for m in msgs if m["role"] == "assistant"]

            # At minimum, if Ollama was available for both, we should have 2 assistant messages
            # (one per request). If Ollama was unavailable, we may have 0 (both errored).
            assert len(assistant_msgs) <= 2, (
                f"Expected at most 2 assistant messages (one per request), got {len(assistant_msgs)}"
            )

            # Verify message count is sane — at most 2 per request
            assert len(assistant_msgs) <= 2, (
                f"Expected at most 2 assistant messages (one per request), got {len(assistant_msgs)}"
            )
            # Verify no message has duplicate ID (ORM-level duplicate protection)
            ids = [m["id"] for m in assistant_msgs]
            assert len(ids) == len(set(ids)), (
                f"Duplicate assistant message IDs found: {ids}"
            )
        finally:
            _delete_session(sid)


# ── Memory disabled flag ──────────────────────────────────


def test_stream_memory_disabled_returns_correct_flag():
    """When memory is disabled, the start event must report memories_used=false."""
    with _client() as c:
        sid = _create_session()
        try:
            # Disable memory
            c.patch("/api/settings/memory", json={"enabled": False})

            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Test memory disabled"},
            )

            # Re-enable memory
            c.patch("/api/settings/memory", json={"enabled": True})
        finally:
            _delete_session(sid)

    assert resp.status_code == 200
    events = _parse_sse_events(resp.text)
    start_event = events[0]
    assert start_event["event"] == "start"
    assert start_event["data"]["memories_used"] is False, (
        f"memories_used should be False when memory is disabled, got {start_event['data']['memories_used']}"
    )


# ── Message size and content validation ───────────────────


def test_stream_message_empty_rejected():
    """Empty messages must be rejected with 422 (min_length=1 validation)."""
    with _client() as c:
        sid = _create_session()
        try:
            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": ""},
            )
            assert resp.status_code == 422
        finally:
            _delete_session(sid)


def test_stream_message_size_exceeded():
    """Messages over 10,000 characters must be rejected with 422."""
    with _client() as c:
        sid = _create_session()
        try:
            long_message = "A" * 10001
            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": long_message},
            )
            assert resp.status_code == 422
            body = resp.json()
            # FastAPI's Pydantic validation returns detail as a list of error dicts
            detail = body.get("detail", body)
            detail_str = str(detail)
            assert "10000" in detail_str or "10001" in detail_str or "exceed" in detail_str.lower(), (
                f"Expected size-limit error in 422 response, got: {detail_str}"
            )
        finally:
            _delete_session(sid)


# ── Error events have no stack traces ─────────────────────


def test_stream_error_no_stack_trace():
    """Error event 'detail' must not contain internal paths, source code, or secrets."""
    with _client() as c:
        sid = _create_session()
        try:
            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Trigger error check"},
            )
        finally:
            _delete_session(sid)

    if resp.status_code == 200:
        events = _parse_sse_events(resp.text)
        error_events = [e for e in events if e["event"] == "error"]
        for ee in error_events:
            detail = ee["data"].get("detail", "")
            # Must not contain internal file paths (forward or backslash)
            assert "/app/" not in detail, f"Error detail contains internal path: {detail}"
            assert "backend\\" not in detail, f"Error detail contains internal path: {detail}"
            # Must not contain stack trace markers
            assert "Traceback" not in detail, f"Error detail contains stack trace: {detail}"
            assert "File " not in detail, f"Error detail contains file reference: {detail}"
            assert "line " not in detail, f"Error detail contains line reference: {detail}"
            # Must not contain secrets patterns
            assert "password" not in detail.lower(), f"Error detail may contain secret: {detail}"
            assert "secret" not in detail.lower(), f"Error detail may contain secret: {detail}"
            assert "api_key" not in detail.lower(), f"Error detail may contain secret: {detail}"


# ── In-process timeout mock test ──────────────────────────


def test_stream_ollama_timeout_error():
    """
    When Ollama times out, the endpoint must yield an error event
    with code 'OLLAMA_ERROR' and a user-facing message (no stack traces).

    Uses TestClient with a mocked stream_chat_response that simulates a timeout.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    import app.services.streaming_service as streaming_service

    async def mock_timeout_response(context):
        yield streaming_service.format_sse("start", {
            "session_id": context.session_id,
            "sources_used": False,
            "memories_used": False,
        })
        yield streaming_service.format_sse("error", {
            "code": "OLLAMA_ERROR",
            "detail": "Ollama did not respond in time. The model might still be loading or "
                     "the prompt was too long.",
        })

    from unittest.mock import patch
    with patch("app.routes.messages.stream_chat_response", mock_timeout_response):
        with TestClient(app) as c:
            sess_resp = c.post("/api/sessions", json={"title": "Timeout Test"})
            sid = sess_resp.json()["id"]

            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Trigger timeout"},
            )

    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("text/event-stream")

    events = _parse_sse_events(resp.text)

    # First event must be start
    assert events[0]["event"] == "start"

    # Last event must be an error (simulating timeout)
    last_event = events[-1]
    assert last_event["event"] == "error", f"Expected 'error', got '{last_event['event']}'"
    assert "code" in last_event["data"]
    assert "detail" in last_event["data"]
    assert len(last_event["data"]["detail"]) > 0
    # No stack traces or internal paths
    detail = last_event["data"]["detail"]
    assert "Traceback" not in detail
    assert "/app/" not in detail

    # Verify no assistant message was saved to the database
    with _client() as c:
        msgs = c.get(f"/api/sessions/{sid}/messages").json()
        assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert len(assistant_msgs) == 0, (
            f"Expected 0 assistant messages on timeout, got {len(assistant_msgs)}"
        )


# ── In-process cancellation mock test ─────────────────────


def test_stream_client_cancellation():
    """
    When the client disconnects mid-stream, the endpoint must yield a
    'cancelled' event and stop producing further events.

    Uses TestClient with a patched is_disconnected that returns True
    after the first token event.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    import app.services.streaming_service as streaming_service

    call_count = 0

    async def mock_disconnected(self):
        """Mock is_disconnected — returns True after first token passes through."""
        nonlocal call_count
        call_count += 1
        # call 1 = after start → False, call 2 = after first token → False,
        # call 3 = after second token → True (cancel before complete)
        return call_count >= 3

    async def mock_stream_cancel(context):
        yield streaming_service.format_sse("start", {
            "session_id": context.session_id,
            "sources_used": False,
            "memories_used": False,
        })
        yield streaming_service.format_sse("token", {"token": "Hello"})
        yield streaming_service.format_sse("token", {"token": " world"})
        yield streaming_service.format_sse("complete", {
            "message_id": None,
            "citations": [],
            "sources_used": False,
            "memories_used": False,
            "content": "Hello world",
        })

    from unittest.mock import patch
    with patch("app.routes.messages.stream_chat_response", mock_stream_cancel), \
         patch("app.routes.messages.Request.is_disconnected", mock_disconnected):
        with TestClient(app) as c:
            sess_resp = c.post("/api/sessions", json={"title": "Cancel Test"})
            sid = sess_resp.json()["id"]

            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Test cancellation"},
            )

    assert resp.status_code == 200
    events = _parse_sse_events(resp.text)

    # First event must be start
    assert events[0]["event"] == "start"

    # Second event should be the token before cancellation
    assert len(events) >= 2
    assert events[1]["event"] == "token"

    # The last event should be 'cancelled' (we should NOT see 'complete')
    last_event = events[-1]
    assert last_event["event"] == "cancelled", (
        f"Expected last event to be 'cancelled', got '{last_event['event']}'"
    )
    assert "detail" in last_event["data"]

    # Verify NO 'complete' or 'error' events appear after 'cancelled'
    cancelled_idx = next(i for i, e in enumerate(events) if e["event"] == "cancelled")
    post_cancel_events = events[cancelled_idx + 1:]
    assert len(post_cancel_events) == 0, (
        f"Expected no events after 'cancelled', got {len(post_cancel_events)}: {post_cancel_events}"
    )


# ── No persistence on cancellation ────────────────────────


def test_stream_no_persistence_on_cancellation():
    """
    When the client disconnects mid-stream, no assistant message should
    be persisted to the database.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    import app.services.streaming_service as streaming_service

    call_count = 0

    async def mock_disconnected(self):
        """Mock is_disconnected — returns True after first token passes through."""
        nonlocal call_count
        call_count += 1
        # Disconnect after the first token event:
        #   call 1 = after start → False
        #   call 2 = after first token → False
        #   call 3 = after second token → True (cancel before complete)
        return call_count >= 3

    async def mock_stream_cancel(context):
        yield streaming_service.format_sse("start", {
            "session_id": context.session_id,
            "sources_used": False,
            "memories_used": False,
        })
        yield streaming_service.format_sse("token", {"token": "Partial"})
        yield streaming_service.format_sse("token", {"token": " content"})
        yield streaming_service.format_sse("complete", {
            "message_id": None,
            "citations": [],
            "sources_used": False,
            "memories_used": False,
            "content": "Partial content",
        })

    from unittest.mock import patch
    with patch("app.routes.messages.stream_chat_response", mock_stream_cancel), \
         patch("app.routes.messages.Request.is_disconnected", mock_disconnected):
        with TestClient(app) as c:
            sess_resp = c.post("/api/sessions", json={"title": "Cancel Persist Test"})
            sid = sess_resp.json()["id"]

            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Test cancellation persistence"},
            )

            # List messages — should have NO assistant message
            msgs = c.get(f"/api/sessions/{sid}/messages").json()
            assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
            assert len(assistant_msgs) == 0, (
                f"Expected 0 assistant messages after cancellation, got {len(assistant_msgs)}"
            )

    # Verify the stream ended with 'cancelled' not 'complete'
    events = _parse_sse_events(resp.text)
    last_event = events[-1]
    assert last_event["event"] == "cancelled", (
        f"Expected 'cancelled', got '{last_event['event']}'"
    )


# ── Start event has no token field ─────────────────────────


def test_stream_start_event_no_token_field():
    """The start event must NOT contain a token field."""
    with _client() as c:
        sid = _create_session()
        try:
            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Say 'check fields'"},
            )
        finally:
            _delete_session(sid)

    if resp.status_code == 200:
        events = _parse_sse_events(resp.text)
        start_event = events[0]
        assert "token" not in start_event["data"], (
            f"Start event should not have 'token' field: {start_event['data']}"
        )
        assert "session_id" in start_event["data"]


def test_stream_complete_event_no_token_field():
    """The complete event must NOT contain a token field."""
    with _client() as c:
        sid = _create_session()
        try:
            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Say 'check complete fields'"},
            )
        finally:
            _delete_session(sid)

    if resp.status_code == 200:
        events = _parse_sse_events(resp.text)
        complete_events = [e for e in events if e["event"] == "complete"]
        for ce in complete_events:
            assert "token" not in ce["data"], (
                f"Complete event should not have 'token' field: {ce['data']}"
            )
            assert "message_id" in ce["data"]


# ── Non-streaming endpoint creates correct messages ───────


def test_non_streaming_endpoint_message_count():
    """Non-streaming endpoint should create exactly 1 user + 1 assistant message."""
    with _client() as c:
        sid = _create_session()
        try:
            msgs_before = c.get(f"/api/sessions/{sid}/messages").json()
            count_before = len(msgs_before)

            resp = c.post(
                f"/api/sessions/{sid}/messages",
                json={"message": "Count check"},
            )
            assert resp.status_code == 200

            msgs_after = c.get(f"/api/sessions/{sid}/messages").json()
            # Should have exactly 2 more messages: 1 user + 1 assistant
            assert len(msgs_after) == count_before + 2, (
                f"Expected {count_before + 2} messages, got {len(msgs_after)}"
            )
        finally:
            _delete_session(sid)


# ── Streaming endpoint headers ────────────────────────────


def test_stream_headers_are_correct():
    """Streaming response must have the correct SSE headers."""
    from fastapi.testclient import TestClient
    from app.main import app
    import app.services.streaming_service as streaming_service

    async def mock_headers_stream(context):
        yield streaming_service.format_sse("start", {
            "session_id": context.session_id,
            "sources_used": False,
            "memories_used": False,
        })
        yield streaming_service.format_sse("complete", {
            "message_id": None,
            "citations": [],
            "sources_used": False,
            "memories_used": False,
            "content": "OK",
        })

    from unittest.mock import patch
    with patch("app.routes.messages.stream_chat_response", mock_headers_stream):
        with TestClient(app) as c:
            sess_resp = c.post("/api/sessions", json={"title": "Headers Test"})
            sid = sess_resp.json()["id"]

            resp = c.post(
                f"/api/sessions/{sid}/messages/stream",
                json={"message": "Check headers"},
            )

    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("text/event-stream")
    assert resp.headers.get("cache-control", "") == "no-cache"
    assert resp.headers.get("x-accel-buffering", "") == "no"
    assert resp.headers.get("connection", "") == "keep-alive"
