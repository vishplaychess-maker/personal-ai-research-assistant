"""Durable Multi-Agent — the versioned sealed inter-agent artifact contract
(Phase 1B.2R MP-3C3D5B).

Pure schema/validation/trust-boundary functions (mirrors
``app.run_engine.research_sources``'s shape: no DB, no network, no LLM) plus
a thin sealed-persistence wrapper (mirrors
``app.run_engine.research_snapshot``'s shape: reuses
``app.run_engine.payloads.seal_payload``/``open_payload`` unchanged, never a
second encryption primitive).

**Foundation only.** No worker calls any of this yet — ``seal_agent_artifact``/
``open_agent_artifact`` are ready for D5C's future worker to call from inside
its own atomic transaction, exactly like ``seal_research_snapshot`` was ready
for D4C before D4C existed.

Trust boundary
---------------
A role's raw text is UNTRUSTED MODEL OUTPUT the instant it exists — it can
carry a tool/action marker, and it can never be allowed to choose its own
role, sequence, parent, citations, provider, model, or next step. This
module enforces that boundary in one place:

* ``role``/``sequence``/``parent_artifact_id`` are SERVER-OWNED metadata —
  supplied by the caller (the future D5C worker, from trusted ``RunStep``
  state), never derived from or overridable by the role's own text.
* ``content``/``summary``/``claims``/``assumptions``/``uncertainties`` are
  the only fields sourced from role text, and every one of them is scanned
  for a reserved marker before an artifact can be built OR re-validated.

Locked decision (D5B, decision 2 — text/evidence-only v1): a COMPLETE
reserved marker (MCP/Python/command/browser/memory/directive — the exact
same closed set durable chat already watches for,
``app.run_engine.chat_stream.ALL_WATCHED_MARKERS``) or a marker left
truncated at the end of the text FAILS THE ARTIFACT CLOSED
(:class:`AgentArtifactMarkerBlocked`) rather than being neutralized and
kept. No marker is ever executed, and none is silently dropped-and-
continued. A ``[COMPUTER_ACTION`` opener is not yet part of the repository's
own canonical marker list (``app.skills.loader._MARKER_OPENS`` has no such
entry, and no persona anywhere emits it) — it is checked defensively here
anyway per this phase's explicit instruction, documented rather than
silently added to the shared canonical list (which is out of this
foundation-only phase's scope). This is defense-in-depth on top of, not a
substitute for, the fact that multi-agent v1 has NO tool-dispatch code path
at all — even an undetected marker-shaped string can never be executed.
Once a text field passes the marker check clean, it is additionally passed
through the SAME ``research_sources.neutralize_markers`` D4B already uses
for untrusted retrieved web content — belt-and-suspenders, not the primary
gate, and reused unchanged (no second, drifting marker list).
"""

from __future__ import annotations

import json
import re
from typing import List, Optional

from sqlalchemy.orm import Session

from app.approval.canonical import canonicalize
from app.models.models import RunPayload, RunStep
from app.run_engine import chat_stream
from app.run_engine.deep_research_citations import WEB_MARKER_PATTERN
from app.run_engine.errors import RunEngineError
from app.run_engine.multi_agent_orchestration import (
    AGENT_PROGRESS_CODES,
    STEP_KIND_FOR_ROLE,
    expected_parent_role,
    is_known_role,
    sequence_for_role,
)
from app.run_engine.payloads import open_payload, seal_payload
from app.run_engine.research_sources import content_hash, neutralize_markers

# ── errors ───────────────────────────────────────────────────


class AgentArtifactError(RunEngineError):
    """Base class for sealed-agent-artifact errors."""


class AgentArtifactInvalid(AgentArtifactError):
    """The artifact is malformed, wrong-versioned, or fails a bound/invariant."""


class AgentArtifactMarkerBlocked(AgentArtifactError):
    """A role's text contained (or was truncated inside) a reserved
    tool/action marker — the artifact fails closed, no marker text is ever
    included in the exception."""


