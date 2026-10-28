"""Focused tests for the D6C-J1 test-only SSE fault injector (RED first).

Run: PYTHONPATH=backend python -m pytest tests/smoke/test_sse_fault_injector.py -q
"""
from __future__ import annotations

import asyncio

from tests.smoke.sse_fault_injector import SSEStreamTruncator

SCOPE = {"type": "http", "method": "POST",
         "path": "/api/sessions/1/messages/stream"}
OTHER = {"type": "http", "method": "POST", "path": "/api/sessions"}

START = b"event: start\ndata: {\"run_id\":\"r1\"}\n\n"
RUNNING = b"id: 3\nevent: run_status\ndata: {\"status\":\"running\"}\n\n"
TOKEN1 = b"id: 4\nevent: token\ndata: {\"token\":\"AAAA\"}\n\n"
TOKEN2 = b"id: 5\nevent: token\ndata: {\"token\":\"BBBB\"}\n\n"
DONE = b"id: 6\nevent: complete\ndata: {\"message_id\":2}\n\n"
FULL = START + RUNNING + TOKEN1 + TOKEN2 + DONE


def _app(chunks, headers=((b"content-type", b"text/event-stream"),)):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": list(headers)})
        for i, c in enumerate(chunks):
            await send({"type": "http.response.body", "body": c, "more_body": i < len(chunks) - 1})
    return app


def _run(injector, scope=SCOPE):
    sent = []

    async def send(m):
        sent.append(m)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(injector(scope, receive, send))
    body = b"".join(m.get("body") or b"" for m in sent
                    if m["type"] == "http.response.body")
    last_more = [m.get("more_body") for m in sent if m["type"] == "http.response.body"]
    return sent, body, last_more


def test_A_disabled_injector_forwards_the_full_stream_unchanged():
    inj = SSEStreamTruncator(_app([FULL]), enabled=False)
    sent, body, more = _run(inj)
    assert body == FULL
    assert inj.cuts == 0
    assert more[-1] is False
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200


def test_B_armed_injector_forwards_through_the_first_token_frame_then_cuts():
    inj = SSEStreamTruncator(_app([FULL]), enabled=True)
    sent, body, more = _run(inj)
    assert inj.cuts == 1
    # exactly the frames up to and including the first token frame were forwarded
    assert body == START + RUNNING + TOKEN1
    assert b"complete" not in body
    assert TOKEN2 not in body
    assert more[-1] is False                      # response ended early
    rec = inj.records[-1]
    assert rec["event_types"] == ["start", "run_status", "token"]
    assert rec["event_ids"] == [None, "3", "4"]
    assert rec["forwarded_bytes"] == len(START + RUNNING + TOKEN1)
    assert rec["terminated_early"] is True and rec["cut_at_ms"] >= 0


def test_C_second_identical_request_is_not_truncated_and_reaches_complete():
    inj = SSEStreamTruncator(_app([FULL]), enabled=True)
    _, first_body, _ = _run(inj)
    _, second_body, more = _run(inj)
    assert inj.cuts == 1
    assert first_body != FULL
    assert second_body == FULL                       # byte-for-byte passthrough
    assert more[-1] is False


def test_D_non_target_routes_are_never_modified():
    inj = SSEStreamTruncator(_app([FULL]), enabled=True)
    _, body, _ = _run(inj, scope=OTHER)
    assert body == FULL
    assert inj.cuts == 0
    # a GET on the stream path is not the authenticated submission
    _, body2, _ = _run(inj, scope={**SCOPE, "method": "GET"})
    assert body2 == FULL and inj.cuts == 0


def test_E_split_utf8_and_split_frame_boundaries_are_reassembled_before_the_cut():
    token = "id: 4\nevent: token\ndata: {\"token\":\"నమస్కారం🌩️\"}\n\n".encode()
    stream = START + RUNNING + token + DONE
    chunks = [stream[:40], stream[40:75], stream[75:76], stream[76:120], stream[120:]]
    inj = SSEStreamTruncator(_app(chunks), enabled=True)
    _, body, more = _run(inj)
    assert inj.cuts == 1
    assert body.decode("utf-8") == (START + RUNNING + token).decode("utf-8")
    assert "నమస్కారం🌩️" in body.decode("utf-8")
    assert more[-1] is False


def test_F_cleanup_leaves_no_pending_asyncio_task():
    async def scenario():
        inj = SSEStreamTruncator(_app([FULL]), enabled=True)
        sent = []

        async def send(m):
            sent.append(m)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        await inj(SCOPE, receive, send)
        await asyncio.sleep(0)
        me = asyncio.current_task()
        return [t for t in asyncio.all_tasks() if t is not me and not t.done()]

    pending = asyncio.run(scenario())
    assert pending == [], "injector left a pending asyncio task"
