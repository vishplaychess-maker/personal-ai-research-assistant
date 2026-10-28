"""Phase 1B.2 — trusted registry, execution identity, proposal dedupe, and the
centralized approval gateway. Hermetic: in-memory / file SQLite, no network,
no live backend.
"""

from __future__ import annotations

import datetime as dt
import threading

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models.models  # noqa: F401
from app.models.models import (
    ActionLogEntry,
    DispatchRecord,
    PendingApproval,
    ToolCall,
    TrustedTool,
)

from app.approval import (
    FixedClock,
    gateway,
    open_tool_call,
    open_turn,
    record_decision,
    registry,
    verify_chain,
)
from app.approval.dedupe import PROPOSAL_DEDUPE_VERSION, proposal_dedupe_key
from app.approval.hashing import args_hash
from app.approval.gateway import GatewayContext


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    # partial-unique dedupe index (created by the migration in production)
    with eng.begin() as c:
        c.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_approvals_active_dedupe "
            "ON pending_approvals (COALESCE(tenant_id, ''), proposal_dedupe_key) "
            "WHERE proposal_dedupe_key IS NOT NULL AND state IN ('requested','approved')"
        ))
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def file_engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'gw.db'}",
                        connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_approvals_active_dedupe "
            "ON pending_approvals (COALESCE(tenant_id, ''), proposal_dedupe_key) "
            "WHERE proposal_dedupe_key IS NOT NULL AND state IN ('requested','approved')"
        ))
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def db(engine):
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def clock():
    return FixedClock()


def _reg(db, clock, *, uid=7, tool_id="mcp__srv__echo", schema=None, unlock=True):
    schema = schema or {"type": "object"}
    registry.register_or_refresh(
        db, owner_user_id=uid, tool_id=tool_id, tool_name=tool_id.split("__")[-1],
        mcp_server_id=3, mcp_server_name="srv", executable_identity="python -m srv @1",
        non_secret_launch_config=registry.non_secret_launch_config("python", ["-m", "srv"]),
        config_ref_id=registry.config_ref_for_server(3), input_schema=schema, clock=clock,
    )
    if unlock:
        registry.register_or_refresh(
            db, owner_user_id=uid, tool_id=tool_id, tool_name=tool_id.split("__")[-1],
            mcp_server_id=3, mcp_server_name="srv", executable_identity="python -m srv @1",
            non_secret_launch_config=registry.non_secret_launch_config("python", ["-m", "srv"]),
            config_ref_id=registry.config_ref_for_server(3), input_schema=schema,
            unlock_if_unchanged=True, clock=clock,
        )
    return registry.resolve_trusted(db, owner_user_id=uid, tool_id=tool_id)


def _ctx(db, *, turn, call, uid=7, tenant=None, sess=3, run="r1"):
    return GatewayContext(db=db, user_id=uid, tenant_id=tenant, session_id=sess,
                          run_id=run, execution_turn_id=turn.id, tool_call_id=call.id)


def _call(db, clock, turn, idx, args, tool_id="mcp__srv__echo"):
    return open_tool_call(db, turn=turn, call_index=idx, tool_id=tool_id,
                          args_hash=args_hash(args), clock=clock)


def _authz_used(db, scope="user:7"):
    return [e for e in db.query(ActionLogEntry).filter_by(chain_scope_id=scope).all()
            if e.event_type == "grant.authorization_used"]


# ── identity & dedupe ───────────────────────────────────────────────


def test_server_owned_ids_and_multi_call_distinct(db, clock):
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c0 = _call(db, clock, turn, 0, {"x": 1})
    c0b = _call(db, clock, turn, 0, {"x": 1})            # same logical slot -> reuse
    c1 = _call(db, clock, turn, 1, {"x": 2})
    assert c0.id == c0b.id and c1.id != c0.id            # multi-call distinct ids
    assert len({c0.id, c1.id}) == 2


def test_tool_call_slot_refuses_to_rebind_a_different_call(db, clock):
    turn = open_turn(db, user_id=7, session_id=3, clock=clock)
    _call(db, clock, turn, 0, {"x": 1})
    with pytest.raises(ValueError):
        _call(db, clock, turn, 0, {"x": 99})             # reordered/regenerated -> different args


