"""Phase 1B.2R MP-3C3D4B — activation-boundary tests.

Proves this phase did NOT loosen anything MP-3C3D3A/D3B already locked
down: `deep_research` is still rejected before persistence on both public
durable routes, the settings capability response still reports it
unavailable under durable cutover, and no worker sweep/route/frontend file
exists for it. Reuses the exact hermetic fixtures MP-3C3D3A/D3B's own test
files already established — this file does not re-derive that
infrastructure, only re-confirms it still holds after D4B's additions.
"""
from __future__ import annotations

from app.config import settings


# ── H52-H53: public routes + capability still reject/hide deep_research ──


def test_direct_run_api_still_calls_the_same_unmodified_gate_for_deep_research():
    """`routes/runs.py:135` calls `_ms.unsupported_mode_reason(submission.resolved_mode)`
    as its executability gate before `create_chat_run` — confirmed
    unchanged by source inspection (not merely by re-running D3A's own
    suite, which this file deliberately does not re-implement)."""
    import inspect

    import app.routes.runs as runs_mod
    from app.run_engine import messages_stream as _ms

    source = inspect.getsource(runs_mod)
    assert "unsupported_mode_reason(submission.resolved_mode)" in source
    assert _ms.unsupported_mode_reason("deep_research") is not None


def test_messages_stream_unsupported_mode_reason_still_rejects_deep_research():
    from app.run_engine.messages_stream import _SUPPORTED_DURABLE_MODES, unsupported_mode_reason

    assert "deep_research" not in _SUPPORTED_DURABLE_MODES
    assert unsupported_mode_reason("deep_research") is not None


def test_settings_capability_still_marks_deep_research_unavailable_when_durable_on(monkeypatch):
    from app.routes.settings import _compute_chat_mode_capabilities

    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", True)
    monkeypatch.setattr(settings, "enable_deep_research", True)  # even with the legacy flag ON
    caps = {c.mode: c for c in _compute_chat_mode_capabilities()}
    assert caps["deep_research"].available is False


def test_settings_capability_unaffected_by_d4b_when_durable_off(monkeypatch):
    from app.routes.settings import _compute_chat_mode_capabilities

    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", False)
    monkeypatch.setattr(settings, "enable_deep_research", True)
    caps = {c.mode: c for c in _compute_chat_mode_capabilities()}
    assert caps["deep_research"].available is True  # unchanged legacy behavior, D4B touched nothing here


# ── H54: no worker sweep registered ───────────────────────────


def test_deep_research_worker_sweep_exists_and_self_gates(monkeypatch):
    """MP-3C3D3B's boundary (this file's original name/purpose) was: no
    worker sweep exists AT ALL for deep research. MP-3C3D4C is the
    subsequent, separately-approved mini-phase whose entire job is to add
    exactly that sweep — so the correct D4C-era invariant is no longer "it
    doesn't exist" but "it exists and is a no-op unless every readiness
    condition is true" (proven exhaustively in
    tests/test_deep_research_worker.py: claim isolation, flags-off no-op,
    full happy path, cancellation, citations, crash recovery — not
    duplicated here). This test only re-confirms the self-gating property
    at the cheapest possible level."""
    import app.run_engine.worker as worker_mod

    assert hasattr(worker_mod.RunWorker, "advance_deep_research_runs")

    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "run_deep_research_enabled", False)
    w = worker_mod.RunWorker(lambda: None)  # session factory never called — no-op short-circuits first
    assert w.advance_deep_research_runs() == []


# ── H55: no frontend file changed (structural check: none of D4B's new
#         files live under frontend/) ─────────────────────────


def test_no_new_d4b_file_lives_under_frontend():
    import app.run_engine.research_acquisition as acquisition_mod
    import app.run_engine.research_snapshot as snapshot_mod
    import app.run_engine.research_sources as sources_mod
    import app.tools.research_fetch as fetch_mod
    import app.tools.research_search as search_mod

    for mod in (acquisition_mod, snapshot_mod, sources_mod, fetch_mod, search_mod):
        assert "frontend" not in mod.__file__.replace("\\", "/")


# ── H56: flags remain OFF ─────────────────────────────────────


def test_durable_flags_remain_off_by_default():
    assert settings.run_messages_stream_durable_enabled is False
    assert settings.run_engine_enabled is False
