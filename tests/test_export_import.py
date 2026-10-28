"""
F5 Shareable Agents — export/import round-trip tests.

Uses FastAPI TestClient (in-process) + the LocalProvider for deterministic
behavior. No live Ollama required.
"""

import pytest

from fastapi.testclient import TestClient

from app.main import app

from tests.auth_helpers import register_and_login, auth_headers


# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def token(client):
    _, tok = register_and_login(client, username=None)
    return tok


def _headers(token):
    return auth_headers(token)


def _create_session(client, token, title="Shareable Session"):
    headers = _headers(token)
    resp = client.post("/api/sessions", json={"title": title}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Export tests ────────────────────────────────────────────


def test_export_shape(client, token):
    session = _create_session(client, token)
    resp = client.get(f"/api/sessions/{session['id']}/export", headers=_headers(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "thunder_ai_export" in body
    ex = body["thunder_ai_export"]
    assert ex["version"] == "1.0"
    assert "exported_at" in ex
    assert ex["session"]["title"] == "Shareable Session"
    assert ex["session"]["model"] is None
    assert ex["session"]["system_prompt"] is None
    assert set(ex["schedule"].keys()) == {"cron_expression", "prompt", "is_active"}
    assert "memory" in ex and "enabled" in ex["memory"]


def test_export_404_unknown_session(client, token):
    resp = client.get("/api/sessions/99999/export", headers=_headers(token))
    assert resp.status_code == 404


def test_export_reflects_session_model_and_prompt(client, token):
    session = _create_session(client, token)
    # set model + system prompt
    client.patch(
        f"/api/sessions/{session['id']}/model",
        json={"model": "llama3.2:1b"},
        headers=_headers(token),
    )
    client.patch(
        f"/api/sessions/{session['id']}/system-prompt",
        json={"system_prompt": "You are a research agent."},
        headers=_headers(token),
    )
    resp = client.get(f"/api/sessions/{session['id']}/export", headers=_headers(token))
    ex = resp.json()["thunder_ai_export"]
    assert ex["session"]["model"] == "llama3.2:1b"
    assert ex["session"]["system_prompt"] == "You are a research agent."


def test_export_includes_schedule(client, token):
    headers = _headers(token)
    session = _create_session(client, token)
    # Add a scheduled task
    resp = client.post(
        "/api/scheduler",
        json={
            "session_id": session["id"],
            "prompt": "Daily summary",
            "cron_expression": "0 8 * * *",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    ex = client.get(f"/api/sessions/{session['id']}/export", headers=headers).json()[
        "thunder_ai_export"
    ]
    assert ex["schedule"]["cron_expression"] == "0 8 * * *"
    assert ex["schedule"]["prompt"] == "Daily summary"
    assert ex["schedule"]["is_active"] is True


# ── Import tests ────────────────────────────────────────────


def test_import_creates_session(client, token):
    headers = _headers(token)
    payload = {
        "thunder_ai_export": {
            "version": "1.0",
            "exported_at": "2026-09-02T00:00:00Z",
            "session": {
                "title": "Imported Agent",
                "model": "llama3.2:1b",
                "system_prompt": "You are an expert.",
            },
            "schedule": {
                "cron_expression": None,
                "prompt": None,
                "is_active": False,
            },
            "memory": {"enabled": True},
        }
    }
    resp = client.post("/api/sessions/import", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["session_id"] > 0
    assert data["title"] == "Imported Agent"
    assert data["schedule_created"] is False

    # Verify the imported session exists with the config applied
    s = client.get(f"/api/sessions/{data['session_id']}", headers=headers).json()
    assert s["model"] == "llama3.2:1b"
    assert s["system_prompt"] == "You are an expert."


def test_import_creates_schedule(client, token):
    headers = _headers(token)
    payload = {
        "thunder_ai_export": {
            "version": "1.0",
            "exported_at": "2026-09-02T00:00:00Z",
            "session": {"title": "With Schedule", "model": None, "system_prompt": None},
            "schedule": {
                "cron_expression": "30 9 * * 1",
                "prompt": "Monday brief",
                "is_active": True,
            },
            "memory": {"enabled": False},
        }
    }
    resp = client.post("/api/sessions/import", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["schedule_created"] is True

    # Scheduled task should exist for the new session
    tasks = client.get("/api/scheduler", headers=headers).json()
    assert any(
        t["session_id"] == data["session_id"] and t["cron_expression"] == "30 9 * * 1"
        for t in tasks
    )


def test_import_rejects_unsupported_version(client, token):
    payload = {
        "thunder_ai_export": {
            "version": "9.9",
            "exported_at": "2026-09-02T00:00:00Z",
            "session": {"title": "Bad", "model": None, "system_prompt": None},
            "schedule": {"cron_expression": None, "prompt": None, "is_active": False},
            "memory": {"enabled": True},
        }
    }
    resp = client.post("/api/sessions/import", json=payload, headers=_headers(token))
    assert resp.status_code == 400


def test_import_rejects_malformed_payload(client, token):
    resp = client.post(
        "/api/sessions/import", json={"thunder_ai_export": {}}, headers=_headers(token)
    )
    assert resp.status_code == 422


# ── Round-trip ──────────────────────────────────────────────


def test_export_import_round_trip(client, token):
    headers = _headers(token)
    # Create a fully-configured session
    session = _create_session(client, token, title="Round Trip Agent")
    client.patch(
        f"/api/sessions/{session['id']}/model",
        json={"model": "llama3.2:1b"},
        headers=headers,
    )
    client.patch(
        f"/api/sessions/{session['id']}/system-prompt",
        json={"system_prompt": "Round trip prompt."},
        headers=headers,
    )
    client.post(
        "/api/scheduler",
        json={
            "session_id": session["id"],
            "prompt": "Scheduled prompt",
            "cron_expression": "0 6 * * *",
        },
        headers=headers,
    )

    export = client.get(f"/api/sessions/{session['id']}/export", headers=headers).json()

    # Import into the same user
    resp = client.post("/api/sessions/import", json=export, headers=headers)
    assert resp.status_code == 201, resp.text

    imported = resp.json()
    s = client.get(f"/api/sessions/{imported['session_id']}", headers=headers).json()
    assert s["title"] == "Round Trip Agent"
    assert s["model"] == "llama3.2:1b"
    assert s["system_prompt"] == "Round trip prompt."

    tasks = client.get("/api/scheduler", headers=headers).json()
    assert any(
        t["session_id"] == imported["session_id"] and t["cron_expression"] == "0 6 * * *"
        for t in tasks
    )
