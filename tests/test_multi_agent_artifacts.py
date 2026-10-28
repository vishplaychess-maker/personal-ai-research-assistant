"""Phase 1B.2R MP-3C3D5B — focused tests for the sealed inter-agent artifact
contract (``app.run_engine.multi_agent_artifacts``).

Hermetic: in-memory SQLite (reusing ``tests/run_engine_helpers.py``'s exact
fixtures) + a per-test Fernet ``ENCRYPTION_KEY``. No worker, no route, no
public run — Run/RunStep rows are created directly as hermetic fixtures,
exactly as D4B's own research-snapshot test file does.
"""
from __future__ import annotations

import json
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models.models import Run, RunPayload, RunStep
from app.run_engine.errors import RunPayloadContextMismatch, RunPayloadIntegrityError
from app.run_engine.multi_agent_artifacts import (
    AGENT_ARTIFACT_PAYLOAD_TYPE,
    AGENT_ARTIFACT_VERSION,
    AgentArtifactBindingMismatch,
    AgentArtifactCitationInvalid,
    AgentArtifactInvalid,
    AgentArtifactMarkerBlocked,
    build_agent_artifact,
    compute_artifact_hash,
    extract_cited_local_markers,
    find_existing_agent_step,
    open_agent_artifact,
    seal_agent_artifact,
    validate_agent_artifact,
    validate_citation_markers_against_context,
)
from app.run_engine.multi_agent_orchestration import (
    ROLE_REVIEWER,
    ROLE_RESEARCHER,
    ROLE_SOLUTION_SPECIALIST,
    STEP_KIND_FOR_ROLE,
)
from app.run_engine.payloads import seal_payload

from tests.run_engine_helpers import ensure_user, mk_engine, mk_session

CANARY_SECRET = "CANARY-secret-content-should-never-leak-plaintext"


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


def _seed_run(db, *, run_id: str, user_id: int = 1, tenant_id=None) -> None:
    ensure_user(db, user_id)
    db.add(Run(
        id=run_id, kind="multi_agent", status="running", user_id=user_id,
        tenant_id=tenant_id, thread_id=f"run:{run_id}", idempotency_key=None,
    ))
    db.commit()


def _artifact(role=ROLE_RESEARCHER, parent_artifact_id=None, **overrides) -> dict:
    kwargs = dict(
        role=role, parent_artifact_id=parent_artifact_id,
        content=f"{CANARY_SECRET} content for {role}.",
        summary=f"{CANARY_SECRET} summary for {role}.",
        claims=["Claim one.", "Claim two."],
        citation_markers=["[1]"],
        assumptions=["Assumption one."],
        uncertainties=["Uncertainty one."],
    )
    kwargs.update(overrides)
    return build_agent_artifact(**kwargs)


# ── 1-3: valid artifact per role with correct lineage shape ────────


def test_valid_researcher_artifact_has_no_parent():
    art = _artifact(ROLE_RESEARCHER, parent_artifact_id=None)
    assert art["role"] == ROLE_RESEARCHER
    assert art["sequence"] == 1
    assert art["parent_artifact_id"] is None


def test_valid_solution_specialist_artifact_with_researcher_parent():
    art = _artifact(ROLE_SOLUTION_SPECIALIST, parent_artifact_id="researcher-step-1")
    assert art["sequence"] == 2
    assert art["parent_artifact_id"] == "researcher-step-1"


def test_valid_reviewer_artifact_with_solution_specialist_parent():
    art = _artifact(ROLE_REVIEWER, parent_artifact_id="solution-step-2")
    assert art["sequence"] == 3
    assert art["parent_artifact_id"] == "solution-step-2"


def test_researcher_with_a_parent_is_rejected():
    with pytest.raises(AgentArtifactInvalid):
        _artifact(ROLE_RESEARCHER, parent_artifact_id="should-not-exist")


def test_solution_specialist_without_a_parent_is_rejected():
    with pytest.raises(AgentArtifactInvalid):
        _artifact(ROLE_SOLUTION_SPECIALIST, parent_artifact_id=None)


