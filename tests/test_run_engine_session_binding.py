"""Phase 1B.2R MP-1A — fresh DB-backed session-family validation.

The cached ``AuthContext.durable_session_binding_valid`` boolean is NOT
authorization. Every RunEngine op re-checks the family against the live DB, so a
context built while the family was active must stop working the instant the
family is revoked / expired / logged out / reuse-revoked.
"""

from __future__ import annotations

import pytest

from app.models.models import Run
from app.run_engine.engine import (
    get_run,
    request_cancellation,
    submit_run,
    transition_run,
)
from app.run_engine.events import append_control_event, read_control_events
from app.run_engine.errors import (
    RunNotFound,
    SessionBindingMismatch,
    SessionBindingRequired,
)
from tests.run_engine_helpers import (
    expire_family,
    make_ctx,
    mk_engine,
    mk_session,
    revoke_family,
    seed_family,
)


@pytest.fixture
def db():
    eng = mk_engine()
    s = mk_session(eng)
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def _all_ops_fail(db, ctx, run, exc):
    with pytest.raises(exc):
        submit_run(db, ctx, idempotency_key="new-key", session_id=1)
    with pytest.raises(exc):
        get_run(db, ctx, run.id)
    with pytest.raises(exc):
        read_control_events(db, ctx, run.id)
    with pytest.raises(exc):
        append_control_event(db, ctx, run.id, type="run.queued")
    with pytest.raises(exc):
        transition_run(db, ctx, run.id, to="queued", expected_version=0)
    with pytest.raises(exc):
        request_cancellation(db, ctx, run.id)


def test_revoke_family_after_context_build_fails_every_op(db):
    ctx = make_ctx(db, sid="fam-1")
    run = submit_run(db, ctx, idempotency_key="k", session_id=1)
    revoke_family(db, "fam-1")          # logout / reuse-detection revocation
    _all_ops_fail(db, ctx, run, SessionBindingRequired)


def test_expire_family_after_context_build_fails_every_op(db):
    ctx = make_ctx(db, sid="fam-2")
    run = submit_run(db, ctx, idempotency_key="k", session_id=1)
    expire_family(db, "fam-2")
    _all_ops_fail(db, ctx, run, SessionBindingRequired)


def test_family_belonging_to_another_user_is_not_accepted(db):
    from sqlalchemy import text

    # user 7 has a run bound to fam-3; that family row is then swapped to user 9
    ctx = make_ctx(db, user_id=7, sid="fam-3")
    run = submit_run(db, ctx, idempotency_key="k", session_id=1)
    db.execute(text("UPDATE refresh_sessions SET user_id=9 WHERE family_id='fam-3'"))
    db.commit()
    # the family no longer belongs to ctx.user_id (7) -> fail closed
    with pytest.raises(SessionBindingRequired):
        get_run(db, ctx, run.id)


def test_legacy_token_without_sid_cannot_touch_a_durable_run(db):
    owner = make_ctx(db, sid="fam-4")
    run = submit_run(db, owner, idempotency_key="k", session_id=1)
    legacy = make_ctx(db, sid=None, binding_valid=False)
    with pytest.raises(SessionBindingRequired):
        get_run(db, legacy, run.id)
    with pytest.raises(SessionBindingRequired):
        submit_run(db, legacy, idempotency_key="k2", session_id=1)


def test_another_active_family_of_the_same_user_cannot_read_the_run(db):
    """5C default = STRICT session-bound access. A user's second active login
    (a live but DIFFERENT family) is refused for a run started elsewhere."""
    owner = make_ctx(db, user_id=5, sid="fam-a")
    run = submit_run(db, owner, idempotency_key="k", session_id=1)
    other_login = make_ctx(db, user_id=5, sid="fam-b")  # live, same user, other family
    with pytest.raises(SessionBindingMismatch):
        get_run(db, other_login, run.id)
    with pytest.raises(SessionBindingMismatch):
        read_control_events(db, other_login, run.id)
    with pytest.raises(SessionBindingMismatch):
        transition_run(db, other_login, run.id, to="queued", expected_version=0)
    with pytest.raises(SessionBindingMismatch):
        request_cancellation(db, other_login, run.id)


def test_no_existence_leak_across_users(db):
    owner = make_ctx(db, user_id=1, sid="fam-o")
    run = submit_run(db, owner, idempotency_key="k", session_id=1)
    stranger = make_ctx(db, user_id=2, sid="fam-s")
    # a run owned by someone else is indistinguishable from a missing one
    with pytest.raises(RunNotFound):
        get_run(db, stranger, run.id)
    with pytest.raises(RunNotFound):
        get_run(db, stranger, "00000000-0000-0000-0000-000000000000")


def test_revocation_between_validate_and_commit_is_caught_before_commit(db, monkeypatch):
    """The final pre-commit re-check (inside the mutation's own session, as
    close to the write as the txn model allows) catches a family revoked after
    the entry check — the state change is never committed."""
    import datetime as _dt

    from sqlalchemy import text

    import app.run_engine.session_binding as sb

    ctx = make_ctx(db, sid="fam-race")
    run = submit_run(db, ctx, idempotency_key="k", session_id=1)

    real = sb.validate_session_binding
    calls = {"n": 0}

    def revoke_then_validate(db_, ctx_, *, run=None):
        calls["n"] += 1
        if calls["n"] == 2:  # after the entry check, before the pre-commit check
            db_.execute(
                text("UPDATE refresh_sessions SET revoked_at = :t "
                     "WHERE family_id = 'fam-race'"),
                {"t": _dt.datetime.utcnow()},
            )  # no commit — same uncommitted transaction as the CAS
        return real(db_, ctx_, run=run)

    monkeypatch.setattr("app.run_engine.engine.validate_session_binding",
                        revoke_then_validate)
    with pytest.raises(SessionBindingRequired):
        transition_run(db, ctx, run.id, to="queued", expected_version=0)
    db.rollback()
    assert db.query(Run).filter(Run.id == run.id).one().status == "created"