def test_new_turn_gets_new_ids(db, clock):
    t1 = open_turn(db, user_id=7, session_id=3, clock=clock)
    t2 = open_turn(db, user_id=7, session_id=3, clock=clock)
    assert t1.id != t2.id
    a = _call(db, clock, t1, 0, {"x": 1})
    b = _call(db, clock, t2, 0, {"x": 1})
    assert a.id != b.id


def test_proposal_dedupe_key_is_distinct_from_every_1b1_hash(db, clock):
    tt = _reg(db, clock)
    common = dict(
        proposal_dedupe_version=PROPOSAL_DEDUPE_VERSION, tenant_id=None, user_id=7,
        session_id=3, run_id="r1", execution_turn_id="T", tool_call_id="C",
        tool_id=tt.tool_id, tool_version=tt.tool_version, source_type="mcp",
        mcp_server_id="3", server_fingerprint=tt.server_fingerprint,
        tool_schema_hash=tt.tool_schema_hash, args_hash=args_hash({"x": 1}),
        affected_resources=[], network_scope=None, filesystem_scope=None,
        credential_scope=None, risk_level="medium", risk_scope_flags=[],
        policy_version=tt.policy_version,
    )
    dk = proposal_dedupe_key(common)
    assert dk != args_hash({"x": 1})
    assert dk != tt.tool_schema_hash and dk != tt.server_fingerprint
    # tool_call_id alone is not the key: changing only the schema hash changes it
    dk2 = proposal_dedupe_key({**common, "tool_schema_hash": "f" * 64})
    assert dk2 != dk
    # changing only run_id changes it too (one-time proposal identity, not grant scope)
    assert proposal_dedupe_key({**common, "run_id": "r2"}) != dk


def test_concurrent_identical_proposals_create_one_active_pending(file_engine, clock):
    Session = sessionmaker(bind=file_engine)
    s0 = Session(); _reg(s0, clock); s0.commit()
    turn = open_turn(s0, user_id=7, session_id=3, run_id="r1", clock=clock)
    call = _call(s0, clock, turn, 0, {"x": 1})
    tid, cid = turn.id, call.id
    s0.close()

    def propose():
        s = Session()
        try:
            ctx = GatewayContext(db=s, user_id=7, tenant_id=None, session_id=3,
                                 run_id="r1", execution_turn_id=tid, tool_call_id=cid)
            r = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                                 execute_mcp=lambda: ("ok", False), clock=clock)
            return r.status
        finally:
            s.close()

    def _run(fn, n):
        out = []
        ts = [threading.Thread(target=lambda: out.append(fn())) for _ in range(n)]
        [t.start() for t in ts]; [t.join() for t in ts]
        return out

    res = _run(propose, 8)
    s = Session()
    active = (s.query(PendingApproval)
              .filter(PendingApproval.tool_call_id == cid,
                      PendingApproval.state.in_(("requested", "approved"))).count())
    s.close()
    assert all(x == "approval_required" for x in res)
    assert active == 1                                    # exactly one active proposal


# ── registry ────────────────────────────────────────────────────────


def test_new_tool_starts_locked_and_fails_closed(db, clock):
    _reg(db, clock, unlock=False)
    assert registry.resolve_trusted(db, owner_user_id=7, tool_id="mcp__srv__echo") is None


def test_unknown_and_unregistered_tool_fail_closed(db, clock):
    assert registry.resolve_trusted(db, owner_user_id=7, tool_id="mcp__nope__x") is None
    turn = open_turn(db, user_id=7, session_id=3, clock=clock)
    c = _call(db, clock, turn, 0, {}, tool_id="mcp__nope__x")
    r = gateway.dispatch(_ctx(db, turn=turn, call=c), mcp_name="mcp__nope__x",
                         args={}, execute_mcp=lambda: ("x", False), clock=clock)
    assert r.status == "blocked" and r.reason_code == "unknown_tool"


def test_schema_change_locks_and_invalidates_authorization(db, clock):
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c = _call(db, clock, turn, 0, {"x": 1})
    r1 = gateway.dispatch(_ctx(db, turn=turn, call=c), mcp_name="mcp__srv__echo",
                          args={"x": 1}, execute_mcp=lambda: ("ok", False), clock=clock)
    record_decision(db, r1.approval_id, approve=True, clock=clock)
    # schema materially changes -> tool re-locks
    _reg(db, clock, schema={"type": "object", "required": ["x"]}, unlock=False)
    r2 = gateway.dispatch(_ctx(db, turn=turn, call=c), mcp_name="mcp__srv__echo",
                          args={"x": 1}, execute_mcp=lambda: ("ok", False), clock=clock)
    assert r2.status == "blocked" and r2.reason_code == "schema_mismatch"


