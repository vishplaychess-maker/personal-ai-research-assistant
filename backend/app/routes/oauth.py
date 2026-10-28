"""OAuth subscription sign-in routes (Grok / xAI for now).

The device-code flow is split across a browser and this server, so the state
lives in the database and the browser drives it:

    POST /api/oauth/grok/start   -> ask xAI for a device code, store it
    GET  /api/oauth/grok/status  -> one throttled poll; pending | connected | ...
    POST /api/oauth/grok/refresh -> force a refresh-token exchange
    DELETE /api/oauth/grok       -> revoke and clear the stored credentials
    GET  /api/oauth/grok/account -> signed-in account + entitled models

Driving the poll from the client rather than from a server-side task is
deliberate: a background coroutine dies on restart (PyInstaller builds and
Render both restart routinely), and a desktop app that closes mid-sign-in should
be able to reopen and finish. ``status`` refuses to talk to xAI before
``next_poll_at``, so a chatty UI cannot turn into a polling storm.

Contract: no response and no log line ever carries a token, a device code, or
the upstream error body.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.models import User
from app.services import oauth_grok
from app.services.auth_service import get_current_user
from app.services.cookie_service import require_csrf
from app.services.oauth_grok import GrokAuthError
from app.services.oauth_token_store import (
    GROK,
    STATUS_CONNECTED,
    STATUS_ERROR,
    STATUS_EXPIRED,
    STATUS_PENDING,
    TokenRefreshError,
    service_for,
    token_store,
)

router = APIRouter(prefix="/api/oauth", tags=["oauth"])

logger = logging.getLogger(__name__)

#: Providers reachable through a subscription sign-in.
SUPPORTED_PROVIDERS = {GROK}

#: Answered when this user has never started a sign-in. That is a normal state,
#: not a missing resource: a 404 here would log a browser console error on every
#: first visit to the Providers tab.
STATUS_NONE = "none"


class DeviceCodeResponse(BaseModel):
    provider: str
    user_code: str
    verification_uri: str
    expires_in: int
    poll_interval_s: int
    status: str


class LoginStatusResponse(BaseModel):
    provider: str
    status: str
    user_code: str = ""
    verification_uri: str = ""
    account_name: str = ""
    account_email: str = ""
    scopes: str = ""
    access_expires_at: Optional[str] = None
    device_expires_at: Optional[str] = None
    poll_interval_s: int = 5
    next_poll_at: Optional[str] = None
    last_error: str = ""
    message: str = ""


class RefreshResponse(BaseModel):
    provider: str
    status: str
    access_expires_at: Optional[str] = None
    message: str = ""


class LogoutResponse(BaseModel):
    provider: str
    status: str
    revoked: bool
    message: str = ""


class AccountResponse(BaseModel):
    provider: str
    status: str
    account_name: str = ""
    account_email: str = ""
    scopes: str = ""
    models: List[str] = []
    entitlement_known: bool = False
    message: str = ""


# ── helpers ──────────────────────────────────────────────────


def _validate_provider(provider: str) -> str:
    name = (provider or "").strip().lower()
    if name not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=404,
            detail=(
                "Unsupported OAuth provider. Valid: "
                f"{', '.join(sorted(SUPPORTED_PROVIDERS))}."
            ),
        )
    return name


def _require_enabled(provider: str) -> None:
    """Fail closed when the subscription sign-in has not been opted in.

    503 with actionable copy rather than the non-disclosing 404 that
    ``routes/runs.py`` uses for a disabled feature: every caller here is
    already an authenticated user of this app, and the thing they need to know
    is which switch to flip.
    """
    if provider == GROK and not settings.grok_oauth_enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "Grok sign-in is disabled. Set GROK_OAUTH_ENABLED=true to "
                "allow using a SuperGrok subscription instead of an API key."
            ),
        )


def _service(provider: str):
    try:
        return service_for(provider)
    except TokenRefreshError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


def _encryption_unavailable(exc: RuntimeError) -> HTTPException:
    logger.warning("OAuth credential encryption failed: %s", exc)
    return HTTPException(
        status_code=500,
        detail=(
            "Token encryption is unavailable: ENCRYPTION_KEY is not configured "
            "on the server. A sign-in cannot be stored without it."
        ),
    )


def _public(status: str, code: str = "") -> str:
    """Fixed, safe copy for the UI, from a login state or OAuth error code."""
    if code:
        return oauth_grok.USER_MESSAGES.get(code, f"Grok sign-in failed ({code}).")
    return {
        STATUS_PENDING: oauth_grok.USER_MESSAGES["authorization_pending"],
        STATUS_CONNECTED: "Signed in.",
        STATUS_EXPIRED: oauth_grok.USER_MESSAGES["expired_token"],
        STATUS_ERROR: "Grok sign-in did not complete.",
    }.get(status, "")


def _status_response(row) -> LoginStatusResponse:
    """Project a row into the login-status payload.

    Everything comes from ``as_public_dict``, which is the one place that
    decides what may leave the server: no token, no device code.
    """
    account = token_store.describe(row)
    public = dict(account.as_public_dict())
    public["message"] = _public(public["status"], public["last_error"])
    return LoginStatusResponse(**public)


# ── endpoints ────────────────────────────────────────────────


@router.post("/{provider}/start", response_model=DeviceCodeResponse)
async def start_sign_in(
    provider: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Request a device code and show the user what to type in a browser."""
    name = _validate_provider(provider)
    _require_enabled(name)
    service = _service(name)
    try:
        device = await service.request_device_code()
    except GrokAuthError as exc:
        logger.info("Grok sign-in could not start (%s)", exc.code)
        raise HTTPException(status_code=502, detail=str(exc))

    row = token_store.save_pending_login(
        db, current_user.id, device, provider=name, scopes=service.scope_string
    )
    return DeviceCodeResponse(
        provider=name,
        user_code=row.user_code or "",
        verification_uri=row.verification_uri or "",
        expires_in=int(device.expires_in),
        poll_interval_s=int(row.poll_interval_s or 5),
        status=row.status,
    )


