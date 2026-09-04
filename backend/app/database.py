from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import settings

def _is_postgres(url: str) -> bool:
    return url.startswith("postgres://") or url.startswith("postgresql://")


def _normalized_database_url(url: str) -> str:
    # Render-style "postgres://" URLs are legacy; SQLAlchemy needs "postgresql://".
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


_database_url = _normalized_database_url(settings.database_url)

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
