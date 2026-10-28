"""MP-4C - the EXISTING worker reservation ordering, proven through the REAL gateway.

Drives _mcp_dispatch through app.approval.gateway.dispatch with the real Run /
approval / DispatchRecord rows and a COUNTING fake broker. No real Python
container is launched; assertions are on the real gateway and durable rows.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import update

from app.config import settings
from app.models.models import DispatchRecord, Message, Run, RunPayload, RunStep
from app.run_engine.worker import RunWorker
from mp4c_contract import grant as g
from tests.run_engine_helpers import mk_factory, mk_engine, mk_session
from tests.test_run_engine_mp3c2 import FakeChat, Transport, _decide_run, _run
from tests import test_mp4c_r2a3_python_proposal_worker as r2a3
from tests.test_mp4c_r2b2c2_python_routing import _park_python, _q

GRANT_KEY = "worker-order-grant-key-0123456789"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "mp4c-worker-order-key-012345")
    monkeypatch.setenv("EXECUTOR_SERVICE_GRANT_KEY", GRANT_KEY)
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


class CountingBroker:
    """A counting fake of the executor-service client. Records EVERY call."""

    def __init__(self, *, reserve_ok=True, on_execute=None):
        self.reserve_calls = []
        self.execute_calls = []
        self.release_calls = []
        self.reserve_ok = reserve_ok
        self.on_execute = on_execute

    def reserve(self, *, grant):
        self.reserve_calls.append(grant)
        return SimpleNamespace(ok=self.reserve_ok,
                               status=("executor_reserved" if self.reserve_ok
                                       else "executor_quota_exceeded"))

    def execute(self, *, grant, code, run_id):
        self.execute_calls.append({"grant": grant, "code": code, "run_id": run_id})
        if self.on_execute is not None:
            self.on_execute(grant, code, run_id)
        return SimpleNamespace(ok=True, status="ok", output="TOOL-RESULT-OK")

    def release(self, *, grant):
        self.release_calls.append(grant)
        return SimpleNamespace(ok=True, status="executor_released")


# The AUTHORIZED test seam: a construction-time fake executor. It satisfies the
# operator-gate guard for tests; the broker path then takes over and the fake is
# never actually invoked (asserted by the seam test).
def _fake_executor(code):
    return SimpleNamespace(text="FAKE-NOT-USED", is_error=False)


def _drive(eng, monkeypatch, wid, broker, *, ready=True, executor=_fake_executor,
           gateway_available=True):
    rid, prov, tr = _park_python(eng, monkeypatch)
    if not gateway_available:
        # AFTER parking: the proposal path needs the gateway too.
        monkeypatch.setattr(settings, "approval_gateway_available", False)
    prov.scripts.append(["FINAL-ANSWER-OK"])
    aid = _run(eng, rid).pending_approval_id
    _decide_run(eng, aid, approve=True)
    worker = RunWorker(mk_factory(eng), worker_id=wid,
                       chat_provider=(lambda ci, **_k: prov), mcp_transport=tr,
                       python_executor=executor, python_broker=broker,
                       broker_ready=(lambda: ready))
    for _ in range(8):
        worker.advance_chat_runs()
        if _run(eng, rid).status in ("succeeded", "failed", "outcome_unknown",
                                     "denied"):
            break
    return rid, prov, tr, worker


def _started_count(eng):
    return _q(eng, lambda db: db.query(DispatchRecord)
              .filter(DispatchRecord.outcome == "started").count())


def _assistant_messages(eng):
    return _q(eng, lambda db: [m for m in db.query(Message).all()
                               if str(m.role).endswith("assistant")])


# ── 1. BUSY: refused BEFORE the gateway ─────────────────────────────


def test_busy_reservation_refuses_before_gateway_dispatch(eng, monkeypatch):
    broker = CountingBroker(reserve_ok=False)
    rid, _prov, tr, _w = _drive(eng, monkeypatch, "busy", broker)

    assert len(broker.reserve_calls) == 1, "the pre-dispatch reserve must happen once"
    assert broker.execute_calls == [], "a busy broker must never be asked to execute"
    assert _started_count(eng) == 0, "ZERO DispatchRecord.started rows"
    assert _q(eng, lambda db: db.query(DispatchRecord).count()) == 0
    assert tr.calls == [], "the MCP transport must never be used for a Python step"

    run = _run(eng, rid)
    # The Run state stays `failed` (NO new state); only the bounded reason is now
    # truthful instead of the generic gateway_error.
    assert run.status == "failed", run.status
    assert run.status_reason == "executor_quota_exceeded", run.status_reason
    step = r2a3._tool_step(eng, rid)
    assert step.status == "blocked"
    assert step.error_code == "executor_quota_exceeded", step.error_code
    assert step.error_code == run.status_reason, "Run and step must agree"
    assert _assistant_messages(eng) == [], "no assistant success message"


# ── 2. ACCEPTED: started committed BEFORE the callback ──────────────


def test_accepted_reservation_commits_started_before_the_execution_callback(
        eng, monkeypatch):
    seen = {}

    def at_callback(grant, code, run_id):
        # INDEPENDENT session: what the durable ledger holds RIGHT NOW.
        seen["started"] = _q(eng, lambda db: [
            (r.tool_call_id, r.outcome, r.user_id, r.action_hash)
            for r in db.query(DispatchRecord).all()])
        seen["run_id"] = run_id
        seen["claims"] = g.parse_and_verify(grant, GRANT_KEY.encode(), code=code)
        seen["rows"] = _q(eng, lambda db: [
            (run.id, run.user_id, run.tenant_id) for run in db.query(Run).all()])

    broker = CountingBroker(on_execute=at_callback)
    rid, _prov, _tr, _w = _drive(eng, monkeypatch, "ok", broker)

    assert len(broker.reserve_calls) == 1
    assert len(broker.execute_calls) == 1, "exactly one execution"

    # started is ALREADY committed when the callback runs.
    assert seen.get("started"), "the ledger must hold a row at callback time"
    assert [row[1] for row in seen["started"]] == ["started"]
    assert len(seen["started"]) == 1

    # The grant's identities match the durable rows.
    claims = seen["claims"]
    run_row = _q(eng, lambda db: db.query(Run).filter(Run.id == rid).one())
    step = r2a3._tool_step(eng, rid)
    assert claims.user_id == run_row.user_id == step.user_id
    assert claims.tenant_id == run_row.tenant_id == step.tenant_id
    assert claims.run_id == rid
    assert claims.step_id == step.id
    assert claims.tool_call_id == step.tool_call_id
    assert claims.action_hash == seen["started"][0][3]
    assert claims.code_sha256 == g.code_sha256("print('PY-ROUTE-1')")[0]

    # One sealed tool_output and one same-Run continuation.
    assert _q(eng, lambda db: db.query(RunPayload)
              .filter(RunPayload.payload_type == "tool_output").count()) == 1
    run = _run(eng, rid)
    assert run.status == "succeeded", (run.status, run.status_reason)
    assistants = _assistant_messages(eng)
    assert len(assistants) <= 1
    assert len(assistants) == 1 and "FINAL-ANSWER-OK" in assistants[0].content


# ── 3. GATEWAY REFUSAL AFTER RESERVE: release exactly once ──────────


def test_gateway_refusal_after_reserve_releases_exactly_once(eng, monkeypatch):
    broker = CountingBroker()
    rid, _prov, _tr, _w = _drive(eng, monkeypatch, "deny", broker,
                                 gateway_available=False)

    assert len(broker.reserve_calls) == 1, "capacity is taken before dispatch"
    assert len(broker.release_calls) == 1, "a refused dispatch releases it ONCE"
    assert broker.execute_calls == []
    assert _started_count(eng) == 0
    run = _run(eng, rid)
    assert run.status == "failed" and run.status_reason == "gateway_unavailable", \
        (run.status, run.status_reason)


# ── 4. BROKER UNREADY: no reserve, no dispatch ──────────────────────


def test_broker_unready_has_zero_reserve_and_zero_dispatch(eng, monkeypatch):
    broker = CountingBroker()
    rid, _prov, tr, _w = _drive(eng, monkeypatch, "unready", broker, ready=False)

    assert broker.reserve_calls == [], "an unready broker must not be reserved"
    assert broker.execute_calls == []
    assert _started_count(eng) == 0
    assert tr.calls == []
    run = _run(eng, rid)
    assert run.status == "failed" and run.status_reason == "broker_not_ready"
    assert r2a3._tool_step(eng, rid).error_code == "broker_not_ready"


# ── 5. GATE CONTROL: broker injection alone is NOT the authority ────


def test_broker_injection_alone_does_not_authorize_execution(eng, monkeypatch):
    """No fake executor, no production authorization: the operator gate must still
    refuse, and the broker must never be consulted as the security authority."""
    broker = CountingBroker()
    rid, _prov, _tr, _w = _drive(eng, monkeypatch, "gate", broker,
                                 executor=None)   # NO fake executor seam

    assert broker.reserve_calls == [], "the broker is NOT the authorization"
    assert broker.execute_calls == []
    assert _started_count(eng) == 0
    run = _run(eng, rid)
    assert run.status == "failed"
    assert run.status_reason == "python_host_isolation_unavailable", run.status_reason


def test_the_counting_broker_path_is_reached_with_the_authorized_seam(eng, monkeypatch):
    """Proof the tests above are not vacuous: with the test seam present, the
    broker path IS the one taken (the fake executor is not used)."""
    used = []

    def fake_executor(code):
        used.append(code)
        return SimpleNamespace(text="FAKE", is_error=False)

    broker = CountingBroker()
    rid, _prov, _tr, _w = _drive(eng, monkeypatch, "seam", broker,
                                 executor=fake_executor)

    assert used == [], "the fake executor must NOT be used when a broker is wired"
    assert _run(eng, rid).status == "succeeded"
