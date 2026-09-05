"""
Core encryption module — symmetric AES (Fernet) encryption for secrets at rest.

This is the canonical API-key encryption interface. ``encryption_service``
wraps it with backward-compatible behaviour for legacy plaintext rows.

Key sourcing:
  - ``ENCRYPTION_KEY`` environment variable (falls back to the pydantic
    ``settings.encryption_key`` so a ``.env`` file works in local dev).
  - If missing, every function raises ``RuntimeError`` with a helpful
    message. Callers are expected to handle this gracefully (routes return
    HTTP 500, runtime resolution falls back to the environment key) — the
    application must never crash at startup because of a missing key.

Rotation:
  - ``rotate_api_key`` uses ``MultiFernet`` to re-encrypt an existing token
    under a new primary key while keeping the current key as fallback.
"""

import logging
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

logger = logging.getLogger(__name__)

# Fernet tokens always start with this base64 prefix (version byte 0x80
# followed by zero-padded timestamp bytes). Used to tell ciphertext apart
# from legacy plaintext values without touching the key.
FERNET_PREFIX = "gAAAAA"


def _load_key_material() -> bytes:
    """Return the configured Fernet key, or raise RuntimeError if unset."""
    key = (os.getenv("ENCRYPTION_KEY") or "").strip()
    if not key:
        try:
            from app.config import settings as _settings

            key = (getattr(_settings, "encryption_key", "") or "").strip()
        except Exception:  # noqa: BLE001 — never let config errors crash callers
            key = ""
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY is not set. Generate one with "
            "`python scripts/generate_encryption_key.py` and add it to your "
            ".env file as ENCRYPTION_KEY. Without it, API keys cannot be "
            "encrypted or decrypted."
        )
    return key.encode("utf-8")


def get_cipher() -> Fernet:
    """Return a Fernet cipher built from the configured ENCRYPTION_KEY."""
    return Fernet(_load_key_material())


def encrypt_api_key(plain_key: str) -> str:
    """Encrypt a plaintext API key and return the Fernet token string."""
    if not plain_key:
        return ""
    token = get_cipher().encrypt(str(plain_key).encode("utf-8"))
    return token.decode("utf-8")


def decrypt_api_key(encrypted_key: str) -> str:
    """Decrypt a Fernet token back to the plaintext API key.

    Raises ``InvalidToken`` when the value was not encrypted with the
    current key, and ``RuntimeError`` when ENCRYPTION_KEY is unset.
    """
    if not encrypted_key:
        return ""
    plain = get_cipher().decrypt(str(encrypted_key).encode("utf-8"))
    return plain.decode("utf-8")


def rotate_api_key(encrypted_key: str, new_key: bytes) -> str:
    """Re-encrypt an existing Fernet token under a new primary key.

    Uses ``MultiFernet``: the new key is the primary (used for re-encryption),
    the currently-configured ENCRYPTION_KEY is the fallback used to decrypt
    the token first. Raises InvalidToken/RuntimeError on failure.
    """
    if not encrypted_key:
        return ""
    multi = MultiFernet([Fernet(new_key), get_cipher()])
    rotated = multi.rotate(str(encrypted_key).encode("utf-8"))
    return rotated.decode("utf-8")


def mask_api_key(plain_key: str) -> str:
    """Return a display-safe mask of a plaintext key, e.g. ``sk-****1234``.

    Never returns enough characters to reconstruct the key. Returns ''
    for empty input and '****' for very short keys.
    """
    if not plain_key:
        return ""
    plain = str(plain_key)
    if len(plain) <= 8:
        return "****"
    return f"{plain[:3]}****{plain[-4:]}"


def looks_encrypted(value: str) -> bool:
    """True when the value has the shape of a Fernet token."""
    return bool(value) and str(value).startswith(FERNET_PREFIX)


__all__ = [
    "FERNET_PREFIX",
    "get_cipher",
    "encrypt_api_key",
    "decrypt_api_key",
    "rotate_api_key",
    "mask_api_key",
    "looks_encrypted",
    "InvalidToken",
]
