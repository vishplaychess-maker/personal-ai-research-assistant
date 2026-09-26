"""One readiness predicate for durable multi-agent routes, capability and worker.

D5C provides the worker and finalizer; deployment flags remain off by default.
"""

from __future__ import annotations

from typing import Optional

from app.config import settings

# CODE-OWNED implementation-readiness boundary — see the module docstring.
# D5C changes this constant (and only this constant); it must never grow a
# config-settable twin.
_MULTI_AGENT_IMPLEMENTATION_READY = True


def multi_agent_executable() -> bool:
    """True only when the implementation and every deployment dependency are ready."""
    if not _MULTI_AGENT_IMPLEMENTATION_READY:
        return False
    if not (
        settings.run_engine_enabled
        and settings.run_worker_enabled
        and settings.run_chat_stream_enabled  # multi-agent reuses the SAME generation bridge
        and settings.run_public_api_enabled
        and settings.run_multi_agent_enabled
    ):
        return False
    try:
        from app.run_engine.submission import _hmac_key  # env-only, no default

        _hmac_key()
    except Exception:  # noqa: BLE001 — any misconfiguration => not executable
        return False
    return True


def multi_agent_unavailable_reason() -> Optional[str]:
    """A bounded, non-secret slug naming WHY multi-agent is unavailable right
    now, or ``None`` when it is executable. Never reveals which specific
    flag/dependency is missing beyond this one fixed slug — matches
    ``deep_research_unavailable_reason``'s exact convention."""
    if multi_agent_executable():
        return None
    return "multi_agent_not_executable"
