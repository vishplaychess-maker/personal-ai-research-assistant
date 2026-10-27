"""Executable skill handlers.

Each module exposes a self-contained function that other services (HTTP routes,
the Telegram bot, background tasks) can call without knowing the provider.
Errors are typed so callers can map them to user-facing messages without
parsing strings.
"""

from .web_search import (
    DEFAULT_LIMIT,
    SkillExecutionError,
    WebSearchFailed,
    WebSearchNotConfigured,
    WebSearchRateLimited,
    execute_web_search,
)

__all__ = [
    "DEFAULT_LIMIT",
    "SkillExecutionError",
    "WebSearchFailed",
    "WebSearchNotConfigured",
    "WebSearchRateLimited",
    "execute_web_search",
]
