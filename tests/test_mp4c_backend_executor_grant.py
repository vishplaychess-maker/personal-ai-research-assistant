"""MP-4C - backend grant DERIVATION from durable rows (tests first).

The derivation is UNWIRED: it is never called from _mcp_dispatch or the worker.
These tests assert that a grant exists ONLY when the durable Run / step /
ToolCall / approval identities and an already-committed DispatchRecord.started
all agree, and that the result round-trips through the executor's verifier.
"""
from __future__ import annotations

import time
import uuid

import pytest

from app.models.models import DispatchRecord, Run, RunStep, ToolCall
from app.run_engine import executor_grant as eg
from mp4c_contract import grant as g
from tests.run_engine_helpers import mk_engine, mk_session

KEY = b"backend-grant-key-0123456789"
CODE = "print('backend')"


@pytest.fixture
def db():
    eng = mk_engine()
    session = mk_session(eng)
    yield session
    session.close()


def _seed(db, *, outcome="started", dispatch_action_hash=None,
          call_args_hash="a" * 64, run_user=7, step_user=7, call_user=7,
          run_tenant=None, step_tenant=None, call_tenant=None,
          step_kind="tool_dispatch", step_tool_call_id=None, tool_id="python_sandbox",
          dispatch_user=None, dispatch_tenant=None, dispatch_approval=None,
          step_approval=None, run_approval=None, turn=None, step_turn=None):
    run_id, step_id, call_id = (str(uuid.uuid4()) for _ in range(3))
    turn = turn or ("turn-" + uuid.uuid4().hex[:8])
    step_turn = step_turn or turn
    db.add(Run(id=run_id, tenant_id=run_tenant, user_id=run_user,
               session_id=None, thread_id="run:" + run_id, kind="chat_stream",
               status="running", pending_approval_id=run_approval))
    db.add(RunStep(id=step_id, run_id=run_id, tenant_id=step_tenant,
                   user_id=step_user, seq=1, kind=step_kind, status="running",
                   tool_call_id=step_tool_call_id or call_id,
                   approval_id=step_approval, execution_turn_id=step_turn))
    db.add(ToolCall(id=call_id, execution_turn_id=turn, tenant_id=call_tenant,
                    user_id=call_user, session_id=None, call_index=0,
                    tool_id=tool_id, args_hash=call_args_hash))
    db.add(DispatchRecord(id=str(uuid.uuid4()), tool_call_id=call_id,
                          tenant_id=dispatch_tenant if dispatch_tenant is not None
                          else run_tenant,
                          user_id=dispatch_user if dispatch_user is not None else run_user,
                          approval_id=dispatch_approval, action_hash=(
                              dispatch_action_hash
                              if dispatch_action_hash is not None else call_args_hash),
                          outcome=outcome))
    db.commit()
    return run_id, step_id, call_id


def _derive(db, ids, code=CODE, _key=KEY, **kw):
    run_id, step_id, call_id = ids
    return eg.derive_executor_grant(db, run_id=run_id, step_id=step_id,
                                    tool_call_id=call_id, code=code, key=_key, **kw)


def test_a_consistent_chain_yields_a_grant_the_executor_verifies(db):
    ids = _seed(db)
    token = _derive(db, ids)
    claims = g.parse_and_verify(token, KEY, code=CODE)      # app -> executor round trip
    run_id, step_id, call_id = ids
    assert (claims.run_id, claims.step_id, claims.tool_call_id) == (run_id, step_id, call_id)
    assert claims.user_id == 7
    assert claims.tenant_id is None
    assert claims.code_sha256 == g.code_sha256(CODE)[0]


def test_two_distinct_users_derive_distinct_grants(db):
    a = _derive(db, _seed(db, run_user=7, step_user=7, call_user=7))
    b = _derive(db, _seed(db, run_user=8, step_user=8, call_user=8))
    ca = g.parse_and_verify(a, KEY, code=CODE)
    cb = g.parse_and_verify(b, KEY, code=CODE)
    assert (ca.user_id, cb.user_id) == (7, 8)
    assert ca.jti != cb.jti


