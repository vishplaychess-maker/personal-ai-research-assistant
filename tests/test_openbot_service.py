"""OpenBot orchestrator — real Phase 1 pipeline (plan/context/research/synthesis).

Covers the task queue, capability gate, polling, live event frames, cancellation,
and strict user isolation. Network access is avoided: with no Tavily key the
research step degrades to a warning, and desktop automation is off by default.
"""

from __future__ import annotations

import time

import pytest
from cryptography.fernet import Fernet

import app.services.openbot_service as openbot_module
from tests.auth_helpers import auth_headers, register_and_login


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    yield


def _enable_capability(client, headers) -> dict:
    r = client.patch(
        "/api/capabilities/openbot_research",
        json={"enabled": True},
        headers=headers,
    )
    assert r.status_code == 200, r.text[:300]
    return r.json()


def _start_task(client, headers, query="Compare vector databases for local RAG"):
    r = client.post(
        "/api/agents/openbot/research",
        json={"query": query},
        headers=headers,
    )
    assert r.status_code == 201 or r.status_code == 200, r.text[:300]
    body = r.json()
    assert set(body) == {"task_id"}
    return body["task_id"]


def test_research_requires_authentication():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        assert c.post(
            "/api/agents/openbot/research", json={"query": "x"}
        ).status_code in (401, 403)
        assert c.get(
            "/api/agents/openbot/tasks/whatever"
        ).status_code in (401, 403)


def test_research_requires_enabled_capability():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        headers = auth_headers(register_and_login(c)[1])
        # capability ships disabled; POST must fail closed with 403
        r = c.post(
            "/api/agents/openbot/research",
            json={"query": "test"},
            headers=headers,
        )
        assert r.status_code == 403, r.text[:300]
        # polling is NOT capability-gated (never strand an accepted task) but
        # an unknown, well-formed id is a plain 404
        assert c.get(
            "/api/agents/openbot/tasks/" + "0" * 32, headers=headers
        ).status_code == 404


def test_research_rejects_empty_query():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        headers = auth_headers(register_and_login(c, username="openbot_empty")[1])
        _enable_capability(c, headers)
        assert c.post(
            "/api/agents/openbot/research", json={"query": ""}, headers=headers
        ).status_code == 422


def test_start_creates_pending_task_row():
    from fastapi.testclient import TestClient

    from app.database import SessionLocal
    from app.main import app
    from app.models.models import OpenbotTask

    with TestClient(app) as c:
        headers = auth_headers(register_and_login(c)[1])
        _enable_capability(c, headers)
        task_id = _start_task(c, headers)

        assert len(task_id) == 32, "task id is a uuid4 hex string"
        assert all(ch in "0123456789abcdef" for ch in task_id)

        db = SessionLocal()
        try:
            rows = (
                db.query(OpenbotTask)
                .filter(OpenbotTask.task_id == task_id)
                .all()
            )
            assert len(rows) == 1
            row = rows[0]
            assert row.query == "Compare vector databases for local RAG"
            # Per the task contract: pending at creation, completed only after
            # the mock worker finishes (row polled straight after POST).
            assert row.status in ("pending", "running", "completed")
        finally:
            db.close()


def test_status_polling_reaches_completed_with_result():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        headers = auth_headers(register_and_login(c)[1])
        _enable_capability(c, headers)
        task_id = _start_task(c, headers)

        deadline = time.time() + 10
        seen_statuses: set[str] = set()
        final = None
        while time.time() < deadline:
            r = c.get(f"/api/agents/openbot/tasks/{task_id}", headers=headers)
            assert r.status_code == 200, r.text[:300]
            body = r.json()
            assert body["task_id"] == task_id
            assert set(body) == {"task_id", "status", "result_text", "created_at"}
            seen_statuses.add(body["status"])
            if body["status"] == "completed":
                final = body
                break
            time.sleep(0.05)

        assert final is not None, f"never completed; saw {seen_statuses}"
        assert seen_statuses <= {"pending", "running", "completed"}
        assert isinstance(final["result_text"], str) and final["result_text"]
        # The real pipeline echoes the request and includes the plan/summary.
        assert "OpenBot Task Result" in final["result_text"]
        assert "Compare vector databases" in final["result_text"]
        assert "## Plan" in final["result_text"]