# ── 4: seal/open round trip (full 3-role chain) ─────────────────


def test_seal_and_open_round_trip_full_chain(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)

    r_id = str(uuid.uuid4())
    r_art = _artifact(ROLE_RESEARCHER, parent_artifact_id=None)
    r_step = seal_agent_artifact(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=r_id, step_seq=2, artifact=r_art, commit=True,
    )
    assert r_step.kind == STEP_KIND_FOR_ROLE[ROLE_RESEARCHER]

    s_id = str(uuid.uuid4())
    s_art = _artifact(ROLE_SOLUTION_SPECIALIST, parent_artifact_id=r_id)
    s_step = seal_agent_artifact(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=s_id, step_seq=3, artifact=s_art, commit=True,
    )

    v_id = str(uuid.uuid4())
    v_art = _artifact(ROLE_REVIEWER, parent_artifact_id=s_id)
    v_step = seal_agent_artifact(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=v_id, step_seq=4, artifact=v_art, commit=True,
    )

    reopened_r = open_agent_artifact(
        db, r_step, expected_tenant_id=None, expected_user_id=1,
        expected_run_id=run_id, expected_role=ROLE_RESEARCHER,
    )
    reopened_v = open_agent_artifact(
        db, v_step, expected_tenant_id=None, expected_user_id=1,
        expected_run_id=run_id, expected_role=ROLE_REVIEWER,
    )
    assert reopened_r == r_art
    assert reopened_v == v_art
    assert reopened_v["parent_artifact_id"] == s_id


