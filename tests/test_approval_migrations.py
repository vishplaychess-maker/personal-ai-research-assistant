"""Phase 1B.2 correction — Decision 1: `session_grants` migration reconciliation.

Hermetic: each test runs the real ``_migrate_approval_domain`` /
``_migrate_approval_domain_1b2`` against a throwaway file SQLite DB (``app.main``
module ``engine`` is monkeypatched). No network, no live backend.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import app.main as main
from app.database import Base
import app.models.models  # noqa: F401  (register the ORM models on Base)
from app.models.models import (
    DispatchRecord,
    ExecutionTurn,
    PendingApproval,
    SessionGrant,
    ToolCall,
    TrustedTool,
)

# The obsolete pre-Phase-1B.1-close-out shape: NOT NULL `allowed_action_hash`
# and no grant_scope_hash / grant_nonce / grant_scope_version.
_LEGACY_SESSION_GRANTS_DDL = """
CREATE TABLE session_grants (
    id VARCHAR(36) PRIMARY KEY,
    allowed_action_hash VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64),
    user_id INTEGER NOT NULL,
    session_id INTEGER NOT NULL,
    tool_id VARCHAR(200) NOT NULL,
    server_fingerprint VARCHAR(64) NOT NULL,
    tool_schema_hash VARCHAR(64) NOT NULL,
    policy_version VARCHAR(32) NOT NULL,
    creating_approval_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    revoked_at TIMESTAMP
)
"""

_NOW = "2026-01-01 00:00:00"


@pytest.fixture
def eng(tmp_path, monkeypatch):
    e = create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    monkeypatch.setattr(main, "engine", e)
    try:
        yield e
    finally:
        e.dispose()


def _tables(e):
    return set(inspect(e).get_table_names())


def _cols(e, table):
    return {c["name"] for c in inspect(e).get_columns(table)}


def _make_legacy_session_grants(e, *, rows: int):
    """Replace `session_grants` with the obsolete shape and `rows` legacy rows."""
    with e.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS session_grants"))
        c.execute(text(_LEGACY_SESSION_GRANTS_DDL))
        for i in range(rows):
            c.execute(text(
                "INSERT INTO session_grants (id, allowed_action_hash, user_id, "
                "session_id, tool_id, server_fingerprint, tool_schema_hash, "
                "policy_version, creating_approval_id, created_at, expires_at) "
                "VALUES (:id, :h, 1, 1, 't', 'fp', 'sh', 'p', 'a', :now, :now)"
            ), {"id": f"g{i}", "h": "0" * 64, "now": _NOW})


# ── clean DB / 1B.1 upgrade / idempotency ─────────────────────────────


def test_clean_database_creates_every_table_and_column(eng):
    main._migrate_approval_domain()
    main._migrate_approval_domain_1b2()
    assert {
        "pending_approvals", "session_grants", "action_log", "action_log_heads",
        "trusted_tools", "execution_turns", "tool_calls", "dispatch_records",
    } <= _tables(eng)
    g = _cols(eng, "session_grants")
    assert "grant_scope_hash" in g and "grant_nonce" in g
    assert "allowed_action_hash" not in g
    p = _cols(eng, "pending_approvals")
    assert {"proposal_dedupe_key", "execution_turn_id", "tool_call_id",
            "trusted_envelope_json"} <= p
    # the partial-unique dedupe index is expression-based (COALESCE) so
    # SQLAlchemy reflection skips it — check the catalogue directly.
    with eng.connect() as c:
        ddl = c.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='uq_pending_approvals_active_dedupe'"
        )).scalar()
    assert ddl and "COALESCE" in ddl.upper() and "WHERE" in ddl.upper()


def test_phase_1b1_database_upgrade(eng):
    main._migrate_approval_domain()                       # only the 1B.1 tables
    assert "trusted_tools" not in _tables(eng)
    main._migrate_approval_domain_1b2()                   # the upgrade
    assert {"trusted_tools", "execution_turns", "tool_calls",
            "dispatch_records"} <= _tables(eng)
    assert {"proposal_dedupe_key", "execution_turn_id", "tool_call_id",
            "trusted_envelope_json"} <= _cols(eng, "pending_approvals")
    assert "allowed_action_hash" not in _cols(eng, "session_grants")


def test_repeat_migration_is_idempotent(eng):
    main._migrate_approval_domain()
    for _ in range(3):
        main._migrate_approval_domain_1b2()               # no error, stable schema
    assert "allowed_action_hash" not in _cols(eng, "session_grants")
    assert "grant_scope_hash" in _cols(eng, "session_grants")


# ── empty straddling legacy table ────────────────────────────────────


def test_empty_legacy_session_grants_is_rebuilt_and_verified(eng):
    main._migrate_approval_domain()
    _make_legacy_session_grants(eng, rows=0)
    assert "allowed_action_hash" in _cols(eng, "session_grants")

    main._migrate_approval_domain_1b2()                   # guarded drop + recreate + verify

    g = _cols(eng, "session_grants")
    assert "allowed_action_hash" not in g
    assert {"grant_scope_hash", "grant_nonce", "grant_scope_version"} <= g
    names = {i["name"] for i in inspect(eng).get_indexes("session_grants")}
    assert {"ix_session_grants_lookup", "ix_session_grants_scope"} <= names
    # and it stays reconciled on a re-run
    main._migrate_approval_domain_1b2()
    assert "allowed_action_hash" not in _cols(eng, "session_grants")


# ── non-empty straddling legacy table -> fail fast, no data loss ─────


def test_nonempty_legacy_session_grants_fails_fast_without_data_loss(eng):
    main._migrate_approval_domain()
    _make_legacy_session_grants(eng, rows=2)

    with pytest.raises(RuntimeError) as ei:
        main._migrate_approval_domain_1b2()
    msg = str(ei.value).lower()
    assert "session_grants" in msg and "allowed_action_hash" in msg and "2" in msg

    # no silent data loss: the rows and the obsolete column are untouched
    with eng.connect() as c:
        assert (c.execute(text("SELECT COUNT(*) FROM session_grants")).scalar()) == 2
    assert "allowed_action_hash" in _cols(eng, "session_grants")

    # the failure is deterministic on every retry (no partial reconciliation)
    with pytest.raises(RuntimeError):
        main._migrate_approval_domain_1b2()
    with eng.connect() as c:
        assert (c.execute(text("SELECT COUNT(*) FROM session_grants")).scalar()) == 2


# ── post-migration ORM compatibility ────────────────────────────────


def test_orm_models_match_the_migrated_schema(eng):
    main._migrate_approval_domain()
    main._migrate_approval_domain_1b2()

    # create_all must agree with the raw DDL (no missing/extra table -> no-op)
    Base.metadata.create_all(eng)

    now = dt.datetime.now(dt.timezone.utc)
    s = sessionmaker(bind=eng)()
    try:
        s.add(TrustedTool(
            id="tt1", owner_user_id=1, tool_id="mcp__s__t", tool_schema_hash="h",
            server_fingerprint="f", discovered_at=now, updated_at=now,
        ))
        s.add(ExecutionTurn(id="turn1", user_id=1, session_id=1, created_at=now))
        s.add(ToolCall(id="call1", execution_turn_id="turn1", user_id=1,
                       call_index=0, tool_id="mcp__s__t", args_hash="a",
                       created_at=now))
        s.add(DispatchRecord(id="d1", tool_call_id="call1", user_id=1,
                             action_hash="ah", outcome="started", started_at=now))
        s.add(SessionGrant(
            id="sg1", grant_nonce="n1", grant_scope_version="1", user_id=1,
            session_id=1, tool_id="mcp__s__t", server_fingerprint="f",
            tool_schema_hash="h", grant_scope_hash="gsh", policy_version="p",
            creating_approval_id="a", created_at=now, expires_at=now,
        ))
        s.add(PendingApproval(
            id="pa1", action_hash="ah", args_hash="a", tool_id="mcp__s__t",
            server_fingerprint="f", tool_schema_hash="h", user_id=1,
            action_nonce="non1", policy_version="p", state="requested",
            proposal_dedupe_key="dk1", execution_turn_id="turn1",
            tool_call_id="call1", trusted_envelope_json="{}",
            created_at=now, expires_at=now,
        ))
        s.commit()
        assert s.get(SessionGrant, "sg1").grant_scope_hash == "gsh"
        assert s.get(PendingApproval, "pa1").proposal_dedupe_key == "dk1"
    finally:
        s.close()