def test_fingerprint_change_locks(db, clock):
    _reg(db, clock)
    # different executable identity -> different server_fingerprint -> lock
    registry.register_or_refresh(
        db, owner_user_id=7, tool_id="mcp__srv__echo", tool_name="echo",
        mcp_server_id=3, mcp_server_name="srv", executable_identity="python -m srv @2",
        non_secret_launch_config=registry.non_secret_launch_config("python", ["-m", "srv"]),
        config_ref_id=registry.config_ref_for_server(3), input_schema={"type": "object"},
        clock=clock,
    )
    row = db.query(TrustedTool).filter_by(owner_user_id=7, tool_id="mcp__srv__echo").one()
    assert row.locked and "fingerprint" in row.lock_reason


def test_registry_never_stores_raw_credentials(db, clock):
    _reg(db, clock)
    row = db.query(TrustedTool).filter_by(owner_user_id=7, tool_id="mcp__srv__echo").one()
    blob = (row.input_schema_json + row.server_fingerprint + (row.config_ref_id or "")
            + row.risk_metadata_json)
    assert "srv" in (row.config_ref_id or "") or row.config_ref_id == "mcp_server:3"
    assert "AUTH" not in blob.upper() and "SECRET" not in blob.upper()
    # non-secret launch config passes command + args ONLY (never env)
    ns = registry.non_secret_launch_config("python", ["-m", "srv"])
    assert "env" not in ns


def test_registry_error_blocks_execution(db, clock, monkeypatch):
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c = _call(db, clock, turn, 0, {"x": 1})
    monkeypatch.setattr(gateway._registry, "resolve_reason",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    r = gateway.dispatch(_ctx(db, turn=turn, call=c), mcp_name="mcp__srv__echo",
                         args={"x": 1}, execute_mcp=lambda: ("x", False), clock=clock)
    assert r.status == "blocked" and r.reason_code == "registry_unavailable"


# ── gateway: authorization & fail-closed ────────────────────────────


def test_first_call_returns_approval_required_and_does_not_execute(db, clock):
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c = _call(db, clock, turn, 0, {"x": 1})
    ex = []
    r = gateway.dispatch(_ctx(db, turn=turn, call=c), mcp_name="mcp__srv__echo",
                         args={"x": 1}, execute_mcp=lambda: ex.append(1) or ("ok", False),
                         clock=clock)
    assert r.status == "approval_required" and r.reason_code == "needs_approval"
    assert not ex and r.approval_id


def test_approved_one_time_executes_once_then_replay_blocked(db, clock):
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c = _call(db, clock, turn, 0, {"x": 1})
    ctx = _ctx(db, turn=turn, call=c)
    ex = []
    r1 = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                          execute_mcp=lambda: ex.append(1) or ("ECHO", False), clock=clock)
    record_decision(db, r1.approval_id, approve=True, clock=clock)
    r2 = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                          execute_mcp=lambda: ex.append(1) or ("ECHO", False), clock=clock)
    assert r2.status == "executed" and r2.reason_code == "ok" and ex == [1]
    r3 = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                          execute_mcp=lambda: ex.append(1) or ("ECHO", False), clock=clock)
    assert r3.status == "blocked" and r3.reason_code == "duplicate_dispatch" and ex == [1]


def test_denied_expired_mismatch_block_execution(db, clock):
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)

    # denied
    c0 = _call(db, clock, turn, 0, {"x": 1})
    r = gateway.dispatch(_ctx(db, turn=turn, call=c0), mcp_name="mcp__srv__echo",
                         args={"x": 1}, execute_mcp=lambda: ("x", False), clock=clock)
    record_decision(db, r.approval_id, approve=False, clock=clock)
    r2 = gateway.dispatch(_ctx(db, turn=turn, call=c0), mcp_name="mcp__srv__echo",
                          args={"x": 1}, execute_mcp=lambda: ("x", False), clock=clock)
    assert r2.status == "blocked" and r2.reason_code == "approval_denied"

    # expired
    c1 = _call(db, clock, turn, 1, {"x": 2})
    r3 = gateway.dispatch(_ctx(db, turn=turn, call=c1), mcp_name="mcp__srv__echo",
                          args={"x": 2}, execute_mcp=lambda: ("x", False), clock=clock)
    record_decision(db, r3.approval_id, approve=True, clock=clock)
    clock.advance(10_000)
    r4 = gateway.dispatch(_ctx(db, turn=turn, call=c1), mcp_name="mcp__srv__echo",
                          args={"x": 2}, execute_mcp=lambda: ("x", False), clock=clock)
    assert r4.status == "blocked" and r4.reason_code == "approval_expired"