class AgentArtifactCitationInvalid(AgentArtifactError):
    """A citation marker is unknown to the authoritative sealed context, or
    is a deferred ``[W#]`` web marker — v1 fails closed on either rather
    than silently dropping it."""


class AgentArtifactBindingMismatch(AgentArtifactError):
    """The requested step is not a sealed agent artifact for the expected
    run/role, or its output payload is missing/wrong-typed."""


# ── versioning ───────────────────────────────────────────────

AGENT_ARTIFACT_VERSION = 1
AGENT_ARTIFACT_PAYLOAD_TYPE = "agent_artifact"

# AGENT_PROGRESS_CODES is imported above from multi_agent_orchestration (the
# ONE canonical definition — see app.run_engine.events.CONTROL_EVENT_SCHEMAS
# ["agent.progress"], which imports the SAME frozenset, never a second
# literal). D5B defines/validates the schema only — no worker exists yet to
# emit any of these.

# ── structural bounds (hardcoded — per-item sub-bounds mirror
# research_sources.py's `_MAX_TITLE_CHARS`-style precedent: not
# independently deployer-tunable, since the task only asked for
# COUNT/total-size bounds to be configurable, not every individual field
# length) ──────────────────────────────────────────────────────────────

_MAX_SUMMARY_CHARS = 2_000
_MAX_LIST_ITEM_CHARS = 500
_MAX_CITATION_MARKER_CHARS = 16   # "[9999999999]" territory and beyond is nonsensical

_REQUIRED_ARTIFACT_FIELDS = frozenset({
    "artifact_version", "role", "sequence", "parent_artifact_id",
    "content", "summary", "claims", "citation_markers",
    "assumptions", "uncertainties",
})

_LOCAL_MARKER_PATTERN = re.compile(r"^\[(\d+)\]$")

# Not (yet) part of the repository's own canonical marker-opener list
# (`app.skills.loader._MARKER_OPENS`) — no persona anywhere emits it. Kept
# as ONE additional, explicitly-documented, hardcoded literal (not a second
# drifting list) per this phase's explicit instruction to guard against it
# defensively. See the module docstring.
_COMPUTER_ACTION_OPENER = re.compile(r"\[computer_action", re.IGNORECASE)


def _is_plain_str(value: object) -> bool:
    return isinstance(value, str)


def _is_bounded_str_list(value: object, *, max_count: int, max_item_chars: int) -> bool:
    if not isinstance(value, list) or len(value) > max_count:
        return False
    return all(
        isinstance(item, str) and 0 < len(item) <= max_item_chars for item in value
    )


def _assert_no_marker(text: str) -> None:
    """Fail closed if *text* contains a complete reserved marker, or ends
    with a possible-but-unconfirmed one (chunk-boundary truncation). Reuses
    ``chat_stream.MarkerScanner`` fed the whole string as one chunk — the
    SAME detector durable chat/deep-research already use, never a second
    implementation of "what counts as a marker"."""
    if not text:
        return
    scanner = chat_stream.MarkerScanner(chat_stream.ALL_WATCHED_MARKERS, capture=False)
    scanner.feed(text)
    scanner.finish()
    if scanner.tripped:
        raise AgentArtifactMarkerBlocked("reserved marker detected in role output")
    if scanner.truncated:
        raise AgentArtifactMarkerBlocked("possible reserved marker at end of role output")
    if _COMPUTER_ACTION_OPENER.search(text):
        raise AgentArtifactMarkerBlocked("reserved marker detected in role output")


def _sanitize_text(text: str) -> str:
    """Fail-closed marker check, then defense-in-depth neutralization
    (reused unchanged from D4B — no second marker list)."""
    _assert_no_marker(text)
    return neutralize_markers(text)


def _sanitize_list(items: List[str]) -> List[str]:
    return [_sanitize_text(item) for item in items]


