"""Phase 1B.2R MP-0 — ORM models for the durable run ledger.

Hermetic: in-memory / file SQLite via Base.metadata.create_all + the two
partial-unique indexes the migration adds.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models.models  # noqa: F401
from app.models.models import Run, RunEvent, RunPayload, RunSignal, RunStep

_NOW = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def _mk_engine(url="sqlite://"):
    eng = create_engine(url, connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_idem_scope ON runs "
            "(COALESCE(tenant_id, ''), user_id, COALESCE(session_id, -1), idempotency_key) "
            "WHERE idempotency_key IS NOT NULL"
        ))
        c.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_run_signals_live ON run_signals "
            "(run_id, kind) WHERE consumed_at IS NULL"
        ))
    return eng


@pytest.fixture
def db():
    eng = _mk_engine()
    s = sessionmaker(bind=eng)()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def _run(db, **kw):
    kw.setdefault("id", "run-" + str(len(db.query(Run).all())))
    kw.setdefault("user_id", 7)
    kw.setdefault("thread_id", "run:" + kw["id"])
    kw.setdefault("created_at", _NOW)
    kw.setdefault("updated_at", _NOW)
    r = Run(**kw)
    db.add(r)
    db.commit()
    return r


# ── basic inserts + defaults ───────────────────────────────────────


def test_insert_all_five_models_with_defaults(db):
    r = _run(db)
    assert r.status == "created" and r.kind == "chat_stream"
    assert r.version == 0 and r.last_event_seq == 0
    db.add(RunStep(id="s1", run_id=r.id, user_id=7, seq=1, kind="llm_generate",
                   created_at=_NOW))
    db.add(RunEvent(id="e1", run_id=r.id, run_seq=1, user_id=7, type="status",
                    created_at=_NOW))
    db.add(RunSignal(id="g1", run_id=r.id, kind="cancel_requested",
                     created_by_user_id=7, created_at=_NOW))
    db.add(RunPayload(id="p1", user_id=7, run_id=r.id, payload_type="tool_args",
                      ciphertext="gAAAAA...", byte_len=3, created_at=_NOW))
    db.commit()
    step = db.get(RunStep, "s1")
    assert step.status == "pending" and step.engine == "native" and step.attempt == 0
    assert db.get(RunEvent, "e1").category == "control"
    assert db.get(RunPayload, "p1").enc_scheme == "fernet.v1"


# ── uniqueness ────────────────────────────────────────────────────


def test_run_steps_unique_run_seq(db):
    r = _run(db)
    db.add(RunStep(id="a", run_id=r.id, user_id=7, seq=1, kind="x", created_at=_NOW))
    db.commit()
    db.add(RunStep(id="b", run_id=r.id, user_id=7, seq=1, kind="y", created_at=_NOW))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_run_events_unique_run_seq(db):
    r = _run(db)
    db.add(RunEvent(id="a", run_id=r.id, run_seq=1, user_id=7, type="token", created_at=_NOW))
    db.commit()
    db.add(RunEvent(id="b", run_id=r.id, run_seq=1, user_id=7, type="token", created_at=_NOW))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_run_signals_one_live_per_kind(db):
    r = _run(db)
    db.add(RunSignal(id="a", run_id=r.id, kind="approval_decided",
                     created_by_user_id=7, created_at=_NOW))
    db.commit()
    # a second LIVE (unconsumed) approval_decided for the same run -> rejected
    db.add(RunSignal(id="b", run_id=r.id, kind="approval_decided",
                     created_by_user_id=7, created_at=_NOW))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    # once the first is consumed, a new one is allowed
    db.query(RunSignal).filter_by(id="a").update({"consumed_at": _NOW})
    db.commit()
    db.add(RunSignal(id="c", run_id=r.id, kind="approval_decided",
                     created_by_user_id=7, created_at=_NOW))
    db.commit()
    # a different kind is independent
    db.add(RunSignal(id="d", run_id=r.id, kind="cancel_requested",
                     created_by_user_id=7, created_at=_NOW))
    db.commit()


# ── idempotency-key scope (D3) ────────────────────────────────────


def test_idempotency_key_uniqueness_is_scoped(db):
    _run(db, id="r1", user_id=7, session_id=1, idempotency_key="K")
    # same key, DIFFERENT scope -> allowed
    _run(db, id="r2", user_id=8, session_id=1, idempotency_key="K")          # diff user
    _run(db, id="r3", user_id=7, session_id=2, idempotency_key="K")          # diff session
    _run(db, id="r4", user_id=7, session_id=1, tenant_id="acme", idempotency_key="K")  # diff tenant
    # same key, SAME (tenant,user,session) -> rejected
    db.add(Run(id="r5", user_id=7, session_id=1, thread_id="run:r5",
               idempotency_key="K", created_at=_NOW, updated_at=_NOW))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    # NULL idempotency_key rows never collide (partial index)
    _run(db, id="n1", user_id=7, session_id=1)
    _run(db, id="n2", user_id=7, session_id=1)


def test_null_session_and_tenant_collapse_consistently(db):
    _run(db, id="r1", user_id=7, session_id=None, tenant_id=None, idempotency_key="K")
    db.add(Run(id="r2", user_id=7, session_id=None, tenant_id=None, thread_id="run:r2",
               idempotency_key="K", created_at=_NOW, updated_at=_NOW))
    with pytest.raises(IntegrityError):
        db.commit()          # COALESCE(NULL,'') / COALESCE(NULL,-1) => same bucket
    db.rollback()


# ── ownership isolation (query-level) ─────────────────────────────


def test_owner_scoped_queries_isolate_users_and_tenants(db):
    _run(db, id="a", user_id=7, tenant_id=None)
    _run(db, id="b", user_id=8, tenant_id=None)
    _run(db, id="c", user_id=7, tenant_id="acme")
    mine = db.query(Run).filter(Run.user_id == 7, Run.tenant_id.is_(None)).all()
    assert {r.id for r in mine} == {"a"}
    assert db.query(RunPayload).filter_by(user_id=999).count() == 0
