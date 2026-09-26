"""Phase 1B.2R MP-3C3D5C — durable multi-agent submission: `Run.kind`
mapping, semantic idempotency, and the active-run cap
(`submission.enforce_multi_agent_active_run_cap`). Reuses D3A's exact
`/api/runs` hermetic TestClient fixtures for the real-route proofs (ladder
rung 2) rather than re-deriving a second copy.
"""
from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.models.models import Run
from app.run_engine import submission as sub
from app.run_engine.errors import IdempotencyKeyConflict
from app.run_engine.multi_agent_orchestration import MULTI_AGENT_RUN_KIND
from tests.run_engine_helpers import ensure_user, make_ctx, mk_engine, mk_session

_HMAC = "unit-test-hmac-key-multi-agent-submit0"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_IDEMPOTENCY_HMAC_KEY", _HMAC)


@pytest.fixture
def eng():
    e = mk_engine()
    try:
        yield e
    finally:
        e.dispose()


def _submission(**overrides):
    kwargs = dict(
        session_id=1, message="hi", image_url=None, provider="p", model="m",
        mode="multi_agent", system_prompt=None, options={},
    )
    kwargs.update(overrides)
    return sub.build_chat_submission(**kwargs)


def _seed_session(db, *, uid=7, sid=1):
    ensure_user(db, uid)
    from app.models.models import ResearchSession
    if db.query(ResearchSession).filter(ResearchSession.id == sid).first() is None:
        db.add(ResearchSession(id=sid, user_id=uid, title="t"))
        db.commit()


# ── Run.kind mapping ──────────────────────────────────────────────


def test_multi_agent_mode_maps_to_multi_agent_run_kind(eng):
    db = mk_session(eng)
    try:
        _seed_session(db)
        ctx = make_ctx(db, user_id=7, sid="fam-a")
        run, created = sub.create_chat_run(
            db, ctx, submission=_submission(), idempotency_key=uuid.uuid4().hex,
        )
        assert created and run.kind == MULTI_AGENT_RUN_KIND == "multi_agent"
    finally:
        db.close()


# ── semantic idempotency: replay vs conflict ───────────────────────


def test_same_key_same_submission_returns_same_run_zero_duplicate_rows(eng):
    db = mk_session(eng)
    try:
        _seed_session(db)
        ctx = make_ctx(db, user_id=7, sid="fam-a")
        key = uuid.uuid4().hex
        run1, created1 = sub.create_chat_run(db, ctx, submission=_submission(),
                                             idempotency_key=key)
        run2, created2 = sub.create_chat_run(db, ctx, submission=_submission(),
                                             idempotency_key=key)
        assert created1 is True and created2 is False
        assert run1.id == run2.id
        assert db.query(Run).filter(Run.id == run1.id).count() == 1
    finally:
        db.close()


@pytest.mark.parametrize("changed_field,changed_value", [
    ("message", "a different prompt entirely"),
    ("model", "different-model"),
    ("provider", "different-provider"),
    ("mode", "chat"),
])
def test_same_key_changed_semantics_conflicts(eng, changed_field, changed_value):
    db = mk_session(eng)
    try:
        _seed_session(db)
        ctx = make_ctx(db, user_id=7, sid="fam-a")
        key = uuid.uuid4().hex
        sub.create_chat_run(db, ctx, submission=_submission(), idempotency_key=key)
        with pytest.raises(IdempotencyKeyConflict):
            sub.create_chat_run(db, ctx, submission=_submission(**{changed_field: changed_value}),
                                idempotency_key=key)
    finally:
        db.close()


# ── active-run cap ──────────────────────────────────────────────


def test_cap_is_a_noop_for_non_multi_agent_modes(eng):
    db = mk_session(eng)
    try:
        ctx = make_ctx(db, user_id=7, sid="fam-a")
        sub.enforce_multi_agent_active_run_cap(
            db, ctx, resolved_mode="chat", idempotency_key="k", session_id=1,
        )  # must not raise regardless of any cap setting
    finally:
        db.close()


