"""OAuth sign-in routes: the HTTP surface of the device-code flow.

A stubbed OAuth client stands in for xAI (no network), while the database,
auth, CSRF, owner scoping, polling throttle, and the "nothing secret leaves the
server" contract are exercised for real through the ASGI app.
"""

import base64
import datetime
import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models.models import ProviderOAuthToken, User
from app.routes import oauth as oauth_routes
from app.services.oauth_grok import (
    AccountInfo,
    DeviceCode,
    GrokAuthError,
    PollOutcome,
    TokenSet,
)

from tests.auth_helpers import auth_headers, register_and_login

GROK_URL = "/api/oauth/grok"

STUB_ACCESS = "eyJhbGciOiJFUzI1NiJ9." + base64.urlsafe_b64encode(
    json.dumps({"exp": 4_000_000_000}).encode()
).decode().rstrip("=") + ".sig"
STUB_REFRESH = "rt-route-refresh-secret"
STUB_DEVICE = "dc-route-device-secret"

AUTHORIZED_POLL = PollOutcome(
    "authorized",
    tokens=TokenSet(access_token=STUB_ACCESS, refresh_token=STUB_REFRESH, expires_in=3600),
)


class _StubOAuth:
    """The whole xAI surface, scripted by the test."""

    def __init__(self):
        self.scope_string = "openid grok-cli:access"
        self.device_calls = 0
        self.poll_calls = 0
        self.refresh_calls = 0
        self.revoke_calls = []
        self.next_poll = PollOutcome("pending", interval=5.0)
        self.next_refresh = TokenSet(
            access_token=STUB_ACCESS, refresh_token="rt-route-rotated", expires_in=3600
        )
        self.account = AccountInfo(subject="sub-1", name="Teja", email="teja@example.com")
        self.device_error = None

    async def request_device_code(self):
        self.device_calls += 1
        if self.device_error:
            raise self.device_error
        return DeviceCode(
            device_code=STUB_DEVICE,
            user_code="ABCD-1234",
            verification_uri="https://accounts.x.ai/oauth2/device",
            verification_uri_complete=None,
            expires_in=900,
            interval=5,
        )

    async def poll_once(self, device_code, *, interval=5.0):
        self.poll_calls += 1
        return self.next_poll

    async def refresh(self, refresh_token):
        self.refresh_calls += 1
        return self.next_refresh

    async def fetch_account(self, access_token):
        return self.account

    async def revoke(self, token, *, token_type_hint="refresh_token"):
        self.revoke_calls.append(token)
        return True


@pytest.fixture
def stub_oauth(monkeypatch):
    stub = _StubOAuth()
    monkeypatch.setattr(oauth_routes, "service_for", lambda provider: stub)
    monkeypatch.setattr(settings, "grok_oauth_enabled", True)
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    return stub


def _delete_user(user_id: int) -> None:
    db = SessionLocal()
    try:
        db.query(ProviderOAuthToken).filter(ProviderOAuthToken.user_id == user_id).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture
def signed_in(stub_oauth):
    """A live app + one fresh authenticated user: (client, user_id, headers)."""
    with TestClient(app) as client:
        user_id, token = register_and_login(client)
        try:
            yield client, user_id, auth_headers(token)
        finally:
            _delete_user(user_id)


def _make_poll_due(user_id: int) -> None:
    """Pretend the server's polling interval has elapsed."""
    db = SessionLocal()
    try:
        row = (
            db.query(ProviderOAuthToken)
            .filter(ProviderOAuthToken.user_id == user_id)
            .one()
        )
        row.next_poll_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        db.commit()
    finally:
        db.close()


def _connected(signed_in, stub_oauth):
    """Drive start + the authorized poll so the user holds live credentials."""
    client, user_id, headers = signed_in
    client.post(f"{GROK_URL}/start", headers=headers)
    stub_oauth.next_poll = AUTHORIZED_POLL
    client.get(f"{GROK_URL}/status", headers=headers)
    return client, user_id, headers


def _row(user_id: int) -> ProviderOAuthToken:
    db = SessionLocal()
    try:
        return (
            db.query(ProviderOAuthToken)
            .filter(ProviderOAuthToken.user_id == user_id)
            .one()
        )
    finally:
        db.close()


