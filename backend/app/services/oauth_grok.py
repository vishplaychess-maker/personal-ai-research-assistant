"""Grok (xAI) subscription sign-in — OAuth 2.0 device-code flow (RFC 8628).

Lets a user spend an existing SuperGrok / X Premium+ subscription instead of a
pay-per-token API key. The endpoints below are the values published by xAI's
own OIDC discovery document
(``https://auth.x.ai/.well-known/openid-configuration``); the device flow is the
one the official Grok CLI uses, so it needs no loopback callback server and
works over SSH, inside Docker, and behind a tunnel.

Two things this deliberately does NOT do, because they are not what xAI offers:

* No ``client_secret``. The device client is a *public* client (a native CLI),
  so its id is not a secret and xAI publishes no OAuth-app registration console.
  ``GROK_OAUTH_CLIENT_ID`` exists only to override the id if xAI rotates it.
* No PKCE. ``code_challenge`` belongs to the browser authorization-code flow;
  the device flow's requests carry only ``client_id`` and ``scope``, and a
  verifier would be theatre here.

The device flow spans two requests the user's browser sits between, so this
client exposes one step at a time (``request_device_code`` then ``poll_once``)
and lets the caller persist the state; a ``poll_until_authorized`` loop is
provided for headless use and tests.

Logging invariant: access tokens, refresh tokens, device codes, and the client
id are never logged or placed in an exception message. Errors carry an OAuth
error code (``expired_token``, ``access_denied``, ...) plus a fixed blurb.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

#: The public Grok-CLI device client. Not a secret; overridable by config.
DEFAULT_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_GRANT_TYPE = "refresh_token"

# Poll states the caller distinguishes.
PENDING = "pending"
AUTHORIZED = "authorized"
SLOW_DOWN = "slow_down"
FAILED = "failed"

#: ``slow_down`` means "wait longer"; RFC 8628 says add 5 seconds.
SLOW_DOWN_STEP_S = 5.0
#: Upper bound so a hostile or broken response cannot stall a sign-in forever.
MAX_POLL_INTERVAL_S = 60.0

CONNECT_TIMEOUT = 15.0
REQUEST_TIMEOUT = 20.0

#: Human-readable blurb per OAuth error code. Token material never appears.
USER_MESSAGES = {
    "authorization_pending": "Waiting for you to finish sign-in in the browser.",
    "slow_down": "Slowing down: the sign-in is still waiting for you.",
    "expired_token": "The sign-in code expired. Start sign-in again.",
    "access_denied": "Sign-in was cancelled. Start sign-in again.",
    "authorization_denied": "Sign-in was denied by the account. Try another account.",
    "invalid_grant": "The sign-in code is no longer valid. Start sign-in again.",
    "invalid_client": "Grok rejected this application's client id.",
    "unsupported_grant_type": "Grok no longer accepts the device sign-in grant.",
    "invalid_token": "Grok rejected the access token. Sign in again.",
    "invalid_response": "Grok returned an unreadable sign-in response.",
    "invalid_request": "Grok rejected the sign-in request.",
    "network_error": "Could not reach Grok. Check your connection.",
    "timeout": "Grok did not respond in time.",
    "subscription_required": (
        "This Grok account has no subscription access to the model catalog."
    ),
}


class GrokAuthError(RuntimeError):
    """A bounded, user-safe authentication failure.

    ``code`` is the OAuth error code (safe to show and to log); ``message`` is
    fixed human copy. Neither carries a token, a device code, or a server body.
    """

    def __init__(
        self,
        code: str,
        message: Optional[str] = None,
        *,
        status: Optional[int] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message or USER_MESSAGES.get(code, f"Grok sign-in failed ({code})."))
        self.code = code
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class DeviceCode:
    """What the user needs to see, plus what the poller needs to send."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: Optional[str]
    expires_in: int
    interval: float


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str = ""
    id_token: str = ""

    @property
    def expires_at(self) -> float:
        """Wall-clock expiry of the access token (no skew applied)."""
        return time.time() + max(0, int(self.expires_in))