def test_output_ref_contains_no_plaintext(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    step = seal_agent_artifact(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=str(uuid.uuid4()), step_seq=2, artifact=_artifact(), commit=True,
    )
    assert CANARY_SECRET not in step.output_ref
    ref = json.loads(step.output_ref)
    assert set(ref.keys()) == {
        "role", "sequence", "artifact_version", "content_hash",
        "claim_count", "citation_marker_count",
    }


def test_plaintext_absent_from_ciphertext_and_step_columns(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    step = seal_agent_artifact(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=str(uuid.uuid4()), step_seq=2, artifact=_artifact(), commit=True,
    )
    payload_row = db.query(RunPayload).filter(RunPayload.id == step.output_payload_id).first()
    assert CANARY_SECRET not in payload_row.ciphertext
    assert CANARY_SECRET not in (step.input_ref or "")
    assert CANARY_SECRET not in (step.output_ref or "")
    assert CANARY_SECRET not in (step.error_code or "")


# ── 5-6: deterministic hash / content & lineage sensitivity ────────


def test_hash_is_deterministic_for_equivalent_artifacts():
    a1 = _artifact(ROLE_RESEARCHER, parent_artifact_id=None)
    a2 = _artifact(ROLE_RESEARCHER, parent_artifact_id=None)
    assert compute_artifact_hash(a1) == compute_artifact_hash(a2)


def test_hash_changes_when_content_changes():
    a1 = _artifact(ROLE_RESEARCHER, parent_artifact_id=None)
    a2 = _artifact(ROLE_RESEARCHER, parent_artifact_id=None, content="different content entirely")
    assert compute_artifact_hash(a1) != compute_artifact_hash(a2)


def test_hash_changes_when_lineage_changes():
    a1 = _artifact(ROLE_SOLUTION_SPECIALIST, parent_artifact_id="parent-a")
    a2 = _artifact(ROLE_SOLUTION_SPECIALIST, parent_artifact_id="parent-b")
    assert compute_artifact_hash(a1) != compute_artifact_hash(a2)


# ── 7: unknown role / version / extra fields ───────────────────


def test_unknown_role_rejected():
    art = _artifact(ROLE_RESEARCHER, parent_artifact_id=None)
    art["role"] = "coder"
    with pytest.raises(AgentArtifactInvalid):
        validate_agent_artifact(art)


def test_unsupported_version_rejected():
    art = _artifact(ROLE_RESEARCHER, parent_artifact_id=None)
    art["artifact_version"] = AGENT_ARTIFACT_VERSION + 1
    with pytest.raises(AgentArtifactInvalid):
        validate_agent_artifact(art)


def test_bool_version_rejected():
    art = _artifact(ROLE_RESEARCHER, parent_artifact_id=None)
    art["artifact_version"] = True
    with pytest.raises(AgentArtifactInvalid):
        validate_agent_artifact(art)


def test_extra_field_rejected():
    art = _artifact(ROLE_RESEARCHER, parent_artifact_id=None)
    art["extra_model_supplied_field"] = "not allowed"
    with pytest.raises(AgentArtifactInvalid):
        validate_agent_artifact(art)


def test_missing_required_field_rejected():
    art = _artifact(ROLE_RESEARCHER, parent_artifact_id=None)
    del art["summary"]
    with pytest.raises(AgentArtifactInvalid):
        validate_agent_artifact(art)


def test_not_a_dict_rejected():
    with pytest.raises(AgentArtifactInvalid):
        validate_agent_artifact(["not", "a", "dict"])


# ── 8: role/sequence mismatch ────────────────────────────────


def test_role_sequence_mismatch_rejected():
    art = _artifact(ROLE_RESEARCHER, parent_artifact_id=None)
    art["sequence"] = 2  # researcher must be sequence 1
    with pytest.raises(AgentArtifactInvalid):
        validate_agent_artifact(art)


def test_bool_sequence_rejected():
    art = _artifact(ROLE_RESEARCHER, parent_artifact_id=None)
    art["sequence"] = True
    with pytest.raises(AgentArtifactInvalid):
        validate_agent_artifact(art)


# ── 9: missing / wrong / skipped / self / cross-run parent (DB-real) ──


def test_seal_rejects_missing_parent_step(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    art = _artifact(ROLE_SOLUTION_SPECIALIST, parent_artifact_id=str(uuid.uuid4()))
    with pytest.raises(AgentArtifactBindingMismatch):
        seal_agent_artifact(
            db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
            step_id=str(uuid.uuid4()), step_seq=3, artifact=art, commit=True,
        )
    assert db.query(RunStep).filter(RunStep.run_id == run_id).count() == 0


def test_seal_rejects_wrong_role_parent(key, db):
    """Reviewer's parent must be a Solution Specialist step — pointing it at
    a real Researcher step (skipping a role) fails closed."""
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    r_id = str(uuid.uuid4())
    seal_agent_artifact(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=r_id, step_seq=2, artifact=_artifact(ROLE_RESEARCHER, parent_artifact_id=None),
        commit=True,
    )
    reviewer_art = _artifact(ROLE_REVIEWER, parent_artifact_id=r_id)  # wrong role
    with pytest.raises(AgentArtifactBindingMismatch):
        seal_agent_artifact(
            db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
            step_id=str(uuid.uuid4()), step_seq=4, artifact=reviewer_art, commit=True,
        )


def test_seal_rejects_self_parenting(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    self_id = str(uuid.uuid4())
    art = _artifact(ROLE_SOLUTION_SPECIALIST, parent_artifact_id=self_id)
    with pytest.raises(AgentArtifactBindingMismatch):
        seal_agent_artifact(
            db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
            step_id=self_id, step_seq=3, artifact=art, commit=True,
        )


def test_seal_rejects_cross_run_parent(key, db):
    run_a = str(uuid.uuid4())
    run_b = str(uuid.uuid4())
    _seed_run(db, run_id=run_a)
    _seed_run(db, run_id=run_b)
    r_id = str(uuid.uuid4())
    seal_agent_artifact(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_a,
        step_id=r_id, step_seq=2, artifact=_artifact(ROLE_RESEARCHER, parent_artifact_id=None),
        commit=True,
    )
    art = _artifact(ROLE_SOLUTION_SPECIALIST, parent_artifact_id=r_id)
    with pytest.raises(AgentArtifactBindingMismatch):
        seal_agent_artifact(
            db, tenant_id=None, user_id=1, session_id=7, run_id=run_b,  # different run
            step_id=str(uuid.uuid4()), step_seq=3, artifact=art, commit=True,
        )


def test_reviewer_before_researcher_is_structurally_impossible(key, db):
    """No Solution Specialist step exists yet, so Reviewer (whose parent
    must be one) cannot be sealed — proving role-skip/reversal is rejected
    by REAL lineage state, not merely by convention."""
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    art = _artifact(ROLE_REVIEWER, parent_artifact_id=str(uuid.uuid4()))
    with pytest.raises(AgentArtifactBindingMismatch):
        seal_agent_artifact(
            db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
            step_id=str(uuid.uuid4()), step_seq=4, artifact=art, commit=True,
        )


def test_duplicate_sequence_ownership_rejected_by_real_unique_constraint(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    winner_id = str(uuid.uuid4())
    seal_agent_artifact(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=winner_id, step_seq=2, artifact=_artifact(ROLE_RESEARCHER, parent_artifact_id=None),
        commit=True,
    )
    loser_id = str(uuid.uuid4())
    seal_agent_artifact(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=loser_id, step_seq=2, artifact=_artifact(ROLE_RESEARCHER, parent_artifact_id=None),
        commit=False,
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    assert db.query(RunStep).filter(RunStep.id == loser_id).first() is None
    surviving = find_existing_agent_step(db, run_id, 2)
    assert surviving is not None and surviving.id == winner_id


# ── 10: oversized strings / lists / serialized bytes ────────────


def test_oversized_content_rejected(monkeypatch):
    monkeypatch.setattr(settings, "multi_agent_max_content_chars", 10)
    with pytest.raises(AgentArtifactInvalid):
        _artifact(ROLE_RESEARCHER, parent_artifact_id=None, content="x" * 50)


def test_oversized_claims_list_rejected(monkeypatch):
    monkeypatch.setattr(settings, "multi_agent_max_claims", 1)
    with pytest.raises(AgentArtifactInvalid):
        _artifact(ROLE_RESEARCHER, parent_artifact_id=None, claims=["a", "b"])


def test_oversized_serialized_artifact_bytes_rejected(monkeypatch):
    monkeypatch.setattr(settings, "multi_agent_max_artifact_bytes", 50)
    with pytest.raises(AgentArtifactInvalid):
        _artifact(ROLE_RESEARCHER, parent_artifact_id=None)


# ── 11: malformed JSON / pathological structures on open ────────


def test_open_rejects_non_json_decrypted_payload(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    step_id = str(uuid.uuid4())
    payload = seal_payload(
        db, tenant_id=None, user_id=1, run_id=run_id, step_id=step_id,
        payload_type=AGENT_ARTIFACT_PAYLOAD_TYPE, plaintext="not-valid-json{{{",
        commit=True,
    )
    step = RunStep(
        id=step_id, run_id=run_id, user_id=1, session_id=7, seq=2,
        kind=STEP_KIND_FOR_ROLE[ROLE_SOLUTION_SPECIALIST], engine="native",
        status="succeeded", output_payload_id=payload.id,
    )
    db.add(step)
    db.commit()
    with pytest.raises(AgentArtifactInvalid):
        open_agent_artifact(
            db, step, expected_tenant_id=None, expected_user_id=1,
            expected_run_id=run_id, expected_role=ROLE_SOLUTION_SPECIALIST,
        )


def test_open_rejects_pathologically_deep_json(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    step_id = str(uuid.uuid4())
    deeply_nested = "[" * 5000 + "]" * 5000
    payload = seal_payload(
        db, tenant_id=None, user_id=1, run_id=run_id, step_id=step_id,
        payload_type=AGENT_ARTIFACT_PAYLOAD_TYPE, plaintext=deeply_nested,
        commit=True,
    )
    step = RunStep(
        id=step_id, run_id=run_id, user_id=1, session_id=7, seq=2,
        kind=STEP_KIND_FOR_ROLE[ROLE_SOLUTION_SPECIALIST], engine="native",
        status="succeeded", output_payload_id=payload.id,
    )
    db.add(step)
    db.commit()
    with pytest.raises(AgentArtifactInvalid):
        open_agent_artifact(
            db, step, expected_tenant_id=None, expected_user_id=1,
            expected_run_id=run_id, expected_role=ROLE_SOLUTION_SPECIALIST,
        )


# ── 12-14: marker fail-closed policy ─────────────────────────

_EXECUTABLE_MARKERS = [
    "[mcp_call: {\"server\": \"x\"}]",
    "[python_code]\nprint(1)\n[/python_code]",
    "[proposed_command: rm -rf /]",
    "[browser_action: click]",
    "[computer_action: click]",
    "[save_memory: secret]",
    "[save_directive: do x]",
]


@pytest.mark.parametrize("marker_text", _EXECUTABLE_MARKERS)
def test_every_executable_marker_family_fails_closed(marker_text):
    with pytest.raises(AgentArtifactMarkerBlocked):
        _artifact(ROLE_RESEARCHER, parent_artifact_id=None,
                  content=f"Here is a marker: {marker_text} end of text.")


@pytest.mark.parametrize("marker_text", [m.upper() for m in _EXECUTABLE_MARKERS])
def test_mixed_case_marker_fails_closed(marker_text):
    with pytest.raises(AgentArtifactMarkerBlocked):
        _artifact(ROLE_RESEARCHER, parent_artifact_id=None,
                  content=f"Mixed case attempt: {marker_text} trailing text.")


def test_partial_marker_at_end_of_text_fails_closed():
    """A truncated prefix of a reserved marker at the very end of the text
    (chunk-boundary-style truncation) must fail closed, never be silently
    accepted as benign."""
    with pytest.raises(AgentArtifactMarkerBlocked):
        _artifact(ROLE_RESEARCHER, parent_artifact_id=None,
                  content="Some text ending mid-marker [mcp_ca")


def test_benign_text_without_any_marker_is_accepted():
    art = _artifact(ROLE_RESEARCHER, parent_artifact_id=None,
                     content="Ordinary prose discussing brackets like [1] and [citations].")
    assert art["role"] == ROLE_RESEARCHER


def test_marker_in_claims_list_fails_closed():
    with pytest.raises(AgentArtifactMarkerBlocked):
        _artifact(ROLE_RESEARCHER, parent_artifact_id=None,
                  claims=["a normal claim", "[python_code] evil [/python_code]"])


# ── 15-18: citation contract ─────────────────────────────────


def test_local_citation_extraction_preserves_order_and_dedupes():
    valid = frozenset({"[1]", "[2]", "[3]"})
    text = "See [2] and again [2], then [1], and an unknown [9]."
    out = extract_cited_local_markers(text, valid)
    assert out == ["[2]", "[1]"]  # first-reference order, deduped, unknown excluded


def test_unknown_local_citation_rejected_against_context():
    with pytest.raises(AgentArtifactCitationInvalid):
        validate_citation_markers_against_context(["[7]"], frozenset({"[1]", "[2]"}))


def test_web_marker_rejected_even_if_syntactically_well_formed():
    with pytest.raises(AgentArtifactCitationInvalid):
        validate_citation_markers_against_context(["[W1]"], frozenset({"[W1]"}))


def test_valid_local_citation_against_context_passes():
    validate_citation_markers_against_context(["[1]"], frozenset({"[1]", "[2]"}))  # no raise


def test_model_url_in_content_never_becomes_a_citation_marker():
    valid = frozenset({"[1]"})
    text = "Source: https://attacker.example/evil?x=1 but also cite [1]."
    out = extract_cited_local_markers(text, valid)
    assert out == ["[1]"]
    assert not any("attacker.example" in m for m in out)


def test_malformed_citation_marker_format_rejected():
    with pytest.raises(AgentArtifactInvalid):
        _artifact(ROLE_RESEARCHER, parent_artifact_id=None, citation_markers=["not-a-marker"])


# ── 19-20: encryption binding / tampered payload ───────────────


def test_open_rejects_wrong_run_id(key, db):
    run_id = str(uuid.uuid4())
    other_run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    step = seal_agent_artifact(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=str(uuid.uuid4()), step_seq=2, artifact=_artifact(), commit=True,
    )
    with pytest.raises(AgentArtifactBindingMismatch):
        open_agent_artifact(
            db, step, expected_tenant_id=None, expected_user_id=1,
            expected_run_id=other_run_id, expected_role=ROLE_RESEARCHER,
        )


def test_open_rejects_wrong_expected_role_binding(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    step = seal_agent_artifact(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=str(uuid.uuid4()), step_seq=2,
        artifact=_artifact(ROLE_RESEARCHER, parent_artifact_id=None),
        commit=True,
    )
    with pytest.raises(AgentArtifactBindingMismatch):
        open_agent_artifact(
            db, step, expected_tenant_id=None, expected_user_id=1,
            expected_run_id=run_id, expected_role=ROLE_SOLUTION_SPECIALIST,
        )


def test_open_rejects_wrong_tenant_via_payload_envelope(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id, tenant_id="tenant-a")
    step = seal_agent_artifact(
        db, tenant_id="tenant-a", user_id=1, session_id=7, run_id=run_id,
        step_id=str(uuid.uuid4()), step_seq=2, artifact=_artifact(), commit=True,
    )
    with pytest.raises(RunPayloadContextMismatch):
        open_agent_artifact(
            db, step, expected_tenant_id="tenant-b", expected_user_id=1,
            expected_run_id=run_id, expected_role=ROLE_RESEARCHER,
        )


def test_open_rejects_a_step_with_no_output_payload(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    bare_step = RunStep(
        id=str(uuid.uuid4()), run_id=run_id, user_id=1, session_id=7, seq=1,
        kind=STEP_KIND_FOR_ROLE[ROLE_RESEARCHER], engine="native", status="pending",
    )
    db.add(bare_step)
    db.commit()
    with pytest.raises(AgentArtifactBindingMismatch):
        open_agent_artifact(
            db, bare_step, expected_tenant_id=None, expected_user_id=1,
            expected_run_id=run_id, expected_role=ROLE_RESEARCHER,
        )


def test_tampered_ciphertext_fails_closed(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    step = seal_agent_artifact(
        db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
        step_id=str(uuid.uuid4()), step_seq=2, artifact=_artifact(), commit=True,
    )
    payload_row = db.query(RunPayload).filter(RunPayload.id == step.output_payload_id).first()
    body = list(payload_row.ciphertext)
    body[-4] = "A" if body[-4] != "A" else "B"
    payload_row.ciphertext = "".join(body)
    db.commit()
    with pytest.raises(RunPayloadIntegrityError):
        open_agent_artifact(
            db, step, expected_tenant_id=None, expected_user_id=1,
            expected_run_id=run_id, expected_role=ROLE_RESEARCHER,
        )


# ── 21: secret canary absent everywhere non-plaintext-scoped ────


def test_canary_absent_from_exceptions_raised_during_marker_block():
    try:
        _artifact(ROLE_RESEARCHER, parent_artifact_id=None,
                  content=f"{CANARY_SECRET} [python_code] evil [/python_code]")
        pytest.fail("expected AgentArtifactMarkerBlocked")
    except AgentArtifactMarkerBlocked as exc:
        assert CANARY_SECRET not in str(exc)
        assert "python_code" not in str(exc).lower() or "[python_code" not in str(exc)


def test_canary_absent_from_binding_mismatch_exception_text(key, db):
    run_id = str(uuid.uuid4())
    _seed_run(db, run_id=run_id)
    art = _artifact(ROLE_SOLUTION_SPECIALIST, parent_artifact_id=str(uuid.uuid4()))
    try:
        seal_agent_artifact(
            db, tenant_id=None, user_id=1, session_id=7, run_id=run_id,
            step_id=str(uuid.uuid4()), step_seq=3, artifact=art, commit=True,
        )
        pytest.fail("expected AgentArtifactBindingMismatch")
    except AgentArtifactBindingMismatch as exc:
        assert CANARY_SECRET not in str(exc)
