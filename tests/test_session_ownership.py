"""
Phase 7C bug-fix — session ownership isolation tests.

Verifies that all authenticated session/message/document routes scope to
the authenticated user and never fall back to user 1 or leak another
user's data.

Uses FastAPI's TestClient in-process (no Docker required) and registers
two real test users via the auth API.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.models.models import User, ResearchSession, Message, Document
from app.services.rate_limiter import get_rate_limiter

from tests.auth_helpers import register_and_login, auth_headers


@pytest.fixture(autouse=True)
def _cleanup_users():
    """Remove test users and their data before/after each test."""
    # Clear in-process rate-limiter state left by earlier test files so
    # register/login here is never throttled by their activity.
    get_rate_limiter().reset()
    db = SessionLocal()
    try:
        _delete_test_users(db)
        yield
        _delete_test_users(db)
    finally:
        db.close()


def _delete_test_users(db):
    for username in ("owner_a", "owner_b"):
        user = db.query(User).filter(User.username == username).first()
        if not user:
            continue
        session_ids = [
            s.id for s in db.query(ResearchSession).filter(ResearchSession.user_id == user.id).all()
        ]
        if session_ids:
            db.query(Message).filter(Message.session_id.in_(session_ids)).delete(
                synchronize_session=False
            )
            db.query(Document).filter(Document.session_id.in_(session_ids)).delete(
                synchronize_session=False
            )
            db.query(ResearchSession).filter(ResearchSession.id.in_(session_ids)).delete(
                synchronize_session=False
            )
        db.delete(user)
        db.commit()


@pytest.fixture
def two_users():
    """Register two independent users; returns (client, user_a, user_b)."""
    with TestClient(app) as client:
        _, token_a = register_and_login(client, username="owner_a")
        _, token_b = register_and_login(client, username="owner_b")
        headers_a = auth_headers(token_a)
        headers_b = auth_headers(token_b)

        # User A creates a session
        resp = client.post(
            "/api/sessions",
            json={"title": "A's private session"},
            headers=headers_a,
        )
        assert resp.status_code == 201
        session_a = resp.json()

        yield client, headers_a, headers_b, session_a


# ── Session ownership ──────────────────────────────────────


def test_user_b_cannot_list_user_a_sessions(two_users):
    """User B's session list must not include User A's sessions."""
    client, headers_a, headers_b, session_a = two_users
    listed = client.get("/api/sessions", headers=headers_b).json()
    ids = [s["id"] for s in listed]
    assert session_a["id"] not in ids
    assert len(listed) == 0  # B has no sessions


def test_user_b_cannot_open_user_a_session(two_users):
    """User B cannot read User A's session (404)."""
    client, headers_a, headers_b, session_a = two_users
    resp = client.get(f"/api/sessions/{session_a['id']}", headers=headers_b)
    assert resp.status_code == 404


def test_user_b_cannot_rename_user_a_session(two_users):
    """User B cannot rename User A's session (404)."""
    client, headers_a, headers_b, session_a = two_users
    resp = client.patch(
        f"/api/sessions/{session_a['id']}",
        json={"title": "Hijacked"},
        headers=headers_b,
    )
    assert resp.status_code == 404


def test_user_b_cannot_delete_user_a_session(two_users):
    """User B cannot delete User A's session (404)."""
    client, headers_a, headers_b, session_a = two_users
    resp = client.delete(f"/api/sessions/{session_a['id']}", headers=headers_b)
    assert resp.status_code == 404


def test_user_b_cannot_set_user_a_session_model(two_users):
    """User B cannot change the model on User A's session (404)."""
    client, headers_a, headers_b, session_a = two_users
    resp = client.patch(
        f"/api/sessions/{session_a['id']}/model",
        json={"model": "llama3.2:3b"},
        headers=headers_b,
    )
    assert resp.status_code == 404


def test_user_b_cannot_read_user_a_system_prompt(two_users):
    """User B cannot read User A's system prompt (404)."""
    client, headers_a, headers_b, session_a = two_users
    resp = client.get(f"/api/sessions/{session_a['id']}/system-prompt", headers=headers_b)
    assert resp.status_code == 404


def test_session_list_and_detail_use_same_owner(two_users):
    """User A sees their own session in both list and detail."""
    client, headers_a, headers_b, session_a = two_users
    listed = client.get("/api/sessions", headers=headers_a).json()
    ids = [s["id"] for s in listed]
    assert session_a["id"] in ids
    detail = client.get(f"/api/sessions/{session_a['id']}", headers=headers_a)
    assert detail.status_code == 200


def test_missing_session_returns_404_for_owner(two_users):
    """A genuinely missing session returns 404 even for the owner."""
    client, headers_a, headers_b, session_a = two_users
    resp = client.get("/api/sessions/99999", headers=headers_a)
    assert resp.status_code == 404


# ── Message ownership ──────────────────────────────────────


def test_user_b_cannot_list_user_a_messages(two_users):
    """User B cannot list messages in User A's session (404)."""
    client, headers_a, headers_b, session_a = two_users
    resp = client.get(f"/api/sessions/{session_a['id']}/messages", headers=headers_b)
    assert resp.status_code == 404


def test_user_b_cannot_send_message_to_user_a_session(two_users):
    """User B cannot send a message into User A's session (404)."""
    client, headers_a, headers_b, session_a = two_users
    resp = client.post(
        f"/api/sessions/{session_a['id']}/messages",
        json={"message": "intrusion"},
        headers=headers_b,
    )
    assert resp.status_code == 404


def test_user_b_cannot_stream_into_user_a_session(two_users):
    """User B cannot stream a message into User A's session (404)."""
    client, headers_a, headers_b, session_a = two_users
    resp = client.post(
        f"/api/sessions/{session_a['id']}/messages/stream",
        json={"message": "intrusion"},
        headers=headers_b,
    )
    assert resp.status_code == 404


# ── Document ownership ─────────────────────────────────────


def test_user_b_cannot_list_user_a_documents(two_users):
    """User B cannot list documents in User A's session (404)."""
    client, headers_a, headers_b, session_a = two_users
    resp = client.get(f"/api/sessions/{session_a['id']}/documents", headers=headers_b)
    assert resp.status_code == 404


def test_user_b_cannot_upload_to_user_a_session(two_users):
    """User B cannot upload a document to User A's session (404)."""
    client, headers_a, headers_b, session_a = two_users
    resp = client.post(
        f"/api/sessions/{session_a['id']}/documents",
        files={"file": ("x.txt", b"hello", "text/plain")},
        headers=headers_b,
    )
    assert resp.status_code == 404
