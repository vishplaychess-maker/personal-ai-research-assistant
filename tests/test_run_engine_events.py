"""Phase 1B.2R MP-1A — durable control-event primitives + closed per-event schemas."""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.models.models import RunEvent
from app.run_engine.engine import submit_run
from app.run_engine.events import (
    CONTROL_EVENT_TYPES,
    append_control_event,
    read_control_events,
    validate_event_data,
)
from app.run_engine.errors import (
    ControlEventContention,
    ControlEventFieldError,
    ControlEventTooLarge,
    DisallowedControlEventType,
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


@pytest.fixture
def run(db):
    return submit_run(db, make_ctx(db), idempotency_key="k", session_id=1)


# ── append / seq behaviour ───────────────────────────────────────────


def test_append_allowlisted_event_increments_seq(db, run):
    ctx = make_ctx(db)
    e2 = append_control_event(db, ctx, run.id, type="run.queued")
    e3 = append_control_event(db, ctx, run.id, type="run.status",
                              data={"from_status": "created", "to_status": "queued"})
    assert (e2.run_seq, e3.run_seq) == (2, 3)
    db.refresh(run)
    assert run.last_event_seq == 3


def test_disallowed_type_is_refused(db, run):
    with pytest.raises(DisallowedControlEventType):
        append_control_event(db, make_ctx(db), run.id, type="token")
    with pytest.raises(DisallowedControlEventType):
        append_control_event(db, make_ctx(db), run.id, type="run.running")  # not in MP-1A set
    db.refresh(run)
    assert run.last_event_seq == 1


def test_monotonic_no_gaps(db, run):
    ctx = make_ctx(db)
    seqs = [append_control_event(db, ctx, run.id, type="run.queued").run_seq
            for _ in range(4)]
    assert seqs == [2, 3, 4, 5]


def test_failed_insert_does_not_advance_last_event_seq(db, run):
    ctx = make_ctx(db)
    db.add(RunEvent(
        id=str(uuid.uuid4()), run_id=run.id, run_seq=2, user_id=run.user_id,
        category="control", type="run.queued", data="{}",
    ))
    db.commit()

    with pytest.raises(ControlEventContention):
        append_control_event(db, ctx, run.id, type="run.queued")

    db.refresh(run)
    assert run.last_event_seq == 1


def test_read_is_ascending_and_after_seq_bounded(db, run):
    ctx = make_ctx(db)
    for _ in range(4):
        append_control_event(db, ctx, run.id, type="run.queued")
    rows = read_control_events(db, ctx, run.id, after_seq=2)
    assert [r.run_seq for r in rows] == [3, 4, 5]


def test_read_page_size_is_clamped(db, run, monkeypatch):
    ctx = make_ctx(db)
    monkeypatch.setattr(settings, "run_event_read_max_page", 2)
    for _ in range(6):
        append_control_event(db, ctx, run.id, type="run.queued")
    rows = read_control_events(db, ctx, run.id, after_seq=0, limit=1000)
    assert len(rows) == 2


def test_events_are_session_bound(db, run):
    with pytest.raises(RunNotFound):
        append_control_event(db, make_ctx(db, user_id=999, sid="fam-x"), run.id,
                             type="run.queued")
    with pytest.raises(RunNotFound):
        read_control_events(db, make_ctx(db, user_id=999, sid="fam-x"), run.id)


def test_no_active_binding_fails_closed_on_append(db, run):
    with pytest.raises(SessionBindingRequired):
        append_control_event(db, make_ctx(db, binding_valid=False), run.id,
                             type="run.queued")


# ── closed per-event schema registry ─────────────────────────────────


def test_run_status_requires_valid_from_and_to(db, run):
    ctx = make_ctx(db)
    with pytest.raises(ControlEventFieldError):
        append_control_event(db, ctx, run.id, type="run.status",
                             data={"from_status": "created"})  # missing to_status
    with pytest.raises(ControlEventFieldError):
        append_control_event(db, ctx, run.id, type="run.status",
                             data={"from_status": "created", "to_status": "banana"})
    db.refresh(run)
    assert run.last_event_seq == 1  # nothing committed


def test_run_error_only_accepts_allowlisted_codes(db, run):
    ctx = make_ctx(db)
    ok = append_control_event(db, ctx, run.id, type="run.error",
                              data={"error_code": "state_conflict"})
    assert ok.run_seq == 2
    with pytest.raises(ControlEventFieldError):
        append_control_event(db, ctx, run.id, type="run.error",
                             data={"error_code": "NullPointerException at line 5"})


def test_run_created_kind_is_enum_bound(db, run):
    with pytest.raises(ControlEventFieldError):
        append_control_event(db, make_ctx(db), run.id, type="run.created",
                             data={"kind": "arbitrary"})


@pytest.mark.parametrize("bad_key", [
    "note", "value", "debug", "authorization", "secret", "api_key",
    "prompt", "args", "output", "exception",
])
def test_arbitrary_scalar_keys_are_rejected(db, run, bad_key):
    """Even a plain-string value under an innocent-looking unknown key is refused
    by the closed schema — no denylist, no 'non-secret scalar' escape hatch."""
    ctx = make_ctx(db)
    with pytest.raises(ControlEventFieldError):
        append_control_event(db, ctx, run.id, type="run.status",
                             data={"from_status": "created", "to_status": "queued",
                                   bad_key: "just a string"})
    db.refresh(run)
    assert run.last_event_seq == 1


def test_secret_canary_never_reaches_db_logs_or_errors(db, run, caplog):
    """A unique secret under an otherwise-innocent unknown key must not land in
    the DB, in any log record, or in the raised exception's text."""
    canary = "CANARY-9f2a7c1e-secret-do-not-store"
    ctx = make_ctx(db)
    with caplog.at_level("DEBUG"):
        with pytest.raises(ControlEventFieldError) as ei:
            append_control_event(db, ctx, run.id, type="run.status",
                                 data={"from_status": "created", "to_status": "queued",
                                       "telemetry": canary})
    assert canary not in str(ei.value)
    assert canary not in repr(ei.value)
    assert all(canary not in r.getMessage() for r in caplog.records)
    rows = db.query(RunEvent).filter(RunEvent.run_id == run.id).all()
    assert all(canary not in (r.data or "") for r in rows)
    assert len(rows) == 1  # only run.created


def test_nested_values_are_rejected(db, run):
    with pytest.raises(ControlEventFieldError):
        append_control_event(db, make_ctx(db), run.id, type="run.status",
                             data={"from_status": "created", "to_status": "queued",
                                   "extra": {"nested": 1}})


def test_oversize_data_is_refused(db, run, monkeypatch):
    monkeypatch.setattr(settings, "run_control_event_max_bytes", 8)
    with pytest.raises(ControlEventTooLarge):
        append_control_event(db, make_ctx(db), run.id, type="run.status",
                             data={"from_status": "created",
                                   "to_status": "waiting_for_approval"})


def test_validate_event_data_unknown_type():
    with pytest.raises(DisallowedControlEventType):
        validate_event_data("run.nonsense", {})
    assert "run.nonsense" not in CONTROL_EVENT_TYPES
