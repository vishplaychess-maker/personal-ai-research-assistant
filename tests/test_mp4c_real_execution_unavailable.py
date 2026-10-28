"""MP-4C policy — REAL model-generated Python execution is UNAVAILABLE.

Decision: keep real execution unavailable until a host-isolated runtime that
demonstrably prevents host filesystem AND network access exists. The hardened
subprocess provides sanitized env / byte+output bounds / wall-clock timeout /
tree-kill / temp cwd — NOT host isolation and NO setrlimit memory/CPU cap.

These tests pin the refusal at every production entry: the durable proposal
path, the durable dispatch path, and every public real-executor wrapper. The
test-only bypass is a `python_executor` supplied AT RunWorker CONSTRUCTION;
production (worker_supervisor) never supplies one.
"""
from __future__ import annotations

import asyncio
import ast
import glob
import os
import pathlib
import tempfile

import pytest

from app.config import settings
from app.models.models import DispatchRecord, PendingApproval
from app.run_engine import sandbox_policy
from app.run_engine.worker import RunWorker
from tests import test_mp4c_r2a3_python_proposal_worker as r2a3
from tests.run_engine_helpers import mk_engine, mk_factory, mk_session
from tests.test_run_engine_mp3c2 import _decide_run

REPO = pathlib.Path(__file__).resolve().parents[1]
REASON = "python_host_isolation_unavailable"
SUPERVISOR = REPO / "backend" / "app" / "run_engine" / "worker_supervisor.py"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "mp4c-real-exec-unavailable-key1")
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 30)
    monkeypatch.setattr(settings, "run_worker_handler_timeout_s", 300)
    monkeypatch.setattr(settings, "run_chat_max_tool_cycles", 3)
    monkeypatch.setattr(settings, "run_chat_tool_marker_max_bytes", 4096)
    monkeypatch.setattr(settings, "run_engine_sandbox", False)


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


def _started(eng) -> int:
    db = mk_session(eng)
    try:
        return db.query(DispatchRecord).filter(
            DispatchRecord.outcome == "started").count()
    finally:
        db.close()


def _approvals(eng) -> int:
    db = mk_session(eng)
    try:
        return db.query(PendingApproval).count()
    finally:
        db.close()


def _prod_worker(eng, prov, wid):
    """A NORMAL production-shaped worker: no python_executor injected."""
    return RunWorker(mk_factory(eng), worker_id=wid,
                     chat_provider=(lambda ci, **_k: prov))


# ── 0: the ONE readiness predicate ──────────────────────────────────


def test_no_host_isolated_runtime_is_available():
    assert sandbox_policy.real_python_execution_available() is False
    assert sandbox_policy.REAL_EXECUTION_UNAVAILABLE_REASON == REASON


# ── 1: flag True + no isolated runtime ⇒ NO proposal/approval created ──


