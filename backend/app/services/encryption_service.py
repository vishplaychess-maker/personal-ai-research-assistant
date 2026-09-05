"""
Encryption service — backward-compatible wrapper around ``encryption``.

Historically this module was the API-key encryption interface with an
ephemeral-key fallback when ``ENCRYPTION_KEY`` was unset. It now delegates
to ``app.services.encryption`` (canonical Fernet/MultiFernet API) and keeps
only the legacy-compatibility behaviours:

  - ``encrypt_key`` propagates ``RuntimeError`` when ENCRYPTION_KEY is unset
    (routes translate this into HTTP 500; the app never crashes at startup).
  - ``decrypt_key`` never raises: legacy plaintext rows written before
    encryption existed are returned unchanged, and values that cannot be
    decrypted with the current key return '' (callers fall back to the
    environment-configured provider key).

API keys stored in the database must never be plaintext. Fernet (AES-128-CBC
+ HMAC-SHA256, base64-encoded) encrypts/decrypts them with a 32-byte URL-safe
base64 key.
"""

import logging

from cryptography.fernet import InvalidToken

from app.services.encryption import (
    encrypt_api_key,
    decrypt_api_key,
    looks_encrypted,
)

logger = logging.getLogger(__name__)


def encrypt_key(plaintext: str) -> str:
    """Encrypt a plaintext secret and return a Fernet token string.

    Returns '' for None/empty input (no-op) so callers can write '' safely.
    Raises RuntimeError when ENCRYPTION_KEY is not configured — callers are
    expected to turn that into an HTTP 500 for encryption-requiring routes.
    """
    if not plaintext:
        return ""
    return encrypt_api_key(plaintext)


def decrypt_key(ciphertext: str) -> str:
    """Decrypt a Fernet token back to the original plaintext.

    Backward-compatibility rules:
      - Legacy plaintext rows (written before encryption existed) are
        returned unchanged — they never needed a key to be readable.
      - A Fernet token that cannot be decrypted with the current key
        (key rotated/lost, or written under an ephemeral key) returns ''
        so runtime callers can fall back to the environment key.
      - A missing ENCRYPTION_KEY decrypts nothing but never raises here.
    """
    if not ciphertext:
        return ""
    value = str(ciphertext)
    if not looks_encrypted(value):
        # Legacy plaintext row — return as-is for backward compat.
        return value
    try:
        return decrypt_api_key(value)
    except RuntimeError:
        logger.warning(
            "ENCRYPTION_KEY is not configured — cannot decrypt a stored "
            "API key; falling back to the environment provider key if any."
        )
        return ""
    except InvalidToken:
        logger.warning(
            "Stored API key could not be decrypted with the current "
            "ENCRYPTION_KEY (the key changed or the value was written "
            "under an ephemeral key); falling back to the environment "
            "provider key if any."
        )
        return ""
    except Exception as exc:  # noqa: BLE001 — never crash a read on decrypt
        logger.error("decrypt_key failed: %s", exc)
        return ""


def is_encrypted(value: str) -> bool:
    """Return True if the value looks like a Fernet token.

    Used to avoid double-encrypting a value that is already a token.
    """
    return looks_encrypted(value)
