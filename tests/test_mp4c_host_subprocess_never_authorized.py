"""MP-4C SAFETY GATE - the unisolated HOST subprocess must be PERMANENTLY unreachable.

The operator opt-in (EXECUTOR_OPERATOR_OPT_IN) makes
`sandbox_policy.real_python_execution_available()` able to return True. Before
that flip is safe, this file pins the invariant the slice depends on:

  * `app.tools.python_sandbox._run_python_async` NEVER consults the authority and
    NEVER reaches `_run_python_unisolated` - the refusal is unconditional, so no
    flag combination, env var or monkeypatched predicate can unlock the old
    bounded-but-NOT-host-isolated launcher;
  * the durable dispatch path refuses with ZERO DispatchRecord.started rows when
    no broker client is wired, even while the authority reports True;
  * the durable PROPOSAL path refuses before creating any PendingApproval for the
    same reason.

Only the approved durable broker route may execute real model code.
"""
from __future__ import annotations

import ast
import asyncio
import glob
import inspect
import os
import pathlib
import re
import tempfile
import textwrap

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import DispatchRecord, PendingApproval
from app.run_engine import sandbox_policy
from app.run_engine.worker import RunWorker
from tests import test_mp4c_r2a3_python_proposal_worker as r2a3
from tests.run_engine_helpers import mk_engine, mk_factory, mk_session
from tests.test_run_engine_mp3c2 import _decide_run
from tests.test_mp4c_r2b2c2_python_routing import _q

NL = chr(10)
REPO = pathlib.Path(__file__).resolve().parents[1]
APP = REPO / "backend" / "app"
REASON = "python_host_isolation_unavailable"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "mp4c-host-never-key-0123")
    monkeypatch.setenv("EXECUTOR_SERVICE_GRANT_KEY", "host-never-grant-key-0123456")
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 30)
    monkeypatch.setattr(settings, "run_worker_handler_timeout_s", 300)
    monkeypatch.setattr(settings, "run_chat_max_tool_cycles", 3)
    monkeypatch.setattr(settings, "run_chat_tool_marker_max_bytes", 4096)
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    monkeypatch.delenv("EXECUTOR_OPERATOR_OPT_IN", raising=False)
    monkeypatch.delenv("EXECUTOR_SERVICE_URL", raising=False)


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


def _started(eng) -> int:
    return _q(eng, lambda db: db.query(DispatchRecord)
              .filter(DispatchRecord.outcome == "started").count())


def _approvals(eng) -> int:
    return _q(eng, lambda db: db.query(PendingApproval).count())


def _force_authority_true(monkeypatch):
    """Make EVERY authority signal report True, including a registered probe."""
    monkeypatch.setenv("EXECUTOR_OPERATOR_OPT_IN", "1")
    monkeypatch.setenv("EXECUTOR_SERVICE_URL", "http://executor:8091")
    monkeypatch.setattr(sandbox_policy, "real_python_execution_available",
                        lambda: True)


def _record_unisolated(monkeypatch):
    """Record (and forbid) every attempt to reach the host launcher."""
    import app.tools.python_sandbox as ps
    seen = []

    async def _never(code):
        seen.append(code)
        raise AssertionError("the unisolated host launcher was reached")
    monkeypatch.setattr(ps, "_run_python_unisolated", _never)
    return seen


# ── 1. the source itself: the refusal cannot consult the authority ──


def test_run_python_async_is_structurally_unconditional():
    """The refusal is unconditional: it may name neither the authority nor the
    unisolated launcher. Any future drift here re-opens the hole."""
    import app.tools.python_sandbox as ps
    fn = ast.parse(textwrap.dedent(inspect.getsource(ps._run_python_async))).body[0]
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value,
                                                          ast.Constant):
        body = body[1:]                      # drop the docstring
    code = NL.join(ast.unparse(node) for node in body)
    assert code.strip(), "the refusal body must not be empty"
    assert "real_python_execution_available" not in code, code
    assert "_run_python_unisolated" not in code, code


def test_unisolated_launcher_is_referenced_only_by_its_own_module():
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name == "python_sandbox.py":
            continue
        if re.search(r"_run_python_unisolated", path.read_text(encoding="utf-8")):
            offenders.append(str(path))
    assert offenders == [], offenders


# ── 2. every public wrapper refuses with the authority forced True ──