def test_gateway_unavailable_blocks(db, clock, monkeypatch):
    _reg(db, clock)
    monkeypatch.setattr(gateway.settings, "approval_gateway_available", False)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c = _call(db, clock, turn, 0, {"x": 1})
    r = gateway.dispatch(_ctx(db, turn=turn, call=c), mcp_name="mcp__srv__echo",
                         args={"x": 1}, execute_mcp=lambda: ("x", False), clock=clock)
    assert r.status == "blocked" and r.reason_code == "gateway_unavailable"


def test_actionlog_failure_at_started_blocks_execution(db, clock, monkeypatch):
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c = _call(db, clock, turn, 0, {"x": 1})
    ctx = _ctx(db, turn=turn, call=c)
    r1 = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                          execute_mcp=lambda: ("ok", False), clock=clock)
    record_decision(db, r1.approval_id, approve=True, clock=clock)
    real = gateway._log.append_event

    def boom(dbx, **kw):
        if kw.get("event_type") == "tool.execution.started":
            raise RuntimeError("actionlog down")
        return real(dbx, **kw)

    monkeypatch.setattr(gateway._log, "append_event", boom)
    ex = []
    r2 = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                          execute_mcp=lambda: ex.append(1) or ("ok", False), clock=clock)
    assert r2.status == "blocked" and r2.reason_code == "action_log_failure" and not ex


# ── execution lifecycle events ─────────────────────────────────────


def test_started_then_succeeded_events_sanitized(db, clock):
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c = _call(db, clock, turn, 0, {"x": 1})
    ctx = _ctx(db, turn=turn, call=c)
    r1 = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                          execute_mcp=lambda: ("SECRET-OUTPUT sk-abcdefghijklmnopqrstuvwx", False),
                          clock=clock)
    record_decision(db, r1.approval_id, approve=True, clock=clock)
    gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                     execute_mcp=lambda: ("SECRET-OUTPUT sk-abcdefghijklmnopqrstuvwx", False),
                     clock=clock)
    import json
    evs = {e.event_type: json.loads(e.event_metadata)
           for e in db.query(ActionLogEntry).filter_by(chain_scope_id="user:7").all()}
    assert "tool.execution.started" in evs and "tool.execution.succeeded" in evs
    ok_meta = evs["tool.execution.succeeded"]
    assert "SECRET-OUTPUT" not in json.dumps(ok_meta)          # raw output not logged
    assert "output_sha256" in ok_meta and "output_len" in ok_meta
    for f in ("action_hash", "run_id", "execution_turn_id", "tool_call_id",
              "tenant_id", "user_id", "session_id", "tool_id", "server_fingerprint",
              "tool_schema_hash", "policy_version"):
        assert f in ok_meta, f


def test_started_then_failed_on_tool_error(db, clock):
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c = _call(db, clock, turn, 0, {"x": 1})
    ctx = _ctx(db, turn=turn, call=c)
    r1 = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                          execute_mcp=lambda: ("boom", True), clock=clock)
    record_decision(db, r1.approval_id, approve=True, clock=clock)
    r2 = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                          execute_mcp=lambda: ("boom", True), clock=clock)
    assert r2.status == "executed" and r2.is_error
    types = {e.event_type for e in db.query(ActionLogEntry).filter_by(chain_scope_id="user:7").all()}
    assert "tool.execution.failed" in types and "tool.execution.succeeded" not in types
    rec = db.query(DispatchRecord).filter_by(tool_call_id=c.id).one()
    assert rec.outcome == "failed"


