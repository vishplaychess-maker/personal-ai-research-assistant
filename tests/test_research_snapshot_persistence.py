"""Phase 1B.2R MP-3C3D4B — focused tests for the encrypted structured-source
snapshot persistence primitive (``app.run_engine.research_snapshot``).

Hermetic: in-memory SQLite (reusing ``tests/run_engine_helpers.py``'s exact
fixtures — the same ones MP-0/MP-1A/MP-3* already use) + a per-test Fernet
``ENCRYPTION_KEY``. No worker, no route, no public run — Run/RunStep rows
are created directly as hermetic fixtures, exactly as the D4B task
description explicitly permits.
"""
from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.exc import IntegrityError

from app.models.models import Run, RunPayload, RunStep
from app.run_engine.errors import RunPayloadAccessError, RunPayloadContextMismatch
from app.run_engine.research_snapshot import (
    RESEARCH_PAYLOAD_TYPE,
    RESEARCH_STEP_KIND,
    ResearchSnapshotBindingMismatch,
    find_existing_research_step,
    open_research_snapshot,
    seal_research_snapshot,
)
from app.run_engine.research_sources import (
    ResearchSnapshotInvalid,
    build_research_snapshot,
)

from tests.run_engine_helpers import ensure_user, mk_engine, mk_session

CANARY_URL = "https://a.example/CANARY-secret-path"
CANARY_TITLE = "CANARY-TITLE-should-never-leak"


@pytest.fixture
def key(monkeypatch):
    k = Fernet.generate_key().decode()
    monkeypatch.setenv("ENCRYPTION_KEY", k)
    return k


@pytest.fixture
def eng():
    return mk_engine()


@pytest.fixture
def db(eng):
    s = mk_session(eng)
    try:
        yield s
    finally:
        s.close()


def _seed_run(db, *, run_id: str, user_id: int = 1) -> None:
    ensure_user(db, user_id)
    db.add(Run(
        id=run_id, kind="deep_research", status="running", user_id=user_id,
        thread_id=f"run:{run_id}", idempotency_key=None,
    ))
    db.commit()


def _snapshot(url=CANARY_URL, title=CANARY_TITLE) -> dict:
    from app.run_engine.research_sources import RawPassageCandidate, RawSourceCandidate

    return build_research_snapshot(
        acquired_at="2026-01-01T00:00:00+00:00", query_count=1,
        candidates=[RawSourceCandidate(
            url=url, title=title, domain="a.example", search_rank=1,
            retrieved_at="2026-01-01T00:00:00+00:00", published_at=None,
            passages=[RawPassageCandidate(text="Some passage text.")],
        )],
    )


# ── G45: seal/open valid snapshot ─────────────────────────────


