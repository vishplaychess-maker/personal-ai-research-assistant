"""Grok (xAI) OAuth device-code service.

Every test drives the service through ``httpx.MockTransport``: no Telegram-style
network, no real xAI call, no browser. The point is the protocol contract —
form-encoded requests, the RFC 8628 polling states, refresh-token rotation, and
the invariant that token material never reaches a log line or an exception.
"""

import asyncio
import base64
import json
import logging
import time

import httpx
import pytest

from app.services import oauth_grok
from app.services.oauth_grok import (
    AUTHORIZED,
    DEFAULT_CLIENT_ID,
    FAILED,
    GrokAuthError,
    GrokOAuthService,
    PENDING,
    SLOW_DOWN,
)

DEVICE_URL = "https://auth.x.ai/oauth2/device/code"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
USERINFO_URL = "https://auth.x.ai/oauth2/userinfo"
REVOKE_URL = "https://auth.x.ai/oauth2/revoke"

ACCESS = "eyJhbGciOiJFUzI1NiJ9." + base64.urlsafe_b64encode(
    json.dumps({"exp": int(time.time()) + 3600, "sub": "user-1"}).encode()
).decode().rstrip("=") + ".signature-material"
REFRESH = "rt-TOPSECRET-refresh-token"
DEVICE = "dc-TOPSECRET-device-code"

SECRET_MARKERS = (ACCESS, REFRESH, DEVICE)


def _service(handler, **kwargs) -> GrokOAuthService:
    return GrokOAuthService(
        device_auth_url=kwargs.get("device_auth_url", DEVICE_URL),
        token_url=kwargs.get("token_url", TOKEN_URL),
        userinfo_url=kwargs.get("userinfo_url", USERINFO_URL),
        revocation_url=kwargs.get("revocation_url", REVOKE_URL),
        client_id=kwargs.get("client_id"),
        scopes=kwargs.get("scopes"),
        transport=httpx.MockTransport(handler),
    )


