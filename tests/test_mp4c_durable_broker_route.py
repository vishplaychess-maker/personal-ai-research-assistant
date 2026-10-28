"""MP-4C - the durable broker route proven through the REAL gateway and rows.

These tests drive `RunWorker.advance_chat_runs()` with the production broker
client (its transport is a recording opener, so no socket is opened) over the
REAL approval gateway, the REAL Run/RunStep/ToolCall/DispatchRecord rows and the
REAL sealed payloads. They pin:

  * a broker OUTAGE refuses before dispatch - zero DispatchRecord.started rows,
    zero reserve calls, no MCP transport, no assistant success message;
  * ordinary chat still completes while the broker is down;
  * a tampered args_hash and a re-swapped sealed payload both fail closed;
  * a reservation is released exactly once when the gateway refuses.
"""
from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import update

from app.config import settings
from app.models.models import DispatchRecord, Message, Run, RunPayload, RunStep
from app.run_engine import executor_broker as eb
from app.run_engine import sandbox_policy
from app.run_engine.payloads import seal_payload
from app.run_engine.worker import RunWorker
from tests import test_mp4c_r2a3_python_proposal_worker as r2a3
from tests.run_engine_helpers import mk_factory, mk_engine
from tests.test_run_engine_mp3c2 import _decide_run
from tests.test_mp4c_r2b2c2_python_routing import _park_python, _q

URL = "http://executor:8091"
KEY = "durable-broker-route-key-0123456789"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", "mp4c-durable-broker-key1")
    monkeypatch.setenv("EXECUTOR_SERVICE_GRANT_KEY", KEY)
    monkeypatch.setenv("EXECUTOR_SERVICE_URL", URL)
    monkeypatch.setenv("EXECUTOR_OPERATOR_OPT_IN", "1")
    monkeypatch.setattr(settings, "run_public_api_enabled", True)
    monkeypatch.setattr(settings, "run_engine_enabled", True)
    monkeypatch.setattr(settings, "run_chat_stream_enabled", True)
    monkeypatch.setattr(settings, "run_worker_lease_ttl_s", 30)
    monkeypatch.setattr(settings, "run_worker_handler_timeout_s", 300)
    monkeypatch.setattr(settings, "run_chat_max_tool_cycles", 3)
    monkeypatch.setattr(settings, "run_chat_tool_marker_max_bytes", 4096)
    monkeypatch.setattr(settings, "run_engine_sandbox", True)
    sandbox_policy.reset_executor_readiness_probe()
    yield
    sandbox_policy.reset_executor_readiness_probe()


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


class Recorder:
    def __init__(self, *responses):
        self.calls = []
        self.responses = list(responses)

    def __call__(self, method, url, data, timeout):
        self.calls.append((method, url, data))
        if not self.responses:
            raise RuntimeError("executor is down")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _ready_recorder(*extra):
    """TWO health responses: the explicit broker check AND the authority probe."""
    return Recorder((200, b'{"ready": true}'), (200, b'{"ready": true}'), *extra)


def _prod_worker(eng, prov, wid, *, rec, **kw):
    """The PRODUCTION shape: a real broker client, never the test seam."""
    import unittest.mock as _mock
    with _mock.patch.object(eb, "_http_opener", rec):
        client = eb.wire_from_env()
    assert client is not None
    return RunWorker(mk_factory(eng), worker_id=wid,
                     chat_provider=(lambda ci, **_k: prov), python_broker=client,
                     broker_ready=client.ready, **kw)


def _drive_to_terminal(worker, eng, rid, limit=8):
    for _ in range(limit):
        worker.advance_chat_runs()
        if r2a3._run(eng, rid).status in ("succeeded", "failed", "denied",
                                          "outcome_unknown"):
            break
    return r2a3._run(eng, rid)


def _started(eng) -> int:
    return _q(eng, lambda db: db.query(DispatchRecord)
              .filter(DispatchRecord.outcome == "started").count())


def _assistants(eng):
    return _q(eng, lambda db: [m for m in db.query(Message).all()
                               if str(m.role).endswith("assistant")])


def _park_and_approve(eng, monkeypatch, *, code="print('PY-ROUTE-1')"):
    rid, prov, tr = _park_python(eng, monkeypatch, code=code)
    aid = r2a3._run(eng, rid).pending_approval_id
    _decide_run(eng, aid, approve=True)
    return rid, prov, tr


# ── 1. broker outage: refused before dispatch, nothing durable moves ──


def test_broker_outage_refuses_before_dispatch(eng, monkeypatch):
    rid, prov, tr = _park_and_approve(eng, monkeypatch)
    rec = Recorder()                       # every call raises: the executor is down
    worker = _prod_worker(eng, prov, "outage", rec=rec)
    run = _drive_to_terminal(worker, eng, rid)

    assert [c[1] for c in rec.calls] == [URL + "/healthz"], \
        "an outage must not proceed past the readiness probe"
    assert _started(eng) == 0, "ZERO DispatchRecord.started rows"
    assert _q(eng, lambda db: db.query(DispatchRecord).count()) == 0
    assert tr.calls == [], "the MCP transport is never used for Python"
    assert run.status == "failed", (run.status, run.status_reason)
    assert run.status_reason == "broker_not_ready", run.status_reason
    step = r2a3._tool_step(eng, rid)
    assert step.status == "blocked" and step.error_code == "broker_not_ready"
    assert _assistants(eng) == [], "no assistant success message"


