import logging
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app import paths
from app.config import settings

logger = logging.getLogger(__name__)


def _is_postgres(url: str) -> bool:
    return url.startswith("postgres://") or url.startswith("postgresql://")


def _normalized_database_url(url: str) -> str:
    # Render-style "postgres://" URLs are legacy; SQLAlchemy needs "postgresql://".
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def resolve_database_url() -> str:
    """Resolve the SQLAlchemy URL for this process.

    Invariants (Phase 1 - .exe packaging):

    * An explicit DATABASE_URL always wins - the process environment first,
      then the pydantic-settings value (which covers a .env file). The
      Render/cloud and docker-compose paths are therefore unchanged.
    * With no DATABASE_URL anywhere, the portable SQLite file from
      app.paths.get_db_path() is used: repo-relative in dev, and
      %APPDATA%/ThunderAI/thunder.db when running frozen.
    """
    explicit = (os.environ.get("DATABASE_URL") or "").strip()
    if explicit:
        return _normalized_database_url(explicit)
    configured = (settings.database_url or "").strip()
    if configured:
        return _normalized_database_url(configured)
    return "sqlite:///" + paths.get_db_path().as_posix()


def _ensure_sqlite_parent_dir(url: str) -> None:
    """Best-effort: create the parent directory of a file-backed SQLite DB.

    Fail-closed: an unusable location only logs a bounded warning here; the
    real connection error surfaces later where callers already handle it.
    """
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return
    tail = url[len(prefix):]
    if not tail or tail == ":memory:":
        return
    try:
        Path(tail).expanduser().parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "database: could not create SQLite directory (%s)", type(exc).__name__
        )


_database_url = resolve_database_url()
_ensure_sqlite_parent_dir(_database_url)

# SQLite requires check_same_thread=False for FastAPI's threadpool sessions.
# PostgreSQL drivers (psycopg2) reject unknown connect_args, so pass them
# conditionally and use pool_pre_ping to survive managed-DB idle disconnects.
if _database_url.startswith("sqlite"):
    engine = create_engine(
        _database_url,
        connect_args={"check_same_thread": False},
        echo=False,
    )
elif _is_postgres(_database_url):
    engine = create_engine(
        _database_url,
        pool_pre_ping=True,
        echo=False,
    )
else:
    engine = create_engine(_database_url, echo=False)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency that yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all SQLAlchemy tables."""
    Base.metadata.create_all(bind=engine)