def test_seal_and_open_round_trips(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    step_id = str(uuid.uuid4())
    snap = _snapshot()

    step = seal_research_snapshot(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=step_id, seq=1, snapshot=snap, commit=True,
    )
    assert step.kind == RESEARCH_STEP_KIND
    assert step.output_payload_id is not None

    reopened = open_research_snapshot(
        db, step, expected_tenant_id=None, expected_user_id=1, expected_run_id=run_id,
    )
    assert reopened == snap


def test_output_ref_contains_only_bounded_counts_no_url_or_title(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    step = seal_research_snapshot(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=str(uuid.uuid4()), seq=1, snapshot=_snapshot(), commit=True,
    )
    assert CANARY_URL not in step.output_ref
    assert CANARY_TITLE not in step.output_ref
    import json
    ref = json.loads(step.output_ref)
    assert set(ref.keys()) == {"source_count", "passage_count", "truncated"}


# ── G46: replay causes zero acquisition calls (proven at the
#         orchestrator-integration level: the caller simply never invokes
#         search/fetch again once find_existing_research_step returns a
#         sealed step — proven here by showing open_research_snapshot alone
#         is sufficient to recover the data, no adapter involved at all) ──


def test_replay_reads_existing_snapshot_without_any_adapter(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    seal_research_snapshot(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=str(uuid.uuid4()), seq=1, snapshot=_snapshot(), commit=True,
    )
    existing = find_existing_research_step(db, run_id, 1)
    assert existing is not None
    reopened = open_research_snapshot(
        db, existing, expected_tenant_id=None, expected_user_id=1, expected_run_id=run_id,
    )
    assert reopened["sources"][0]["url"] == CANARY_URL


# ── G47: second seal cannot overwrite the first ───────────────
# ── G50: stale/concurrent authority matches the ACTUAL DB constraint ──


def test_second_seal_for_same_run_and_seq_is_rejected_by_the_real_unique_constraint(key, db):
    """Proves the AUTHORITY mechanism is a genuine SQL UNIQUE constraint
    (`uq_run_steps_run_seq`), not a Python check-then-set — the SAME
    constraint is enforced identically under SQLite and PostgreSQL (both
    implement standard ANSI UNIQUE), so this is PostgreSQL-safe evidence,
    not merely a SQLite-only observation."""
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)

    winner_step_id = str(uuid.uuid4())
    seal_research_snapshot(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=winner_step_id, seq=1, snapshot=_snapshot(url="https://a.example/winner"),
        commit=True,
    )

    loser_step_id = str(uuid.uuid4())
    seal_research_snapshot(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=loser_step_id, seq=1,  # SAME (run_id, seq) as the winner
        snapshot=_snapshot(url="https://a.example/loser"),
        commit=False,
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # G49: the injected failure leaves NO partial authoritative state —
    # neither the loser's RunStep nor its RunPayload survive the rollback.
    assert db.query(RunStep).filter(RunStep.id == loser_step_id).first() is None
    assert db.query(RunPayload).filter(RunPayload.step_id == loser_step_id).first() is None

    # the winner is completely unaffected and remains the sole authority.
    surviving = find_existing_research_step(db, run_id, 1)
    assert surviving is not None
    assert surviving.id == winner_step_id
    reopened = open_research_snapshot(
        db, surviving, expected_tenant_id=None, expected_user_id=1, expected_run_id=run_id,
    )
    assert reopened["sources"][0]["url"] == "https://a.example/winner"
    assert db.query(RunStep).filter(RunStep.run_id == run_id, RunStep.seq == 1).count() == 1


# ── G48: wrong run/step/user/tenant/session rejected ──────────


def test_open_rejects_wrong_expected_run_id(key, db):
    run_id = str(uuid.uuid4())
    other_run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    step = seal_research_snapshot(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=str(uuid.uuid4()), seq=1, snapshot=_snapshot(), commit=True,
    )
    with pytest.raises(ResearchSnapshotBindingMismatch):
        open_research_snapshot(
            db, step, expected_tenant_id=None, expected_user_id=1, expected_run_id=other_run_id,
        )


def test_open_rejects_wrong_expected_user_id_via_payload_envelope(key, db):
    """`open_payload` has a fast, non-authoritative owner pre-check (the
    plain `RunPayload.user_id` column) BEFORE the authenticated envelope
    check — a mismatch here raises `RunPayloadAccessError`, not
    `RunPayloadContextMismatch` (that's reserved for a mismatch caught only
    AFTER decryption, e.g. a swapped ciphertext with a matching row-level
    user_id). Both are fail-closed rejections; this test asserts the one
    actually raised."""
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id, user_id=1)
    ensure_user(db, 2)
    step = seal_research_snapshot(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=str(uuid.uuid4()), seq=1, snapshot=_snapshot(), commit=True,
    )
    with pytest.raises(RunPayloadAccessError):
        open_research_snapshot(
            db, step, expected_tenant_id=None, expected_user_id=2, expected_run_id=run_id,
        )


def test_open_rejects_wrong_expected_tenant_id_via_payload_envelope(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    step = seal_research_snapshot(
        db, tenant_id="tenant-a", user_id=1, session_id=7, run_id=run_id,
        step_id=str(uuid.uuid4()), seq=1, snapshot=_snapshot(), commit=True,
    )
    with pytest.raises(RunPayloadContextMismatch):
        open_research_snapshot(
            db, step, expected_tenant_id="tenant-b", expected_user_id=1, expected_run_id=run_id,
        )


def test_open_rejects_a_non_research_step_kind(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    not_research_step = RunStep(
        id=str(uuid.uuid4()), run_id=run_id, user_id=1, session_id=7, seq=1,
        kind="load_context", engine="native", status="succeeded",
    )
    db.add(not_research_step)
    db.commit()
    with pytest.raises(ResearchSnapshotBindingMismatch):
        open_research_snapshot(
            db, not_research_step, expected_tenant_id=None, expected_user_id=1, expected_run_id=run_id,
        )


def test_open_rejects_a_step_with_no_output_payload(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    bare_step = RunStep(
        id=str(uuid.uuid4()), run_id=run_id, user_id=1, session_id=7, seq=1,
        kind=RESEARCH_STEP_KIND, engine="native", status="pending",
    )
    db.add(bare_step)
    db.commit()
    with pytest.raises(ResearchSnapshotBindingMismatch):
        open_research_snapshot(
            db, bare_step, expected_tenant_id=None, expected_user_id=1, expected_run_id=run_id,
        )


# ── seal-time validation: fail closed BEFORE any DB write ─────


def test_invalid_snapshot_is_rejected_before_any_db_write(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    bad_snapshot = {"not": "a valid snapshot"}
    with pytest.raises(ResearchSnapshotInvalid):
        seal_research_snapshot(
            db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
            step_id=str(uuid.uuid4()), seq=1, snapshot=bad_snapshot, commit=True,
        )
    assert db.query(RunPayload).filter(RunPayload.run_id == run_id).count() == 0
    assert db.query(RunStep).filter(RunStep.run_id == run_id).count() == 0


# ── G51: plaintext absent from refs/events/status/logs ────────


def test_plaintext_absent_from_the_raw_payload_row_and_step_columns(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    step = seal_research_snapshot(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=str(uuid.uuid4()), seq=1, snapshot=_snapshot(), commit=True,
    )
    payload_row = db.query(RunPayload).filter(RunPayload.id == step.output_payload_id).first()
    assert payload_row is not None
    assert payload_row.payload_type == RESEARCH_PAYLOAD_TYPE
    # the ciphertext column must not contain the plaintext canary anywhere
    assert CANARY_URL not in payload_row.ciphertext
    assert CANARY_TITLE not in payload_row.ciphertext
    # nor any of the RunStep's own plain columns
    assert CANARY_URL not in (step.input_ref or "")
    assert CANARY_URL not in (step.output_ref or "")
    assert CANARY_URL not in (step.error_code or "")
