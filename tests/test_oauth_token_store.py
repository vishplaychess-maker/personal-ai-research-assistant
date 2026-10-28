"""Encrypted OAuth token storage: at-rest encryption, rotation, single flight.

These run against the real database (the repo's convention for anything that
persists) with a stubbed OAuth client, so the HTTP shape is covered elsewhere
and what is tested here is the credential lifecycle: what is written, what is
readable, what survives a restart, and what happens when refresh fails.
"""

import asyncio
import base64
import datetime
import json
import logging
import threading
import uuid

import pytest
from cryptography.fernet import Fernet

from app.database import SessionLocal
from app.models.models import ProviderOAuthToken, User
from app.services.oauth_grok import DeviceCode, GrokAuthError, TokenSet
from app.services.oauth_token_store import (
    GROK,
    STATUS_CONNECTED,
    STATUS_ERROR,
    STATUS_PENDING,
    OAuthTokenStore,
    TokenRefreshError,
)

def _jwt(exp_offset_seconds: float) -> str:
    payload = {"exp": _epoch() + exp_offset_seconds}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.sig"


def _epoch() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


#: Fixed once at import so a token string can be compared across calls.
FRESH_ACCESS = _jwt(7200)


class _FakeOAuth:
    """Stand-in for GrokOAuthService: records refreshes, controls expiry."""

    def __init__(self, *, tokens=None, error=None, exhausted=True):
        self.tokens = tokens
        self.error = error
        self.exhausted = exhausted
        self.refresh_calls = []

    def access_token_is_exhausted(self, access_token, expires_at, *, skew_s=300):
        if callable(self.exhausted):
            return bool(self.exhausted(access_token))
        return self.exhausted

    async def refresh(self, refresh_token):
        self.refresh_calls.append(refresh_token)
        if self.error is not None:
            raise self.error
        return self.tokens


@pytest.fixture
def encryption_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))


@pytest.fixture
def signed_in_user(encryption_key):
    """A real user row plus a connected Grok credential row, cleaned up."""
    db = SessionLocal()
    name = f"grokoauth_{uuid.uuid4().hex[:10]}"
    user = User(username=name, email=f"{name}@test.com")
    db.add(user)
    db.commit()
    db.refresh(user)
    user_id = user.id
    db.close()
    try:
        yield user_id
    finally:
        db = SessionLocal()
        db.query(ProviderOAuthToken).filter(
            ProviderOAuthToken.user_id == user_id
        ).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.commit()
        db.close()


def _store() -> OAuthTokenStore:
    return OAuthTokenStore(session_factory=SessionLocal)


def _device(**overrides) -> DeviceCode:
    kwargs = dict(
        device_code="dc-PLAINTEXT-device-code",
        user_code="ABCD-1234",
        verification_uri="https://accounts.x.ai/oauth2/device",
        verification_uri_complete=None,
        expires_in=900,
        interval=5,
    )
    kwargs.update(overrides)
    return DeviceCode(**kwargs)


def _token_set(access="at-PLAINTEXT-access", refresh="rt-PLAINTEXT-refresh", expires_in=3600):
    return TokenSet(access_token=access, refresh_token=refresh, expires_in=expires_in)


# ── pending login ───────────────────────────────────────────


def test_pending_login_encrypts_the_device_code(signed_in_user, encryption_key):
    store = _store()
    db = SessionLocal()
    try:
        row = store.save_pending_login(db, signed_in_user, _device(), scopes="openid")
        raw = db.query(ProviderOAuthToken).filter(
            ProviderOAuthToken.id == row.id
        ).first()
        assert "dc-PLAINTEXT-device-code" not in (raw.encrypted_device_code or "")
        assert raw.encrypted_device_code.startswith("gAAAAA")
        assert row.status == STATUS_PENDING
        assert row.user_code == "ABCD-1234"
        assert row.verification_uri.endswith("/oauth2/device")
    finally:
        db.close()


def test_a_new_device_code_replaces_the_previous_attempt(signed_in_user, encryption_key):
    store = _store()
    db = SessionLocal()
    try:
        store.save_pending_login(db, signed_in_user, _device(user_code="AAAA-1111"))
        store.save_pending_login(db, signed_in_user, _device(user_code="BBBB-2222"))

        rows = (
            db.query(ProviderOAuthToken)
            .filter(ProviderOAuthToken.user_id == signed_in_user)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].user_code == "BBBB-2222"
        assert rows[0].status == STATUS_PENDING
    finally:
        db.close()