def test_the_grant_binds_the_exact_code(db):
    token = _derive(db, _seed(db))
    with pytest.raises(g.GrantError) as exc:
        g.parse_and_verify(token, KEY, code=CODE + "\n")
    assert exc.value.code == g.CODE_BINDING_MISMATCH


def test_a_dispatch_record_that_is_not_started_is_refused(db):
    for outcome in ("succeeded", "failed", "incomplete"):
        with pytest.raises(eg.GrantDerivationError) as exc:
            _derive(db, _seed(db, outcome=outcome))
        assert exc.value.code == eg.DISPATCH_NOT_STARTED


def test_a_missing_dispatch_record_is_refused(db):
    run_id, step_id, call_id = _seed(db)
    db.query(DispatchRecord).filter(DispatchRecord.tool_call_id == call_id).delete()
    db.commit()
    with pytest.raises(eg.GrantDerivationError) as exc:
        _derive(db, (run_id, step_id, call_id))
    assert exc.value.code == eg.DISPATCH_NOT_STARTED


def test_cross_user_rows_are_refused(db):
    with pytest.raises(eg.GrantDerivationError) as exc:
        _derive(db, _seed(db, run_user=7, step_user=8, call_user=7))
    assert exc.value.code == eg.OWNER_MISMATCH
    with pytest.raises(eg.GrantDerivationError) as exc:
        _derive(db, _seed(db, run_user=7, step_user=7, call_user=9))
    assert exc.value.code == eg.OWNER_MISMATCH
    with pytest.raises(eg.GrantDerivationError) as exc:
        _derive(db, _seed(db, run_user=7, dispatch_user=8))
    assert exc.value.code == eg.OWNER_MISMATCH


def test_tenant_substitution_is_refused(db):
    ids = _seed(db, run_tenant="t-1", step_tenant="t-1", call_tenant="t-1",
                dispatch_tenant="t-1")
    with pytest.raises(eg.GrantDerivationError) as exc:
        _derive(db, ids, tenant_id="t-2")
    assert exc.value.code == eg.OWNER_MISMATCH
    with pytest.raises(eg.GrantDerivationError) as exc:
        _derive(db, _seed(db, run_tenant="t-1", step_tenant="t-1",
                          call_tenant="t-1", dispatch_tenant="t-9"))
    assert exc.value.code == eg.OWNER_MISMATCH


def test_the_grant_binds_the_COMMITTED_action_hash_not_the_tool_call_hash(db):
    """SUPERSEDED: an earlier version required DispatchRecord.action_hash ==
    ToolCall.args_hash and refused otherwise. The real gateway hashes its OWN
    executor-facing action, so the two legitimately differ; the committed record
    is the execution authority and ITS hash is what the grant binds."""
    ids = _seed(db, dispatch_action_hash="b" * 64, call_args_hash="a" * 64)
    claims = g.parse_and_verify(_derive(db, ids), KEY, code=CODE)
    assert claims.action_hash == "b" * 64, "the COMMITTED hash is authoritative"


def test_a_non_dispatch_step_is_refused(db):
    with pytest.raises(eg.GrantDerivationError) as exc:
        _derive(db, _seed(db, step_kind="llm_generate"))
    assert exc.value.code == eg.BINDING_MISMATCH


def test_a_different_tool_is_refused(db):
    with pytest.raises(eg.GrantDerivationError) as exc:
        _derive(db, _seed(db, tool_id="terminal"))
    assert exc.value.code == eg.BINDING_MISMATCH


def test_a_step_pointing_at_another_tool_call_is_refused(db):
    with pytest.raises(eg.GrantDerivationError) as exc:
        _derive(db, _seed(db, step_tool_call_id=str(uuid.uuid4())))
    assert exc.value.code == eg.OWNER_MISMATCH


def test_disagreeing_approval_references_are_refused(db):
    ids = _seed(db, run_approval="ap-1", step_approval="ap-2", dispatch_approval="ap-1")
    with pytest.raises(eg.GrantDerivationError) as exc:
        _derive(db, ids)
    assert exc.value.code == eg.APPROVAL_MISMATCH