def _row_count(user_id: int) -> int:
    db = SessionLocal()
    try:
        return (
            db.query(ProviderOAuthToken)
            .filter(ProviderOAuthToken.user_id == user_id)
            .count()
        )
    finally:
        db.close()


# ── gating and validation ───────────────────────────────────


def test_anonymous_caller_is_rejected(stub_oauth):
    with TestClient(app) as client:
        response = client.post(f"{GROK_URL}/start")

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


def test_start_requires_csrf_for_a_browser_origin(signed_in, stub_oauth):
    client, _user_id, headers = signed_in

    response = client.post(
        f"{GROK_URL}/start", headers={**headers, "Origin": "http://evil.example"}
    )

    assert response.status_code == 403


def test_unknown_provider_is_404(signed_in, stub_oauth):
    client, _user_id, headers = signed_in

    response = client.post("/api/oauth/chatgpt/start", headers=headers)

    assert response.status_code == 404
    assert "Unsupported OAuth provider" in response.json()["detail"]


def test_start_is_refused_while_the_feature_is_disabled(signed_in, stub_oauth, monkeypatch):
    client, _user_id, headers = signed_in
    monkeypatch.setattr(settings, "grok_oauth_enabled", False)

    response = client.post(f"{GROK_URL}/start", headers=headers)

    assert response.status_code == 503
    assert "disabled" in response.json()["detail"]


def test_start_reports_an_upstream_failure_as_a_bounded_502(signed_in, stub_oauth):
    client, _user_id, headers = signed_in
    stub_oauth.device_error = GrokAuthError("network_error")

    response = client.post(f"{GROK_URL}/start", headers=headers)

    assert response.status_code == 502
    assert "connection" in response.json()["detail"].lower()


# ── the flow ────────────────────────────────────────────────


def test_start_shows_the_code_and_hides_the_device_secret(signed_in, stub_oauth):
    client, _user_id, headers = signed_in

    response = client.post(f"{GROK_URL}/start", headers=headers)

    body = response.json()
    assert body["user_code"] == "ABCD-1234"
    assert body["verification_uri"].startswith("https://accounts.x.ai/")
    assert body["expires_in"] == 900
    assert body["poll_interval_s"] == 5
    assert body["status"] == "pending"
    assert STUB_DEVICE not in response.text


def test_full_flow_pending_then_authorized(signed_in, stub_oauth):
    client, user_id, headers = signed_in
    client.post(f"{GROK_URL}/start", headers=headers)

    assert client.get(f"{GROK_URL}/status", headers=headers).json()["status"] == "pending"
    assert stub_oauth.poll_calls == 1

    stub_oauth.next_poll = AUTHORIZED_POLL
    _make_poll_due(user_id)
    response = client.get(f"{GROK_URL}/status", headers=headers)

    body = response.json()
    assert body["status"] == "connected"
    assert body["account_email"] == "teja@example.com"
    assert STUB_ACCESS not in response.text
    assert STUB_REFRESH not in response.text


def test_status_throttles_before_the_server_interval_elapses(signed_in, stub_oauth):
    client, _user_id, headers = signed_in
    client.post(f"{GROK_URL}/start", headers=headers)

    assert client.get(f"{GROK_URL}/status", headers=headers).json()["status"] == "pending"
    for _ in range(6):
        client.get(f"{GROK_URL}/status", headers=headers)

    # Seven fast ticks must not become seven calls to xAI.
    assert stub_oauth.poll_calls == 1