def test_public_wrappers_refuse_with_the_authority_forced_true(monkeypatch):
    import app.tools.python_sandbox as ps
    _force_authority_true(monkeypatch)
    assert sandbox_policy.real_python_execution_available() is True
    seen = _record_unisolated(monkeypatch)
    def _boom(*a, **k):
        raise AssertionError("a subprocess was launched")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "thunder_sbx_*")))

    typed = asyncio.run(ps.run_python_code_outcome_async("print(1)"))
    assert typed.is_error is True
    assert ps.RESULT_EXECUTION_UNAVAILABLE in typed.text, typed.text
    assert ps.RESULT_EXECUTION_UNAVAILABLE in ps.run_python_code("print(1)")
    sync = ps.run_python_code_outcome_sync("print(1)")
    assert sync.is_error is True
    assert ps.RESULT_EXECUTION_UNAVAILABLE in sync.text, sync.text
    assert ps.RESULT_EXECUTION_UNAVAILABLE in str(
        ps.execute_python_code.invoke({"code": "print(1)"}))

    assert seen == [], "the host launcher must never be reached"
    after = set(glob.glob(os.path.join(tempfile.gettempdir(), "thunder_sbx_*")))
    assert after == before, "no temp execution root may be created"


def test_unisolated_launcher_still_works_for_the_bounds_suite(monkeypatch):
    """The test-only boundary stays usable - the fix is not a blanket break."""
    import app.tools.python_sandbox as ps
    out = asyncio.run(ps._run_python_unisolated("print('BOUNDS-OK')"))
    assert out.is_error is False and "BOUNDS-OK" in out.text


# ── 3. the durable DISPATCH path: authority True is still not enough ──


def _prod_worker(eng, prov, wid):
    return RunWorker(mk_factory(eng), worker_id=wid,
                     chat_provider=(lambda ci, **_k: prov))


def test_durable_dispatch_without_a_broker_never_starts_and_never_launches(
        eng, monkeypatch):
    prov = r2a3.FakeChat([r2a3._py_script("", "print('PARKED')")])
    rid = r2a3._seed(eng)
    r2a3._worker(eng, prov).advance_chat_runs()
    parked = r2a3._run(eng, rid)
    assert parked.status == "waiting_for_approval" and parked.pending_approval_id
    _decide_run(eng, parked.pending_approval_id, approve=True)

    _force_authority_true(monkeypatch)
    seen = _record_unisolated(monkeypatch)
    _prod_worker(eng, prov, "authority-true-no-broker").advance_chat_runs()

    assert seen == [], "the durable path must not reach the host launcher"
    assert _started(eng) == 0, "no DispatchRecord.started may be written"
    run = r2a3._run(eng, rid)
    assert run.status == "failed", (run.status, run.status_reason)
    assert run.status_reason == REASON, run.status_reason


def test_durable_proposal_without_a_broker_creates_no_approval(eng, monkeypatch):
    prov = r2a3.FakeChat([r2a3._py_script("", "print('NOPE')")])
    rid = r2a3._seed(eng)
    _force_authority_true(monkeypatch)
    seen = _record_unisolated(monkeypatch)

    _prod_worker(eng, prov, "authority-true-proposal").advance_chat_runs()

    assert seen == []
    assert _approvals(eng) == 0, "no approval may be created without a broker"
    run = r2a3._run(eng, rid)
    assert run.status == "failed", (run.status, run.status_reason)
    assert run.status_reason == REASON, run.status_reason


def test_broker_injection_is_still_not_the_authority(eng, monkeypatch):
    """The inverse: a broker alone (authority False) authorizes nothing."""
    prov = r2a3.FakeChat([r2a3._py_script("", "print('X')")])
    rid = r2a3._seed(eng)

    class _B:
        def __init__(self):
            self.calls = []
        def reserve(self, *, grant):
            self.calls.append(grant)
            return r2a3._FakeOutcome()
        def execute(self, *, grant, code, run_id):
            raise AssertionError("must not execute")
        def release(self, *, grant):
            return None

    b = _B()
    w = RunWorker(mk_factory(eng), worker_id="broker-only",
                  chat_provider=(lambda ci, **_k: prov), python_broker=b,
                  broker_ready=(lambda: True))
    w.advance_chat_runs()
    assert b.calls == []
    assert _started(eng) == 0
    run = r2a3._run(eng, rid)
    assert run.status == "failed" and run.status_reason == REASON, run.status_reason

