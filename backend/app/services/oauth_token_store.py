"""Encrypted storage for provider OAuth credentials (Grok / xAI today).

One ``ProviderOAuthToken`` row per (user, provider) holds both the in-flight
device-code sign-in and, once the user authorises, the token set. Access and
refresh tokens are Fernet-encrypted with the same ``ENCRYPTION_KEY`` the
provider API keys use, so:

* a missing ``ENCRYPTION_KEY`` makes saving credentials fail loudly rather than
  write something that cannot survive a restart;
* nothing here ever returns token material to a route — the public view is a
  fixed set of non-secret account fields.

Refresh is single-flight per (user, provider): xAI rotates the refresh token on
every exchange and consumes the old one, so two concurrent requests that both
decided to refresh would leave the second holding a dead token.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.models import ProviderOAuthToken
from app.services.encryption_service import decrypt_key, encrypt_key
from app.services.oauth_grok import DeviceCode, GrokAuthError, TokenSet

logger = logging.getLogger(__name__)

GROK = "grok"

#: Login states, mirrored by the UI copy in Settings.tsx.
STATUS_PENDING = "pending"
STATUS_CONNECTED = "connected"
STATUS_EXPIRED = "expired"
STATUS_ERROR = "error"

_TERMINAL_STATUSES = (STATUS_EXPIRED, STATUS_ERROR)


class TokenRefreshError(RuntimeError):
    """The stored grant cannot produce a usable access token.

    Fail-closed by contract: callers must stop and tell the user to sign in
    again. There is no silent fallback to an API key.
    """

    def __init__(self, message: str, *, code: str = "reauth_required") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OAuthAccount:
    """The only view of a stored sign-in that may reach an API response."""

    provider: str
    status: str
    account_name: str = ""
    account_email: str = ""
    scopes: str = ""
    access_expires_at: Optional[datetime.datetime] = None
    user_code: str = ""
    verification_uri: str = ""
    poll_interval_s: int = 5
    next_poll_at: Optional[datetime.datetime] = None
    device_expires_at: Optional[datetime.datetime] = None
    last_error: str = ""

    def as_public_dict(self) -> Dict[str, object]:
        return {
            "provider": self.provider,
            "status": self.status,
            "account_name": self.account_name,
            "account_email": self.account_email,
            "scopes": self.scopes,
            "access_expires_at": _iso(self.access_expires_at),
            "user_code": self.user_code,
            "verification_uri": self.verification_uri,
            "poll_interval_s": self.poll_interval_s,
            "next_poll_at": _iso(self.next_poll_at),
            "device_expires_at": _iso(self.device_expires_at),
            "last_error": self.last_error,
        }


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _from_epoch(epoch: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).replace(
        tzinfo=None
    )


def _to_epoch(moment: Optional[datetime.datetime]) -> Optional[float]:
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=datetime.timezone.utc).timestamp()
    return moment.timestamp()


def _iso(moment: Optional[datetime.datetime]) -> Optional[str]:
    return moment.replace(microsecond=0).isoformat() + "Z" if moment else None


class OAuthTokenStore:
    """Reads and writes the encrypted credential rows for one user/provider."""

    def __init__(self, session_factory=None) -> None:
        if session_factory is None:
            from app.database import SessionLocal

            session_factory = SessionLocal
        self._session_factory = session_factory
        self._refresh_locks: Dict[Tuple[int, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ── lookups ──────────────────────────────────────────────

    def get_row(
        self, db: Session, user_id: int, provider: str = GROK
    ) -> Optional[ProviderOAuthToken]:
        return (
            db.query(ProviderOAuthToken)
            .filter(
                ProviderOAuthToken.user_id == user_id,
                ProviderOAuthToken.provider_name == provider,
            )
            .one_or_none()
        )

    def account(
        self, db: Session, user_id: int, provider: str = GROK
    ) -> Optional[OAuthAccount]:
        row = self.get_row(db, user_id, provider)
        return self.describe(row) if row is not None else None

    def describe(self, row: ProviderOAuthToken) -> OAuthAccount:
        """Non-secret projection of a row. Never decrypts anything."""
        return OAuthAccount(
            provider=row.provider_name,
            status=row.status or STATUS_PENDING,
            account_name=row.account_name or "",
            account_email=row.account_email or "",
            scopes=row.scopes or "",
            access_expires_at=row.access_expires_at,
            user_code=row.user_code or "",
            verification_uri=row.verification_uri or "",
            poll_interval_s=int(row.poll_interval_s or 5),
            next_poll_at=row.next_poll_at,
            device_expires_at=row.device_expires_at,
            last_error=row.last_error or "",
        )

    # ── writes ───────────────────────────────────────────────

    def save_pending_login(
        self,
        db: Session,
        user_id: int,
        device: DeviceCode,
        *,
        provider: str = GROK,
        scopes: str = "",
    ) -> ProviderOAuthToken:
        """Persist a freshly issued device code, replacing any earlier attempt.

        The device code is encrypted: it is the credential that redeems the
        grant, so a database dump must not be enough to finish a sign-in.
        """
        row = self.get_row(db, user_id, provider)
        if row is None:
            row = ProviderOAuthToken(
                user_id=user_id,
                provider_name=provider,
            )
            db.add(row)
        row.status = STATUS_PENDING
        row.encrypted_device_code = encrypt_key(device.device_code)
        row.device_expires_at = _from_epoch(_now_epoch() + device.expires_in)
        row.poll_interval_s = int(max(1, min(60, round(device.interval))))
        row.next_poll_at = _utcnow()
        row.user_code = device.user_code
        row.verification_uri = device.verification_uri_complete or device.verification_uri
        row.scopes = scopes
        row.last_error = None
        # A fresh sign-in invalidates whatever credentials were stored before.
        row.encrypted_access_token = ""
        row.encrypted_refresh_token = ""
        row.access_expires_at = None
        db.commit()
        db.refresh(row)
        logger.info("Grok sign-in started (user=%s, expires_in=%ss)", user_id, device.expires_in)
        return row

    def save_tokens(
        self,
        db: Session,
        user_id: int,
        tokens: TokenSet,
        *,
        provider: str = GROK,
        account_name: str = "",
        account_email: str = "",
        account_subject: str = "",
        scopes: str = "",
    ) -> ProviderOAuthToken:
        """Store a token set encrypted at rest, marking the sign-in connected.

        Raises RuntimeError when ENCRYPTION_KEY is unavailable (the route turns
        that into HTTP 500), because an unencrypted or ephemeral-key token would
        silently stop working at the next restart.
        """
        access = encrypt_key(tokens.access_token)
        refresh = encrypt_key(tokens.refresh_token)
        row = self.get_row(db, user_id, provider)
        if row is None:
            row = ProviderOAuthToken(user_id=user_id, provider_name=provider)
            db.add(row)
        row.status = STATUS_CONNECTED
        row.encrypted_access_token = access
        row.encrypted_refresh_token = refresh
        row.access_expires_at = _from_epoch(tokens.expires_at)
        row.encrypted_device_code = ""
        row.next_poll_at = None
        if scopes:
            row.scopes = scopes
        if account_name:
            row.account_name = account_name[:255]
        if account_email:
            row.account_email = account_email[:255]
        if account_subject:
            row.account_subject = account_subject[:128]
        row.last_error = None
        db.commit()
        db.refresh(row)
        logger.info(
            "Grok sign-in complete (user=%s, expires_in=%ss)", user_id, tokens.expires_in
        )
        return row

    def set_status(
        self,
        db: Session,
        row: ProviderOAuthToken,
        status: str,
        *,
        error_code: str = "",
        poll_interval_s: Optional[int] = None,
        next_poll_at: Optional[datetime.datetime] = None,
    ) -> ProviderOAuthToken:
        """Record a poll outcome. ``error_code`` is an OAuth code, never a body."""
        row.status = status
        if status in _TERMINAL_STATUSES:
            row.last_error = (error_code or "error")[:64]
            row.encrypted_device_code = ""
            row.next_poll_at = None
        else:
            if error_code:
                row.last_error = error_code[:64]
            if poll_interval_s:
                row.poll_interval_s = int(max(1, min(60, poll_interval_s)))
            row.next_poll_at = (
                next_poll_at if next_poll_at is not None else _utcnow()
            )
        db.commit()
        db.refresh(row)
        return row

    def delete_tokens(
        self, db: Session, user_id: int, provider: str = GROK
    ) -> bool:
        """Remove the stored credentials. Returns whether a row existed."""
        row = self.get_row(db, user_id, provider)
        if row is None:
            return False
        db.delete(row)
        db.commit()
        logger.info("Grok credentials cleared (user=%s)", user_id)
        return True

    # ── credential access ────────────────────────────────────

    def connected_access_token(
        self, db: Session, user_id: int, provider: str = GROK
    ) -> Optional[str]:
        """The stored access token, or None when the user is not signed in."""
        row = self.get_row(db, user_id, provider)
        if row is None or row.status != STATUS_CONNECTED:
            return None
        return decrypt_key(row.encrypted_access_token or "") or None

    def get_valid_token(
        self,
        user_id: int,
        provider: str = GROK,
        *,
        service=None,
        skew_s: Optional[float] = None,
    ) -> str:
        """Return a usable access token, refreshing first when it is near expiry.

        Raises TokenRefreshError when there is no sign-in, the stored grant no
        longer works, or encryption is unavailable. Callers must treat that as
        "sign in again" and must not fall back to another credential.
        """
        from app.config import settings

        oauth = service or service_for(provider)
        skew = (
            float(skew_s)
            if skew_s is not None
            else float(settings.grok_oauth_expiry_skew_s)
        )

        with self._lock_for(user_id, provider):
            db = self._session_factory()
            try:
                row = self._require_connected(db, user_id, provider)
                access = decrypt_key(row.encrypted_access_token or "")
                if access and not oauth.access_token_is_exhausted(
                    access, _to_epoch(row.access_expires_at), skew_s=skew
                ):
                    return access
                return self._refresh_into(db, row, oauth, skew)
            finally:
                db.close()

    async def get_valid_token_async(
        self,
        user_id: int,
        provider: str = GROK,
        *,
        service=None,
        skew_s: Optional[float] = None,
    ) -> str:
        """Async access for streaming callers.

        Runs the same guarded sync path on a worker thread rather than
        duplicating the single-flight logic with an asyncio lock: one lock
        covers sync and async refreshes together, and the worker thread has no
        running loop for ``_run_sync`` to trip over. Same precedent as
        ``streaming_service`` bridging sync providers.
        """
        return await asyncio.to_thread(
            self.get_valid_token, user_id, provider, service=service, skew_s=skew_s
        )

    def refresh_now(
        self,
        user_id: int,
        provider: str = GROK,
        *,
        service=None,
    ) -> datetime.datetime:
        """Force a refresh (the settings "Refresh now" action / 401 retry).

        Returns the new access-token expiry. Raises TokenRefreshError on a dead
        grant so the UI can prompt a re-login. It opens its own session, so a
        caller on an event loop must use ``refresh_now_async`` instead.
        """
        from app.config import settings

        oauth = service or service_for(provider)
        with self._lock_for(user_id, provider):
            db = self._session_factory()
            try:
                row = self._require_connected(db, user_id, provider)
                self._refresh_into(
                    db, row, oauth, float(settings.grok_oauth_expiry_skew_s)
                )
                return row.access_expires_at
            finally:
                db.close()

    async def refresh_now_async(
        self, user_id: int, provider: str = GROK, *, service=None
    ) -> datetime.datetime:
        """Async forced refresh, on a worker thread like ``get_valid_token_async``.

        A SQLAlchemy session is not shareable across threads, so this always
        opens (and closes) its own; callers holding a request session must
        expire it to observe the rotated credentials.
        """
        return await asyncio.to_thread(
            self.refresh_now, user_id, provider, service=service
        )

    def _require_connected(
        self, db: Session, user_id: int, provider: str
    ) -> ProviderOAuthToken:
        row = self.get_row(db, user_id, provider)
        if row is None or row.status != STATUS_CONNECTED:
            raise TokenRefreshError(
                "You are not signed in to Grok. Use Sign in with Grok.",
                code="not_signed_in",
            )
        return row

    def _refresh_into(self, db, row, oauth, skew: float) -> str:
        refresh = decrypt_key(row.encrypted_refresh_token or "")
        if not refresh:
            row.status = STATUS_ERROR
            row.last_error = "invalid_grant"
            db.commit()
            raise TokenRefreshError(
                "Grok sign-in could not be renewed. Sign in again.",
                code="refresh_unavailable",
            )
        try:
            tokens = _run_sync(oauth.refresh(refresh))
        except GrokAuthError as exc:
            # The grant is dead (consumed, revoked, or the tier no longer
            # entitles API access). Keep the row so the UI can explain, but
            # never keep a token that cannot be renewed.
            row.status = STATUS_ERROR
            row.last_error = exc.code[:64]
            row.encrypted_access_token = ""
            row.encrypted_refresh_token = ""
            db.commit()
            logger.info("Grok token refresh failed (%s)", exc.code)
            raise TokenRefreshError(str(exc), code=exc.code) from exc

        try:
            self.save_tokens(
                db,
                row.user_id,
                tokens,
                provider=row.provider_name,
                scopes=tokens.scope or row.scopes or "",
            )
        except RuntimeError as exc:
            logger.warning("Grok refreshed tokens could not be encrypted: %s", exc)
            raise TokenRefreshError(
                "Grok sign-in could not be saved. Configure ENCRYPTION_KEY.",
                code="encryption_unavailable",
            ) from exc
        return tokens.access_token

    def _lock_for(self, user_id: int, provider: str) -> threading.Lock:
        key = (user_id, provider)
        with self._locks_guard:
            lock = self._refresh_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._refresh_locks[key] = lock
            return lock


# A process-wide store so the provider (built per request, and sometimes without
# a Session at hand) and the routes share one set of refresh locks.
token_store = OAuthTokenStore()


def service_for(provider: str):
    """Build the OAuth client for a provider name."""
    if provider == GROK:
        from app.services.oauth_grok import GrokOAuthService

        return GrokOAuthService()
    raise TokenRefreshError(
        f"No OAuth sign-in is implemented for '{provider}'.", code="unsupported"
    )


def _now_epoch() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


def _run_sync(coro):
    """Run one coroutine to completion from sync code (provider path).

    ``get_valid_token`` is called from both async routes and the sync provider
    methods; a nested loop is not expected here, and if one appears the refresh
    is refused rather than silently skipped.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Already inside a loop: the caller must await instead of calling sync.
    coro.close()
    raise TokenRefreshError(
        "Grok token refresh must be awaited on this path.", code="loop_nested"
    )
