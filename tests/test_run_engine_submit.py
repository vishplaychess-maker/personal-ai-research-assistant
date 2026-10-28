"""Phase 1B.2R MP-1A — scoped idempotent run submission."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.models import Run, RunEvent
from app.run_engine.engine import get_run, submit_run
from app.run_engine.errors import (
    IdempotencyKeyConflict,
    IdempotencyKeyTooLong,
    RunNotFound,
    SessionBindingRequired,
)
from tests.run_engine_helpers import make_ctx, mk_engine, mk_session


@pytest.fixture
def db():
    eng = mk_engine()
    s = mk_session(eng)
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def test_first_submit_creates_run_with_server_id_and_one_event(db):
    ctx = make_ctx(db, sid="fam-1")
    run = submit_run(db, ctx, idempotency_key="k-1", session_id=3)

    assert run.status == "created"
    assert run.user_id == ctx.user_id
    assert run.auth_session_ref == "fam-1"
    assert run.version == 0
    assert run.last_event_seq == 1
    # server-generated opaque id (not the client key)
    assert run.id and run.id != "k-1" and len(run.id) == 36
    assert run.thread_id == f"run:{run.id}"

    events = db.query(RunEvent).filter(RunEvent.run_id == run.id).all()
    assert [e.type for e in events] == ["run.created"]
    assert events[0].run_seq == 1
    assert events[0].category == "control"


def test_replay_same_key_and_identity_returns_same_run_no_new_event(db):
    ctx = make_ctx(db)
    first = submit_run(db, ctx, idempotency_key="k-2", session_id=1)
    again = submit_run(db, ctx, idempotency_key="k-2", session_id=1)

    assert again.id == first.id
    assert db.query(Run).count() == 1
    assert db.query(RunEvent).filter(RunEvent.run_id == first.id).count() == 1


def test_same_key_different_identity_is_a_deterministic_conflict(db):
    ctx = make_ctx(db)
    first = submit_run(db, ctx, idempotency_key="k-3", session_id=1, kind="chat_stream")
    with pytest.raises(IdempotencyKeyConflict) as ei:
        submit_run(db, ctx, idempotency_key="k-3", session_id=1, kind="chat_sync")
    assert ei.value.run_id == first.id
    assert db.query(Run).count() == 1


def test_client_cannot_inject_identity_via_fingerprint_kwarg(db):
    """`request_fingerprint` was removed — a client-supplied value must never be
    an identity input. Passing it is a hard error, not a silent identity knob."""
    ctx = make_ctx(db)
    with pytest.raises(TypeError):
        submit_run(db, ctx, idempotency_key="k-4", session_id=1,
                   request_fingerprint="aaa")


def test_scope_is_per_user_and_per_session(db):
    a = make_ctx(db, user_id=1)
    b = make_ctx(db, user_id=2)
    ra = submit_run(db, a, idempotency_key="shared", session_id=9)
    rb = submit_run(db, b, idempotency_key="shared", session_id=9)
    rc = submit_run(db, a, idempotency_key="shared", session_id=10)
    assert len({ra.id, rb.id, rc.id}) == 3
    assert db.query(Run).count() == 3


def test_key_too_long_is_rejected(db):
    ctx = make_ctx(db)
    with pytest.raises(IdempotencyKeyTooLong):
        submit_run(db, ctx, idempotency_key="x" * 81, session_id=1)


def test_missing_key_is_rejected(db):
    ctx = make_ctx(db)
    with pytest.raises(ValueError):
        submit_run(db, ctx, idempotency_key="   ", session_id=1)


def test_bad_kind_is_rejected(db):
    ctx = make_ctx(db)
    with pytest.raises(ValueError):
        submit_run(db, ctx, idempotency_key="k", session_id=1, kind="nope")


def test_no_active_binding_fails_closed(db):
    ctx = make_ctx(db, binding_valid=False)
    with pytest.raises(SessionBindingRequired):
        submit_run(db, ctx, idempotency_key="k", session_id=1)
    assert db.query(Run).count() == 0


def test_unrelated_integrity_error_is_not_swallowed(db, monkeypatch):
    ctx = make_ctx(db)

    real_flush = db.flush
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("stmt", {}, Exception("NOT NULL constraint failed: runs.foo"))
        return real_flush(*a, **k)

    monkeypatch.setattr(db, "flush", boom)
    with pytest.raises(IntegrityError):
        submit_run(db, ctx, idempotency_key="k", session_id=1)


def test_insert_race_falls_back_to_the_winner(db, monkeypatch):
    """When the pre-SELECT misses but the INSERT then hits `uq_runs_idem_scope`
    (a concurrent submit won between the two), the loser rolls back, re-selects,
    and returns the winner's run — DB uniqueness is the final authority."""
    import app.run_engine.engine as eng_mod

    ctx = make_ctx(db)
    winner = submit_run(db, ctx, idempotency_key="race", session_id=5)

    real_select = eng_mod._select_by_idem_scope
    calls = {"n": 0}

    def blind_first_call(*a, **k):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_select(*a, **k)

    monkeypatch.setattr(eng_mod, "_select_by_idem_scope", blind_first_call)

    got = submit_run(db, ctx, idempotency_key="race", session_id=5)
    assert got.id == winner.id
    assert db.query(Run).count() == 1
    # no second run.created event for the winner
    assert db.query(RunEvent).filter(RunEvent.run_id == winner.id).count() == 1


def test_get_run_is_owner_scoped(db):
    owner = make_ctx(db, user_id=1)
    other = make_ctx(db, user_id=2)
    run = submit_run(db, owner, idempotency_key="k", session_id=1)
    assert get_run(db, owner, run.id).id == run.id
    with pytest.raises(RunNotFound):
        get_run(db, other, run.id)
