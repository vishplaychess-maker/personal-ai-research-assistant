"""Injectable clock for the approval domain.

Existing models use naive ``datetime.datetime.utcnow``; the approval domain
deliberately uses **timezone-aware UTC** everywhere (spec 1B.1 sec.5/6) so
expiry comparisons are unambiguous, and takes its clock as a parameter so
tests are deterministic. The server — never the client — decides "now".
"""

from __future__ import annotations

import datetime as _dt
from typing import Protocol


class Clock(Protocol):
    def now(self) -> _dt.datetime:  # tz-aware UTC
        ...


class SystemClock:
    """Real wall clock, tz-aware UTC."""

    def now(self) -> _dt.datetime:
        return _dt.datetime.now(_dt.timezone.utc)


class FixedClock:
    """Deterministic clock for tests. ``advance()`` moves it forward."""

    def __init__(self, start: _dt.datetime | None = None) -> None:
        if start is None:
            start = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
        if start.tzinfo is None:
            raise ValueError("FixedClock requires a tz-aware datetime")
        self._now = start.astimezone(_dt.timezone.utc)

    def now(self) -> _dt.datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + _dt.timedelta(seconds=seconds)


_DEFAULT = SystemClock()


def now() -> _dt.datetime:
    """Module-level convenience: tz-aware UTC now from the default system clock."""
    return _DEFAULT.now()


def ensure_utc(value: _dt.datetime) -> _dt.datetime:
    """Coerce a datetime to tz-aware UTC. A naive datetime is assumed to be UTC
    (that is how every existing column in this repo is written)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=_dt.timezone.utc)
    return value.astimezone(_dt.timezone.utc)
