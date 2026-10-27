"""The suite must never touch the developer's real database.

These tests are the guard rail, not the mechanism: they would have failed for
years while every run quietly wrote to ``data/thunder.db``.
"""

import sqlite3
import uuid
from pathlib import Path

import pytest

from app import database
from app.database import SessionLocal
from app.models.models import User
from tests.conftest import REAL_DB_PATH, TEST_DB_PATH


def _engine_file() -> Path:
    return Path(str(database.engine.url.database or "")).resolve()


def test_engine_is_pointed_at_the_throwaway_database():
    assert TEST_DB_PATH is not None, "conftest did not install a test database"
    assert _engine_file() == TEST_DB_PATH.resolve()


def test_engine_is_not_the_real_dev_database():
    assert _engine_file() != REAL_DB_PATH


def test_conftest_database_url_wins_over_the_repo_default():
    import os

    url = os.environ["DATABASE_URL"]
    assert "thunder-test.db" in url.replace("\\", "/")
    assert "data/thunder.db" not in url.replace("\\", "/")


def test_a_written_user_never_reaches_the_real_dev_database():
    """Write through the app session, then read the real file read-only."""
    if not REAL_DB_PATH.exists():
        pytest.skip("no developer database on this machine")

    marker = f"isolation_probe_{uuid.uuid4().hex[:10]}"
    db = SessionLocal()
    try:
        db.add(User(username=marker, email=f"{marker}@example.com"))
        db.commit()
        assert db.query(User).filter(User.username == marker).count() == 1

        real = sqlite3.connect(f"file:{REAL_DB_PATH.as_posix()}?mode=ro", uri=True)
        try:
            found = real.execute(
                "select count(*) from users where username = ?", (marker,)
            ).fetchone()[0]
        except sqlite3.OperationalError:
            found = 0  # no schema in the developer database yet
        finally:
            real.close()
        assert found == 0, "a test write leaked into data/thunder.db"
    finally:
        db.query(User).filter(User.username == marker).delete()
        db.commit()
        db.close()


def test_the_default_user_is_seeded_in_the_test_database():
    """Suites that skip the app lifespan still get the account the app assumes."""
    db = SessionLocal()
    try:
        assert db.query(User).filter(User.username == "default").count() == 1
    finally:
        db.close()