def test_agreeing_approval_references_are_bound(db):
    ids = _seed(db, run_approval="ap-1", step_approval="ap-1", dispatch_approval="ap-1")
    claims = g.parse_and_verify(_derive(db, ids), KEY, code=CODE)
    assert claims.approval_id == "ap-1"


def test_missing_rows_are_refused(db):
    ids = _seed(db)
    with pytest.raises(eg.GrantDerivationError) as exc:
        _derive(db, (str(uuid.uuid4()), ids[1], ids[2]))
    assert exc.value.code == eg.RUN_NOT_FOUND
    with pytest.raises(eg.GrantDerivationError) as exc:
        _derive(db, (ids[0], str(uuid.uuid4()), ids[2]))
    assert exc.value.code == eg.STEP_NOT_FOUND
    with pytest.raises(eg.GrantDerivationError) as exc:
        _derive(db, (ids[0], ids[1], str(uuid.uuid4())))
    assert exc.value.code == eg.TOOL_CALL_NOT_FOUND


def test_ttl_is_clamped_and_validated_by_the_contract(db):
    ids = _seed(db)
    now = int(time.time())
    token = _derive(db, ids, ttl_s=10_000, now=now)
    claims = g.parse_and_verify(token, KEY, code=CODE, now=now)
    assert claims.exp - claims.iat == g.MAX_TTL_S


def test_the_key_comes_from_the_environment_and_is_never_returned(db, monkeypatch):
    from mp4c_contract import grant as gc
    monkeypatch.delenv(gc.KEY_ENV, raising=False)
    monkeypatch.setenv(gc.KEY_FILE_ENV, "/nonexistent/key")
    run_id, step_id, call_id = _seed(db)
    with pytest.raises(gc.GrantError) as exc:
        eg.derive_executor_grant(db, run_id=run_id, step_id=step_id,
                                 tool_call_id=call_id, code=CODE, key=None)
    assert exc.value.code == gc.KEY_MISSING


def test_the_derivation_is_wired_only_through_the_authorized_broker_route():
    """SUPERSEDES the previous slice's "production wires no broker" invariant.

    THIS slice deliberately wires the production broker client through
    worker_supervisor - INERTLY. The invariant that must now hold forever:

      * the supervisor constructs the broker and REGISTERS the readiness probe,
        performing no I/O, so a broker outage cannot stop startup;
      * it NEVER supplies the construction-time executor test seam;
      * real execution still requires the single authority
        (real_python_execution_available: explicit operator opt-in + a live,
        verified executor) AND a wired, live-ready broker;
      * the worker never falls back to the in-process host subprocess.
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    worker = open(os.path.join(root, "backend", "app", "run_engine", "worker.py"),
                  encoding="utf-8").read()
    supervisor = open(os.path.join(root, "backend", "app", "run_engine",
                                   "worker_supervisor.py"), encoding="utf-8").read()
    assert "derive_reservation_grant" in worker
    assert "derive_executor_grant" in worker
    assert "self._python_broker is not None" in worker

    # The supervisor wires the broker inertly, and registers (never calls) the
    # probe. No network call, no daemon check, no derived grant.
    assert "python_broker=broker" in supervisor
    assert "broker_ready=_broker_is_ready" in supervisor
    assert "wire_from_env" in supervisor
    for forbidden in ("_http_opener", "urlopen", "requests.", "socket.",
                      "python_executor", "executor_grant",
                      "derive_reservation_grant", "derive_executor_grant"):
        assert forbidden not in supervisor, forbidden

    # No in-process host-subprocess fallback CALL survives anywhere in the worker.
    assert "run_python_code_outcome_sync(" not in worker
    assert "_run_python_unisolated" not in worker


def test_no_second_approval_system_is_introduced():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "backend", "app", "run_engine",
                            "executor_grant.py"), encoding="utf-8").read()
    for forbidden in ("PendingApproval", "approve(", "request_approval",
                      "SessionGrant"):
        assert forbidden not in src, forbidden