def test_status_without_a_started_login_reports_not_signed_in(signed_in, stub_oauth):
    """A user who has never signed in is a normal state, not a 404: a 404 here
    puts a console error on every first visit to the Providers tab."""
    client, _user_id, headers = signed_in

    response = client.get(f"{GROK_URL}/status", headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "none"


def test_an_expired_device_code_is_never_polled(signed_in, stub_oauth):
    client, user_id, headers = signed_in
    client.post(f"{GROK_URL}/start", headers=headers)

    db = SessionLocal()
    try:
        row = db.query(ProviderOAuthToken).filter(
            ProviderOAuthToken.user_id == user_id
        ).one()
        row.device_expires_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    response = client.get(f"{GROK_URL}/status", headers=headers)

    assert response.json()["status"] == "expired"
    assert stub_oauth.poll_calls == 0
    assert "expired" in response.json()["message"].lower()


def test_denied_authorization_ends_the_attempt(signed_in, stub_oauth):
    client, _user_id, headers = signed_in
    client.post(f"{GROK_URL}/start", headers=headers)
    stub_oauth.next_poll = PollOutcome("failed", error=GrokAuthError("access_denied"))

    body = client.get(f"{GROK_URL}/status", headers=headers).json()

    assert body["status"] == "error"
    assert body["last_error"] == "access_denied"
    assert "cancelled" in body["message"].lower()


# ── refresh, logout, account ────────────────────────────────


def test_refresh_rotates_the_stored_pair(signed_in, stub_oauth):
    client, user_id, headers = _connected(signed_in, stub_oauth)

    response = client.post(f"{GROK_URL}/refresh", headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "connected"
    assert response.json()["access_expires_at"]
    assert STUB_ACCESS not in response.text

    from app.services.encryption_service import decrypt_key

    assert decrypt_key(_row(user_id).encrypted_refresh_token) == "rt-route-rotated"


def test_refresh_requires_an_active_sign_in(signed_in, stub_oauth):
    client, _user_id, headers = signed_in

    assert client.post(f"{GROK_URL}/refresh", headers=headers).status_code == 409


def test_logout_revokes_and_clears(signed_in, stub_oauth):
    client, user_id, headers = _connected(signed_in, stub_oauth)

    response = client.delete(f"{GROK_URL}", headers=headers)

    assert response.json() == {
        "provider": "grok",
        "status": "signed_out",
        "revoked": True,
        "message": "Signed out of Grok.",
    }
    # xAI is asked to revoke the *refresh* token: the long-lived credential.
    assert stub_oauth.revoke_calls == [STUB_REFRESH]
    assert _row_count(user_id) == 0


def test_logout_clears_locally_even_when_revocation_fails(signed_in, stub_oauth):
    client, user_id, headers = _connected(signed_in, stub_oauth)

    async def refusing_revoke(token, *, token_type_hint="refresh_token"):
        return False

    stub_oauth.revoke = refusing_revoke

    response = client.delete(f"{GROK_URL}", headers=headers)

    assert response.json()["revoked"] is False
    assert "locally" in response.json()["message"]
    assert _row_count(user_id) == 0


def test_logout_without_a_sign_in_is_idempotent(signed_in, stub_oauth):
    client, _user_id, headers = signed_in

    response = client.delete(f"{GROK_URL}", headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "signed_out"


def test_account_reports_the_entitled_model_catalog(
    signed_in, stub_oauth, monkeypatch
):
    client, _user_id, headers = _connected(signed_in, stub_oauth)

    from app.services.llm_providers.grok_provider import GrokProvider

    monkeypatch.setattr(
        GrokProvider, "fetch_available_chat_models", lambda self: ["grok-4.6", "grok-4"]
    )

    body = client.get(f"{GROK_URL}/account", headers=headers).json()

    assert body["status"] == "connected"
    assert body["account_email"] == "teja@example.com"
    # The route surfaces the catalog the account can list, in provider order.
    assert body["models"] == ["grok-4.6", "grok-4"]
    assert body["entitlement_known"] is True


def test_account_requires_a_sign_in(signed_in, stub_oauth):
    client, _user_id, headers = signed_in

    assert client.get(f"{GROK_URL}/account", headers=headers).status_code == 409


def test_a_second_user_cannot_see_or_reuse_the_firsts_sign_in(
    signed_in, stub_oauth
):
    client, owner_id, owner_headers = _connected(signed_in, stub_oauth)

    second_id, second_token = register_and_login(client)
    second_headers = auth_headers(second_token)
    try:
        other = client.get(f"{GROK_URL}/status", headers=second_headers)
        assert other.status_code == 200
        assert other.json()["status"] == "none"
        assert other.json()["user_code"] == ""

        own = client.post(f"{GROK_URL}/start", headers=second_headers)
        assert own.status_code == 200
        assert _row_count(second_id) == 1
        assert _row_count(owner_id) == 1
    finally:
        _delete_user(second_id)
