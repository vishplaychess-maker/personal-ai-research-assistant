"""
F6 Shareable Agent Card — share endpoint tests.

Covers the authenticated create endpoint (POST /api/sessions/{id}/share) and
the public read endpoint (GET /api/share/agents/{share_id}):

  * create returns share_id + share_url (absolute, points at frontend origin)
  * create requires ownership (404 for another user's session)
  * create requires auth (401) — the public read must NOT
  * public read is unauthenticated and returns the snapshot fields
  * public read increments the view counter on each fetch
  * system_prompt / preview_message / has_schedule reflect the session snapshot
  * unknown share_id -> 404
  * sharing the same session twice yields distinct share records/URLs

Uses FastAPI TestClient (in-process) + LocalProvider. No live Ollama.
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


def _create_session(client, token, title="Shareable Card Session"):
    resp = client.post("/api/sessions", json={"title": title}, headers=_headers(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _send_user_message(client, token, session_id, text):
    """Post a message to the session so it has a preview_message."""
    resp = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"message": text},
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text


# ── Create (authenticated) ─────────────────────────────────


def test_create_share_returns_url(client, token):
    session = _create_session(client, token)
    resp = client.post(f"/api/sessions/{session['id']}/share", headers=_headers(token))
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["share_id"]
    assert len(data["share_id"]) == 8
    assert data["share_url"] == f"http://localhost:5173/share/agents/{data['share_id']}"


def test_create_share_requires_auth(client):
    # Create a session while authed, then try to share it WITHOUT a token.
    _, token_owner = register_and_login(client, username="authlessSharer")
    session = _create_session(client, token_owner)
    resp = client.post(f"/api/sessions/{session['id']}/share")
    assert resp.status_code == 401


def test_create_share_404_for_other_users_session(client):
    # User A creates + shares a session
    _, token_a = register_and_login(client, username="ownerA")
    session = _create_session(client, token_a)
    client.post(f"/api/sessions/{session['id']}/share", headers=_headers(token_a))

    # User B cannot share A's session
    _, token_b = register_and_login(client, username="ownerB")
    resp = client.post(f"/api/sessions/{session['id']}/share", headers=_headers(token_b))
    assert resp.status_code == 404


def test_share_snapshots_are_distinct_per_call(client, token):
    session = _create_session(client, token)
    headers = _headers(token)
    r1 = client.post(f"/api/sessions/{session['id']}/share", headers=headers).json()
    r2 = client.post(f"/api/sessions/{session['id']}/share", headers=headers).json()
    assert r1["share_id"] != r2["share_id"]
    assert r1["share_url"] != r2["share_url"]


# ── Public read (unauthenticated) ───────────────────────────


def _publish(client, token, session_id):
    resp = client.post(f"/api/sessions/{session_id}/share", headers=_headers(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_public_read_is_unauthenticated(client, token):
    session = _create_session(client, token, title="Public Agent")
    # Set a prompt + add a preview message so the snapshot is non-trivial
    client.patch(
        f"/api/sessions/{session['id']}/system-prompt",
        json={"system_prompt": "You are a friendly research bot."},
        headers=_headers(token),
    )
    _send_user_message(client, token, session["id"], "What causes the northern lights?")
    link = _publish(client, token, session["id"])

    # No Authorization header at all
    resp = client.get(f"/api/share/agents/{link['share_id']}")
    assert resp.status_code == 200, resp.text
    card = resp.json()
    assert card["title"] == "Public Agent"
    assert card["system_prompt"] == "You are a friendly research bot."
    assert card["preview_message"] == "What causes the northern lights?"
    # Public payload must not leak internal ids
    assert "session_id" not in card
    assert "user_id" not in card


def test_public_read_increments_view_count(client, token):
    session = _create_session(client, token)
    link = _publish(client, token, session["id"])

    c1 = client.get(f"/api/share/agents/{link['share_id']}").json()
    c2 = client.get(f"/api/share/agents/{link['share_id']}").json()
    c3 = client.get(f"/api/share/agents/{link['share_id']}").json()
    assert c1["views"] == 1
    assert c2["views"] == 2
    assert c3["views"] == 3


def test_public_read_has_schedule_flag(client, token):
    headers = _headers(token)
    session = _create_session(client, token, title="Scheduled Agent")
    client.post(
        "/api/scheduler",
        json={
            "session_id": session["id"],
            "prompt": "Daily brief",
            "cron_expression": "0 9 * * *",
        },
        headers=headers,
    )
    link = _publish(client, token, session["id"])
    card = client.get(f"/api/share/agents/{link['share_id']}").json()
    assert card["has_schedule"] is True


def test_public_read_defaults_with_no_schedule_or_prompt(client, token):
    session = _create_session(client, token, title="Bare Agent")
    link = _publish(client, token, session["id"])
    card = client.get(f"/api/share/agents/{link['share_id']}").json()
    assert card["has_schedule"] is False
    assert card["model"] is None
    assert card["system_prompt"] is None
    assert card["preview_message"] is None
    assert card["tool_count"] == 0


def test_public_read_404_unknown(client, token):
    _create_session(client, token)
    resp = client.get("/api/share/agents/zzzzzzzz")
    assert resp.status_code == 404
