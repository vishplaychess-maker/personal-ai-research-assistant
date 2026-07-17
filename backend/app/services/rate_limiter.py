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
- Thread-safe: each public method acquires a reentrant lock
- Automatic probabilistic cleanup of expired records to prevent memory growth
"""

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)

# ── Probabilistic cleanup constant ─────────────────────────

_DEFAULT_CLEANUP_INTERVAL: int = 100
"""Default mutations between auto-cleanup passes. Each instance uses its own copy."""


# ── Abstract interface ─────────────────────────────────────


class RateLimiterInterface(ABC):
    """Abstract interface for rate limiting.

    Implementations must be thread-safe.
    A Redis-backed implementation would replace InMemoryRateLimiter
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

        Returns the number of keys that were fully cleaned up.
        """

    @abstractmethod
    def stop(self) -> None:
        """Gracefully shut down the rate limiter, releasing any resources.

        Called during application shutdown (lifespan teardown).
        The InMemory implementation is a lightweight no-op; Redis or
        other stateful implementations should close connections here.
        """

    @abstractmethod
    def mutation_count(self) -> int:
        """Return the total number of mutations handled.

        Used for probabilistic cleanup scheduling.
        """


# ── In-memory implementation (single-process only) ─────────


class InMemoryRateLimiter(RateLimiterInterface):
    """In-memory rate limiter for single-process deployments.

    NOT suitable for multi-instance or multi-worker deployments.
    Replace with RedisRateLimiter for production multi-instance setups.

    Thread-safety: a reentrant lock protects all internal state.
    """

    def __init__(self, cleanup_interval: int = _DEFAULT_CLEANUP_INTERVAL):
        # key -> list of Unix timestamps (attempt times)
        self._attempts: dict[str, list[float]] = defaultdict(list)
        # Reentrant lock so internal helpers that hold the lock can call
        # each other without deadlock
        self._lock = threading.RLock()
        # Mutation counter used for probabilistic cleanup scheduling
        self._mutations = 0
        # How many mutations between auto-cleanup passes (0 = disabled)
        self.cleanup_interval = cleanup_interval

    # ── Internal helpers ───────────────────────────────────

    def _prune_key(self, key: str, cutoff: float) -> int:
        """Remove entries older than *cutoff* for a specific key.

        Removes the key entirely if all entries are expired.
        Caller must hold self._lock.

        Returns:
            Number of entries removed.
        """
        entries = self._attempts.get(key)
        if entries is None:
            return 0
        before = len(entries)
        valid = [t for t in entries if t > cutoff]
        if valid:
            self._attempts[key] = valid
        else:
            # All entries expired: remove key entirely
            self._attempts.pop(key, None)
        return before - len(valid)

    def _maybe_cleanup(self) -> None:
        """Probabilistically trigger cleanup_expired after every N mutations.

        Caller does NOT need to hold the lock — cleanup_expired acquires it.
        """
        if self.cleanup_interval > 0 and self._mutations >= self.cleanup_interval:
            self._mutations = 0
            self.cleanup_expired()

    # ── Public API ─────────────────────────────────────────

    def is_rate_limited(self, key: str, max_attempts: int, window_seconds: int) -> bool:
        """Check if key is rate-limited, recording the attempt if not.

        Returns True if the key has exceeded max_attempts within window_seconds.
        """
        now = time.time()
        cutoff = now - window_seconds

        with self._lock:
            self._prune_key(key, cutoff)

            if len(self._attempts[key]) >= max_attempts:
                self._mutations += 1
                self._maybe_cleanup()
                return True

            # Record this attempt
            self._attempts[key].append(now)
            self._mutations += 1
            self._maybe_cleanup()
            return False

    def peek_rate_limit(self, key: str, max_attempts: int, window_seconds: int) -> tuple[bool, int]:
        """Check rate limit without recording an attempt, AND prune expired entries.

        Returns (is_limited, remaining_attempts).
        """
        now = time.time()
        cutoff = now - window_seconds

        with self._lock:
            # Write-through pruning: remove stale entries from storage
            removed = self._prune_key(key, cutoff)

            entries = self._attempts.get(key, [])
            is_limited = len(entries) >= max_attempts
            remaining = max(0, max_attempts - len(entries))

            if removed > 0:
                self._mutations += 1
                self._maybe_cleanup()
            return is_limited, remaining

    def record_attempt(self, key: str, window_seconds: int = 60) -> None:
        """Record an attempt, pruning expired entries first.

        Args:
            key: The rate-limit key.
            window_seconds: Prune entries older than this many seconds
                            (defaults to 60, matching the standard window).
        """
        now = time.time()

        with self._lock:
            cutoff = now - window_seconds
            self._prune_key(key, cutoff)

            self._attempts[key].append(now)
            self._mutations += 1
            self._maybe_cleanup()

    def reset_attempts(self, key: str) -> None:
        """Clear all recorded attempts for the given key."""
        with self._lock:
            self._attempts.pop(key, None)
            self._mutations += 1

    def reset(self) -> None:
        """Clear ALL rate limit records. Used for testing."""
        with self._lock:
            self._attempts.clear()
            self._mutations = 0

    def cleanup_expired(self, max_age_seconds: int = 3600) -> int:
        """Remove entries older than max_age_seconds.

        Iterates over ALL keys and prunes expired entries. Keys that
        become empty are removed entirely.

        Returns the number of keys that were fully cleaned up.
        """
        now = time.time()
        cutoff = now - max_age_seconds
        cleaned = 0

        with self._lock:
            keys_to_delete = []
            for key, timestamps in list(self._attempts.items()):
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

    def stop(self) -> None:
        """Gracefully shut down (no-op for in-memory implementation)."""
        with self._lock:
            self._attempts.clear()
            self._mutations = 0
            logger.debug("Rate limiter stopped and cleared")

    def mutation_count(self) -> int:
        """Return the total number of mutations handled."""
        with self._lock:
            return self._mutations


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


def reset_rate_limiter() -> None:
    """Replace the global rate limiter with a fresh instance.

    Used during tests to ensure a clean state without relying on
    the test-only reset() method.
    """
    global _rate_limiter
    _rate_limiter = InMemoryRateLimiter()
    logger.debug("Rate limiter replaced with fresh instance")


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
