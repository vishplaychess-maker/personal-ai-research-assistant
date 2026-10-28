"""Portable secret files: JWT signing secret and API-key encryption key.

Both secrets live under app.paths.get_data_dir() - %APPDATA%/ThunderAI in a
frozen build, <repo>/data in a source checkout - and are created on first use
with mode 0600 where the platform supports it. The value is NEVER logged.
"""

from __future__ import annotations

import logging
import os
import secrets as _secrets
from pathlib import Path
from typing import Callable, Optional

from app import paths

logger = logging.getLogger(__name__)


def generate_fernet_key() -> str:
    """A fresh url-safe base64 32-byte key (a valid Fernet key)."""
    try:
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode("ascii")
    except Exception:  # noqa: BLE001 - stdlib fallback, still a valid Fernet key
        import base64

        return base64.urlsafe_b64encode(_secrets.token_bytes(32)).decode("ascii")


def secret_path(filename: str) -> Path:
    """Absolute path of a secret file under the portable data dir."""
    return paths.get_data_dir() / filename


def load_or_create_secret(
    filename: str,
    generate: Optional[Callable[[], str]] = None,
) -> str:
    """Return the persisted secret, creating it on first use.

    Returns "" when the file cannot be read or written (fail-closed); callers
    decide how to degrade. The secret value is never logged.
    """
    generator = generate or generate_fernet_key
    try:
        path = secret_path(filename)
        if path.is_file():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        value = generator()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        logger.info("Generated a new secret file: %s", filename)
        return value
    except Exception as exc:  # noqa: BLE001 - never crash on secret setup
        logger.warning(
            "Could not load or create secret file %s (%s)",
            filename,
            type(exc).__name__,
        )
        return ""


__all__ = ["generate_fernet_key", "secret_path", "load_or_create_secret"]
