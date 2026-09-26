"""Bounded provider-failure classification.

A classification carries NO upstream text, status line, header, credential or
body fragment - only a fixed slug. Anything not classified here stays generic.
"""
from __future__ import annotations

PROVIDER_RATE_LIMITED = "provider_rate_limited"


class ProviderRateLimited(Exception):
    """The upstream provider answered HTTP 429 (rate limited).

    Raised INSTEAD of the generic error so the durable path can classify the
    failure without ever surfacing the upstream response text.
    """

    code = PROVIDER_RATE_LIMITED


__all__ = ["ProviderRateLimited", "PROVIDER_RATE_LIMITED"]