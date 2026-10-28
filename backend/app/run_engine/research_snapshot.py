"""Durable Deep Research — encrypted structured-source snapshot persistence
primitive (Phase 1B.2R MP-3C3D4B).

**Not a worker, not a route.** A narrow, reusable persistence primitive —
exactly like ``app.run_engine.payloads`` itself, or the exec/context
snapshot sealing inline in ``submission.build_chat_submission`` — meant to
be invoked from INSIDE a caller-owned atomic transaction (D4C's future
worker step). Never commits internally. Tests exercise it directly against
hermetic ``Run``/``RunStep`` fixtures (permitted per the D4B task scope);
this module itself creates no worker sweep, no route, and no publicly
reachable run.

At-most-one authority
----------------------
``RunPayload`` (``app.run_engine.payloads``) has **no** database-level
uniqueness constraint on ``(run_id, step_id, payload_type)`` — only plain,
non-unique indexes (confirmed by inspecting ``models.RunPayload.__table_args__``).
A Python check-then-set over that table alone would NOT be PostgreSQL-safe
under real concurrency, and this module does not attempt one.

Instead, authority is anchored to :class:`app.models.models.RunStep`'s
**real** database-level ``UniqueConstraint("run_id", "seq", name="uq_run_steps_run_seq")``
(``models.py`` — confirmed present, standard ANSI SQL, enforced identically
by SQLite and PostgreSQL, not an application-level check). A snapshot is
sealed as one ``RunStep``'s output payload; two concurrent callers racing to
seal a *different* snapshot for the *same* ``(run_id, seq)`` will have the
loser's ``RunStep`` insert rejected by that constraint at flush/commit —
not by any logic in this file. The caller (D4C's worker) must catch that
failure and re-read the winning step via :func:`find_existing_research_step`
rather than assume its own attempt won. This is proven under a real SQL
UNIQUE constraint in the focused tests, not merely asserted.

Session binding note
---------------------
The encrypted envelope (``app.run_engine.payloads``) binds
``tenant_id/user_id/run_id/step_id/payload_type`` — it has **no**
``session_id`` field (a pre-existing characteristic shared by every other
``seal_payload`` caller in the repo, e.g. ``chat_stream``'s context
snapshot). Session-family validation is therefore NOT this module's job —
it is the orchestrator's job (``check_session_binding()``, reusing
``app.run_engine.session_binding.validate_session_binding`` at the caller
level), exactly as every other durable step already does. This module only
guarantees payload-envelope binding + step-kind/version binding.
"""

from __future__ import annotations

import json
from typing import Optional

from sqlalchemy.orm import Session

from app.approval.canonical import canonicalize
from app.models.models import RunStep
from app.run_engine.errors import RunEngineError
from app.run_engine.payloads import open_payload, seal_payload
from app.run_engine.research_sources import validate_research_snapshot

RESEARCH_PAYLOAD_TYPE = "research_sources"
RESEARCH_STEP_KIND = "research_fetch_extract"


class ResearchSnapshotBindingMismatch(RunEngineError):
    """The requested step is not a sealed research snapshot for the
    expected run, or its output payload is missing/wrong-typed."""


def find_existing_research_step(db: Session, run_id: str, seq: int) -> Optional[RunStep]:
    """Look up an already-sealed (or in-flight) research step by its
    authoritative ``(run_id, seq)`` — the same lookup a caller who lost the
    seal race (or who is replaying/reconnecting) uses to find the winner."""
    return (
        db.query(RunStep)
        .filter(RunStep.run_id == run_id, RunStep.seq == seq, RunStep.kind == RESEARCH_STEP_KIND)
        .first()
    )


def seal_research_snapshot(
    db: Session,
    *,
    tenant_id: Optional[str],
    user_id: int,
    session_id: Optional[int],
    run_id: str,
    step_id: str,
    seq: int,
    snapshot: dict,
    commit: bool = False,
) -> RunStep:
    """Validate *snapshot*, seal it as an encrypted ``RunPayload``, and bind
    it to a NEW ``RunStep`` row at ``(run_id, seq)`` — all in the CALLER's
    own transaction (``commit=False`` by default; the caller decides when to
    flush/commit, exactly like ``submission.build_chat_submission``'s own
    payload+step insertion).

    Raises :class:`app.run_engine.research_sources.ResearchSnapshotInvalid`
    (fail closed BEFORE any DB write) if *snapshot* is malformed. Raises a
    SQLAlchemy ``IntegrityError`` at flush/commit — never caught here — if a
    concurrent caller already won ``(run_id, seq)``; the caller must catch
    that, roll back, and use :func:`find_existing_research_step` instead.

    ``RunStep.output_ref`` receives ONLY bounded structural counts (source
    count, passage count, the real ``truncated`` flag) — never a URL, title,
    query, passage, citation snippet, or exception text (matches the
    existing ``output_ref`` convention: ``{output_sha256, output_len,
    message_id}`` for chat, never raw content).
    """
    validate_research_snapshot(snapshot)  # fail closed before any DB write
    plaintext = canonicalize(snapshot).decode("utf-8")
    payload = seal_payload(
        db,
        tenant_id=tenant_id, user_id=user_id, run_id=run_id, step_id=step_id,
        payload_type=RESEARCH_PAYLOAD_TYPE, plaintext=plaintext,
        commit=False,
    )
    step = RunStep(
        id=step_id, run_id=run_id, tenant_id=tenant_id, user_id=user_id,
        session_id=session_id, seq=seq, kind=RESEARCH_STEP_KIND, engine="native",
        status="succeeded", output_payload_id=payload.id,
        output_ref=json.dumps({
            "source_count": len(snapshot["sources"]),
            "passage_count": snapshot["total_passage_count"],
            "truncated": snapshot["truncated"],
        }, sort_keys=True),
    )
    db.add(step)
    if commit:
        db.commit()
    return step


def open_research_snapshot(
    db: Session,
    step: RunStep,
    *,
    expected_tenant_id: Optional[str],
    expected_user_id: int,
    expected_run_id: str,
) -> dict:
    """Re-open (never re-search/re-fetch) the snapshot already sealed for
    *step* — the ONLY way any later stage (D4C's synthesis, a reconnect, a
    restart) reads research sources. Fails closed on any binding mismatch,
    wrong payload type, or wrong/unsupported snapshot version (both via the
    payload envelope's own authenticated context check AND this function's
    own step-level checks, defense in depth)."""
    if step.run_id != expected_run_id:
        raise ResearchSnapshotBindingMismatch("run_id mismatch")
    if step.kind != RESEARCH_STEP_KIND:
        raise ResearchSnapshotBindingMismatch("not a research snapshot step")
    if not step.output_payload_id:
        raise ResearchSnapshotBindingMismatch("step has no sealed output payload")
    plaintext = open_payload(
        db, step.output_payload_id,
        expected_tenant_id=expected_tenant_id, expected_user_id=expected_user_id,
        expected_run_id=expected_run_id, expected_step_id=step.id,
        expected_payload_type=RESEARCH_PAYLOAD_TYPE, expected_action_hash=None,
    )
    return validate_research_snapshot(json.loads(plaintext))