@router.get("/{provider}/status", response_model=LoginStatusResponse)
async def sign_in_status(
    provider: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the login state, polling xAI once when it is time to.

    Read-only from the caller's perspective, so no CSRF requirement; it is
    still owner-scoped and authenticated, and never returns a token.
    """
    name = _validate_provider(provider)
    row = token_store.get_row(db, current_user.id, name)
    if row is None:
        return LoginStatusResponse(
            provider=name,
            status=STATUS_NONE,
            message="Not signed in.",
        )
    account = token_store.describe(row)

    if account.status != STATUS_PENDING:
        return _status_response(row)

    now = _utcnow()
    if account.next_poll_at is not None and now < account.next_poll_at:
        # Not due yet: answer from the row so a fast UI costs nothing upstream.
        return _status_response(row)
    if account.device_expires_at is not None and now >= account.device_expires_at:
        row = token_store.set_status(db, row, STATUS_EXPIRED, error_code="expired_token")
        return _status_response(row)

    device_code = decrypt_device_code(row)
    if not device_code:
        row = token_store.set_status(db, row, STATUS_EXPIRED, error_code="expired_token")
        return _status_response(row)

    service = _service(name)
    outcome = await service.poll_once(device_code, interval=float(account.poll_interval_s))

    if outcome.state == oauth_grok.AUTHORIZED and outcome.tokens is not None:
        tokens = outcome.tokens
        claims = None
        try:
            claims = await service.fetch_account(tokens.access_token)
        except GrokAuthError as exc:
            # The grant worked; only the profile lookup failed. Sign in anyway
            # and let the next /account call retry the claims.
            logger.info("Grok account claims unavailable (%s)", exc.code)
        try:
            row = token_store.save_tokens(
                db,
                current_user.id,
                tokens,
                provider=name,
                account_name=claims.name if claims else "",
                account_email=claims.email if claims else "",
                account_subject=claims.subject if claims else "",
                scopes=tokens.scope or account.scopes,
            )
        except RuntimeError as exc:
            raise _encryption_unavailable(exc)
        return _status_response(row)

    if outcome.state == oauth_grok.FAILED:
        code = outcome.error.code if outcome.error else "invalid_grant"
        status = STATUS_EXPIRED if code in ("expired_token",) else STATUS_ERROR
        row = token_store.set_status(db, row, status, error_code=code)
        return _status_response(row)

    interval = int(outcome.interval or account.poll_interval_s)
    row = token_store.set_status(
        db,
        row,
        STATUS_PENDING,
        poll_interval_s=interval,
        next_poll_at=_utcnow_plus(interval),
    )
    return _status_response(row)


@router.post("/{provider}/refresh", response_model=RefreshResponse)
async def refresh_tokens(
    provider: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Exchange the stored refresh token now (settings action / recovery)."""
    name = _validate_provider(provider)
    _require_enabled(name)
    row = token_store.get_row(db, current_user.id, name)
    if row is None or row.status != STATUS_CONNECTED:
        raise HTTPException(
            status_code=409, detail="No active sign-in to refresh for this provider"
        )

    service = _service(name)
    try:
        await token_store.refresh_now_async(current_user.id, name, service=service)
    except TokenRefreshError as exc:
        logger.info("%s refresh failed (%s)", name, exc.code)
        raise HTTPException(status_code=401, detail=str(exc))

    # The refresh wrote through its own session; drop this one's cache so the
    # rotated expiry is read from the database rather than the stale row.
    db.expire_all()
    account = token_store.describe(token_store.get_row(db, current_user.id, name))
    return RefreshResponse(
        provider=name,
        status=account.status,
        access_expires_at=account.as_public_dict()["access_expires_at"],
        message="Access token renewed.",
    )


@router.delete("/{provider}", response_model=LogoutResponse)
async def sign_out(
    provider: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
):
    """Revoke on xAI when reachable, then always clear the local credentials."""
    name = _validate_provider(provider)
    row = token_store.get_row(db, current_user.id, name)
    if row is None:
        return LogoutResponse(
            provider=name, status="signed_out", revoked=True, message="Nothing to sign out."
        )

    revoked = True
    if row.status == STATUS_CONNECTED:
        refresh_token = decrypt_refresh_token(row)
        if refresh_token:
            try:
                revoked = await _service(name).revoke(refresh_token)
            except GrokAuthError as exc:
                logger.info("Grok revocation refused (%s)", exc.code)
                revoked = False

    removed = token_store.delete_tokens(db, current_user.id, name)
    if not removed:
        raise HTTPException(status_code=404, detail="No Grok sign-in found to remove")
    return LogoutResponse(
        provider=name,
        status="signed_out",
        revoked=revoked,
        message=(
            "Signed out of Grok."
            if revoked
            else "Signed out locally; xAI did not confirm the revocation."
        ),
    )


@router.get("/{provider}/account", response_model=AccountResponse)
async def account_details(
    provider: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Who is signed in, and which models the subscription can actually list.

    xAI exposes no plan claim, so the entitled-model catalog is the honest
    status: it is fetched with the account's own bearer token.
    """
    name = _validate_provider(provider)
    row = token_store.get_row(db, current_user.id, name)
    if row is None or row.status != STATUS_CONNECTED:
        raise HTTPException(status_code=409, detail="Not signed in to Grok")

    account = token_store.describe(row)
    models: List[str] = []
    entitlement_known = False
    if name == GROK:
        from app.services.llm_providers.grok_provider import GrokProvider

        provider_instance = GrokProvider(user_id=current_user.id)
        # Blocking catalog fetch (it may refresh a token first): keep it off the
        # event loop, the same way the chat path bridges sync providers.
        fetched = await asyncio.to_thread(provider_instance.fetch_available_chat_models)
        if fetched is not None:
            models = fetched
            entitlement_known = bool(fetched)

    return AccountResponse(
        provider=account.provider,
        status=account.status,
        account_name=account.account_name,
        account_email=account.account_email,
        scopes=account.scopes,
        models=models,
        entitlement_known=entitlement_known,
        message=(
            "Subscription models available."
            if entitlement_known
            else "Signed in, but no subscription models were listed."
        ),
    )


# ── credential reads (the only places plaintext tokens exist) ─


def decrypt_device_code(row) -> str:
    from app.services.encryption_service import decrypt_key

    return decrypt_key(row.encrypted_device_code or "")


def decrypt_refresh_token(row) -> str:
    from app.services.encryption_service import decrypt_key

    return decrypt_key(row.encrypted_refresh_token or "")


def _utcnow():
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _utcnow_plus(seconds: int):
    import datetime

    return _utcnow() + datetime.timedelta(seconds=max(1, int(seconds)))


__all__ = ["router"]