def test_flag_on_production_worker_creates_no_python_proposal(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    prov = r2a3.FakeChat([r2a3._py_script("", "print('NOPE')")])
    rid = r2a3._seed(eng)

    _prod_worker(eng, prov, "prod-nofake").advance_chat_runs()

    run = r2a3._run(eng, rid)
    assert run.status == "failed", (run.status, run.status_reason)
    assert run.status_reason == REASON, run.status_reason
    assert run.pending_approval_id is None, "no approval may be created"
    assert _approvals(eng) == 0, "no PendingApproval may be created"


# ── 2: an EXISTING waiting approval never executes / never starts ────


def test_existing_waiting_approval_never_executes_or_starts_dispatch(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    prov = r2a3.FakeChat([r2a3._py_script("", "print('PARKED')")])
    rid = r2a3._seed(eng)
    # park using the constructor-injected fake (isolated approval-flow setup)
    r2a3._worker(eng, prov).advance_chat_runs()
    parked = r2a3._run(eng, rid)
    assert parked.status == "waiting_for_approval" and parked.pending_approval_id
    aid = parked.pending_approval_id

    _decide_run(eng, aid, approve=True)          # a genuine approved decision

    # a PRODUCTION worker (no injected fake) must refuse before the gateway
    _prod_worker(eng, prov, "prod-nofake-2").advance_chat_runs()

    assert _started(eng) == 0, "DispatchRecord.started must never be written"
    run = r2a3._run(eng, rid)
    assert run.status == "failed", (run.status, run.status_reason)
    assert run.status_reason == REASON, run.status_reason
    # The refusal deliberately does NOT invent an approval-state policy: the row
    # is left as the decision left it (never consumed, never executed).
    db = mk_session(eng)
    try:
        row = db.query(PendingApproval).filter(PendingApproval.id == aid).one()
        assert row.state == "approved" and row.consumed_at is None
    finally:
        db.close()


# ── 3: EVERY public real-executor wrapper refuses before any spawn ───


def test_public_executor_wrappers_refuse_before_spawning(monkeypatch):
    import app.tools.python_sandbox as ps

    launched = []

    async def _boom(*a, **k):          # pragma: no cover - only on violation
        launched.append(a)
        raise AssertionError("a subprocess was launched")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "thunder_sbx_*")))

    typed = asyncio.run(ps.run_python_code_outcome_async("print(1)"))
    assert typed.is_error is True and ps.RESULT_EXECUTION_UNAVAILABLE in typed.text
    assert ps.RESULT_EXECUTION_UNAVAILABLE in ps.run_python_code("print(1)")
    assert ps.RESULT_EXECUTION_UNAVAILABLE in asyncio.run(
        ps.run_python_code_async("print(1)"))
    sync = ps.run_python_code_outcome_sync("print(1)")
    assert sync.is_error is True and ps.RESULT_EXECUTION_UNAVAILABLE in sync.text
    tool_out = ps.execute_python_code.invoke({"code": "print(1)"})
    assert ps.RESULT_EXECUTION_UNAVAILABLE in str(tool_out)

    assert launched == [], "no public wrapper may launch a subprocess"
    after = set(glob.glob(os.path.join(tempfile.gettempdir(), "thunder_sbx_*")))
    assert after == before, "no temp execution root may be created"


# ── 4: the constructor-injected fake keeps the approve/deny flow ─────


def test_constructor_injected_fake_still_dispatches_the_approved_action(eng, monkeypatch):
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    calls = []
    started_at_call = []

    def fake(code):
        calls.append(code)
        # observed AT CALL TIME: the gateway commits started BEFORE the callback
        # (and transitions it to succeeded once the callback returns).
        started_at_call.append(_started(eng))
        return r2a3._FakeOutcome()

    prov = r2a3.FakeChat([r2a3._py_script("", "print('SEAM')")])
    rid = r2a3._seed(eng)
    r2a3._worker(eng, prov, python_executor=fake).advance_chat_runs()
    aid = r2a3._run(eng, rid).pending_approval_id
    _decide_run(eng, aid, approve=True)

    w = RunWorker(mk_factory(eng), worker_id="seam",
                  chat_provider=(lambda ci, **_k: prov), python_executor=fake)
    w.advance_chat_runs()

    assert calls == ["print('SEAM')"], calls
    assert started_at_call == [1], "started must be committed before the callback"


def test_a_later_assignment_does_not_bypass_the_refusal(eng, monkeypatch):
    """The bypass is CONSTRUCTION-time only; post-hoc assignment must not work."""
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    prov = r2a3.FakeChat([r2a3._py_script("", "print('LATE')")])
    rid = r2a3._seed(eng)
    w = _prod_worker(eng, prov, "late")
    w._python_executor = lambda c: r2a3._FakeOutcome()   # post-construction
    w.advance_chat_runs()
    run = r2a3._run(eng, rid)
    assert run.status == "failed" and run.status_reason == REASON
    assert _approvals(eng) == 0


# ── 5: production never injects the fake ─────────────────────────────


def test_production_supervisor_never_injects_a_python_executor():
    src = SUPERVISOR.read_text(encoding="utf-8")
    assert "python_executor" not in src, "production must never inject the seam"
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "RunWorker"]
    assert calls, "expected the supervisor to construct RunWorker"
    for c in calls:
        assert all(k.arg != "python_executor" for k in c.keywords), ast.unparse(c)
