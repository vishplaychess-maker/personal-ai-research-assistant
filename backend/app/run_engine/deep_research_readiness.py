"""Durable Deep Research — the ONE authoritative executability predicate
(Phase 1B.2R MP-3C3D4C).

Reused, unmodified, by every mode-aware surface: ``/messages/stream``
(via ``messages_stream.unsupported_mode_reason``), the direct Run API
(``routes/runs.py``, via the SAME function), the settings/mode-capability
response (``routes/settings.py``), and the worker sweep gate
(``RunWorker.advance_deep_research_runs``). No second, drifting copy of this
logic exists anywhere.

Deep-research synthesis reuses the SAME generation bridge as durable chat
(``chat_stream.run_stream``, ``default_chat_provider``) — so
``run_chat_stream_enabled`` is a genuine, correct prerequisite here, not an
accidental coupling.
"""

from __future__ import annotations

from typing import Optional

from app.config import settings


def deep_research_executable() -> bool:
    """True iff a durable ``deep_research`` run could actually be claimed
    and finished end-to-end right now. Checked BEFORE persistence by every
    caller — never advertised/accepted/claimed when False."""
    if not (
        settings.run_engine_enabled
        and settings.run_worker_enabled
        and settings.run_chat_stream_enabled
        and settings.run_public_api_enabled
        and settings.run_deep_research_enabled
    ):
        return False
    try:
        from app.run_engine.submission import _hmac_key  # env-only, no default

        _hmac_key()
    except Exception:  # noqa: BLE001 — any misconfiguration ⇒ not executable
        return False
    return True


def deep_research_unavailable_reason() -> Optional[str]:
    """A bounded, non-secret slug naming WHY deep research is unavailable
    right now, or ``None`` when it is executable. Never reveals which
    specific flag/dependency is missing beyond this one fixed slug — matches
    the existing ``mode_unsupported_<slug>`` bounded-reason convention."""
    if deep_research_executable():
        return None
    return "deep_research_not_executable"