def test_concurrent_admission_serializes_through_commit(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import create_engine, text
    from app.database import Base
    from app.models.models import Message, RunPayload, RunStep
    from tests.run_engine_helpers import _PARTIAL_INDEXES

    engine = create_engine(f"sqlite:///{tmp_path / 'admission.db'}",
                           connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for ddl in _PARTIAL_INDEXES:
            conn.execute(text(ddl))
    with mk_session(engine) as db:
        _seed_session(db)
        ctx = make_ctx(db, user_id=7, sid="concurrent-family")
    monkeypatch.setattr(settings, "multi_agent_max_active_runs_per_user", 1)
    barrier = Barrier(2)

    def submit(key):
        with mk_session(engine) as db:
            barrier.wait(timeout=10)
            try:
                run, created = sub.create_chat_run(db, ctx, submission=_submission(), idempotency_key=key)
                return key, created
            except sub.MultiAgentActiveRunLimitExceeded:
                db.rollback()
                return key, False

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, ["concurrent-a", "concurrent-b"]))
        assert sorted(created for _, created in results) == [False, True]
        winner = next(key for key, created in results if created)
        with mk_session(engine) as db:
            assert [db.query(model).count() for model in (Run, Message, RunStep, RunPayload)] == [1, 1, 1, 1]
            _, created = sub.create_chat_run(db, ctx, submission=_submission(), idempotency_key=winner)
            assert created is False
            with pytest.raises(IdempotencyKeyConflict):
                sub.create_chat_run(db, ctx, submission=_submission(message="changed"), idempotency_key=winner)
            db.rollback()
    finally:
        engine.dispose()


def test_cap_rejects_a_new_run_once_at_the_limit(eng, monkeypatch):
    monkeypatch.setattr(settings, "multi_agent_max_active_runs_per_user", 1)
    db = mk_session(eng)
    try:
        _seed_session(db)
        ctx = make_ctx(db, user_id=7, sid="fam-a")
        sub.create_chat_run(db, ctx, submission=_submission(),
                            idempotency_key=uuid.uuid4().hex)  # 1 active run now
        with pytest.raises(sub.MultiAgentActiveRunLimitExceeded):
            sub.enforce_multi_agent_active_run_cap(
                db, ctx, resolved_mode="multi_agent",
                idempotency_key=uuid.uuid4().hex, session_id=1,
            )
    finally:
        db.close()


def test_cap_allows_idempotent_replay_of_an_existing_run_even_at_the_limit(eng, monkeypatch):
    monkeypatch.setattr(settings, "multi_agent_max_active_runs_per_user", 1)
    db = mk_session(eng)
    try:
        _seed_session(db)
        ctx = make_ctx(db, user_id=7, sid="fam-a")
        key = uuid.uuid4().hex
        sub.create_chat_run(db, ctx, submission=_submission(), idempotency_key=key)
        # replay of the SAME key — must never be rejected by the cap even
        # though the user is already at the limit.
        sub.enforce_multi_agent_active_run_cap(
            db, ctx, resolved_mode="multi_agent", idempotency_key=key, session_id=1,
        )
    finally:
        db.close()


def test_cap_does_not_count_terminal_runs(eng, monkeypatch):
    monkeypatch.setattr(settings, "multi_agent_max_active_runs_per_user", 1)
    db = mk_session(eng)
    try:
        _seed_session(db)
        ctx = make_ctx(db, user_id=7, sid="fam-a")
        run, _ = sub.create_chat_run(db, ctx, submission=_submission(),
                                     idempotency_key=uuid.uuid4().hex)
        run.status = "succeeded"
        db.commit()
        # the one existing run is now terminal — a fresh submission must be allowed.
        sub.enforce_multi_agent_active_run_cap(
            db, ctx, resolved_mode="multi_agent",
            idempotency_key=uuid.uuid4().hex, session_id=1,
        )
    finally:
        db.close()


def test_cap_is_per_user_isolated(eng, monkeypatch):
    monkeypatch.setattr(settings, "multi_agent_max_active_runs_per_user", 1)
    db = mk_session(eng)
    try:
        _seed_session(db, uid=7, sid=1)
        _seed_session(db, uid=8, sid=2)
        ctx7 = make_ctx(db, user_id=7, sid="fam-a")
        ctx8 = make_ctx(db, user_id=8, sid="fam-b")
        sub.create_chat_run(db, ctx7, submission=_submission(session_id=1),
                            idempotency_key=uuid.uuid4().hex)
        # user 8 is unaffected by user 7's active run.
        sub.enforce_multi_agent_active_run_cap(
            db, ctx8, resolved_mode="multi_agent",
            idempotency_key=uuid.uuid4().hex, session_id=2,
        )
    finally:
        db.close()


# ── real route-level proof: /api/runs surfaces 429 before persistence ──


from tests.test_run_engine_mp3c3d3a import (  # noqa: E402
    _configure_run_api_ready,
    _run_api_client,
    _run_api_teardown,
    _seed_run_api_session,
    run_api_eng,
)


