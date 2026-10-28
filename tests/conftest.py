"""Shared pytest fixtures and session-scoped setup for integration tests.

The suite runs against its own SQLite file, never the repo's real
``data/thunder.db`` (see ``_configure_test_database``). Ollama is pre-warmed once
per session so streaming tests do not pay the cold start.
"""

import logging
import os
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest

logger = logging.getLogger(__name__)

OLLAMA_WARMUP_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2:3b"

REPO_ROOT = Path(__file__).resolve().parents[1]
#: The developer's real database. No test run may read or write it.
REAL_DB_PATH = (REPO_ROOT / "data" / "thunder.db").resolve()


def _test_db_dir() -> Path:
    """Directory holding the throwaway test database.

    ``THUNDER_TEST_DB_DIR`` overrides it (set it to a fast local disk); the
    system temp directory is the portable default.
    """
    override = (os.environ.get("THUNDER_TEST_DB_DIR") or "").strip()
    base = Path(override) if override else Path(tempfile.gettempdir()) / "thunder-tests"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _configure_test_database() -> Path | None:
    """Point ``DATABASE_URL`` at a throwaway SQLite file.

    This runs at conftest import, i.e. before any test module imports ``app``:
    ``app.database`` resolves the URL exactly once, at import time, so a later
    assignment would be ignored and the suite would quietly use the real
    ``data/thunder.db`` (it did, for a long time).

    An operator who deliberately set ``DATABASE_URL`` to some other database
    (Postgres in CI, a scratch file) keeps it; only the unset/default case and
    an explicit pointer at the repo database are redirected.
    """
    explicit = (os.environ.get("DATABASE_URL") or "").strip()
    if explicit and "data/thunder.db" not in explicit.replace("\\", "/"):
        logger.info("Tests will use the operator-provided DATABASE_URL")
        # Nothing of ours to clean up: the operator owns that database.
        return None

    db_path = (_test_db_dir() / "thunder-test.db").resolve()
    # A leftover file from an aborted run would make results depend on it. When
    # another run still holds it (two sessions overlapping on Windows), fall
    # back to a unique name instead of failing the whole session.
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(db_path) + suffix)
        if not stale.exists():
            continue
        try:
            stale.unlink()
        except OSError:
            db_path = (
                _test_db_dir() / f"thunder-test-{os.getpid()}-{uuid.uuid4().hex[:6]}.db"
            ).resolve()
            break
    os.environ["DATABASE_URL"] = "sqlite:///" + db_path.as_posix()
    logger.info("Tests will use an isolated database at %s", db_path)
    return db_path


#: Import-time side effect: must stay above any ``app`` import in this file.
TEST_DB_PATH = _configure_test_database()

# Mem0 warm-up opens the real local Qdrant store and Ollama embedder; the test
# harness must never do that (individual tests mock the Mem0 seams instead).
os.environ["MEM0_WARMUP_ENABLED"] = "false"


def assert_test_database_is_isolated() -> None:
    """Fail fast when the engine would touch the repo's real database.

    Runs after ``app.database`` is imported, because that module is what turns
    the environment into an engine. A silent fallback here would let a whole
    run mutate developer data, which is exactly the bug this guards.
    """
    from app import database

    url = database.engine.url
    if url.get_backend_name() != "sqlite":
        return
    engine_file = Path(str(url.database or "")).resolve()
    if engine_file == REAL_DB_PATH:
        raise RuntimeError(
            "Refusing to run tests against the real dev database "
            f"({REAL_DB_PATH}). Set THUNDER_TEST_DB_DIR to a scratch directory, "
            "or unset DATABASE_URL so the suite can create its own."
        )


def _seed_default_user() -> None:
    """Create the 'default' account the app assumes exists (see main._create_default_user)."""
    from app.database import SessionLocal
    from app.models.models import User

    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == "default").first() is None:
            db.add(User(username="default", email="default@example.com"))
            db.commit()
    except Exception as exc:  # noqa: BLE001 - schema may be unavailable in a DB-less suite
        db.rollback()
        logger.warning("Could not seed the default test user (%s)", type(exc).__name__)
    finally:
        db.close()


def warmup_ollama() -> None:
    """Send a minimal generation request to load the Ollama model into memory.

    This is a best-effort warmup. If Ollama is unavailable, the model will
    be loaded on first actual request (cold start), which is slower but
    functionally correct.
    """
    try:
        with httpx.Client(timeout=60.0) as client:
            payload = {
                "model": OLLAMA_MODEL,
                "prompt": "Hello",
                "stream": False,
                "options": {"num_predict": 1},
            }
            resp = client.post(OLLAMA_WARMUP_URL, json=payload)
            if resp.status_code == 200:
                logger.info("Ollama model %s pre-warmed successfully", OLLAMA_MODEL)
            else:
                logger.warning(
                    "Ollama warmup responded with HTTP %s: %.100s",
                    resp.status_code, resp.text,
                )
    except Exception as exc:
        logger.warning(
            "Ollama warmup failed (model will load on first request): %s", exc,
        )


@pytest.fixture(scope="session", autouse=True)
def _ensure_app_database_schema() -> None:
    """Create the app database schema once per session.

    Some suites (for example tests/test_approvals_api.py) wipe their tables in
    an autouse fixture that runs BEFORE TestClient enters the app lifespan. On a
    fresh database that wipe raises "no such table". Preparing the schema here
    keeps the existing suites runnable on a clean checkout and on the new
    portable database path.

    Fail-closed: a database that cannot be prepared only logs a warning.
    """
    try:
        import app.models.models  # noqa: F401 - register all ORM tables

        from app.database import init_db

        init_db()
        assert_test_database_is_isolated()
        _seed_default_user()
    except RuntimeError:
        raise  # the isolation guard must stop the run, not be swallowed
    except Exception as exc:  # noqa: BLE001 - DB-less suites must still run
        logger.warning(
            "Could not pre-create the app database schema: %s",
            type(exc).__name__,
        )
    yield


@pytest.fixture(scope="session", autouse=True)
def _ollama_warmup() -> None:
    """Session-scoped autouse fixture: pre-warms Ollama once per test run."""
    warmup_ollama()


def pytest_sessionfinish(session, exitstatus) -> None:
    """Remove the throwaway database (and its WAL sidecars) after the run."""
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if "thunder-test.db" not in url.replace("\\", "/"):
        return
    try:
        from app import database

        database.engine.dispose()
    except Exception:  # noqa: BLE001 - cleanup is best effort
        pass
    if TEST_DB_PATH is None:
        return
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(TEST_DB_PATH) + suffix)
        try:
            if candidate.exists():
                candidate.unlink()
        except OSError as exc:
            logger.warning(
                "Could not remove test database file %s (%s)",
                candidate.name,
                type(exc).__name__,
            )
