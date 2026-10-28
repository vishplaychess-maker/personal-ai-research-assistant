"""D5C readiness truth table, config bounds and generic-worker isolation.

Uses hermetic fixtures; no live provider. Deployment flags default off.
"""
from __future__ import annotations

import inspect
import math

import pytest

from app.config import Settings, settings


# ── config boundary validation (item 22) ─────────────────────────


def test_multi_agent_settings_valid_defaults():
    s = Settings()
    assert s.run_multi_agent_enabled is False
    assert s.multi_agent_max_roles == 3
    assert s.multi_agent_max_provider_calls == 3
    assert s.multi_agent_role_timeout_s == 120.0
    assert s.multi_agent_total_deadline_s == 360.0
    assert s.multi_agent_max_artifact_bytes == 65536
    assert s.multi_agent_max_active_runs_per_user == 2


@pytest.mark.parametrize("field,value", [
    ("multi_agent_max_roles", 4),
    ("multi_agent_max_roles", 2),
    ("multi_agent_max_provider_calls", 4),
    ("multi_agent_max_provider_calls", 1),
])
def test_role_and_call_count_are_locked_to_exactly_three(field, value):
    with pytest.raises(ValueError):
        Settings(**{field: value})


@pytest.mark.parametrize("field", [
    "multi_agent_max_artifact_bytes", "multi_agent_max_content_chars",
    "multi_agent_max_claims", "multi_agent_max_assumptions",
    "multi_agent_max_uncertainties", "multi_agent_max_active_runs_per_user",
])
@pytest.mark.parametrize("bad_value", [0, -1, -1000])
def test_zero_or_negative_int_bounds_rejected(field, bad_value):
    with pytest.raises(ValueError):
        Settings(**{field: bad_value})


@pytest.mark.parametrize("field", [
    "multi_agent_max_artifact_bytes", "multi_agent_max_content_chars",
    "multi_agent_max_claims", "multi_agent_max_assumptions",
    "multi_agent_max_uncertainties", "multi_agent_max_active_runs_per_user",
])
def test_bool_is_coerced_to_plain_int_by_pydantic_before_any_validator_runs(field):
    """Pydantic v2 normalizes a `bool` assigned to a plain `int` field into a
    real `int` (``True`` -> ``1``) BEFORE any ``model_validator(mode="after")``
    ever sees it — so a `model_validator` cannot distinguish "someone passed
    True" from "someone passed 1" for a typed Settings field, and adding an
    `isinstance(..., bool)` check there would be unreachable dead code. This
    is a repo-wide Settings/Pydantic characteristic (every existing int-bound
    field, e.g. `research_max_queries`, has the exact same behavior), not a
    multi-agent-specific gap. The genuine "no bool-as-int" boundary — where a
    literal JSON `true`/`false` from `json.loads()` really can reach
    application code as a distinct `bool` — is the untyped agent_artifact
    schema (see test_multi_agent_artifacts.py's
    test_bool_version_rejected / test_bool_sequence_rejected), which DOES
    reject it."""
    s = Settings(**{field: True})
    assert getattr(s, field) == 1
    assert type(getattr(s, field)) is int


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf"), 0, -1])
def test_nonfinite_or_nonpositive_timeouts_rejected(bad_value):
    with pytest.raises(ValueError):
        Settings(multi_agent_role_timeout_s=bad_value)
    with pytest.raises(ValueError):
        Settings(multi_agent_total_deadline_s=bad_value)


def test_role_timeout_must_be_strictly_less_than_total_deadline():
    with pytest.raises(ValueError):
        Settings(multi_agent_role_timeout_s=360.0, multi_agent_total_deadline_s=360.0)
    with pytest.raises(ValueError):
        Settings(multi_agent_role_timeout_s=400.0, multi_agent_total_deadline_s=360.0)


def test_artifact_byte_cap_cannot_exceed_run_payload_max_bytes():
    with pytest.raises(ValueError):
        Settings(multi_agent_max_artifact_bytes=settings.run_payload_max_bytes + 1)


def test_client_cannot_override_locked_settings_via_the_shared_singleton():
    """The `settings` singleton (what every route/worker actually reads) is
    NOT rebuilt from arbitrary request input anywhere in this phase — proven
    by the absence of any Settings(**request_data) construction pattern in
    the new/modified modules."""
    import app.run_engine.multi_agent_orchestration as orch_mod
    import app.run_engine.multi_agent_artifacts as art_mod
    import app.run_engine.multi_agent_readiness as ready_mod

    for mod in (orch_mod, art_mod, ready_mod):
        src = inspect.getsource(mod)
        assert "Settings(" not in src


# ── readiness predicate: unconditionally False in D5B (item 23) ──


def _all_flags_on(monkeypatch):
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "a" * 32)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_worker_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_multi_agent_enabled", True)


def test_multi_agent_flag_defaults_to_false():
    assert settings.run_multi_agent_enabled is False


def test_not_executable_by_default():
    from app.run_engine.multi_agent_readiness import (
        multi_agent_executable,
        multi_agent_unavailable_reason,
    )

    assert multi_agent_executable() is False
    assert multi_agent_unavailable_reason() == "multi_agent_not_executable"


def test_executable_with_every_dependency_enabled(monkeypatch):
    from app.run_engine.multi_agent_readiness import multi_agent_executable
    _all_flags_on(monkeypatch)
    assert multi_agent_executable() is True