def test_crash_after_dispatch_leaves_started_and_is_not_auto_retried(db, clock):
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c = _call(db, clock, turn, 0, {"x": 1})
    ctx = _ctx(db, turn=turn, call=c)
    r1 = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                          execute_mcp=lambda: ("ok", False), clock=clock)
    record_decision(db, r1.approval_id, approve=True, clock=clock)

    def _crash():
        raise KeyboardInterrupt("process died mid-tool")

    with pytest.raises(KeyboardInterrupt):
        gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                         execute_mcp=_crash, clock=clock)
    rec = db.query(DispatchRecord).filter_by(tool_call_id=c.id).one()
    assert rec.outcome == "incomplete"                            # honest unknown state
    # a later attempt on the same logical call is blocked, not re-run
    ex = []
    r3 = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                          execute_mcp=lambda: ex.append(1) or ("ok", False), clock=clock)
    assert r3.status == "blocked" and r3.reason_code == "dispatch_incomplete" and not ex


# ── multi-call isolation ───────────────────────────────────────────


def test_mixed_authorized_and_unauthorized_calls_do_not_cross_authorize(db, clock):
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c0 = _call(db, clock, turn, 0, {"x": 1})
    c1 = _call(db, clock, turn, 1, {"x": 2})
    r0 = gateway.dispatch(_ctx(db, turn=turn, call=c0), mcp_name="mcp__srv__echo",
                          args={"x": 1}, execute_mcp=lambda: ("a", False), clock=clock)
    record_decision(db, r0.approval_id, approve=True, clock=clock)      # only call 0 approved
    ex0, ex1 = [], []
    a0 = gateway.dispatch(_ctx(db, turn=turn, call=c0), mcp_name="mcp__srv__echo",
                          args={"x": 1}, execute_mcp=lambda: ex0.append(1) or ("a", False),
                          clock=clock)
    a1 = gateway.dispatch(_ctx(db, turn=turn, call=c1), mcp_name="mcp__srv__echo",
                          args={"x": 2}, execute_mcp=lambda: ex1.append(1) or ("b", False),
                          clock=clock)
    assert a0.status == "executed" and ex0 == [1]
    assert a1.status == "approval_required" and ex1 == []              # call 1 NOT authorized


def test_one_call_failure_does_not_corrupt_another(db, clock):
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c0 = _call(db, clock, turn, 0, {"x": 1})
    c1 = _call(db, clock, turn, 1, {"x": 2})
    for c, a in ((c0, {"x": 1}), (c1, {"x": 2})):
        r = gateway.dispatch(_ctx(db, turn=turn, call=c), mcp_name="mcp__srv__echo",
                             args=a, execute_mcp=lambda: ("x", False), clock=clock)
        record_decision(db, r.approval_id, approve=True, clock=clock)
    gateway.dispatch(_ctx(db, turn=turn, call=c0), mcp_name="mcp__srv__echo",
                     args={"x": 1}, execute_mcp=lambda: ("boom", True), clock=clock)
    ok = gateway.dispatch(_ctx(db, turn=turn, call=c1), mcp_name="mcp__srv__echo",
                          args={"x": 2}, execute_mcp=lambda: ("fine", False), clock=clock)
    assert ok.status == "executed" and not ok.is_error
    assert db.query(DispatchRecord).filter_by(tool_call_id=c0.id).one().outcome == "failed"
    assert db.query(DispatchRecord).filter_by(tool_call_id=c1.id).one().outcome == "succeeded"


def test_external_exactly_once_is_not_claimed():
    src = (__import__("app.approval.gateway", fromlist=["x"]).__doc__ or "").lower()
    assert "external exactly-once is not claimed" in src or "not claim" in src


# ── audit chain stays intact ───────────────────────────────────────


def test_gateway_chain_verifies_after_full_lifecycle(db, clock):
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c = _call(db, clock, turn, 0, {"x": 1})
    ctx = _ctx(db, turn=turn, call=c)
    r1 = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                          execute_mcp=lambda: ("ok", False), clock=clock)
    record_decision(db, r1.approval_id, approve=True, clock=clock)
    gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                     execute_mcp=lambda: ("ok", False), clock=clock)
    assert verify_chain(db, user_id=7).ok


# ── Decision 3: tool.execution.blocked lifecycle event ─────────────


def _events_by_type(db, scope="user:7"):
    import json
    out: dict[str, list] = {}
    for e in db.query(ActionLogEntry).filter_by(chain_scope_id=scope).all():
        out.setdefault(e.event_type, []).append(json.loads(e.event_metadata))
    return out


