"""Phase 1B.2R MP-0 — additive run-engine migration + schema verification.

Hermetic: each test runs the real ``_migrate_*`` functions against a throwaway
file SQLite DB (``app.main.engine`` monkeypatched). Additive only — no ALTER of
any existing table, no data mutation.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import app.main as main
from app.database import Base
import app.models.models  # noqa: F401

_RUN_TABLES = ("runs", "run_steps", "run_events", "run_signals", "run_payloads")
_NOW = "2026-01-01 00:00:00"


@pytest.fixture
def eng(tmp_path, monkeypatch):
    e = create_engine(f"sqlite:///{tmp_path / 'mp0.db'}")
    monkeypatch.setattr(main, "engine", e)
    try:
        yield e
    finally:
        e.dispose()


def _cols(e, table):
    return {c["name"] for c in inspect(e).get_columns(table)}


def _index_names(e, table):
    return {i["name"] for i in inspect(e).get_indexes(table)}


def _sqlite_indexes(e):
    with e.connect() as c:
        return {r[0] for r in c.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )).fetchall()}


def _base_1b(e):
    main._migrate_approval_domain()
    main._migrate_approval_domain_1b2()


# ── clean DB ───────────────────────────────────────────────────────


def test_clean_database_creates_all_run_tables_and_indexes(eng):
    _base_1b(eng)
    main._migrate_run_engine()
    main._verify_run_engine_schema()          # must not raise

    tables = set(inspect(eng).get_table_names())
    assert set(_RUN_TABLES) <= tables

    for t, required in main._RUN_ENGINE_REQUIRED_COLUMNS.items():
        assert required <= _cols(eng, t), f"{t} missing {required - _cols(eng, t)}"

    idx = _sqlite_indexes(eng)
    assert {"uq_runs_idem_scope", "uq_run_signals_live"} <= idx
    # the partial-unique index text carries COALESCE + WHERE
    with eng.connect() as c:
        ddl = c.execute(text(
            "SELECT sql FROM sqlite_master WHERE name='uq_runs_idem_scope'"
        )).scalar()
    assert ddl and "COALESCE" in ddl.upper() and "WHERE" in ddl.upper()


def test_clean_run_payloads_omits_the_obsolete_plaintext_digest(eng):
    _base_1b(eng)
    main._migrate_run_engine()
    cols = _cols(eng, "run_payloads")
    assert "plaintext_sha256" not in cols
    assert "plaintext_sha256" not in main._RUN_ENGINE_REQUIRED_COLUMNS["run_payloads"]
    # no confidentiality-relevant digest/hash column at all (action_hash is a
    # hash of trusted NON-secret fields, allowed)
    for c in cols:
        assert "sha" not in c.lower()
        assert "hash" not in c.replace("action_hash", "").lower()


_STALE_RUN_PAYLOADS_DDL = """
    CREATE TABLE run_payloads (
        id VARCHAR(36) PRIMARY KEY,
        tenant_id VARCHAR(64), user_id INTEGER NOT NULL,
        run_id VARCHAR(36) NOT NULL, step_id VARCHAR(36),
        payload_type VARCHAR(32) NOT NULL,
        enc_scheme VARCHAR(24) NOT NULL DEFAULT 'fernet.v1',
        ciphertext TEXT NOT NULL,
        plaintext_sha256 VARCHAR(64) NOT NULL,          -- obsolete round-1 column
        envelope_action_hash VARCHAR(64),
        byte_len INTEGER NOT NULL,
        expires_at TIMESTAMP, deleted_at TIMESTAMP, created_at TIMESTAMP NOT NULL
    )