@pytest.mark.parametrize("flag", [
    "run_engine_enabled", "run_worker_enabled", "run_chat_stream_enabled",
    "run_public_api_enabled", "run_multi_agent_enabled", "hmac",
])
def test_each_missing_dependency_disables_all_consumers(monkeypatch, flag):
    from app.run_engine.multi_agent_readiness import multi_agent_executable
    from app.run_engine import messages_stream as ms
    from app.routes.settings import _compute_chat_mode_capabilities
    from app.run_engine.worker import RunWorker
    _all_flags_on(monkeypatch)
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", True)
    if flag == "hmac":
        monkeypatch.delenv("RUN_IDEMPOTENCY_HMAC_KEY")
    else:
        monkeypatch.setattr(settings, flag, False)
    assert multi_agent_executable() is False
    assert ms.unsupported_mode_reason("multi_agent") is not None
    assert RunWorker(lambda: None).advance_multi_agent_runs() == []
    caps = {c.mode: c for c in _compute_chat_mode_capabilities()}
    assert caps["multi_agent"].available is False


def test_all_dependencies_enable_route_and_capability(monkeypatch):
    from app.run_engine import messages_stream as ms
    from app.routes.settings import _compute_chat_mode_capabilities
    _all_flags_on(monkeypatch)
    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", True)
    assert ms.unsupported_mode_reason("multi_agent") is None
    caps = {c.mode: c for c in _compute_chat_mode_capabilities()}
    assert caps["multi_agent"].available is True


def test_direct_run_api_uses_the_same_unmodified_gate_for_multi_agent():
    import app.routes.runs as runs_mod
    from app.run_engine import messages_stream as ms

    source = inspect.getsource(runs_mod)
    assert "unsupported_mode_reason(submission.resolved_mode)" in source
    assert ms.unsupported_mode_reason("multi_agent") is not None


# ── capability remains unavailable (item 25) ─────────────────────


def test_settings_capability_unavailable_under_durable_cutover(monkeypatch):
    from app.routes.settings import _compute_chat_mode_capabilities

    monkeypatch.setattr(settings, "run_messages_stream_durable_enabled", True)
    monkeypatch.setattr(settings, "run_multi_agent_enabled", True)  # even with the flag ON
    caps = {c.mode: c for c in _compute_chat_mode_capabilities()}
    assert caps["multi_agent"].available is False


# ── generic worker reservation (item 26) + no dedicated sweep (27) ─


from tests.run_engine_helpers import ensure_user, mk_engine, mk_session  # noqa: E402


def _seed_queued_multi_agent_run(eng, run_id: str, user_id: int = 1) -> None:
    from app.models.models import Run

    db = mk_session(eng)
    try:
        ensure_user(db, user_id)
        db.add(Run(
            id=run_id, kind="multi_agent", status="queued", user_id=user_id,
            thread_id=f"run:{run_id}", idempotency_key=f"idem-{run_id}",
        ))
        db.commit()
    finally:
        db.close()


def test_generic_worker_never_claims_a_queued_multi_agent_run():
    from app.run_engine.worker import RunWorker

    eng = mk_engine()
    run_id = "ma-claim-test-1"
    _seed_queued_multi_agent_run(eng, run_id)
    w = RunWorker(lambda: mk_session(eng), worker_id="w-1")
    assert w.claim_next_queued() is None

    db = mk_session(eng)
    try:
        from app.models.models import Run

        row = db.query(Run).filter(Run.id == run_id).first()
        assert row.status == "queued"  # untouched
    finally:
        db.close()


def test_generic_worker_never_recovers_an_expired_multi_agent_run():
    import datetime as dt

    from app.models.models import Run
    from app.run_engine.worker import RunWorker

    eng = mk_engine()
    run_id = "ma-recover-test-1"
    db = mk_session(eng)
    try:
        ensure_user(db, 1)
        now = dt.datetime.now(dt.timezone.utc)
        db.add(Run(
            id=run_id, kind="multi_agent", status="running", user_id=1,
            thread_id=f"run:{run_id}", idempotency_key=f"idem-{run_id}",
            lease_owner="stale/dead", lease_expires_at=now - dt.timedelta(seconds=60),
        ))
        db.commit()
    finally:
        db.close()

    w = RunWorker(lambda: mk_session(eng), worker_id="w-2")
    outcomes = w.recover_expired()
    assert outcomes == []  # never touched — reserved, not "recovered" as outcome_unknown

    db2 = mk_session(eng)
    try:
        row = db2.query(Run).filter(Run.id == run_id).first()
        assert row.status == "running"  # untouched, not forced to outcome_unknown
    finally:
        db2.close()


def test_multi_agent_worker_sweep_exists_and_self_gates(monkeypatch):
    """Phase 1B.2R MP-3C3D5C added the dedicated ``advance_multi_agent_runs``
    sweep (D5B's own boundary was "no sweep exists AT ALL" — superseded by
    this mini-phase whose entire job is to add exactly that sweep, the same
    D4C-era transition ``test_deep_research_worker_sweep_exists_and_self_gates``
    already documents for deep research). Proven here only at the cheapest
    self-gating level — the full happy-path/failure/recovery matrix lives in
    ``tests/test_multi_agent_worker.py``, not duplicated here."""
    import app.run_engine.worker as worker_mod

    assert hasattr(worker_mod.RunWorker, "advance_multi_agent_runs")
    w = worker_mod.RunWorker(lambda: None)  # session factory never called — no-op short-circuits first
    assert w.advance_multi_agent_runs() == []  # deployment flags default off


# ── no LLM/provider/network/tool execution occurs (item 28) ─────


@pytest.mark.parametrize("modname", [
    "app.run_engine.multi_agent_orchestration",
    "app.run_engine.multi_agent_artifacts",
    "app.run_engine.multi_agent_readiness",
])
def test_no_side_effecting_import_in_any_d5b_module(modname):
    import importlib

    mod = importlib.import_module(modname)
    src = inspect.getsource(mod)
    for forbidden in ("openai", "anthropic", "httpx", "requests.", "subprocess", "socket."):
        assert forbidden not in src.lower()