def test_blocked_event_emitted_for_each_failure_class(db, clock, monkeypatch):
    import json
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)

    # (1) gateway unavailable
    monkeypatch.setattr(gateway.settings, "approval_gateway_available", False)
    c = _call(db, clock, turn, 0, {"x": 1})
    r = gateway.dispatch(_ctx(db, turn=turn, call=c), mcp_name="mcp__srv__echo",
                         args={"x": 1}, execute_mcp=lambda: ("x", False), clock=clock)
    assert r.status == "blocked" and r.reason_code == "gateway_unavailable"
    monkeypatch.setattr(gateway.settings, "approval_gateway_available", True)

    # (2) unknown tool
    c = _call(db, clock, turn, 1, {}, tool_id="mcp__srv__echo")
    r2 = gateway.dispatch(_ctx(db, turn=turn, call=c), mcp_name="mcp__no__such",
                          args={}, execute_mcp=lambda: ("x", False), clock=clock)
    assert r2.reason_code == "unknown_tool"

    # (3) locked (newly discovered)
    _reg(db, clock, tool_id="mcp__srv__locked", unlock=False)
    c = _call(db, clock, turn, 2, {}, tool_id="mcp__srv__locked")
    ex = []
    r3 = gateway.dispatch(_ctx(db, turn=turn, call=c), mcp_name="mcp__srv__locked",
                          args={}, execute_mcp=lambda: ex.append(1) or ("x", False),
                          clock=clock)
    assert r3.reason_code == "tool_locked" and not ex

    evs = _events_by_type(db)
    blocked = evs.get("tool.execution.blocked", [])
    codes = {m["reason_code"] for m in blocked}
    assert {"gateway_unavailable", "unknown_tool", "tool_locked"} <= codes
    for m in blocked:
        assert m["execution_attempted"] is False
        assert m["reason_code"] in gateway.BLOCK_REASONS
        blob = json.dumps(m)
        assert "SECRET" not in blob.upper() and "\"x\": 1" not in blob  # no raw args
    # a blocked attempt never emits a started / succeeded / failed event
    assert "tool.execution.started" not in evs
    assert "tool.execution.succeeded" not in evs
    assert "tool.execution.failed" not in evs


def test_blocked_event_has_only_trusted_correlation_fields(db, clock):
    import json
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c = _call(db, clock, turn, 0, {"secret_arg": "sk-abcdefghijklmnopqrstuvwx"},
              tool_id="mcp__no__such")
    gateway.dispatch(_ctx(db, turn=turn, call=c), mcp_name="mcp__no__such",
                     args={"secret_arg": "sk-abcdefghijklmnopqrstuvwx"},
                     execute_mcp=lambda: ("x", False), clock=clock)
    m = _events_by_type(db)["tool.execution.blocked"][0]
    assert "sk-abcdefghijklmnopqrstuvwx" not in json.dumps(m)
    assert m["tool_call_id"] == c.id and m["execution_turn_id"] == turn.id
    assert m["user_id"] == 7 and m["reason_code"] == "unknown_tool"


def test_action_log_failure_reason_does_not_recurse(db, clock, monkeypatch):
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c = _call(db, clock, turn, 0, {"x": 1})
    ctx = _ctx(db, turn=turn, call=c)
    r1 = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                          execute_mcp=lambda: ("ok", False), clock=clock)
    record_decision(db, r1.approval_id, approve=True, clock=clock)

    seen: list[str] = []
    real = gateway._log.append_event

    def boom(dbx, **kw):
        seen.append(kw.get("event_type"))
        if kw.get("event_type") == "tool.execution.started":
            raise RuntimeError("actionlog down")
        return real(dbx, **kw)

    monkeypatch.setattr(gateway._log, "append_event", boom)
    ex = []
    r2 = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                          execute_mcp=lambda: ex.append(1) or ("ok", False), clock=clock)
    assert r2.reason_code == "action_log_failure" and not ex
    # the failed ActionLog path is NOT retried by writing a blocked event
    assert "tool.execution.blocked" not in seen


# ── Decision 4: call_index is an allocation slot, not authority ─────


def test_same_slot_different_tool_fails_closed(db, clock):
    turn = open_turn(db, user_id=7, session_id=3, clock=clock)
    open_tool_call(db, turn=turn, call_index=0, tool_id="mcp__srv__a",
                   args_hash=args_hash({"x": 1}), clock=clock)
    with pytest.raises(ValueError):
        open_tool_call(db, turn=turn, call_index=0, tool_id="mcp__srv__b",
                       args_hash=args_hash({"x": 1}), clock=clock)


