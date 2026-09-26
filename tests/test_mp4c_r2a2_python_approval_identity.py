"""Phase 1B.2R MP-4C-R2A-2 — the server-owned Python approval identity.

Proves the identity is deterministic and idempotent, that it can produce a real
gateway `approval_required`, and that NO execution happens on the proposal
path. Also proves the mandatory lock semantics: pristine first-party bootstrap
only; operator locks and drifted identities are never auto-unlocked.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.approval import registry
from app.approval.gateway import GatewayContext
from app.approval.gateway import dispatch as gw_dispatch
from app.approval.hashing import args_hash
from app.approval.identity import open_tool_call, open_turn
from app.models.models import Base, DispatchRecord, PendingApproval, TrustedTool
from app.run_engine import python_approval_identity as pai

CANARY = "PYTHON_SECRET_SENTINEL_R2A2"
CODE = "print('" + CANARY + "')"


class FixedClock:
    def now(self):
        import datetime
        return datetime.datetime(2026, 9, 17, tzinfo=datetime.timezone.utc)


@pytest.fixture
def engine(tmp_path):
    eng = create_engine("sqlite:///" + str(tmp_path / "id.db"),
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


def _rows(db):
    return db.query(TrustedTool).filter(TrustedTool.tool_id == pai.PYTHON_TOOL_ID).all()


# 1 — first registration creates the expected deterministic identity
def test_first_registration_creates_deterministic_identity(db, clock):
    row = pai.ensure_python_approval_identity(db, 7, clock=clock)
    assert row.tool_id == pai.PYTHON_TOOL_ID
    assert row.owner_user_id == 7
    assert row.tool_schema_hash and row.server_fingerprint
    sch, tsh, fp = registry.compute_identity(
        tool_id=pai.PYTHON_TOOL_ID, tool_name=pai.PYTHON_TOOL_NAME,
        server_manifest_id=pai.PYTHON_SERVER_MANIFEST_ID,
        executable_identity=pai.PYTHON_EXECUTABLE_IDENTITY,
        non_secret_launch_config={}, config_ref_id=pai.PYTHON_CONFIG_REF_ID,
        input_schema=dict(pai.PYTHON_INPUT_SCHEMA),
    )
    assert row.tool_schema_hash == tsh and row.server_fingerprint == fp
    assert row.locked is False, "pristine first-party identity is proposal-eligible"


# 2 — idempotent: same row, same hashes, no duplicate
def test_re_registration_is_idempotent(db, clock):
    a = pai.ensure_python_approval_identity(db, 7, clock=clock)
    snapshot = (a.id, a.tool_schema_hash, a.server_fingerprint)
    b = pai.ensure_python_approval_identity(db, 7, clock=clock)
    assert (b.id, b.tool_schema_hash, b.server_fingerprint) == snapshot
    assert len(_rows(db)) == 1
    assert b.locked is False


# 12 — strict schema
def test_schema_is_strict():
    s = pai.PYTHON_INPUT_SCHEMA
    assert s["type"] == "object"
    assert s["required"] == ["code"]
    assert s["additionalProperties"] is False
    assert s["properties"]["code"] == {"type": "string", "minLength": 1}
    assert set(s["properties"]) == {"code"}


# 3 / 4 — real gateway produces approval_required with zero execution
def test_identity_produces_approval_required_without_execution(db, clock):
    pai.ensure_python_approval_identity(db, 7, clock=clock)
    assert pai.resolve_python_identity(db, 7) is not None
    turn = open_turn(db, user_id=7, tenant_id=None, session_id=3, run_id="r1",
                     kind="reference", clock=clock)
    args = {"code": CODE}
    call = open_tool_call(db, turn=turn, call_index=0, tool_id=pai.PYTHON_TOOL_ID,
                          args_hash=args_hash(args), clock=clock)
    db.commit()

    calls = {"n": 0}

    def _forbidden(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("execute callback must never run on the proposal pass")

    ctx = GatewayContext(db=db, user_id=7, tenant_id=None, session_id=3, run_id="r1",
                         execution_turn_id=turn.id, tool_call_id=call.id)
    res = gw_dispatch(ctx, mcp_name=pai.PYTHON_TOOL_ID, args=args,
                      execute_mcp=_forbidden, clock=clock)

    assert res.status == "approval_required"
    assert calls["n"] == 0
    assert db.query(PendingApproval).count() == 1
    assert db.query(DispatchRecord).filter(DispatchRecord.outcome == "started").count() == 0


# 5 — a manual lock survives re-registration
def test_manual_lock_is_preserved(db, clock):
    pai.ensure_python_approval_identity(db, 7, clock=clock)
    assert registry.lock_tool(db, owner_user_id=7, tool_id=pai.PYTHON_TOOL_ID,
                              reason="operator review", clock=clock) is True
    row = pai.ensure_python_approval_identity(db, 7, clock=clock)
    assert row.locked is True
    assert pai.resolve_python_identity(db, 7) is None


# 6 — changed schema locks
def test_changed_schema_locks_identity(db, clock):
    pai.ensure_python_approval_identity(db, 7, clock=clock)
    registry.register_or_refresh(
        db, owner_user_id=7, tool_id=pai.PYTHON_TOOL_ID, tool_name=pai.PYTHON_TOOL_NAME,
        mcp_server_name=pai.PYTHON_SERVER_MANIFEST_ID,
        executable_identity=pai.PYTHON_EXECUTABLE_IDENTITY, non_secret_launch_config={},
        config_ref_id=pai.PYTHON_CONFIG_REF_ID,
        input_schema={"type": "object", "properties": {"code": {"type": "string"}},
                      "required": ["code"]},
        clock=clock,
    )
    row = _rows(db)[0]
    assert row.locked is True and "schema" in (row.lock_reason or "")
    assert pai.resolve_python_identity(db, 7) is None


# 7 — changed executable/config identity locks (fingerprint drift)
def test_changed_fingerprint_locks_identity(db, clock):
    pai.ensure_python_approval_identity(db, 7, clock=clock)
    registry.register_or_refresh(
        db, owner_user_id=7, tool_id=pai.PYTHON_TOOL_ID, tool_name=pai.PYTHON_TOOL_NAME,
        mcp_server_name=pai.PYTHON_SERVER_MANIFEST_ID,
        executable_identity="builtin:app.tools.python_sandbox@2",
        non_secret_launch_config={}, config_ref_id="builtin:python_sandbox:v2",
        input_schema=dict(pai.PYTHON_INPUT_SCHEMA), clock=clock,
    )
    row = _rows(db)[0]
    assert row.locked is True and "fingerprint" in (row.lock_reason or "")
    assert pai.resolve_python_identity(db, 7) is None


# 8 — a drifted identity is BLOCKED by the gateway, not approval-required
def test_drifted_identity_is_blocked_not_approval_required(db, clock):
    pai.ensure_python_approval_identity(db, 7, clock=clock)
    registry.lock_tool(db, owner_user_id=7, tool_id=pai.PYTHON_TOOL_ID,
                       reason="tool schema changed", clock=clock)
    turn = open_turn(db, user_id=7, tenant_id=None, session_id=3, run_id="r1",
                     kind="reference", clock=clock)
    args = {"code": CODE}
    call = open_tool_call(db, turn=turn, call_index=0, tool_id=pai.PYTHON_TOOL_ID,
                          args_hash=args_hash(args), clock=clock)
    db.commit()
    ctx = GatewayContext(db=db, user_id=7, tenant_id=None, session_id=3, run_id="r1",
                         execution_turn_id=turn.id, tool_call_id=call.id)
    res = gw_dispatch(ctx, mcp_name=pai.PYTHON_TOOL_ID, args=args,
                      execute_mcp=lambda *a, **k: (_ for _ in ()).throw(
                          AssertionError("no execution")), clock=clock)
    assert res.status == "blocked"
    assert db.query(PendingApproval).count() == 0


# 9 — unknown identity blocked
def test_unknown_identity_is_blocked(db, clock):
    turn = open_turn(db, user_id=7, tenant_id=None, session_id=3, run_id="r1",
                     kind="reference", clock=clock)
    args = {"code": CODE}
    call = open_tool_call(db, turn=turn, call_index=0, tool_id="python_sandbox_unknown",
                          args_hash=args_hash(args), clock=clock)
    db.commit()
    ctx = GatewayContext(db=db, user_id=7, tenant_id=None, session_id=3, run_id="r1",
                         execution_turn_id=turn.id, tool_call_id=call.id)
    res = gw_dispatch(ctx, mcp_name="python_sandbox_unknown", args=args,
                      execute_mcp=lambda *a, **k: (_ for _ in ()).throw(
                          AssertionError("no execution")), clock=clock)
    assert res.status == "blocked"
    assert db.query(PendingApproval).count() == 0


# 10 — another owner cannot resolve this identity
def test_other_user_cannot_resolve_identity(db, clock):
    pai.ensure_python_approval_identity(db, 7, clock=clock)
    assert pai.resolve_python_identity(db, 8) is None
    assert pai.resolve_python_identity(db, 7) is not None
    row = _rows(db)[0]
    assert row.owner_user_id == 7
    assert len(_rows(db)) == 1


# 11 — registering Python touches no unrelated tool row
def test_registration_does_not_touch_other_tools(db, clock):
    registry.register_or_refresh(
        db, owner_user_id=7, tool_id="mcp__srv__echo", tool_name="echo", mcp_server_id=3,
        mcp_server_name="srv", executable_identity="python -m srv @1",
        non_secret_launch_config=registry.non_secret_launch_config("python", ["-m", "srv"]),
        config_ref_id=registry.config_ref_for_server(3), input_schema={"type": "object"},
        clock=clock,
    )
    other = (db.query(TrustedTool)
             .filter(TrustedTool.tool_id == "mcp__srv__echo").one())
    before = (other.id, other.tool_schema_hash, other.server_fingerprint,
              other.locked, other.lock_reason)
    pai.ensure_python_approval_identity(db, 7, clock=clock)
    other = (db.query(TrustedTool)
             .filter(TrustedTool.tool_id == "mcp__srv__echo").one())
    assert (other.id, other.tool_schema_hash, other.server_fingerprint,
            other.locked, other.lock_reason) == before


# 13 — the code canary never lands in registry plaintext
def test_canary_absent_from_trusted_tool_plaintext(db, clock):
    pai.ensure_python_approval_identity(db, 7, clock=clock)
    row = _rows(db)[0]
    # Enumerate the REAL columns rather than guessing field names.
    blob = " ".join(str(getattr(row, c)) for c in row.__table__.columns.keys())
    assert CANARY not in blob
    assert "code" not in (row.input_schema_json or "").lower().replace('"code"', "")


# 14 — no raw code is persisted by identity/proposal creation
def test_proposal_metadata_never_echoes_raw_code(db, clock):
    pai.ensure_python_approval_identity(db, 7, clock=clock)
    turn = open_turn(db, user_id=7, tenant_id=None, session_id=3, run_id="r1",
                     kind="reference", clock=clock)
    args = {"code": CODE}
    call = open_tool_call(db, turn=turn, call_index=0, tool_id=pai.PYTHON_TOOL_ID,
                          args_hash=args_hash(args), clock=clock)
    db.commit()
    ctx = GatewayContext(db=db, user_id=7, tenant_id=None, session_id=3, run_id="r1",
                         execution_turn_id=turn.id, tool_call_id=call.id)
    gw_dispatch(ctx, mcp_name=pai.PYTHON_TOOL_ID, args=args,
                execute_mcp=lambda *a, **k: None, clock=clock)
    for row in db.query(PendingApproval).all():
        blob = " ".join(str(getattr(row, c)) for c in row.__table__.columns.keys())
        assert CANARY not in blob