def _json(payload, status=200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _device_body(**overrides):
    body = {
        "device_code": DEVICE,
        "user_code": "ZBS2-K2G2",
        "verification_uri": "https://accounts.x.ai/oauth2/device",
        "verification_uri_complete": "https://accounts.x.ai/oauth2/device?user_code=ZBS2-K2G2",
        "expires_in": 1800,
        "interval": 5,
    }
    body.update(overrides)
    return body


def _run(coro):
    return asyncio.run(coro)


# ── device authorization request ────────────────────────────


def test_device_request_is_form_encoded_with_client_id_and_scopes():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers.get("content-type")
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        return _json(_device_body())

    service = _service(handler, scopes=["openid", "grok-cli:access"])

    device = _run(service.request_device_code())

    assert seen["url"] == DEVICE_URL
    assert seen["content_type"] == "application/x-www-form-urlencoded"
    assert seen["form"] == {"client_id": DEFAULT_CLIENT_ID, "scope": "openid grok-cli:access"}
    assert device.user_code == "ZBS2-K2G2"
    assert device.device_code == DEVICE
    assert device.interval == 5
    assert device.expires_in == 1800


def test_verification_uri_complete_is_surfaced_for_qr_free_mobile_signin():
    service = _service(lambda request: _json(_device_body()))

    device = _run(service.request_device_code())

    assert device.verification_uri_complete.endswith("user_code=ZBS2-K2G2")


def test_configured_client_id_overrides_the_baked_in_public_one():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        return _json(_device_body())

    service = _service(handler, client_id="rotated-client-id")

    _run(service.request_device_code())

    assert seen["form"]["client_id"] == "rotated-client-id"


def test_missing_client_id_is_reported_as_an_oauth_code():
    service = _service(
        lambda request: _json({"error": "invalid_request", "error_description": "client_id is required"}, 400)
    )

    with pytest.raises(GrokAuthError) as caught:
        _run(service.request_device_code())

    assert caught.value.code == "invalid_request"


def test_device_response_missing_fields_fails_closed():
    service = _service(lambda request: _json({"user_code": "ABCD-1234"}))

    with pytest.raises(GrokAuthError) as caught:
        _run(service.request_device_code())

    assert caught.value.code == "invalid_response"


# ── polling states ──────────────────────────────────────────


def test_poll_reports_pending_without_sleeping():
    service = _service(lambda request: _json({"error": "authorization_pending"}, 400))

    outcome = _run(service.poll_once(DEVICE, interval=5.0))

    assert outcome.state == PENDING
    assert outcome.interval == 5.0


def test_poll_body_carries_the_device_grant_type():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        return _json({"error": "authorization_pending"}, 400)

    _run(_service(handler).poll_once(DEVICE))

    assert seen["form"] == {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": DEFAULT_CLIENT_ID,
        "device_code": DEVICE,
    }


def test_slow_down_raises_the_interval_by_five_seconds():
    service = _service(lambda request: _json({"error": "slow_down"}, 400))

    outcome = _run(service.poll_once(DEVICE, interval=5.0))

    assert outcome.state == SLOW_DOWN
    assert outcome.interval == 10.0


def test_slow_down_honours_a_server_interval_and_caps_it():
    service = _service(lambda request: _json({"error": "slow_down", "interval": 120}, 400))

    outcome = _run(service.poll_once(DEVICE, interval=5.0))

    assert outcome.interval == oauth_grok.MAX_POLL_INTERVAL_S


def test_expired_device_code_fails_with_user_copy():
    service = _service(lambda request: _json({"error": "expired_token"}, 400))

    outcome = _run(service.poll_once(DEVICE))

    assert outcome.state == FAILED
    assert outcome.error.code == "expired_token"
    assert "expired" in str(outcome.error).lower()


def test_denied_authorization_fails_with_its_own_message():
    service = _service(lambda request: _json({"error": "access_denied"}, 400))

    outcome = _run(service.poll_once(DEVICE))

    assert outcome.error.code == "access_denied"
    assert "cancelled" in str(outcome.error).lower()


def test_unknown_device_code_is_an_invalid_grant():
    service = _service(
        lambda request: _json(
            {"error": "invalid_grant", "error_description": "Unknown device code"}, 400
        )
    )

    outcome = _run(service.poll_once(DEVICE))

    assert outcome.error.code == "invalid_grant"


def test_successful_poll_returns_both_tokens():
    service = _service(
        lambda request: _json(
            {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600,
             "scope": "openid grok-cli:access"}
        )
    )

    outcome = _run(service.poll_once(DEVICE))

    assert outcome.state == AUTHORIZED
    assert outcome.tokens.refresh_token == REFRESH
    assert outcome.tokens.scope == "openid grok-cli:access"
    assert outcome.tokens.expires_at > time.time()


def test_token_response_without_a_refresh_token_is_rejected():
    """A grant that cannot be renewed would silently strand the user in an hour."""
    service = _service(lambda request: _json({"access_token": ACCESS, "expires_in": 3600}))

    outcome = _run(service.poll_once(DEVICE))

    assert outcome.state == FAILED
    assert outcome.error.code == "invalid_response"


def test_html_error_page_does_not_leak_into_the_error():
    """A Cloudflare-style block page is not JSON; the body must not be echoed."""
    service = _service(
        lambda request: httpx.Response(403, text="<html>Attention Required! " + REFRESH + "</html>")
    )

    outcome = _run(service.poll_once(DEVICE))

    assert outcome.state == FAILED
    for marker in SECRET_MARKERS:
        assert marker not in str(outcome.error)


def test_network_and_timeout_failures_stay_pollable():
    """A transient blip must not end a sign-in the user is still working on."""

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route to host")

    outcome = _run(_service(timeout_handler).poll_once(DEVICE))
    assert outcome.state == PENDING

    def error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    outcome = _run(_service(error_handler).poll_once(DEVICE))
    assert outcome.state == PENDING


# ── the headless loop ───────────────────────────────────────


def test_poll_until_authorized_loops_through_pending():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return _json({"error": "authorization_pending"}, 400)
        return _json({"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600})

    service = _service(handler)

    tokens = _run(service.poll_until_authorized(DEVICE, interval=1, timeout=30))

    assert len(calls) == 3
    assert tokens.access_token == ACCESS


def test_poll_until_authorized_stops_at_the_deadline():
    service = _service(lambda request: _json({"error": "authorization_pending"}, 400))

    with pytest.raises(GrokAuthError) as caught:
        _run(service.poll_until_authorized(DEVICE, interval=1, timeout=0))

    assert caught.value.code == "expired_token"


def test_poll_until_authorized_raises_on_denial():
    service = _service(lambda request: _json({"error": "access_denied"}, 400))

    with pytest.raises(GrokAuthError) as caught:
        _run(service.poll_until_authorized(DEVICE, interval=1, timeout=30))

    assert caught.value.code == "access_denied"


# ── refresh ─────────────────────────────────────────────────


def test_refresh_sends_the_refresh_grant_and_returns_the_rotated_pair():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        return _json({"access_token": ACCESS, "refresh_token": "rt-ROTATED", "expires_in": 1800})

    service = _service(handler)

    tokens = _run(service.refresh(REFRESH))

    assert seen["form"]["grant_type"] == "refresh_token"
    assert seen["form"]["refresh_token"] == REFRESH
    assert tokens.refresh_token == "rt-ROTATED"


def test_refresh_403_means_the_account_is_not_entitled_not_the_token():
    service = _service(lambda request: _json({"error": "insufficient_scope"}, 403))

    with pytest.raises(GrokAuthError) as caught:
        _run(service.refresh(REFRESH))

    # Any 402/403 is an entitlement gate: re-signing-in cannot fix it, so the
    # code the UI sees is subscription_required rather than the raw scope error.
    assert caught.value.status == 403
    assert caught.value.code == "subscription_required"


def test_refresh_invalid_grant_asks_for_a_new_sign_in():
    service = _service(lambda request: _json({"error": "invalid_grant"}, 400))

    with pytest.raises(GrokAuthError) as caught:
        _run(service.refresh(REFRESH))

    assert caught.value.code == "invalid_grant"
    assert "sign-in again" in str(caught.value).lower()


# ── userinfo and revocation ─────────────────────────────────


def test_userinfo_is_a_bearer_get_and_returns_the_claims():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["method"] = request.method
        return _json({"sub": "user-1", "name": "Teja", "email": "teja@example.com"})

    account = _run(_service(handler).fetch_account(ACCESS))

    assert seen["auth"] == f"Bearer {ACCESS}"
    assert seen["method"] == "GET"
    assert account.email == "teja@example.com"
    assert account.display == "teja@example.com"


def test_userinfo_reveals_no_subscription_tier_claim():
    """Documented reality: userinfo has no plan field, so status is model-based."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _json({"sub": "user-1", "name": "Teja"})

    account = _run(_service(handler).fetch_account(ACCESS))

    assert account.email == ""
    assert account.display == "Teja"


def test_userinfo_rejection_is_bounded():
    service = _service(lambda request: _json({"error": "invalid_token"}, 401))

    with pytest.raises(GrokAuthError) as caught:
        _run(service.fetch_account(ACCESS))

    assert caught.value.status == 401


def test_revoke_posts_the_refresh_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(200)

    assert _run(_service(handler).revoke(REFRESH)) is True
    assert seen["form"]["token"] == REFRESH
    assert seen["form"]["token_type_hint"] == "refresh_token"


def test_revoke_unreachable_is_reported_not_raised():
    """Logout must still clear local credentials when xAI cannot be reached."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    assert _run(_service(handler).revoke(REFRESH)) is False


def test_revoke_without_a_token_is_a_no_op():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not call the network")

    assert _run(_service(handler).revoke("")) is True


# ── expiry maths ────────────────────────────────────────────


def _jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def test_a_token_inside_the_skew_window_counts_as_exhausted():
    service = _service(lambda request: httpx.Response(200, json={}))
    about_to_die = _jwt(time.time() + 120)

    assert service.access_token_is_exhausted(about_to_die, None, skew_s=300) is True
    assert service.access_token_is_exhausted(about_to_die, None, skew_s=60) is False


def test_the_jwt_claim_wins_over_a_stale_stored_expiry():
    service = _service(lambda request: httpx.Response(200, json={}))
    fresh_claim = _jwt(time.time() + 7200)

    assert service.access_token_is_exhausted(fresh_claim, time.time() + 1, skew_s=300) is False


def test_opaque_tokens_fall_back_to_the_stored_expiry():
    service = _service(lambda request: httpx.Response(200, json={}))

    assert service.access_token_is_exhausted("not-a-jwt", time.time() + 7200, skew_s=300) is False
    assert service.access_token_is_exhausted("not-a-jwt", time.time() + 5, skew_s=300) is True
    assert service.access_token_is_exhausted("not-a-jwt", None) is True


# ── secrets never appear in logs or messages ────────────────


def test_no_flow_step_logs_or_raises_token_material(caplog):
    steps = []

    def handler(request: httpx.Request) -> httpx.Response:
        steps.append(str(request.url))
        if "device/code" in str(request.url):
            return _json(_device_body())
        if "userinfo" in str(request.url):
            return _json({"sub": "user-1", "email": "teja@example.com"})
        if str(request.url) == REVOKE_URL:
            return httpx.Response(200)
        return _json({"access_token": ACCESS, "refresh_token": "rt-ROTATED", "expires_in": 3600})

    service = _service(handler)
    with caplog.at_level(logging.DEBUG):
        device = _run(service.request_device_code())
        outcome = _run(service.poll_once(device.device_code))
        tokens = outcome.tokens
        _run(service.fetch_account(tokens.access_token))
        _run(service.revoke(tokens.refresh_token))

    assert len(steps) == 4
    blob = "\n".join(record.getMessage() for record in caplog.records)
    for marker in (ACCESS, REFRESH, DEVICE, "rt-ROTATED"):
        assert marker not in blob
