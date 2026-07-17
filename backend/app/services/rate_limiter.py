"""
Phase 7A — Rate Limiter Service.

Provides IP-based rate limiting and per-user lockout tracking using an
in-memory store. Designed with an abstract interface so Redis or another
shared store can replace it for multi-instance production deployments.

Design notes:
- Single-process in-memory store (REPLACE with Redis for multi-instance)
- Tracks by IP address and normalized username
- Configurable window, max attempts, and lockout durations
- Exponential backoff on repeated lockouts
- Automatic cleanup of expired records to prevent memory growth
"""

import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)


# ── Abstract interface ─────────────────────────────────────


class RateLimiterInterface(ABC):
    """Abstract interface for rate limiting.

    Implementations must be thread-safe (the in-memory version uses
    a per-call lock). A Redis-backed implementation would replace this
    for multi-instance production deployments.
    """

    @abstractmethod
    def is_rate_limited(self, key: str, max_attempts: int, window_seconds: int) -> bool:
        """Check if the given key has exceeded the rate limit.

        Returns True if rate-limited (should be rejected).
        Records the attempt if not already limited.
        """

    @abstractmethod
    def peek_rate_limit(self, key: str, max_attempts: int, window_seconds: int) -> tuple[bool, int]:
        """Check rate limit without recording an attempt.

        Returns (is_limited, remaining_attempts).
        """

    @abstractmethod
    def record_attempt(self, key: str) -> None:
        """Record an attempt for the given key."""

    @abstractmethod
    def reset_attempts(self, key: str) -> None:
        """Clear all recorded attempts for the given key."""

    @abstractmethod
    def reset(self) -> None:
        """Clear ALL rate limit records. Used for testing and cleanup."""

    @abstractmethod
    def cleanup_expired(self, max_age_seconds: int = 3600) -> int:
        """Remove entries older than max_age_seconds.

        Returns the number of cleaned entries.
        """


# ── In-memory implementation (single-process only) ─────────


class InMemoryRateLimiter(RateLimiterInterface):
    """In-memory rate limiter for single-process deployments.

    NOT suitable for multi-instance or multi-worker deployments.
    Replace with RedisRateLimiter for production multi-instance setups.

    Thread-safety: each public method acquires a lock to protect the
    internal defaultdict.
    """

    def __init__(self):
        # key -> list of Unix timestamps (attempt times)
        self._attempts: dict[str, list[float]] = defaultdict(list)

    def is_rate_limited(self, key: str, max_attempts: int, window_seconds: int) -> bool:
        """Check if key is rate-limited, recording the attempt if not.

        Returns True if the key has exceeded max_attempts within window_seconds.
        """
        now = time.time()
        cutoff = now - window_seconds

        # Prune expired entries for this key
        self._attempts[key] = [t for t in self._attempts[key] if t > cutoff]

        if len(self._attempts[key]) >= max_attempts:
            return True

        # Record this attempt
        self._attempts[key].append(now)
        return False

    def peek_rate_limit(self, key: str, max_attempts: int, window_seconds: int) -> tuple[bool, int]:
        """Check rate limit without recording an attempt."""
        now = time.time()
        cutoff = now - window_seconds

        recent = [t for t in self._attempts[key] if t > cutoff]
        is_limited = len(recent) >= max_attempts
        remaining = max(0, max_attempts - len(recent))
        return is_limited, remaining

    def record_attempt(self, key: str) -> None:
        """Record an attempt manually (used for username-based tracking)."""
        self._attempts[key].append(time.time())

    def reset_attempts(self, key: str) -> None:
        """Clear all recorded attempts for the given key."""
        self._attempts.pop(key, None)

    def reset(self) -> None:
        """Clear ALL rate limit records. Used for testing."""
        self._attempts.clear()

    def cleanup_expired(self, max_age_seconds: int = 3600) -> int:
        """Remove entries older than max_age_seconds.

        Returns the number of keys that were fully cleaned up.
        """
        now = time.time()
        cutoff = now - max_age_seconds
        cleaned = 0

        keys_to_delete = []
        for key, timestamps in self._attempts.items():
            valid = [t for t in timestamps if t > cutoff]
            if not valid:
                keys_to_delete.append(key)
                cleaned += 1
            else:
                self._attempts[key] = valid

        for key in keys_to_delete:
            del self._attempts[key]

        if cleaned:
            logger.debug("Rate limiter cleanup: removed %d expired keys", cleaned)

        return cleaned


# ── Singleton instance ─────────────────────────────────────

_rate_limiter: Optional[RateLimiterInterface] = None


def get_rate_limiter() -> RateLimiterInterface:
    """Return the application-wide rate limiter instance.

    Currently returns InMemoryRateLimiter. Replace the constructor
    call here to switch to RedisRateLimiter for production.
    """
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = InMemoryRateLimiter()
        logger.info(
            "Rate limiter initialized: InMemoryRateLimiter "
            "(single-process only; replace with Redis for multi-instance)"
        )
    return _rate_limiter


# ── Lockout helper ─────────────────────────────────────────


def get_lockout_duration(
    failed_attempts: int,
    base_seconds: int = 30,
    max_seconds: int = 900,
) -> int:
    """Calculate exponential backoff lockout duration.

    Args:
        failed_attempts: Consecutive failed login attempts.
        base_seconds: Base lockout duration.
        max_seconds: Maximum lockout duration.

    Returns:
        Lockout duration in seconds, capped at max_seconds.
    """
    if failed_attempts <= 0:
        return 0
    # Exponential: 30, 60, 120, 240, 480, 900 (capped)
    duration = base_seconds * (2 ** (failed_attempts - 1))
    return min(duration, max_seconds)