def test_verification_uri_complete_preferred_for_the_shown_link(signed_in_user, encryption_key):
    store = _store()
    db = SessionLocal()
    try:
        row = store.save_pending_login(
            db,
            signed_in_user,
            _device(verification_uri_complete="https://accounts.x.ai/oauth2/device?user_code=ABCD-1234"),
        )
        assert "user_code=ABCD-1234" in row.verification_uri
    finally:
        db.close()


def test_server_interval_is_clamped_to_a_sane_range(signed_in_user, encryption_key):
    store = _store()
    db = SessionLocal()
    try:
        row = store.save_pending_login(db, signed_in_user, _device(interval=10_000))
        assert row.poll_interval_s == 60
    finally:
        db.close()


# ── connected credentials ───────────────────────────────────


def test_tokens_are_encrypted_at_rest_and_never_projected(signed_in_user, encryption_key):
    store = _store()
    db = SessionLocal()
    try:
        store.save_tokens(
            db,
            signed_in_user,
            _token_set(),
            account_name="Teja",
            account_email="teja@example.com",
            scopes="openid grok-cli:access",
        )
        raw = (
            db.query(ProviderOAuthToken)
            .filter(ProviderOAuthToken.user_id == signed_in_user)
            .one()
        )
        assert "at-PLAINTEXT-access" not in raw.encrypted_access_token
        assert "rt-PLAINTEXT-refresh" not in raw.encrypted_refresh_token

        account = store.account(db, signed_in_user, GROK)
        public = account.as_public_dict()
        blob = json.dumps(public)
        for secret in ("at-PLAINTEXT-access", "rt-PLAINTEXT-refresh", "gAAAAA"):
            assert secret not in blob
        assert account.status == STATUS_CONNECTED
        assert account.account_email == "teja@example.com"
    finally:
        db.close()


def test_get_valid_token_returns_the_stored_access_token(signed_in_user, encryption_key):
    store = _store()
    db = SessionLocal()
    store.save_pending_login(db, signed_in_user, _device())
    store.save_tokens(
        db, signed_in_user, _token_set(access=FRESH_ACCESS, refresh="rt-original")
    )
    db.close()

    oauth = _FakeOAuth(exhausted=False)
    assert store.get_valid_token(signed_in_user, GROK, service=oauth) == FRESH_ACCESS
    assert oauth.refresh_calls == []


def test_get_valid_token_refreshes_and_persists_the_rotated_pair(signed_in_user, encryption_key):
    """xAI consumes the old refresh token, so the rotated one must be stored."""
    store = _store()
    db = SessionLocal()
    store.save_pending_login(db, signed_in_user, _device())
    store.save_tokens(db, signed_in_user, _token_set(refresh="rt-original"))
    db.close()

    rotated = _token_set(access=FRESH_ACCESS, refresh="rt-rotated")
    oauth = _FakeOAuth(tokens=rotated, exhausted=True)

    assert store.get_valid_token(signed_in_user, GROK, service=oauth) == FRESH_ACCESS
    assert oauth.refresh_calls == ["rt-original"]

    db = SessionLocal()
    try:
        from app.services.encryption_service import decrypt_key

        raw = (
            db.query(ProviderOAuthToken)
            .filter(ProviderOAuthToken.user_id == signed_in_user)
            .one()
        )
        assert decrypt_key(raw.encrypted_refresh_token) == "rt-rotated"
        assert raw.status == STATUS_CONNECTED
    finally:
        db.close()


def test_dead_refresh_marks_the_row_and_clears_credentials(signed_in_user, encryption_key):
    store = _store()
    db = SessionLocal()
    store.save_pending_login(db, signed_in_user, _device())
    store.save_tokens(db, signed_in_user, _token_set(refresh="rt-consumed"))
    db.close()

    oauth = _FakeOAuth(error=GrokAuthError("invalid_grant"), exhausted=True)

    with pytest.raises(TokenRefreshError) as caught:
        store.get_valid_token(signed_in_user, GROK, service=oauth)

    assert caught.value.code == "invalid_grant"
    db = SessionLocal()
    try:
        raw = (
            db.query(ProviderOAuthToken)
            .filter(ProviderOAuthToken.user_id == signed_in_user)
            .one()
        )
        assert raw.status == STATUS_ERROR
        assert raw.last_error == "invalid_grant"
        assert (raw.encrypted_access_token or "") == ""
        assert (raw.encrypted_refresh_token or "") == ""
    finally:
        db.close()