def test_ordinary_chat_completes_while_the_broker_is_down(eng, monkeypatch):
    prov = r2a3.FakeChat([["HELLO-WITHOUT-EXECUTOR"]])
    rid = r2a3._seed(eng)
    rec = Recorder()
    worker = _prod_worker(eng, prov, "chat-down", rec=rec)
    run = _drive_to_terminal(worker, eng, rid)

    assert run.status == "succeeded", (run.status, run.status_reason)
    assistants = _assistants(eng)
    assert len(assistants) == 1 and "HELLO-WITHOUT-EXECUTOR" in assistants[0].content
    assert _started(eng) == 0


# ── 2. the reopened sealed code must match its recorded hashes ────────


def _tool_step_row(eng, rid):
    return r2a3._tool_step(eng, rid)


def test_a_tampered_input_ref_args_hash_fails_closed(eng, monkeypatch):
    rid, prov, _tr = _park_and_approve(eng, monkeypatch)
    step = _tool_step_row(eng, rid)
    ref = json.loads(step.input_ref)
    ref["args_hash"] = "0" * 64
    _q(eng, lambda db: (db.execute(update(RunStep).where(RunStep.id == step.id)
                                   .values(input_ref=json.dumps(ref))),
                        db.commit()))
    rec = _ready_recorder()
    worker = _prod_worker(eng, prov, "tampered-hash", rec=rec)
    run = _drive_to_terminal(worker, eng, rid)

    assert run.status == "failed" and run.status_reason == "gateway_error", \
        (run.status, run.status_reason)
    assert _started(eng) == 0
    assert [c[1] for c in rec.calls] == [URL + "/healthz"] * 2, \
        "no reservation may be attempted for a mismatched action"


def test_a_reswapped_sealed_payload_fails_closed(eng, monkeypatch):
    rid, prov, _tr = _park_and_approve(eng, monkeypatch)
    step = _tool_step_row(eng, rid)
    run_row = _q(eng, lambda db: db.query(Run).filter(Run.id == rid).one())
    from app.run_engine import chat_tools

    def _reswap(db):
        other = seal_payload(
            db, tenant_id=run_row.tenant_id, user_id=run_row.user_id,
            run_id=run_row.id, step_id=step.id,
            payload_type=chat_tools.TOOL_ARGS_PAYLOAD_TYPE,
            plaintext=chat_tools.normalized_args_json(
                {"code": "print('ATTACKER-CODE')"}), commit=True)
        db.execute(update(RunStep).where(RunStep.id == step.id)
                   .values(input_payload_id=other.id))
        db.commit()
    _q(eng, _reswap)

    rec = _ready_recorder()
    worker = _prod_worker(eng, prov, "reswapped", rec=rec)
    run = _drive_to_terminal(worker, eng, rid)

    assert run.status == "failed" and run.status_reason == "gateway_error", \
        (run.status, run.status_reason)
    assert _started(eng) == 0
    assert [c[1] for c in rec.calls] == [URL + "/healthz"] * 2
    outputs = _q(eng, lambda db: db.query(RunPayload)
                 .filter(RunPayload.payload_type == "tool_output").count())
    assert outputs == 0, "no tool output may be sealed for a refused action"


# ── 3. release on refusal ────────────────────────────────────────────


def test_a_refused_dispatch_releases_the_reservation_exactly_once(eng, monkeypatch):
    """Through the REAL broker client: reserve -> gateway refusal -> release ONCE."""
    rec = _ready_recorder(
        (200, b'{"ok": true, "status": "executor_reserved"}'),
        (200, b'{"ok": true, "status": "executor_released"}'),
    )
    rid, prov, _tr = _park_and_approve(eng, monkeypatch)
    # The gateway becomes unavailable AFTER the reservation was taken.
    monkeypatch.setattr(settings, "approval_gateway_available", False)
    worker = _prod_worker(eng, prov, "release", rec=rec)
    run = _drive_to_terminal(worker, eng, rid)

    urls = [c[1] for c in rec.calls]
    assert urls == [URL + "/healthz", URL + "/healthz",
                    URL + "/v1/reserve", URL + "/v1/release"], urls
    bodies = {c[1].rsplit("/", 1)[-1]: json.loads(c[2].decode()) for c in rec.calls
              if c[2] is not None}
    assert set(bodies["reserve"]) == {"grant"}
    assert bodies["release"] == bodies["reserve"], \
        "the SAME reservation credential is returned"
    assert _started(eng) == 0, "a refused dispatch writes no started row"
    assert run.status == "failed", (run.status, run.status_reason)
