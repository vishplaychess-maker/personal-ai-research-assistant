"""First-run configuration (Gemini API key) stored beside the portable data dir.

The value lives in app.paths.get_config_path() (config.json). Resolution order
for the effective Gemini API key:

1. GEMINI_API_KEY in the live process environment.
2. The pydantic settings value (covers .env files).
3. config.json - only while llm_provider == "gemini".

The key is never logged and is never returned to a client.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from app import paths
from app.config import settings

logger = logging.getLogger(__name__)

GEMINI_API_KEY_FIELD = "gemini_api_key"
_GEMINI_ENV = "GEMINI_API_KEY"


def load_config() -> Dict[str, Any]:
    """The stored config as a dict (never raises; {} when absent)."""
    data = paths.load_config()
    return data if isinstance(data, dict) else {}


def stored_gemini_api_key() -> str:
    """The key persisted in config.json, or "" when absent/not a string."""
    value = load_config().get(GEMINI_API_KEY_FIELD)
    return value.strip() if isinstance(value, str) else ""


def env_gemini_api_key() -> str:
    """The key supplied by the environment or .env, or "".

    os.environ is read first so a test (or the launcher) can set the value
    after app.config has already been imported.
    """
    live = os.environ.get(_GEMINI_ENV, "")
    if live and live.strip():
        return live.strip()
    value = getattr(settings, GEMINI_API_KEY_FIELD, "") or ""
    return value.strip() if isinstance(value, str) else ""


def resolve_gemini_api_key() -> str:
    """Effective Gemini key: env first, then config.json for the gemini provider."""
    env_key = env_gemini_api_key()
    if env_key:
        return env_key
    provider = str(getattr(settings, "llm_provider", "") or "").strip().lower()
    if provider == "gemini":
        return stored_gemini_api_key()
    return ""


def is_configured() -> bool:
    """True when a usable Gemini key is available from env or config.json."""
    return bool(env_gemini_api_key() or stored_gemini_api_key())


def save_gemini_api_key(api_key: str) -> bool:
    """Persist a non-empty key to config.json. Never logs the value."""
    key = (api_key or "").strip()
    if not key:
        return False
    return paths.save_config({GEMINI_API_KEY_FIELD: key})


__all__ = [
    "GEMINI_API_KEY_FIELD",
    "load_config",
    "stored_gemini_api_key",
    "env_gemini_api_key",
    "resolve_gemini_api_key",
    "is_configured",
    "save_gemini_api_key",
]