def test_missing_refresh_token_fails_closed_without_network(signed_in_user, encryption_key):
    store = _store()
    db = SessionLocal()
    store.save_pending_login(db, signed_in_user, _device())
    store.save_tokens(db, signed_in_user, _token_set())
    db.query(ProviderOAuthToken).filter(
        ProviderOAuthToken.user_id == signed_in_user
    ).update({"encrypted_refresh_token": ""})
    db.commit()
    db.close()

    oauth = _FakeOAuth(tokens=_token_set(), exhausted=True)

    with pytest.raises(TokenRefreshError) as caught:
        store.get_valid_token(signed_in_user, GROK, service=oauth)

    assert caught.value.code == "refresh_unavailable"
    assert oauth.refresh_calls == []


def test_no_sign_in_row_cannot_produce_a_token(signed_in_user, encryption_key):
    store = _store()

    with pytest.raises(TokenRefreshError) as caught:
        store.get_valid_token(signed_in_user, GROK, service=_FakeOAuth())

    assert caught.value.code == "not_signed_in"


def test_a_pending_login_is_not_a_usable_credential(signed_in_user, encryption_key):
    store = _store()
    db = SessionLocal()
    store.save_pending_login(db, signed_in_user, _device())
    db.close()

    with pytest.raises(TokenRefreshError):
        store.get_valid_token(signed_in_user, GROK, service=_FakeOAuth())


# ── concurrency ─────────────────────────────────────────────


def test_two_threads_refresh_only_once(signed_in_user, encryption_key):
    """Single flight: a racing second refresh would consume the rotated token.

    The stub calls the pre-rotation token exhausted and the rotated one fresh,
    so the second thread must find the work already done rather than refresh
    against a token xAI has already burned.
    """
    store = _store()
    db = SessionLocal()
    store.save_pending_login(db, signed_in_user, _device())
    store.save_tokens(
        db, signed_in_user, _token_set(access="at-stale", refresh="rt-original")
    )
    db.close()

    slow = _FakeOAuth(
        tokens=_token_set(access=FRESH_ACCESS, refresh="rt-rotated"),
        exhausted=lambda token: token != FRESH_ACCESS,
    )

    original_refresh = slow.refresh

    async def slow_refresh(token):
        await asyncio.sleep(0.05)
        return await original_refresh(token)

    slow.refresh = slow_refresh

    results, errors = [], []

    def worker():
        try:
            results.append(store.get_valid_token(signed_in_user, GROK, service=slow))
        except TokenRefreshError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert results == [FRESH_ACCESS, FRESH_ACCESS]
    assert len(slow.refresh_calls) == 1


# ── logout ──────────────────────────────────────────────────


def test_delete_tokens_removes_the_row(signed_in_user, encryption_key):
    store = _store()
    db = SessionLocal()
    try:
        store.save_pending_login(db, signed_in_user, _device())
        store.save_tokens(db, signed_in_user, _token_set())
    finally:
        db.close()

    db = SessionLocal()
    try:
        assert store.delete_tokens(db, signed_in_user, GROK) is True
        assert store.account(db, signed_in_user, GROK) is None
    finally:
        db.close()


def test_delete_tokens_reports_nothing_to_remove(signed_in_user, encryption_key):
    store = _store()
    db = SessionLocal()
    try:
        assert store.delete_tokens(db, signed_in_user, GROK) is False
    finally:
        db.close()


# ── fail-closed encryption ──────────────────────────────────


def test_credentials_are_refused_without_an_encryption_key(
    signed_in_user, encryption_key, monkeypatch
):
    """No ENCRYPTION_KEY must mean no stored token, not a plaintext one."""
    store = _store()
    db = SessionLocal()
    try:
        store.save_pending_login(db, signed_in_user, _device())
    finally:
        db.close()

    def explode(_plaintext):
        raise RuntimeError("ENCRYPTION_KEY is not set")

    monkeypatch.setattr("app.services.oauth_token_store.encrypt_key", explode)

    db = SessionLocal()
    try:
        with pytest.raises(RuntimeError):
            store.save_tokens(db, signed_in_user, _token_set())
    finally:
        db.close()


def test_store_operations_log_no_token_material(signed_in_user, encryption_key, caplog):
    store = _store()
    db = SessionLocal()
    with caplog.at_level(logging.DEBUG):
        store.save_pending_login(db, signed_in_user, _device())
        store.save_tokens(db, signed_in_user, _token_set())
        store.account(db, signed_in_user, GROK)
        store.delete_tokens(db, signed_in_user, GROK)
    db.close()

    blob = "\n".join(record.getMessage() for record in caplog.records)
    for secret in ("at-PLAINTEXT-access", "rt-PLAINTEXT-refresh", "dc-PLAINTEXT-device-code"):
        assert secret not in blob
