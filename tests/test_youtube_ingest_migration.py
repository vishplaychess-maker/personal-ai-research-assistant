"""YouTube Knowledge Ingestion (Phase 1) — additive ``memories`` migration.

Hermetic: runs the REAL ``app.main._migrate_memories_source_columns`` against a
throwaway FILE SQLite database (``app.main.engine`` is monkeypatched). No
network, no live backend, no PostgreSQL.

What is pinned:
  * the pre-existing (legacy) column set is preserved exactly — no column is
    dropped or retyped, and existing ROWS survive with NULL in the new columns;
  * the five new columns and their indexes appear;
  * a second run is a no-op (idempotent);
  * the migration is skipped cleanly when the table does not exist.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

import app.main as main
import app.models.models  # noqa: F401  (register the ORM models on Base)

#: The pre-existing shape of `memories` before this slice.
_LEGACY_MEMORIES_DDL = """
CREATE TABLE memories (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    session_id INTEGER,
    content TEXT NOT NULL,
    category VARCHAR(30) NOT NULL,
    created_at TIMESTAMP NOT NULL,
    last_used_at TIMESTAMP NOT NULL,
    last_accessed_at TIMESTAMP,
    access_count INTEGER NOT NULL DEFAULT 0
)
"""

_NEW_COLUMNS = ("source_url", "source_title", "source_kind", "source_meta",
                "content_hash")
_NEW_INDEXES = ("ix_memories_source_url", "ix_memories_source_kind",
                "ix_memories_content_hash")
_NOW = "2026-01-01 00:00:00"


@pytest.fixture
def eng(tmp_path, monkeypatch):
    e = create_engine(f"sqlite:///{tmp_path / 'yt-mig.db'}")
    monkeypatch.setattr(main, "engine", e)
    try:
        yield e
    finally:
        e.dispose()


def _cols(e, table):
    return {c["name"] for c in inspect(e).get_columns(table)}


def _indexes(e, table):
    return {i["name"] for i in inspect(e).get_indexes(table)}


def _make_legacy_memories(e, *, rows: int = 1):
    with e.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS memories"))
        c.execute(text(_LEGACY_MEMORIES_DDL))
        for i in range(rows):
            c.execute(text(
                "INSERT INTO memories (id, user_id, session_id, content, category, "
                "created_at, last_used_at, access_count) "
                "VALUES (:id, 1, 1, :content, 'fact', :now, :now, 0)"
            ), {"id": i + 1, "content": f"legacy memory {i + 1}", "now": _NOW})


def test_new_columns_and_indexes_are_added(eng):
    _make_legacy_memories(eng)
    before = _cols(eng, "memories")
    assert not (set(_NEW_COLUMNS) & before), "fixture must start without the new columns"

    main._migrate_memories_source_columns()

    after = _cols(eng, "memories")
    for column in _NEW_COLUMNS:
        assert column in after, column
    # Every pre-existing column is untouched.
    assert before <= after
    for index in _NEW_INDEXES:
        assert index in _indexes(eng, "memories"), index


def test_existing_rows_survive_with_null_sources(eng):
    _make_legacy_memories(eng, rows=3)

    main._migrate_memories_source_columns()

    with eng.begin() as c:
        rows = list(c.execute(text(
            "SELECT id, content, source_url, source_title, source_kind, "
            "source_meta, content_hash FROM memories ORDER BY id")))
    assert len(rows) == 3
    assert [r[1] for r in rows] == ["legacy memory 1", "legacy memory 2", "legacy memory 3"]
    for row in rows:
        assert row[2] is None and row[3] is None and row[4] is None
        assert row[5] is None and row[6] is None


def test_migration_is_idempotent(eng):
    _make_legacy_memories(eng)
    main._migrate_memories_source_columns()
    first = _cols(eng, "memories")
    first_idx = _indexes(eng, "memories")

    main._migrate_memories_source_columns()      # second run must be a clean no-op
    main._migrate_memories_source_columns()      # and a third

    assert _cols(eng, "memories") == first
    assert _indexes(eng, "memories") == first_idx
    with eng.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM memories")).scalar() == 1


def test_migration_skips_cleanly_without_the_table(eng):
    with eng.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS memories"))
    # Must not raise.
    main._migrate_memories_source_columns()
    assert "memories" not in set(inspect(eng).get_table_names())


def test_fresh_create_all_already_has_the_columns(eng):
    """A fresh install is covered by create_all() — the ALTERs are only for
    pre-existing databases, so this must be a no-op on a fresh schema."""
    from app.database import Base

    Base.metadata.create_all(eng)
    cols = _cols(eng, "memories")
    for column in _NEW_COLUMNS:
        assert column in cols, column
    main._migrate_memories_source_columns()      # idempotent on a fresh schema
    assert _cols(eng, "memories") == cols