# ── citation-marker contract (section F) ──────────────────────────────
#
# D5B adds NO live web search (locked decision 4) — only the sealed D2
# local/document citation set may ever back a citation marker in v1.
# `[W#]` validation/reuse is explicitly deferred to a later combined
# research-team phase; a `[W#]` marker appearing here fails closed rather
# than being silently dropped.


def extract_cited_local_markers(text: str, valid_markers: frozenset) -> List[str]:
    """Every ``[N]`` marker actually present in *text* that ALSO exists in
    *valid_markers* (the authoritative sealed D2 citation set), in
    first-reference order, deduplicated — mirrors
    ``deep_research_citations.extract_cited_web_markers``'s exact contract,
    adapted to the local numeric namespace."""
    seen: set = set()
    out: List[str] = []
    for m in re.finditer(r"\[(\d+)\]", text or ""):
        marker = f"[{m.group(1)}]"
        if marker in valid_markers and marker not in seen:
            seen.add(marker)
            out.append(marker)
    return out


def has_unknown_local_citation_marker(text: str, valid_markers: frozenset) -> bool:
    """True if *text* references a numeric ``[N]`` marker absent from
    *valid_markers* (the authoritative sealed D2 context) — a
    model-invented/out-of-range local citation. Mirrors
    ``deep_research_citations.has_hallucinated_web_marker``'s exact
    contract for the local numeric namespace: callers use this (together
    with a plain ``WEB_MARKER_PATTERN.search`` for any ``[W#]``, deferred
    entirely in v1 — decision 6) to FAIL CLOSED with
    ``citation_validation_failed`` BEFORE calling
    :func:`extract_cited_local_markers`, rather than silently dropping a
    hallucinated marker and persisting the rest — ``extract_cited_local_markers``
    itself only returns the intersection, so it alone can never detect that
    something invalid was asked for in the first place."""
    for m in re.finditer(r"\[(\d+)\]", text or ""):
        if f"[{m.group(1)}]" not in valid_markers:
            return True
    return False


def validate_citation_markers_against_context(
    markers: List[str], valid_markers: frozenset,
) -> None:
    """Fail closed (:class:`AgentArtifactCitationInvalid`) if *markers*
    contains a ``[W#]`` web marker (deferred entirely in v1) or a local
    ``[N]`` marker absent from *valid_markers* (the authoritative sealed D2
    context this run actually has). This is the pure contract; wiring it
    against a REAL sealed context snapshot is D5C's job — D5B only proves
    the function against synthetic fixtures."""
    for marker in markers:
        if WEB_MARKER_PATTERN.fullmatch(marker):
            raise AgentArtifactCitationInvalid(
                "web citation markers are deferred in multi-agent v1"
            )
        if marker not in valid_markers:
            raise AgentArtifactCitationInvalid("citation marker not in the sealed context")


# ── artifact construction / validation ────────────────────────────────


def build_agent_artifact(
    *,
    role: str,
    parent_artifact_id: Optional[str],
    content: str,
    summary: str,
    claims: List[str],
    citation_markers: List[str],
    assumptions: List[str],
    uncertainties: List[str],
) -> dict:
    """Sanitize role-owned text fields and assemble a validated
    ``agent_artifact_v1`` dict. ``role``/``parent_artifact_id`` are
    SERVER-OWNED — the caller (never the role's own text) supplies them;
    ``sequence`` is derived, never accepted as an independent argument."""
    if not is_known_role(role):
        raise AgentArtifactInvalid(f"unknown role: {role!r}")
    artifact = {
        "artifact_version": AGENT_ARTIFACT_VERSION,
        "role": role,
        "sequence": sequence_for_role(role),
        "parent_artifact_id": parent_artifact_id,
        "content": _sanitize_text(content),
        "summary": _sanitize_text(summary),
        "claims": _sanitize_list(list(claims)),
        "citation_markers": list(citation_markers),
        "assumptions": _sanitize_list(list(assumptions)),
        "uncertainties": _sanitize_list(list(uncertainties)),
    }
    return validate_agent_artifact(artifact)