def test_direct_run_api_returns_429_at_the_cap_with_zero_new_rows(run_api_eng, monkeypatch):
    _configure_run_api_ready(monkeypatch)
    monkeypatch.setattr(settings, "run_worker_enabled", True)
    monkeypatch.setattr(settings, "run_multi_agent_enabled", True)
    monkeypatch.setattr(settings, "multi_agent_max_active_runs_per_user", 1)
    _seed_run_api_session(run_api_eng)
    c, app, db = _run_api_client(run_api_eng, monkeypatch)
    try:
        r1 = c.post("/api/runs", json={"session_id": 1, "message": "hi", "mode": "multi_agent"},
                   headers={"Idempotency-Key": "runapi-cap-1"})
        assert r1.status_code == 201, r1.text
        r2 = c.post("/api/runs", json={"session_id": 1, "message": "a second request", "mode": "multi_agent"},
                   headers={"Idempotency-Key": "runapi-cap-2"})
        assert r2.status_code == 429, r2.text
    finally:
        _run_api_teardown(app)
        db.close()
    db2 = mk_session(run_api_eng)
    try:
        assert db2.query(Run).filter(Run.kind == "multi_agent").count() == 1  # the 429 created nothing
    finally:
        db2.close()


def test_direct_run_api_replay_at_the_cap_still_succeeds(run_api_eng, monkeypatch):
    _configure_run_api_ready(monkeypatch)
    monkeypatch.setattr(settings, "run_worker_enabled", True)
    monkeypatch.setattr(settings, "run_multi_agent_enabled", True)
    monkeypatch.setattr(settings, "multi_agent_max_active_runs_per_user", 1)
    _seed_run_api_session(run_api_eng)
    c, app, db = _run_api_client(run_api_eng, monkeypatch)
    try:
        r1 = c.post("/api/runs", json={"session_id": 1, "message": "hi", "mode": "multi_agent"},
                   headers={"Idempotency-Key": "runapi-replay-1"})
        assert r1.status_code == 201, r1.text
        r2 = c.post("/api/runs", json={"session_id": 1, "message": "hi", "mode": "multi_agent"},
                   headers={"Idempotency-Key": "runapi-replay-1"})  # SAME key — a replay
        assert r2.status_code == 201, r2.text
        assert r2.json()["run_id"] == r1.json()["run_id"]
        assert r2.json()["created"] is False
    finally:
        _run_api_teardown(app)
        db.close()


def test_stream_submission_uses_shared_admission_and_preserves_replay(eng, monkeypatch):
    from app.run_engine import messages_stream as ms
    from app.run_engine.chat_stream import build_context_snapshot
    from app.models.models import Message, RunPayload, RunStep

    monkeypatch.setattr(settings, "multi_agent_max_active_runs_per_user", 1)
    monkeypatch.setattr(ms, "_build_context_snapshot", lambda *a, **kw: build_context_snapshot())
    with mk_session(eng) as db:
        _seed_session(db)
        ctx = make_ctx(db, user_id=7, sid="stream-family")
        kwargs = dict(session_id=1, message="hi", mode="multi_agent")
        first, created = ms.resolve_and_submit(db, ctx, idempotency_key="stream-a", **kwargs)
        first_id = first.id
        assert created
        again, created = ms.resolve_and_submit(db, ctx, idempotency_key="stream-a", **kwargs)
        assert not created and again.id == first_id
        with pytest.raises(ms.DurableStreamError) as exc:
            ms.resolve_and_submit(db, ctx, idempotency_key="stream-b", **kwargs)
        assert exc.value.http_status == 429
        db.rollback()
        assert [db.query(model).count() for model in (Run, Message, RunStep, RunPayload)] == [1, 1, 1, 1]


def test_failed_submission_rolls_back_admission_and_rows(eng, monkeypatch):
    from app.models.models import Message, RunPayload, RunStep

    monkeypatch.setattr(settings, "multi_agent_max_active_runs_per_user", 1)
    with mk_session(eng) as db:
        _seed_session(db)
        ctx = make_ctx(db, user_id=7, sid="rollback-family")
        with monkeypatch.context() as patch:
            def fail(*a, **kw):
                raise RuntimeError("injected seal failure")
            patch.setattr(sub, "seal_payload", fail)
            with pytest.raises(RuntimeError):
                sub.create_chat_run(db, ctx, submission=_submission(), idempotency_key="failed")
        db.rollback()
        assert [db.query(model).count() for model in (Run, Message, RunStep, RunPayload)] == [0, 0, 0, 0]
        _, created = sub.create_chat_run(db, ctx, submission=_submission(), idempotency_key="retry")
        assert created
