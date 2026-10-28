"""OpenBot Autonomous Research Agent — Phase 1 (task queue + mock execution).

Covers:
* task creation via POST /api/agents/openbot/research (non-blocking, immediate
  task id),
* the capability gate (disabled -> 403, enabled -> accepted, auth required),
* status polling via GET /api/agents/openbot/tasks/{task_id} through the
  pending/running -> completed transition with a result,
* strict user isolation — service and route level cross-user access is
  indistinguishable from a missing task (None / 404, never a leak).

The mock execution delay is monkeypatched to ~0 so the suite stays fast; a
separate test asserts the shipped default is the promised ~5 seconds.
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
    # Shrink the mock agent runtime so the completed-state poll is instant.
    monkeypatch.setattr(openbot_module, "MOCK_COMPLETION_SECONDS", 0.0)
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


def test_mock_default_completion_is_five_seconds():
    """The shipped constant keeps the promised ~5s mock runtime.

    Checked at source level because the autouse fixture shrinks the runtime
    value for the whole module (poll tests must stay fast).
    """
    import inspect

    source = inspect.getsource(openbot_module)
    assert "MOCK_COMPLETION_SECONDS = 5.0" in source


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
        # the mock summary must not echo the raw query back
        assert "Compare vector databases" not in final["result_text"]


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