def test_reordered_markers_cannot_cross_authorize(db, clock):
    _reg(db, clock, tool_id="mcp__srv__a")
    _reg(db, clock, tool_id="mcp__srv__b")
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c0 = open_tool_call(db, turn=turn, call_index=0, tool_id="mcp__srv__a",
                        args_hash=args_hash({"x": 1}), clock=clock)
    r = gateway.dispatch(_ctx(db, turn=turn, call=c0), mcp_name="mcp__srv__a",
                         args={"x": 1}, execute_mcp=lambda: ("a", False), clock=clock)
    record_decision(db, r.approval_id, approve=True, clock=clock)
    gateway.dispatch(_ctx(db, turn=turn, call=c0), mcp_name="mcp__srv__a",
                     args={"x": 1}, execute_mcp=lambda: ("a", False), clock=clock)
    # a regenerated response puts a DIFFERENT tool at ordinal 0 — it must not
    # inherit c0's identity / approval
    with pytest.raises(ValueError):
        open_tool_call(db, turn=turn, call_index=0, tool_id="mcp__srv__b",
                       args_hash=args_hash({"x": 9}), clock=clock)


def test_raw_call_index_cannot_return_a_different_calls_id(db, clock):
    turn = open_turn(db, user_id=7, session_id=3, clock=clock)
    c0 = open_tool_call(db, turn=turn, call_index=0, tool_id="mcp__srv__a",
                        args_hash=args_hash({"x": 1}), clock=clock)
    # an attacker-chosen index just allocates a fresh distinct slot; it never
    # yields c0's server-owned id
    c99 = open_tool_call(db, turn=turn, call_index=99, tool_id="mcp__srv__b",
                         args_hash=args_hash({"x": 2}), clock=clock)
    assert c99.id != c0.id
    t2 = open_turn(db, user_id=7, session_id=3, clock=clock)
    c2 = open_tool_call(db, turn=t2, call_index=0, tool_id="mcp__srv__a",
                        args_hash=args_hash({"x": 1}), clock=clock)
    assert c2.id != c0.id  # different turns never collide on the slot


# ── Decision 5: bounded active proposals ──────────────────────────


def _dispatch_new(db, clock, turn, idx, args, *, uid=7, sess=3):
    c = _call(db, clock, turn, idx, args)
    ex = []
    r = gateway.dispatch(_ctx(db, turn=turn, call=c, uid=uid, sess=sess),
                         mcp_name="mcp__srv__echo", args=args,
                         execute_mcp=lambda: ex.append(1) or ("ok", False), clock=clock)
    return r, ex


def test_active_proposal_limit_per_turn(db, clock, monkeypatch):
    monkeypatch.setattr(gateway.settings, "max_active_proposals_per_turn", 2)
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    r0, _ = _dispatch_new(db, clock, turn, 0, {"x": 1})
    r1, _ = _dispatch_new(db, clock, turn, 1, {"x": 2})
    r2, ex2 = _dispatch_new(db, clock, turn, 2, {"x": 3})
    assert r0.status == "approval_required" and r1.status == "approval_required"
    assert r2.status == "blocked" and r2.reason_code == "active_proposal_limit"
    assert not ex2  # callback never invoked
    evs = _events_by_type(db)
    lim = evs.get("approval.limit_exceeded", [])
    assert any(m["scope"] == "execution_turn" and m["limit"] == 2 for m in lim)
    assert any(m["reason_code"] == "active_proposal_limit"
               for m in evs.get("tool.execution.blocked", []))
    active = (db.query(PendingApproval)
              .filter(PendingApproval.state.in_(("requested", "approved"))).count())
    assert active == 2


def test_active_proposal_limit_does_not_block_retry_of_pending_call(db, clock, monkeypatch):
    monkeypatch.setattr(gateway.settings, "max_active_proposals_per_turn", 1)
    _reg(db, clock)
    turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    c0 = _call(db, clock, turn, 0, {"x": 1})
    ctx0 = _ctx(db, turn=turn, call=c0)
    a = gateway.dispatch(ctx0, mcp_name="mcp__srv__echo", args={"x": 1},
                         execute_mcp=lambda: ("ok", False), clock=clock)
    b = gateway.dispatch(ctx0, mcp_name="mcp__srv__echo", args={"x": 1},
                         execute_mcp=lambda: ("ok", False), clock=clock)
    assert a.status == "approval_required" and b.status == "approval_required"
    r1, _ = _dispatch_new(db, clock, turn, 1, {"x": 2})
    assert r1.status == "blocked" and r1.reason_code == "active_proposal_limit"


