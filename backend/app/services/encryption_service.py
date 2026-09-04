"""
Encryption service — symmetric AES (Fernet) encryption for secrets at rest.

API keys stored in the database must never be plaintext. Fernet (AES-128-CBC
+ HMAC-SHA256, base64-encoded) encrypts/decrypts them with a 32-byte URL-safe
base64 key.

Key sourcing:
  - Read ``ENCRYPTION_KEY`` from the environment / config.
  - If missing, generate a random key for the process lifetime and warn loudly
    (secrets encrypted with an ephemeral key are unusable after restart).

Design:
  - ``encrypt_key(plaintext) -> str``: encrypt; returns an empty string on None.
  - ``decrypt_key(ciphertext) -> str``: decrypt; returns the input unchanged if
    it does not look like Fernet ciphertext (backward compat with legacy
    plaintext rows written before this fix).
"""

import base64
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Sentinel meaning "no stable key configured". Less chatty than a bare None.
_EPHEMERAL_KEY: str = ""


def _get_key() -> bytes:
    """Return the URL-safe base64-encoded Fernet key to use.

    Uses ``ENCRYPTION_KEY`` from the environment / settings when present.
    Otherwise falls back to a random per-process key and warns loudly.
    """
    global _EPHEMERAL_KEY
    try:
        from app.config import settings as _settings

        key_material = getattr(_settings, "encryption_key", "") or ""
    except Exception:  # noqa: BLE001 — never block encryption with config errors
        key_material = os.environ.get("ENCRYPTION_KEY", "") or ""

    key_material = (key_material or "").strip()

    if key_material:
        try:
            return key_material.encode("utf-8")
        except Exception:  # noqa: BLE001
            pass

    if not _EPHEMERAL_KEY:
        _EPHEMERAL_KEY = Fernet.generate_key().decode("utf-8")
        logger.warning(
            "ENCRYPTION_KEY is not configured. Generating an EPHEMERAL key. "
            "Secrets encrypted with it will be unrecoverable after restart. "
            "Set ENCRYPTION_KEY in .env to persist encryption access."
        )
    return _EPHEMERAL_KEY.encode("utf-8")


def encrypt_key(plaintext: str) -> str:
    """Encrypt a plaintext secret and return a Fernet token string.

    Returns '' for None/empty input (no-op) so callers can write '' safely.
    """
    if not plaintext:
        return ""
    try:
        return Fernet(_get_key()).encrypt(str(plaintext).encode("utf-8")).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 — never crash on encrypt
        logger.error("encrypt_key failed: %s", exc)
        return ""


def decrypt_key(ciphertext: str) -> str:
    """Decrypt a Fernet token back to the original plaintext.

    If the value does not decrypt (e.g. it is a legacy plaintext row from
    before encryption was enabled), it is returned unchanged.
    """
    if not ciphertext:
        return ""
    try:
        return Fernet(_get_key()).decrypt(str(ciphertext).encode("utf-8")).decode("utf-8")
    except InvalidToken:
        # Likely a legacy plaintext row — return as-is for backward compat.
        logger.debug("decrypt_key: value not a Fernet token; returning as-is")
        return ciphertext
    except Exception as exc:  # noqa: BLE001
        logger.error("decrypt_key failed: %s", exc)
        return ciphertext


def is_encrypted(value: str) -> bool:
    """Return True if the value looks like a Fernet token we can decrypt.

    Used to avoid double-encrypting a value that is already a token.
    """
    if not value:
        return False
    try:
        Fernet(_get_key()).decrypt(str(value).encode("utf-8"))
        return True
    except Exception:  # noqa: BLE001
        return False