def test_user_isolation_service_level():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import openbot_service

    with TestClient(app) as c:
        # two fresh users
        user_a = register_and_login(c, username="openbot_iso_a")
        user_b = register_and_login(c, username="openbot_iso_b")
        headers_a = auth_headers(user_a[1])
        headers_b = auth_headers(user_b[1])
        _enable_capability(c, headers_a)
        task_id = _start_task(c, headers_a)

        # Owner sees the task; the other user must not (None at the service
        # layer -> 404 at the route layer, same treatment as a missing task).
        assert openbot_service.get_task_status(task_id, user_a[0]) is not None
        assert openbot_service.get_task_status(task_id, user_b[0]) is None


def test_user_isolation_on_poll_route():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        user_a = register_and_login(c, username="openbot_iso_c")
        user_b = register_and_login(c, username="openbot_iso_d")
        headers_a = auth_headers(user_a[1])
        headers_b = auth_headers(user_b[1])
        _enable_capability(c, headers_a)
        _enable_capability(c, headers_b)
        task_id = _start_task(c, headers_a)

        # B (capability enabled) polling A's task -> 404, not 403.
        r = c.get(f"/api/agents/openbot/tasks/{task_id}", headers=headers_b)
        assert r.status_code == 404, r.text[:300]

        # The owner still polls fine.
        assert (
            c.get(f"/api/agents/openbot/tasks/{task_id}", headers=headers_a).status_code
            == 200
        )

        # A fabricated, well-formed id that does not exist -> 404 as well
        # (users must not be able to distinguish "exists but not yours").
        fake = "a" * 32
        assert c.get(f"/api/agents/openbot/tasks/{fake}", headers=headers_a).status_code == 404


def test_unknown_task_is_404_not_500():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        headers = auth_headers(register_and_login(c)[1])
        _enable_capability(c, headers)
        # malformed shapes must not blow up the poll endpoint
        assert c.get("/api/agents/openbot/tasks/", headers=headers).status_code in (404, 307)
        assert c.get(
            "/api/agents/openbot/tasks/" + "!" + "x" * 40, headers=headers
        ).status_code == 404


def test_live_events_are_recorded_and_user_scoped():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import openbot_service

    with TestClient(app) as c:
        user_id, token = register_and_login(c, username="openbot_events")
        headers = auth_headers(token)
        _enable_capability(c, headers)
        task_id = _start_task(c, headers, query="Summarize the workspace")

        deadline = time.time() + 10
        while time.time() < deadline:
            status = openbot_service.get_task_status(task_id, user_id)
            if status and status["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.05)

        events = openbot_service.get_task_events(task_id, user_id)
        assert events, "the orchestrator must publish progress frames"
        types = {event["type"] for event in events}
        assert {"plan", "status"} <= types
        assert any(event["type"] == "output" for event in events)

        # Another account cannot read the frames.
        _other_id, other_token = register_and_login(c, username="openbot_events_b")
        assert (
            openbot_service.get_task_events(task_id, _other_id) is None
        )


def test_events_route_replays_frames_for_an_owned_task():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import openbot_service

    with TestClient(app) as c:
        user_id, token = register_and_login(c, username="openbot_sse")
        headers = auth_headers(token)
        _enable_capability(c, headers)
        task_id = _start_task(c, headers, query="Summarize the workspace")

        deadline = time.time() + 10
        while time.time() < deadline:
            status = openbot_service.get_task_status(task_id, user_id)
            if status and status["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.05)

        with c.stream(
            "GET", f"/api/agents/openbot/tasks/{task_id}/events", headers=headers
        ) as response:
            assert response.status_code == 200, response.text[:300]
            body = "".join(response.iter_text())
        assert "event: agent_event" in body
        assert '"type": "plan"' in body


def test_cancellation_flag_marks_the_task_cancelled():
    from fastapi.testclient import TestClient

    from app.database import SessionLocal
    from app.main import app
    from app.models.models import OpenbotTask
    from app.services import agent_events, openbot_service, orchestrator

    with TestClient(app) as c:
        user_id, _token = register_and_login(c, username="openbot_cancel")
        task_id = "c" * 32
        db = SessionLocal()
        try:
            db.add(
                OpenbotTask(
                    user_id=user_id, task_id=task_id, query="anything", status="pending"
                )
            )
            db.commit()
        finally:
            db.close()

        agent_events.request_cancel(task_id)
        orchestrator.run_task(task_id, user_id, "anything")
        status = openbot_service.get_task_status(task_id, user_id)
        assert status is not None and status["status"] == "cancelled"
        agent_events.clear(task_id)