@dataclass(frozen=True)
class AccountInfo:
    """Non-secret identity claims for the settings UI."""

    subject: str = ""
    name: str = ""
    email: str = ""

    @property
    def display(self) -> str:
        return self.email or self.name or self.subject or "Grok account"


@dataclass(frozen=True)
class PollOutcome:
    """Result of exactly one token-endpoint poll."""

    state: str
    tokens: Optional[TokenSet] = None
    error: Optional[GrokAuthError] = None
    interval: Optional[float] = None
    expires_in: Optional[int] = None


def _token_expiry_epoch(access_token: str) -> Optional[float]:
    """The ``exp`` claim of a JWT access token, without verifying anything.

    Only the expiry is read, because xAI's token response has no ``expires_at``;
    signature verification happens server-side on every API call. Returns None
    for an opaque (non-JWT) or unparseable token so callers fall back to
    ``expires_in``.
    """
    parts = (access_token or "").split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    try:
        return float(exp)
    except (TypeError, ValueError):
        return None


def _form(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Reject a body that carries an OAuth error even though HTTP said 2xx."""
    code = str(payload.get("error") or "")
    if code:
        raise _error_from_code(code)
    return payload


def _error_from_code(code: str) -> GrokAuthError:
    """Build a user-safe error. The server's description is deliberately dropped:
    upstream descriptions have been known to quote request details."""
    return GrokAuthError(code)


class GrokOAuthService:
    """Device-code sign-in against xAI's authorization server."""

    def __init__(
        self,
        *,
        client_id: Optional[str] = None,
        scopes: Optional[list] = None,
        device_auth_url: Optional[str] = None,
        token_url: Optional[str] = None,
        userinfo_url: Optional[str] = None,
        revocation_url: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        # Imported lazily so this module stays importable without a configured
        # environment (and so tests can drive it with explicit arguments).
        from app.config import settings

        self._client_id = (client_id or settings.grok_oauth_client_id
                           or DEFAULT_CLIENT_ID).strip()
        self._scopes = list(scopes) if scopes else list(settings.grok_oauth_scopes)
        self._device_auth_url = device_auth_url or settings.grok_oauth_device_auth_url
        self._token_url = token_url or settings.grok_oauth_token_url
        self._userinfo_url = userinfo_url or settings.grok_oauth_userinfo_url
        self._revocation_url = revocation_url or settings.grok_oauth_revocation_url
        self._transport = transport

    @property
    def scope_string(self) -> str:
        """The exact scope value sent upstream (space-separated)."""
        return " ".join(self._scopes)

    # ── requests ─────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT)
        if self._transport is not None:
            return httpx.AsyncClient(transport=self._transport, timeout=timeout)
        return httpx.AsyncClient(timeout=timeout)

    def _headers(self) -> Dict[str, str]:
        return {"Accept": "application/json"}

    async def request_device_code(self) -> DeviceCode:
        """Ask for a device code the user can type into their browser."""
        async with self._client() as client:
            try:
                response = await client.post(
                    self._device_auth_url,
                    headers=self._headers(),
                    data={
                        "client_id": self._client_id,
                        "scope": self.scope_string,
                    },
                )
            except httpx.TimeoutException as exc:
                raise GrokAuthError("timeout") from exc
            except httpx.RequestError as exc:
                raise GrokAuthError("network_error") from exc

        payload = _json_object(response)
        body = _form(payload)
        device_code = str(body.get("device_code") or "")
        user_code = str(body.get("user_code") or "")
        verification_uri = str(body.get("verification_uri") or "")
        if not device_code or not user_code or not verification_uri:
            logger.warning(
                "Grok device response missing fields (status=%s)", response.status_code
            )
            raise GrokAuthError("invalid_response")

        interval = _positive_number(body.get("interval"), default=5.0)
        expires_in = int(_positive_number(body.get("expires_in"), default=900.0))
        logger.info(
            "Grok device code issued (expires_in=%ss, interval=%ss)",
            expires_in,
            interval,
        )
        return DeviceCode(
            device_code=device_code,
            user_code=user_code,
            verification_uri=verification_uri,
            verification_uri_complete=(
                str(body.get("verification_uri_complete") or "") or None
            ),
            expires_in=expires_in,
            interval=interval,
        )

    async def poll_once(self, device_code: str, *, interval: float = 5.0) -> PollOutcome:
        """One token-endpoint poll; never sleeps, so callers own the cadence.

        The caller drives this once per UI tick, which keeps the state in the
        database instead of in a background task that would die on restart.
        """
        async with self._client() as client:
            try:
                response = await client.post(
                    self._token_url,
                    headers=self._headers(),
                    data={
                        "grant_type": DEVICE_GRANT_TYPE,
                        "client_id": self._client_id,
                        "device_code": device_code,
                    },
                )
            except httpx.TimeoutException:
                return PollOutcome(PENDING, interval=interval)
            except httpx.RequestError:
                return PollOutcome(PENDING, interval=interval)

        # RFC 8628 answers "not yet" with an error code in a 400 body, so the
        # body is parsed here without the status being judged.
        payload = _parsed_body(response)
        code = str(payload.get("error") or "")
        if code == "authorization_pending":
            return PollOutcome(PENDING, interval=interval)
        if code == "slow_down":
            raised = min(MAX_POLL_INTERVAL_S, interval + SLOW_DOWN_STEP_S)
            server_interval = payload.get("interval")
            if server_interval:
                raised = min(MAX_POLL_INTERVAL_S, float(server_interval))
            logger.info("Grok sign-in polling slowed to %ss", raised)
            return PollOutcome(SLOW_DOWN, interval=raised)
        if not code and response.status_code >= 400:
            # A block page or gateway error carries no OAuth code; name it by
            # status so the UI can still say something honest.
            code = (
                "subscription_required"
                if response.status_code in (402, 403)
                else "http_%s" % response.status_code
            )
        if code:
            error = _error_from_code(code)
            logger.info("Grok sign-in failed (%s)", error.code)
            return PollOutcome(FAILED, error=error)

        try:
            tokens = _tokens_from_body(payload)
        except GrokAuthError as exc:
            return PollOutcome(FAILED, error=exc)
        return PollOutcome(AUTHORIZED, tokens=tokens)

    async def poll_until_authorized(
        self,
        device_code: str,
        *,
        interval: float = 5.0,
        timeout: float = 900.0,
        on_pending: Optional[Callable[[], None]] = None,
    ) -> TokenSet:
        """Headless convenience loop (CLI sign-in, tests, scripted checks)."""
        deadline = time.monotonic() + max(0.0, timeout)
        wait = max(1.0, float(interval))
        while True:
            outcome = await self.poll_once(device_code, interval=wait)
            if outcome.state == AUTHORIZED and outcome.tokens is not None:
                return outcome.tokens
            if outcome.state == FAILED:
                raise outcome.error or GrokAuthError("invalid_grant")
            if outcome.interval:
                wait = max(1.0, float(outcome.interval))
            if on_pending is not None:
                on_pending()
            if time.monotonic() + wait > deadline:
                raise GrokAuthError("expired_token")
            await asyncio.sleep(wait)

    async def refresh(self, refresh_token: str) -> TokenSet:
        """Exchange a refresh token. xAI rotates it: the old one is consumed,
        so the caller must persist the returned pair immediately."""
        async with self._client() as client:
            try:
                response = await client.post(
                    self._token_url,
                    headers=self._headers(),
                    data={
                        "grant_type": REFRESH_GRANT_TYPE,
                        "client_id": self._client_id,
                        "refresh_token": refresh_token,
                    },
                )
            except httpx.TimeoutException as exc:
                raise GrokAuthError("timeout") from exc
            except httpx.RequestError as exc:
                raise GrokAuthError("network_error") from exc

        try:
            body = _form(_json_object(response))
        except GrokAuthError as exc:
            if exc.status in (402, 403):
                # The grant is valid; the account simply is not entitled to API
                # access. Re-signing-in cannot fix it, so it is its own code.
                logger.info("Grok access denied by subscription gate")
                raise GrokAuthError("subscription_required", status=exc.status) from exc
            raise
        tokens = _tokens_from_body(body)
        logger.info("Grok access token refreshed (expires_in=%ss)", tokens.expires_in)
        return tokens

    async def fetch_account(self, access_token: str) -> AccountInfo:
        """Read the OIDC claims. xAI exposes no plan/tier here — entitlement is
        proven by which models the account can list, not by a claim."""
        async with self._client() as client:
            try:
                response = await client.get(
                    self._userinfo_url,
                    headers={"Authorization": f"Bearer {access_token}",
                             **self._headers()},
                )
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                raise GrokAuthError("network_error") from exc

        if response.status_code != 200:
            logger.info(
                "Grok userinfo returned HTTP %s", response.status_code
            )
            raise GrokAuthError("invalid_token", status=response.status_code)
        try:
            claims = response.json()
        except (json.JSONDecodeError, ValueError):
            claims = {}
        if not isinstance(claims, dict):
            claims = {}
        return AccountInfo(
            subject=str(claims.get("sub") or ""),
            name=str(claims.get("name") or ""),
            email=str(claims.get("email") or ""),
        )

    async def revoke(self, token: str, *, token_type_hint: str = "refresh_token") -> bool:
        """Best-eff server-side revocation; local deletion happens regardless.

        RFC 7009 answers 200 for an unknown or already-revoked token, so a
        failure here is reported as False rather than raised — logout must still
        clear the local credentials.
        """
        if not token:
            return True
        async with self._client() as client:
            try:
                response = await client.post(
                    self._revocation_url,
                    headers=self._headers(),
                    data={
                        "token": token,
                        "token_type_hint": token_type_hint,
                        "client_id": self._client_id,
                    },
                )
            except (httpx.TimeoutException, httpx.RequestError):
                logger.info("Grok revocation request did not reach the server")
                return False
        ok = response.status_code in (200, 204)
        logger.info("Grok token revocation %s", "accepted" if ok else "refused")
        return ok

    # ── expiry ───────────────────────────────────────────────

    def access_token_is_exhausted(
        self,
        access_token: str,
        expires_at: Optional[float],
        *,
        skew_s: float = 300.0,
        now: Optional[float] = None,
    ) -> bool:
        """True when the access token needs refreshing before it is used.

        The JWT ``exp`` wins over the stored expiry (a refresh response can
        shorten a session), and both are approached ``skew_s`` early so an
        in-flight request never carries a token that dies mid-stream.
        """
        current = time.time() if now is None else float(now)
        claim_exp = _token_expiry_epoch(access_token)
        if claim_exp is not None:
            return current >= claim_exp - skew_s
        if expires_at is None:
            return True
        return current >= float(expires_at) - skew_s


def _json_object(response: httpx.Response) -> Dict[str, Any]:
    """A success body, or a raised OAuth error for any non-2xx response.

    The upstream ``error_description`` is dropped rather than propagated:
    server descriptions have historically echoed request parameters, and a
    code plus fixed copy is enough for both the log and the user.
    """
    if not 200 <= response.status_code < 300:
        code = _parsed_body(response).get("error")
        code = str(code or "") or "http_%s" % response.status_code
        raise GrokAuthError(code, status=response.status_code)
    payload = _parsed_body(response)
    if not payload and response.content:
        raise GrokAuthError("invalid_response")
    return payload


def _parsed_body(response: httpx.Response) -> Dict[str, Any]:
    """The JSON object body, or ``{}`` — never raises, for polling to judge."""
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _tokens_from_body(body: Dict[str, Any]) -> TokenSet:
    access_token = str(body.get("access_token") or "")
    refresh_token = str(body.get("refresh_token") or "")
    if not access_token or not refresh_token:
        # Both are required: no refresh token means the sign-in cannot be
        # renewed, which would silently strand the user in an hour.
        logger.warning("Grok token response was incomplete")
        raise GrokAuthError("invalid_response")
    return TokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=int(_positive_number(body.get("expires_in"), default=3600.0)),
        scope=str(body.get("scope") or ""),
        id_token=str(body.get("id_token") or ""),
    )


def _positive_number(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default