def test_active_proposal_limit_per_session_across_turns(db, clock, monkeypatch):
    monkeypatch.setattr(gateway.settings, "max_active_proposals_per_turn", 50)
    monkeypatch.setattr(gateway.settings, "max_active_proposals_per_session", 1)
    _reg(db, clock)
    t1 = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    t2 = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clock)
    r0, _ = _dispatch_new(db, clock, t1, 0, {"x": 1})
    r1, ex1 = _dispatch_new(db, clock, t2, 0, {"x": 2})
    assert r0.status == "approval_required"
    assert r1.status == "blocked" and r1.reason_code == "active_proposal_limit" and not ex1
    assert any(m["scope"] == "session"
               for m in _events_by_type(db).get("approval.limit_exceeded", []))


def test_active_proposal_limit_concurrency(file_engine, clock, monkeypatch):
    monkeypatch.setattr(gateway.settings, "max_active_proposals_per_turn", 50)
    monkeypatch.setattr(gateway.settings, "max_active_proposals_per_session", 50)
    monkeypatch.setattr(gateway.settings, "max_active_proposals_per_user", 3)
    Session = sessionmaker(bind=file_engine)
    s0 = Session(); _reg(s0, clock); s0.commit()
    prepared = []
    for i in range(8):
        turn = open_turn(s0, user_id=7, session_id=3, run_id="r1", clock=clock)
        call = _call(s0, clock, turn, 0, {"x": i})
        prepared.append((turn.id, call.id))
    s0.close()

    results: list = []

    def propose(tid, cid):
        s = Session()
        try:
            ctx = GatewayContext(db=s, user_id=7, tenant_id=None, session_id=3,
                                 run_id="r1", execution_turn_id=tid, tool_call_id=cid)
            r = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 0},
                                 execute_mcp=lambda: ("ok", False), clock=clock)
            results.append((r.status, r.reason_code))
        finally:
            s.close()

    ts = [threading.Thread(target=propose, args=p) for p in prepared]
    [t.start() for t in ts]; [t.join() for t in ts]

    s = Session()
    active = (s.query(PendingApproval)
              .filter(PendingApproval.state.in_(("requested", "approved"))).count())
    s.close()
    assert active <= 3, f"cap bypassed: {active} active proposals"
    assert sum(1 for st, _ in results if st == "blocked") >= 5
    assert all(rc == "active_proposal_limit"
               for st, rc in results if st == "blocked")


def test_client_cannot_raise_or_disable_the_active_proposal_limit():
    import inspect
    from app.approval.service import request_approval
    params = set(inspect.signature(request_approval).parameters)
    assert not any("max_active" in p or "limit" in p for p in params)
    assert not any("limit" in f for f in GatewayContext.__dataclass_fields__)


@pytest.mark.skipif(not __import__("os").environ.get("EVAL_PG_TEST_URL"),
                    reason="set EVAL_PG_TEST_URL for the PostgreSQL gateway path")
def test_postgres_gateway_lifecycle_and_dedupe():
    import os
    pg = create_engine(os.environ["EVAL_PG_TEST_URL"])
    Base.metadata.create_all(pg)
    with pg.begin() as c:
        c.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_approvals_active_dedupe "
            "ON pending_approvals (COALESCE(tenant_id, ''), proposal_dedupe_key) "
            "WHERE proposal_dedupe_key IS NOT NULL AND state IN ('requested','approved')"
        ))
    S = sessionmaker(bind=pg)
    clk = FixedClock()
    try:
        db = S()
        _reg(db, clk)
        turn = open_turn(db, user_id=7, session_id=3, run_id="r1", clock=clk)
        c = _call(db, clk, turn, 0, {"x": 1})
        ctx = GatewayContext(db=db, user_id=7, tenant_id=None, session_id=3, run_id="r1",
                             execution_turn_id=turn.id, tool_call_id=c.id)
        r1 = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                              execute_mcp=lambda: ("ok", False), clock=clk)
        record_decision(db, r1.approval_id, approve=True, clock=clk)
        r2 = gateway.dispatch(ctx, mcp_name="mcp__srv__echo", args={"x": 1},
                              execute_mcp=lambda: ("ok", False), clock=clk)
        assert r2.status == "executed"
        db.close()
    finally:
        Base.metadata.drop_all(pg)
        pg.dispose()