"""


def _make_stale_run_payloads(eng, *, rows: int = 0):
    with eng.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS run_payloads"))
        c.execute(text(_STALE_RUN_PAYLOADS_DDL))
        for i in range(rows):
            c.execute(text(
                "INSERT INTO run_payloads (id, user_id, run_id, payload_type, "
                "ciphertext, plaintext_sha256, byte_len, created_at) VALUES "
                "(:id, 7, 'r1', 'tool_args', :ct, :h, 5, '2026-01-01 00:00:00')"
            ), {"id": f"stale{i}", "ct": f"gAAAAAstale{i}", "h": "d" * 64})


def test_stale_empty_run_payloads_is_repaired_automatically(eng):
    """A local dev DB from the earlier MP-0 iteration (run_payloads WITH
    `plaintext_sha256 NOT NULL`, ZERO rows) is rebuilt to the authoritative
    13-column shape — the obsolete NOT NULL column would otherwise reject
    authoritative inserts."""
    _base_1b(eng)
    _make_stale_run_payloads(eng, rows=0)
    assert "plaintext_sha256" in _cols(eng, "run_payloads")

    main._migrate_run_engine()
    main._verify_run_engine_schema()               # must NOT raise

    cols = _cols(eng, "run_payloads")
    assert "plaintext_sha256" not in cols          # repaired, not tolerated
    assert cols == main._RUN_ENGINE_REQUIRED_COLUMNS["run_payloads"]
    assert len(cols) == 13
    idx = _index_names(eng, "run_payloads")
    assert idx == {"ix_run_payloads_run", "ix_run_payloads_step",
                   "ix_run_payloads_owner", "ix_run_payloads_expiry"}
    # idempotent
    for _ in range(3):
        main._migrate_run_engine()
        main._verify_run_engine_schema()
    assert "plaintext_sha256" not in _cols(eng, "run_payloads")


def test_repaired_run_payloads_matches_authoritative_schema(eng, tmp_path):
    _base_1b(eng)
    _make_stale_run_payloads(eng, rows=0)
    main._migrate_run_engine()

    ref = create_engine(f"sqlite:///{tmp_path / 'ref.db'}")
    try:
        Base.metadata.create_all(ref)
        assert _cols(eng, "run_payloads") == _cols(ref, "run_payloads")
        assert _index_names(eng, "run_payloads") == _index_names(ref, "run_payloads")
        assert (
            {tuple(u["column_names"]) for u in inspect(eng).get_unique_constraints("run_payloads")}
            == {tuple(u["column_names"]) for u in inspect(ref).get_unique_constraints("run_payloads")}
        )
    finally:
        ref.dispose()


def test_stale_nonempty_run_payloads_fails_fast_without_touching_data(eng):
    _base_1b(eng)
    _make_stale_run_payloads(eng, rows=2)

    def _snapshot():
        with eng.connect() as c:
            return (
                c.execute(text("SELECT COUNT(*) FROM run_payloads")).scalar(),
                c.execute(text("SELECT id, ciphertext, plaintext_sha256, byte_len "
                               "FROM run_payloads ORDER BY id")).fetchall(),
                tuple(sorted(_cols(eng, "run_payloads"))),
            )

    before = _snapshot()
    with pytest.raises(RuntimeError) as ei:
        main._migrate_run_engine()
    msg = str(ei.value)
    assert "run_payloads" in msg and "2 row" in msg
    # the obsolete COLUMN NAME may appear; no VALUE from the table may
    assert "gAAAAA" not in msg          # no ciphertext
    assert "d" * 64 not in msg          # no stored digest value
    assert "stale0" not in msg and "stale1" not in msg  # no row ids
    assert _snapshot() == before                    # table, rows, ciphertext, columns unchanged
    # deterministic: a second startup raises the same way, still no mutation
    with pytest.raises(RuntimeError):
        main._migrate_run_engine()
    assert _snapshot() == before


def test_seal_open_round_trip_after_empty_stale_repair(eng, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from sqlalchemy.orm import sessionmaker
    from app.run_engine import seal_payload, open_payload

    _base_1b(eng)
    _make_stale_run_payloads(eng, rows=0)
    main._migrate_run_engine()
    main._verify_run_engine_schema()

    db = sessionmaker(bind=eng)()
    try:
        r = seal_payload(db, tenant_id=None, user_id=7, run_id="r1", step_id="s1",
                         payload_type="tool_args", plaintext="post-repair-ok")
        assert open_payload(db, r.id, expected_tenant_id=None, expected_user_id=7,
                            expected_run_id="r1", expected_step_id="s1",
                            expected_payload_type="tool_args",
                            expected_action_hash=None) == "post-repair-ok"
        r2 = seal_payload(db, tenant_id=None, user_id=7, run_id="r1", step_id=None,
                          payload_type="other", plaintext="")
        assert open_payload(db, r2.id, expected_tenant_id=None, expected_user_id=7,
                            expected_run_id="r1", expected_step_id=None,
                            expected_payload_type="other", expected_action_hash=None) == ""
    finally:
        db.close()


def test_verify_hard_fails_if_obsolete_column_survives(eng):
    """Verification must NOT report success for a shape that rejects
    authoritative inserts (reached only if the reconcile did not run)."""
    _base_1b(eng)
    Base.metadata.create_all(eng)              # all run tables authoritative...
    with eng.begin() as c:
        c.execute(text("DROP TABLE run_payloads"))
        c.execute(text(_STALE_RUN_PAYLOADS_DDL))   # ...but run_payloads is stale
    with pytest.raises(RuntimeError) as ei:
        main._verify_run_engine_schema()      # reconcile deliberately NOT called
    assert "run_payloads" in str(ei.value) and "plaintext_sha256" in str(ei.value)


def test_migration_crash_between_drop_and_create_self_heals(eng):
    _base_1b(eng)
    main._migrate_run_engine()                 # authoritative table exists
    with eng.begin() as c:                     # simulate a crash right after DROP
        c.execute(text("DROP TABLE run_payloads"))
    main._migrate_run_engine()                 # next startup
    main._verify_run_engine_schema()
    assert "run_payloads" in set(inspect(eng).get_table_names())
    assert _cols(eng, "run_payloads") == main._RUN_ENGINE_REQUIRED_COLUMNS["run_payloads"]


def test_unsupported_dialect_obsolete_column_fails_fast(eng, monkeypatch):
    _base_1b(eng)
    _make_stale_run_payloads(eng, rows=0)
    monkeypatch.setattr(eng.dialect, "name", "oracle")
    with pytest.raises(RuntimeError) as ei:
        main._reconcile_run_payloads_obsolete_column()
    assert "SQLite and PostgreSQL" in str(ei.value)
    assert "plaintext_sha256" in _cols(eng, "run_payloads")   # untouched


def test_verify_still_requires_authoritative_columns_even_with_extras(eng):
    _base_1b(eng)
    main._migrate_run_engine()
    with eng.begin() as c:
        c.execute(text("ALTER TABLE run_payloads ADD COLUMN junk_extra TEXT"))
        c.execute(text("ALTER TABLE run_payloads RENAME COLUMN ciphertext TO ciphertext_gone"))
    with pytest.raises(RuntimeError) as ei:
        main._verify_run_engine_schema()
    assert "run_payloads" in str(ei.value) and "ciphertext" in str(ei.value)


# ── upgrade from the current Phase 1B (1B.1 + 1B.2) schema ─────────


def test_upgrade_from_phase_1b_schema(eng):
    _base_1b(eng)
    assert "runs" not in set(inspect(eng).get_table_names())
    main._migrate_run_engine()
    main._verify_run_engine_schema()
    assert set(_RUN_TABLES) <= set(inspect(eng).get_table_names())
    # execution_turns is NOT touched by MP-0 (run_id is reused, no new column)
    et_cols = _cols(eng, "execution_turns")
    assert "run_id" in et_cols            # the existing 1B.2 column
    assert "thread_id" not in et_cols and "idempotency_key" not in et_cols


def test_repeat_migration_is_idempotent(eng):
    _base_1b(eng)
    for _ in range(3):
        main._migrate_run_engine()
        main._verify_run_engine_schema()
    # still exactly the expected tables/indexes, no dup errors
    for t in _RUN_TABLES:
        assert main._RUN_ENGINE_REQUIRED_COLUMNS[t] <= _cols(eng, t)


# ── ORM create_all agreement ─────────────────────────────────────


def test_orm_create_all_agrees_with_raw_ddl(eng, tmp_path):
    _base_1b(eng)
    main._migrate_run_engine()

    other = create_engine(f"sqlite:///{tmp_path / 'orm.db'}")
    try:
        Base.metadata.create_all(other)
        for t in _RUN_TABLES:
            assert _cols(eng, t) == _cols(other, t), f"{t} column mismatch"
            mig = _index_names(eng, t)
            orm = _index_names(other, t)
            # the migration adds partial-unique indexes create_all cannot; ignore
            assert (mig - orm) <= {"uq_runs_idem_scope", "uq_run_signals_live"}, \
                f"{t} migration-only indexes: {mig - orm}"
            assert (orm - mig) == set(), f"{t} create_all-only indexes: {orm - mig}"
            # unique constraints match
            mu = {tuple(u["column_names"]) for u in inspect(eng).get_unique_constraints(t)}
            ou = {tuple(u["column_names"]) for u in inspect(other).get_unique_constraints(t)}
            assert mu == ou, f"{t} unique-constraint mismatch: {mu} vs {ou}"
    finally:
        other.dispose()


# ── schema verification failure path ─────────────────────────────


def test_verify_detects_a_missing_column(eng):
    _base_1b(eng)
    main._migrate_run_engine()
    with eng.begin() as c:
        c.execute(text("ALTER TABLE runs RENAME COLUMN auth_session_ref TO gone"))
    with pytest.raises(RuntimeError) as ei:
        main._verify_run_engine_schema()
    assert "runs" in str(ei.value) and "auth_session_ref" in str(ei.value)


def test_verify_detects_a_missing_partial_unique_index(eng):
    _base_1b(eng)
    main._migrate_run_engine()
    with eng.begin() as c:
        c.execute(text("DROP INDEX uq_run_signals_live"))
    with pytest.raises(RuntimeError) as ei:
        main._verify_run_engine_schema()
    assert "uq_run_signals_live" in str(ei.value)


# ── existing Phase 1B rows are never mutated ──────────────────────


def test_migration_does_not_touch_existing_approval_rows(eng):
    _base_1b(eng)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO pending_approvals (id, action_hash, args_hash, tool_id, "
            "server_fingerprint, tool_schema_hash, user_id, action_nonce, "
            "policy_version, state, created_at, expires_at) VALUES "
            "('pa1','ah','a','t','fp','sh',7,'n','p','requested',:now,:now)"
        ), {"now": _NOW})
        c.execute(text(
            "INSERT INTO dispatch_records (id, tool_call_id, user_id, action_hash, "
            "outcome, started_at) VALUES ('d1','tc1',7,'ah','succeeded',:now)"
        ), {"now": _NOW})

    def _snapshot():
        with eng.connect() as c:
            return (
                c.execute(text("SELECT * FROM pending_approvals")).fetchall(),
                c.execute(text("SELECT * FROM dispatch_records")).fetchall(),
            )

    before = _snapshot()
    main._migrate_run_engine()
    main._migrate_run_engine()
    main._verify_run_engine_schema()
    assert _snapshot() == before


# ── PostgreSQL: enumerated hosted-deployment blocker ─────────────


@pytest.mark.skipif(not __import__("os").environ.get("EVAL_PG_TEST_URL"),
                    reason="PostgreSQL run-engine migration verification is a "
                           "hosted-deployment blocker; set EVAL_PG_TEST_URL")
def test_postgres_run_engine_migration():
    import os
    pg = create_engine(os.environ["EVAL_PG_TEST_URL"])
    import app.main as m
    m.engine = pg
    try:
        m._migrate_run_engine()
        m._migrate_run_engine()
        m._verify_run_engine_schema()
        assert set(_RUN_TABLES) <= set(inspect(pg).get_table_names())
    finally:
        with pg.begin() as c:
            for t in _RUN_TABLES:
                c.execute(text(f"DROP TABLE IF EXISTS {t} CASCADE"))
        pg.dispose()