def validate_agent_artifact(artifact: object) -> dict:
    """Strict, closed-schema validation — called BOTH before sealing (on a
    freshly built artifact) and again after opening (on whatever the
    ciphertext decrypted to), so a tampered-but-decryptable payload or a
    hand-built dict that skipped :func:`build_agent_artifact` is caught
    identically either way. Every failure raises
    :class:`AgentArtifactInvalid` (or the more specific
    :class:`AgentArtifactMarkerBlocked`) — never partially accepted."""
    from app.config import settings

    if not isinstance(artifact, dict):
        raise AgentArtifactInvalid("artifact is not an object")
    if set(artifact) != _REQUIRED_ARTIFACT_FIELDS:
        raise AgentArtifactInvalid("artifact has missing or extra fields")

    version = artifact["artifact_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != AGENT_ARTIFACT_VERSION:
        raise AgentArtifactInvalid(f"unsupported artifact_version {version!r}")

    role = artifact["role"]
    if not is_known_role(role):
        raise AgentArtifactInvalid(f"unknown role: {role!r}")

    sequence = artifact["sequence"]
    if (isinstance(sequence, bool) or not isinstance(sequence, int)
            or sequence != sequence_for_role(role)):
        raise AgentArtifactInvalid("sequence does not correspond to role")

    parent = artifact["parent_artifact_id"]
    expected_parent = expected_parent_role(role)
    if expected_parent is None:
        if parent is not None:
            raise AgentArtifactInvalid(f"role {role!r} must not have a parent_artifact_id")
    else:
        if not isinstance(parent, str) or not parent:
            raise AgentArtifactInvalid(f"role {role!r} requires a non-empty parent_artifact_id")

    content = artifact["content"]
    summary = artifact["summary"]
    if not _is_plain_str(content) or not content:
        raise AgentArtifactInvalid("content must be a non-empty string")
    if not _is_plain_str(summary) or not summary:
        raise AgentArtifactInvalid("summary must be a non-empty string")
    if len(content) > int(settings.multi_agent_max_content_chars):
        raise AgentArtifactInvalid("content exceeds the configured bound")
    if len(summary) > _MAX_SUMMARY_CHARS:
        raise AgentArtifactInvalid("summary exceeds the bound")

    claims = artifact["claims"]
    assumptions = artifact["assumptions"]
    uncertainties = artifact["uncertainties"]
    if not _is_bounded_str_list(claims, max_count=int(settings.multi_agent_max_claims),
                                max_item_chars=_MAX_LIST_ITEM_CHARS):
        raise AgentArtifactInvalid("claims must be a bounded list of non-empty strings")
    if not _is_bounded_str_list(assumptions, max_count=int(settings.multi_agent_max_assumptions),
                                max_item_chars=_MAX_LIST_ITEM_CHARS):
        raise AgentArtifactInvalid("assumptions must be a bounded list of non-empty strings")
    if not _is_bounded_str_list(uncertainties, max_count=int(settings.multi_agent_max_uncertainties),
                                max_item_chars=_MAX_LIST_ITEM_CHARS):
        raise AgentArtifactInvalid("uncertainties must be a bounded list of non-empty strings")

    citation_markers = artifact["citation_markers"]
    if not isinstance(citation_markers, list) or len(citation_markers) > int(settings.multi_agent_max_claims):
        raise AgentArtifactInvalid("citation_markers must be a bounded list")
    for marker in citation_markers:
        if (not isinstance(marker, str) or len(marker) > _MAX_CITATION_MARKER_CHARS
                or not (_LOCAL_MARKER_PATTERN.match(marker) or WEB_MARKER_PATTERN.fullmatch(marker))):
            raise AgentArtifactInvalid("malformed citation marker")

    # Marker fail-closed check, re-run on every text-bearing field — catches
    # a hand-built/tampered artifact that bypassed build_agent_artifact's
    # own sanitization, and re-confirms a legitimately-sealed one on open.
    for text in (content, summary, *claims, *assumptions, *uncertainties):
        _assert_no_marker(text)

    # Reject non-finite numbers / unsupported nested structures anywhere
    # (defense in depth — the checks above already reject every field
    # individually, this is a final structural sweep).
    try:
        canonical = canonicalize(artifact)
    except Exception:  # noqa: BLE001 — canonicalization failure is itself invalid input
        raise AgentArtifactInvalid("artifact cannot be canonicalized") from None
    if len(canonical) > int(settings.multi_agent_max_artifact_bytes):
        raise AgentArtifactInvalid("artifact exceeds the configured serialized byte bound")

    return artifact


def compute_artifact_hash(artifact: dict) -> str:
    """Deterministic content hash of the CANONICAL serialization — same
    input always hashes the same, different content or lineage
    (``parent_artifact_id``/``role``/``sequence`` are all part of the
    canonicalized dict) always hashes differently. No ``created_at`` field
    exists in the schema specifically so this stays true for two
    genuinely-equivalent artifacts sealed at different times (a
    timestamp-inclusive hash would defeat determinism-of-equivalent-input
    by construction)."""
    return content_hash(canonicalize(artifact).decode("utf-8"))


# ── sealed persistence (mirrors app.run_engine.research_snapshot exactly) ──


def find_existing_agent_step(db: Session, run_id: str, seq: int) -> Optional[RunStep]:
    """Look up an already-sealed (or in-flight) role step by its
    authoritative ``(run_id, seq)`` — the same real SQL
    ``UniqueConstraint("run_id", "seq")`` D4B's at-most-one authority
    already rests on (``models.RunStep``) is what prevents duplicate
    sequence ownership here too; this function is only the lookup a caller
    who lost a seal race (or is resuming) uses to find the winner."""
    return db.query(RunStep).filter(RunStep.run_id == run_id, RunStep.seq == seq).first()


def seal_agent_artifact(
    db: Session,
    *,
    tenant_id: Optional[str],
    user_id: int,
    session_id: Optional[int],
    run_id: str,
    step_id: str,
    step_seq: int,
    artifact: dict,
    commit: bool = False,
) -> RunStep:
    """Validate *artifact*, seal it as an encrypted ``RunPayload``, and bind
    it to a NEW ``RunStep`` row at ``(run_id, step_seq)`` — all in the
    CALLER's own transaction (``commit=False`` by default, exactly like
    ``seal_research_snapshot``). ``RunStep.id`` (``step_id``) IS this
    artifact's identity — a later role's ``parent_artifact_id`` names the
    PARENT role's ``step_id``, nothing else.

    ``step_seq`` is an EXPLICIT, CALLER-COMPUTED ``RunStep.seq`` — deliberately
    NOT derived from ``sequence_for_role(role)`` (the artifact's own internal
    1/2/3 lineage-sequence field, part of the sealed JSON, unrelated to
    ``RunStep.seq``). The two numbering schemes differ: ``RunStep.seq`` also
    counts the pre-existing ``load_context`` submission step (seq 1) every
    mode already has, so a role's real ``RunStep.seq`` is
    ``sequence_for_role(role) + 1`` (researcher=2, solution_specialist=3,
    reviewer=4) — mirrors ``seal_research_snapshot``'s own ``seq`` parameter
    (also caller-computed via ``max(existing seqs) + 1``, never derived from
    a document-internal field). Passing the artifact's ``sequence`` directly
    as ``step_seq`` would collide with ``load_context``'s own ``seq=1`` row
    and fail the real ``UniqueConstraint(run_id, seq)`` — a genuine defect
    caught during D5C worker integration, before any caller ever existed to
    trigger it in D5B.

    Raises :class:`AgentArtifactInvalid`/:class:`AgentArtifactMarkerBlocked`
    (fail closed BEFORE any DB write) if *artifact* is malformed, or
    :class:`AgentArtifactBindingMismatch` (also before any write) if a
    non-Researcher role's ``parent_artifact_id`` does not resolve to a REAL,
    already-sealed ``RunStep`` of the expected parent role within THIS
    ``run_id`` and owned by this exact ``tenant_id``/``user_id`` — proving
    lineage against actual prior state, not merely the artifact's own
    self-reported shape. Raises a SQLAlchemy ``IntegrityError`` at
    flush/commit — never caught here — if a
    concurrent caller already won ``(run_id, seq)``; the caller must catch
    that, roll back, and use :func:`find_existing_agent_step` instead
    (identical concurrency contract to D4B's research snapshot — this
    module makes no stronger real-database-concurrency claim than that,
    and this phase has NOT run it against a live PostgreSQL server).

    ``RunStep.output_ref`` receives ONLY bounded structural metadata (role,
    sequence, artifact_version, content hash, and item counts) — never the
    plaintext ``content``/``summary``/``claims``/etc. text itself (matches
    ``seal_research_snapshot``'s own ``output_ref`` convention)."""
    validated = validate_agent_artifact(artifact)
    role = validated["role"]
    parent_id = validated["parent_artifact_id"]
    expected_parent = expected_parent_role(role)
    if expected_parent is not None:
        # Lineage is proved against the REAL prior RunStep, not merely the
        # artifact's own (self-reported-shape) parent_artifact_id string —
        # this is what makes self-parenting, cycles, a skipped/wrong parent
        # role, and cross-run/tenant/user linkage structurally impossible to
        # seal, not merely discouraged by convention. A missing/mismatched
        # parent fails closed BEFORE any write in this call.
        parent_step = (
            db.query(RunStep)
            .filter(RunStep.id == parent_id, RunStep.run_id == run_id)
            .first()
        )
        if (
            parent_step is None
            or parent_step.kind != STEP_KIND_FOR_ROLE[expected_parent]
            or parent_step.tenant_id != tenant_id
            or parent_step.user_id != user_id
        ):
            raise AgentArtifactBindingMismatch(
                "parent_artifact_id does not reference the expected parent "
                "role's sealed step within this run"
            )
    plaintext = canonicalize(validated).decode("utf-8")
    payload = seal_payload(
        db,
        tenant_id=tenant_id, user_id=user_id, run_id=run_id, step_id=step_id,
        payload_type=AGENT_ARTIFACT_PAYLOAD_TYPE, plaintext=plaintext,
        commit=False,
    )
    step = RunStep(
        id=step_id, run_id=run_id, tenant_id=tenant_id, user_id=user_id,
        session_id=session_id, seq=step_seq, kind=STEP_KIND_FOR_ROLE[role], engine="native",
        status="succeeded", output_payload_id=payload.id,
        output_ref=agent_artifact_output_ref(validated),
    )
    db.add(step)
    if commit:
        db.commit()
    return step


def agent_artifact_output_ref(validated: dict) -> str:
    """The bounded, plaintext-free ``RunStep.output_ref`` JSON for a sealed
    artifact — factored out so a caller managing its OWN RunStep lifecycle
    (see :func:`validate_and_seal_agent_artifact`) writes the IDENTICAL
    metadata shape ``seal_agent_artifact`` does, not a second drifting
    format."""
    return json.dumps({
        "role": validated["role"],
        "sequence": validated["sequence"],
        "artifact_version": validated["artifact_version"],
        "content_hash": compute_artifact_hash(validated),
        "claim_count": len(validated["claims"]),
        "citation_marker_count": len(validated["citation_markers"]),
    }, sort_keys=True)


def validate_and_seal_agent_artifact(
    db: Session,
    *,
    tenant_id: Optional[str],
    user_id: int,
    run_id: str,
    step_id: str,
    artifact: dict,
) -> tuple[dict, RunPayload]:
    """Validate *artifact*, prove lineage against a REAL parent ``RunStep``
    (identical check to :func:`seal_agent_artifact`), and seal it as an
    encrypted ``RunPayload`` — WITHOUT creating or touching any ``RunStep``
    row. Returns ``(validated_artifact, sealed_payload)``.

    This is the lower-level primitive :func:`seal_agent_artifact` itself
    calls internally, exposed separately for a caller that manages its OWN
    ``RunStep`` pending -> running -> succeeded lifecycle (a durable,
    attempt-once PROVIDER CALL precedes sealing — the SAME reason
    ``app.run_engine.worker._run_deep_research_synthesis`` manages its own
    step directly with a raw ``update(RunStep)`` rather than going through
    ``seal_research_snapshot``, which is reserved for D4B's retry-safe,
    no-provider-call acquisition step). ``seal_agent_artifact`` remains the
    right choice for any one-shot create-and-seal caller; this function
    exists so D5C's worker never has to duplicate the validation/lineage
    logic to get the pending->running->succeeded shape its provider calls
    require."""
    validated = validate_agent_artifact(artifact)
    role = validated["role"]
    parent_id = validated["parent_artifact_id"]
    expected_parent = expected_parent_role(role)
    if expected_parent is not None:
        parent_step = (
            db.query(RunStep)
            .filter(RunStep.id == parent_id, RunStep.run_id == run_id)
            .first()
        )
        if (
            parent_step is None
            or parent_step.kind != STEP_KIND_FOR_ROLE[expected_parent]
            or parent_step.tenant_id != tenant_id
            or parent_step.user_id != user_id
        ):
            raise AgentArtifactBindingMismatch(
                "parent_artifact_id does not reference the expected parent "
                "role's sealed step within this run"
            )
    plaintext = canonicalize(validated).decode("utf-8")
    payload = seal_payload(
        db,
        tenant_id=tenant_id, user_id=user_id, run_id=run_id, step_id=step_id,
        payload_type=AGENT_ARTIFACT_PAYLOAD_TYPE, plaintext=plaintext,
        commit=False,
    )
    return validated, payload


def open_agent_artifact(
    db: Session,
    step: RunStep,
    *,
    expected_tenant_id: Optional[str],
    expected_user_id: int,
    expected_run_id: str,
    expected_role: str,
) -> dict:
    """Re-open (never re-generate) the artifact already sealed for *step*.
    Fails closed on any binding mismatch, wrong payload type, wrong/missing
    role, or a malformed decrypted artifact (both via the payload
    envelope's own authenticated context check AND this function's own
    step-level + schema checks, defense in depth) — opening with a
    different tenant/user/run, or asking for the wrong role's step,
    both fail closed here, never silently return a substitute."""
    if not is_known_role(expected_role):
        raise AgentArtifactInvalid(f"unknown role: {expected_role!r}")
    if step.run_id != expected_run_id:
        raise AgentArtifactBindingMismatch("run_id mismatch")
    if step.kind != STEP_KIND_FOR_ROLE[expected_role]:
        raise AgentArtifactBindingMismatch("not a sealed artifact step for the expected role")
    if not step.output_payload_id:
        raise AgentArtifactBindingMismatch("step has no sealed output payload")
    plaintext = open_payload(
        db, step.output_payload_id,
        expected_tenant_id=expected_tenant_id, expected_user_id=expected_user_id,
        expected_run_id=expected_run_id, expected_step_id=step.id,
        expected_payload_type=AGENT_ARTIFACT_PAYLOAD_TYPE, expected_action_hash=None,
    )
    try:
        artifact = json.loads(plaintext)
    except (ValueError, TypeError, RecursionError):
        raise AgentArtifactInvalid("decrypted artifact is not valid JSON") from None
    return validate_agent_artifact(artifact)
