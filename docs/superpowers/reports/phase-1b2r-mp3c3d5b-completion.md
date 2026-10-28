# Phase 1B.2R MP-3C3D5B — Completion Report

**Sealed role-artifact foundation and pure orchestration contracts for
durable Multi-Agent.** Foundation only, per the MP-3C3D5A plan. This session
resumed an interrupted implementation (orchestration module, artifact
module, and config settings/validator already existed in the working tree),
inspected it against the repository (not against memory), corrected three
defects, added the missing readiness module and generic-worker reservation,
wrote 153 focused tests, and documented the result. **Not production-ready
by design** — D5C (worker integration + activation) has not started.

Status: **READY FOR USER REVIEW**.

---

## 1. Source-control evidence

- Branch: `phase-1b1-approval-domain`
- HEAD: `b2e67183d4791cbbaed9d3d98e8833983510f3ff` (== merge-base with `main`)
- Pre-existing `stash@{0}: 7ab31a5` — untouched throughout
- Nothing staged, committed, merged, pushed, reset, rebased, stashed,
  cleaned, discarded, or branch-switched this session
- `git diff --check`: clean

## 2. Skills and working style

Loaded `superpowers:using-superpowers` at session start (harness-mandated).
No other Superpowers process skill (brainstorming, systematic-debugging)
applied — this was a scoped resume-and-correct task against an explicit,
already-detailed instruction set, not an open design or bug investigation.
Ponytail (full mode, active for the whole session) governed every
implementation choice; its reuse ladder was applied concretely:

1. **Reused RunPayload encryption/binding** unchanged (`seal_payload`/
   `open_payload`) — no second encryption primitive.
2. **Reused canonical JSON/hashing** (`app.approval.canonical.canonicalize`,
   `research_sources.content_hash`) — no second canonicalizer.
3. **Reused `chat_stream.MarkerScanner`** and `research_sources
   .neutralize_markers` — no second marker detector.
4. **Reused existing test fixtures** (`tests/run_engine_helpers.py`,
   `tests/test_research_snapshot_persistence.py`'s seal/open round-trip
   pattern, `tests/test_run_payloads.py`'s tamper-test pattern,
   `tests/test_deep_research_readiness.py`'s flags-on/predicate pattern) —
   no new fixture module.
5. **New primitive created only once, where repository evidence required
   it**: the parent-lineage DB lookup inside `seal_agent_artifact` (§5) —
   every other file reuses an existing shape verbatim.

Two dead-code / unreachable-check corrections were caught by ponytail's
"lazy code without its check is unfinished" principle: an attempted
config-level bool-vs-int guard turned out to be unreachable (Pydantic v2
coerces `bool` to plain `int` before any `model_validator(mode="after")`
runs) and was reverted rather than shipped as inert code (§8).

## 3. Locked product decisions (unchanged, restated for the record)

1. Fixed single-pass role graph: Researcher → Solution Specialist → Reviewer.
2. Exactly 3 roles, at most 3 provider calls.
3. No revision loop, role recursion, nested agents, parallel execution,
   dynamic role selection, or LLM task-router call.
4. Text/evidence-only v1: no MCP, no Python, no shell/terminal, no
   browser/computer actions, no memory/directive writes, no tool
   continuation.
5. Second role is `solution_specialist`, not `coder`.
6. No live web acquisition, no `[W#]` citations in D5B/D5C v1.
7. Free/local-only — no paid/implicit provider fallback (moot in D5B: zero
   provider calls exist).
8. D5B is foundation-only: no worker sweep, no provider call, no
   public-route activation, no settings-capability activation, no frontend
   change.

## 4. Verified interrupted state (before this pass)

Present and unmodified in shape: `multi_agent_orchestration.py`,
`multi_agent_artifacts.py`, `run_multi_agent_enabled=False` + 8
`multi_agent_*` config bounds + their `_validate_multi_agent_limits()`
validator, additive `"multi_agent"` in `_RUN_KINDS` (`engine.py`,
`events.py`), additive `"agent_artifact"` `RunPayload` type, additive
`agent.progress` control-event schema.

Confirmed missing (via direct file inspection, not memory):
`multi_agent_readiness.py` did not exist, though `config.py`'s own comment
already referenced it by name. Zero test files referenced any of the three
modules.

## 5. Defects found and corrected this pass

| # | Defect | Fix |
|---|---|---|
| 1 | `seal_agent_artifact` validated `parent_artifact_id` for *shape* only (non-empty string) — never verified it resolved to a REAL, already-sealed `RunStep` of the expected parent role in the same run/tenant/user. Self-parenting, a skipped/wrong-role parent, and cross-run linkage were only prevented by *caller convention*, not enforced. | Added a `db.query(RunStep)` lookup inside `seal_agent_artifact`, before any write: fails closed (`AgentArtifactBindingMismatch`) unless the parent step exists, has `kind == STEP_KIND_FOR_ROLE[expected_parent_role(role)]`, and matches `run_id`/`tenant_id`/`user_id`. Uses only already-imported names (`expected_parent_role`, `STEP_KIND_FOR_ROLE`) — no new primitive. |
| 2 | `AGENT_PROGRESS_CODES` was defined twice: once in `multi_agent_artifacts.py`, and again as a second inline literal frozenset inside `events.py`'s `agent.progress` schema (the `research.progress` precedent's stated reason — "`_FieldRule.enum` must be a literal at class-body evaluation time" — does not actually apply here: `CONTROL_EVENT_SCHEMAS` is a plain module-level dict, not a class body, and neither `multi_agent_orchestration` nor its only dependency, `errors.py`, imports `events.py`, so no circular-import blocker exists). | Moved the frozenset to `multi_agent_orchestration.py` (the module with zero side-effecting dependencies) as the ONE canonical `AGENT_PROGRESS_CODES`; `events.py` and `multi_agent_artifacts.py` both import it. Did **not** touch the pre-existing, out-of-scope `research.progress` duplication (D4C, not D5B). |
| 3 | The generic `RunWorker.claim_next_queued`/`recover_expired` sweep excluded `mcp_call`, `chat_stream`, and `deep_research` but **not** `multi_agent`. Since the handler registry is empty for `multi_agent`, a queued run would have been claimed, transitioned to `running`, and immediately failed closed as `unknown_handler` — silently consuming the run before D5C's own dedicated sweep could ever process it. | Added `MULTI_AGENT_RUN_KIND` to `multi_agent_orchestration.py` and a matching `.where(Run.kind != _MULTI_AGENT_RUN_KIND)` exclusion to both queries, mirroring the `deep_research` pattern exactly. Proven with a real hermetic-DB test (queued run stays `queued`; expired-lease run stays `running`), not source inspection alone. |

One additional non-defect correction: an initial attempt to add a
`bool`-rejection check for the six `multi_agent_max_*` Settings fields
inside `_validate_multi_agent_limits` was reverted after testing proved it
unreachable — Pydantic v2 coerces `True`/`False` into plain `int` (`1`/`0`)
*before* any `model_validator(mode="after")` sees the value, so the check
could never fire (a repo-wide Settings/Pydantic characteristic, not
multi-agent-specific). The genuine "no bool-as-int" boundary is the
untyped `agent_artifact` schema (raw `json.loads()` output, where Python's
JSON decoder does preserve `bool` distinctly from `int`) — already enforced
correctly there and covered by tests (`test_bool_version_rejected`,
`test_bool_sequence_rejected`).

## 6. Files changed this pass

- `backend/app/run_engine/multi_agent_orchestration.py` — added
  `MULTI_AGENT_RUN_KIND`, `AGENT_PROGRESS_CODES`.
- `backend/app/run_engine/multi_agent_artifacts.py` — imports
  `AGENT_PROGRESS_CODES` instead of redefining it; added the parent-lineage
  DB check in `seal_agent_artifact`.
- `backend/app/run_engine/events.py` — `agent.progress` schema now imports
  the canonical `AGENT_PROGRESS_CODES` instead of a second literal.
- `backend/app/run_engine/worker.py` — imports `MULTI_AGENT_RUN_KIND`;
  excludes it from `claim_next_queued` and `recover_expired`.
- `backend/app/config.py` — added `multi_agent_max_active_runs_per_user`
  (default `2`) plus its `>= 1` int-bound validation.
- `backend/app/run_engine/multi_agent_readiness.py` — **new file**, the
  future authoritative executability predicate.
- `tests/test_multi_agent_orchestration.py` — **new**, 43 tests.
- `tests/test_multi_agent_artifacts.py` — **new**, 60 tests.
- `tests/test_multi_agent_readiness.py` — **new**, 50 tests.
- `THUNDER_AI_PRD.md` — MP-3C3D5B row updated `not started` → `complete
  (foundation only)`.
- `docs/superpowers/plans/2026-09-08-phase-1b2r-durable-run-resume-architecture.md`
  — new §44 as-built section.
- This report.

No frontend file touched. No migration file created or needed.

## 7. Canonical contracts

**Role graph** (`multi_agent_orchestration.py`):
`ROLE_ORDER = (researcher, solution_specialist, reviewer)`; sequence is
`index + 1` (1/2/3); `STEP_KIND_FOR_ROLE = {researcher: agent_research,
solution_specialist: agent_solution, reviewer: agent_review}`; `seq 1`
(`load_context`) is the pre-existing chat submission step, reused
unmodified. `MAX_PROVIDER_CALLS = 3`. Pure functions —
`is_known_role`, `sequence_for_role`, `role_for_sequence`,
`expected_parent_role` (depth exactly 1), `expected_next_role`,
`validate_role_transition` (skip/repeat/reversal raise
`IllegalRoleTransition`), `is_terminal_role` (Reviewer only),
`assert_call_budget` (raises `ProviderCallBudgetExceeded` at/over the
ceiling). Zero DB/network/LLM/tool/event-emission imports — verified by
both source inspection and a dedicated test
(`test_orchestration_module_is_db_network_and_llm_free`).

**Progress codes**: `AGENT_PROGRESS_CODES = {agent_researching,
agent_research_complete, agent_solving, agent_solution_complete,
agent_reviewing, agent_finalizing}` — the one definition, imported by
`events.py`'s `agent.progress` control-event schema and by
`multi_agent_artifacts.py`. No worker emits any of these yet.

**Run kind**: `MULTI_AGENT_RUN_KIND = "multi_agent"` — already present in
`engine._RUN_KINDS`/`events._RUN_KINDS` (accepted by `submit_run`), now also
consumed by `worker.py`'s reservation.

## 8. `agent_artifact_v1` schema (exact)

```
{
  "artifact_version": 1,                    # strict equality, non-bool int
  "role": "researcher"|"solution_specialist"|"reviewer",   # server-owned
  "sequence": 1|2|3,                        # derived from role, never independent
  "parent_artifact_id": str | null,         # required per role's expected_parent_role
  "content": str (1..multi_agent_max_content_chars),
  "summary": str (1..2000 chars),
  "claims": [str] (0..multi_agent_max_claims, each ≤500 chars),
  "citation_markers": [str] (0..multi_agent_max_claims, "[N]" or "[WN]" shape only),
  "assumptions": [str] (0..multi_agent_max_assumptions, each ≤500 chars),
  "uncertainties": [str] (0..multi_agent_max_uncertainties, each ≤500 chars)
}
```

Closed field set (`set(artifact) != _REQUIRED_ARTIFACT_FIELDS` rejects any
extra/missing key). Every text-bearing field is marker-scanned (§9) before
acceptance and again on every re-validation (built artifact, or a
just-decrypted one). Whole-artifact canonical serialization (RFC 8785,
`app.approval.canonical.canonicalize`) additionally rejects NaN/Infinity,
unsafe integers, `Decimal`/`bytes`/`set`, non-str dict keys, and nesting
past depth 200 — and its byte length is capped at
`multi_agent_max_artifact_bytes` (≤ `run_payload_max_bytes`).
`compute_artifact_hash` hashes that same canonical serialization — no
`created_at` field exists in the schema *specifically* so two genuinely
equivalent artifacts sealed at different times hash identically, while any
content or lineage (`role`/`sequence`/`parent_artifact_id`) difference
changes the hash. Proven by `test_hash_is_deterministic_for_equivalent_artifacts`
/ `test_hash_changes_when_content_changes` /
`test_hash_changes_when_lineage_changes`.

Model output can never control artifact identity, role, sequence, parent,
run/step identity, provider/model, or next role — all of those are either
function parameters supplied by the (future, trusted) D5C caller, or
derived. `citation_markers` format is schema-checked here; *semantic*
validation against the real sealed context is a separate function (§10),
matching D4B's own format-vs-context split.

## 9. Marker fail-closed policy

Reuses `chat_stream.MarkerScanner` fed `ALL_WATCHED_MARKERS` (mcp_call,
python_code, proposed_command, browser_action, save_memory, save_directive,
use_memory, plan, lesson) — the exact same detector durable chat/deep-research
already use. A complete marker or one truncated at the end of the text (the
scanner's `truncated` flag) both raise `AgentArtifactMarkerBlocked` — fail
closed, never neutralized-and-kept. `[computer_action` is checked by one
additional, explicitly-documented hardcoded regex (not yet in the repo's
canonical `_MARKER_OPENS` list; defense-in-depth only, since multi-agent v1
has no tool-dispatch path at all regardless). After the marker check, text
passes through `research_sources.neutralize_markers` (belt-and-suspenders,
not the primary gate). Mixed-case, partial/truncated, and every marker
family are covered by parametrized tests
(`test_every_executable_marker_family_fails_closed`,
`test_mixed_case_marker_fails_closed`,
`test_partial_marker_at_end_of_text_fails_closed`). Benign discussion of
bracket syntax that never forms a real marker (e.g. `[1]`, `[citations]`) is
accepted — documented conservative v1 limitation: any text that *shapes*
like a reserved marker prefix is rejected, not just genuinely executable
ones.

## 10. Citation contract

`extract_cited_local_markers(text, valid_markers)` — regex `\[(\d+)\]` over
the text, filtered to the caller-supplied authoritative set, first-reference
order, deduplicated. Never scans for or accepts a raw URL as a citation
source. `validate_citation_markers_against_context` fails closed
(`AgentArtifactCitationInvalid`) on any `[W#]` marker (deferred entirely in
v1) or any local marker absent from the real sealed D2 context. Both are
pure functions; D5C is responsible for wiring them against a real context
snapshot before calling `build_agent_artifact` — D5B proves them against
synthetic fixtures only (unchanged from the interrupted design, confirmed
correct). No existing plain-chat or Deep Research citation behavior was
touched.

## 11. Encryption, binding, and lineage proof

`seal_agent_artifact`/`open_agent_artifact` reuse `payloads.seal_payload`/
`open_payload` unchanged — the envelope binds `tenant_id`, `user_id`,
`run_id`, `step_id`, `payload_type` (`"agent_artifact"`), and version,
authenticated inside the ciphertext (Fernet HMAC). `session_id` is **not**
part of the payload envelope — confirmed as a pre-existing, repo-wide
characteristic shared by every `seal_payload` caller (`research_snapshot.py`
documents the same thing); it is recorded on the `RunStep` row instead,
identically to `seal_research_snapshot`. Plaintext never appears in
`RunStep.input_ref`/`output_ref` (`output_ref` carries only role, sequence,
version, content hash, and item counts — proven by
`test_output_ref_contains_no_plaintext`) or in raised exception text
(proven by `test_canary_absent_from_binding_mismatch_exception_text`, etc.).
Fails closed, with typed non-secret errors, on: wrong tenant/user/run/step,
wrong payload type/version, tampered ciphertext (`RunPayloadIntegrityError`,
real Fernet-auth-tag test), malformed decrypted JSON, and a pathologically
deep decrypted JSON string (`RecursionError` caught and translated).

**Lineage** (the defect fixed in §5): Researcher has no parent (sequence 1,
bound to the run context itself). Solution Specialist's parent must be a
REAL, already-sealed Researcher step in the same run. Reviewer's parent must
be a REAL, already-sealed Solution Specialist step in the same run (depth
exactly 1 — a grandparent reference is never checked or accepted). Because
this is enforced inductively at each seal (a role can only be sealed once
its real parent already exists), self-parenting, cycles, a skipped parent,
a wrong-role parent, cross-run linkage, and reviewer-before-researcher are
all structurally impossible — proven against a real hermetic DB, not
convention (`test_seal_rejects_missing_parent_step`,
`test_seal_rejects_wrong_role_parent`, `test_seal_rejects_self_parenting`,
`test_seal_rejects_cross_run_parent`,
`test_reviewer_before_researcher_is_structurally_impossible`). Duplicate
sequence ownership is prevented by the real SQL
`UniqueConstraint("run_id", "seq")` (`uq_run_steps_run_seq`), proven under a
genuine `IntegrityError`, PostgreSQL-portable (not a Python
check-then-set).

## 12. Config limits and active-run-limit decision

All 8 pre-existing `multi_agent_*` bounds re-verified: `max_roles`/
`max_provider_calls` locked to exactly 3 (fail-fast if changed);
`role_timeout_s`/`total_deadline_s` must be finite, positive, and
`role_timeout_s < total_deadline_s`; `max_artifact_bytes` ≤
`run_payload_max_bytes`; all five count bounds must be positive ints.
**New this pass**: `multi_agent_max_active_runs_per_user: int = 2` —
foundation-only, matching the task's explicit instruction: D5B defines and
validates the bound, D5C is the actual enforcement point (its submission
path must count the caller's own non-terminal `multi_agent` runs and reject
transactionally, the same shape as `max_active_proposals_per_user`, NOT
`run_messages_stream_max_concurrent_per_user` — that bounds open SSE
streams, not active runs; a client can close and reopen a stream against
the same still-running run). Client cannot override any of these — they are
plain server-side `Settings` fields with no request-body path to them
anywhere in the new code (verified by
`test_client_cannot_override_locked_settings_via_the_shared_singleton`).

## 13. Readiness / public-activation boundary

`multi_agent_readiness.multi_agent_executable()` mirrors
`deep_research_executable()`'s dependency chain (`run_engine_enabled ∧
run_worker_enabled ∧ run_chat_stream_enabled ∧ run_public_api_enabled ∧
run_multi_agent_enabled ∧ RUN_IDEMPOTENCY_HMAC_KEY configured`) plus one
addition: a code-owned `_MULTI_AGENT_IMPLEMENTATION_READY = False` constant
that makes the predicate unconditionally `False` — proven by
`test_remains_false_even_with_every_config_flag_monkeypatched_true`, which
turns every config flag ON and shows the result is still `False`. **Not
wired into any route yet** (per locked decision 8) — but every surface
already rejects/hides `multi_agent` unconditionally without any D5B change:
`messages_stream.unsupported_mode_reason("multi_agent")` falls through to
the generic `mode_unsupported_multi_agent` slug (it is not in
`_SUPPORTED_DURABLE_MODES` and has no `deep_research`-style special case);
`routes/runs.py` calls the same function; `routes/settings.py` hardcodes
`available=False` for the durable-cutover branch regardless of
`run_multi_agent_enabled`. All three proven at runtime, not by source
inspection alone (`test_messages_stream_rejects_multi_agent_unconditionally`,
`test_settings_capability_unavailable_under_durable_cutover`).

## 14. Generic-worker reservation proof

`test_generic_worker_never_claims_a_queued_multi_agent_run` seeds a real
`queued` `multi_agent` run in a hermetic DB, calls
`RunWorker.claim_next_queued()`, asserts it returns `None`, and re-reads the
row to confirm `status == "queued"` (untouched). A parallel test seeds an
expired-lease `running` `multi_agent` run and proves `recover_expired()`
returns `[]` and the row is left `running` (not forced to
`outcome_unknown`). `test_no_dedicated_multi_agent_worker_sweep_exists`
confirms `RunWorker.advance_multi_agent_runs` does not exist.

## 15. Exact test commands and results

```
PYTHONPATH=backend .venv/Scripts/python.exe -m pytest \
  tests/test_multi_agent_orchestration.py \
  tests/test_multi_agent_artifacts.py \
  tests/test_multi_agent_readiness.py -q
```
→ **153 passed**, 0 failed, 0 skipped (43 orchestration + 60 artifacts + 50
readiness/config/worker-reservation).

```
PYTHONPATH=backend .venv/Scripts/python.exe -m pytest \
  tests/test_run_payloads.py tests/test_run_engine_events.py \
  tests/test_run_engine_models.py tests/test_research_config.py \
  tests/test_research_activation_boundary.py \
  tests/test_settings_mode_capability.py tests/test_run_engine_mp3c3d3a.py \
  tests/test_deep_research_readiness.py -q
```
→ **179 passed**, 0 failed, 0 skipped.

```
PYTHONPATH=backend .venv/Scripts/python.exe -m pytest \
  tests/test_run_engine_worker.py tests/test_run_engine_recovery.py \
  tests/test_run_engine_mp3b.py tests/test_run_engine_mp3c1.py \
  tests/test_deep_research_worker.py -q
```
→ **80 passed**, 0 failed, 0 skipped.

```
PYTHONPATH=backend .venv/Scripts/python.exe -m py_compile \
  backend/app/run_engine/multi_agent_orchestration.py \
  backend/app/run_engine/multi_agent_artifacts.py \
  backend/app/run_engine/multi_agent_readiness.py backend/app/config.py \
  backend/app/run_engine/payloads.py backend/app/run_engine/engine.py \
  backend/app/run_engine/events.py backend/app/run_engine/worker.py
```
→ clean.

`git diff --check` → clean. `PYTHONPATH=backend .venv/Scripts/python.exe -c
"import app.main"` → OK.

Not run (explicitly out of scope for this focused pass): the broad Phase 1B
suite, Docker, PostgreSQL, live providers, DuckDuckGo, browser tests,
ChromaDB/Ollama, frontend tests/build.

## 16. Security / adversarial coverage

Every executable marker family (mcp_call, python_code, proposed_command,
browser_action, computer_action, save_memory, save_directive), mixed-case
and partial/truncated variants; oversized content/claims/serialized-bytes;
malformed and pathologically-deep decrypted JSON; unknown/extra/missing
schema fields; bool-as-int on `artifact_version`/`sequence` (the layer where
it is actually reachable); every lineage attack (missing, wrong-role,
self-parenting, cross-run, skipped-role); duplicate sequence ownership
against a real `IntegrityError`; wrong tenant/user/run/step/role/payload-type
binding; tampered ciphertext; secret-canary absence from `output_ref` and
from every raised exception's `str()`; unknown and `[W#]` citation
rejection; model-supplied URLs never becoming citation metadata; generic
worker exclusion under a real hermetic claim/recovery race.

## 17. Migration decision

**No migration required or created.** `Run.kind`, `RunStep.kind`, and
`RunPayload.payload_type` remain unconstrained string columns (confirmed
unchanged in `models.py`); D5A's original no-migration conclusion holds and
was not contradicted by anything found this pass.

## 18. Remaining risks and the D5C boundary

- D5C must wire `multi_agent_executable()` into `messages_stream
  .unsupported_mode_reason` (a `deep_research`-style special case),
  `routes/settings._compute_chat_mode_capabilities`, and flip
  `_MULTI_AGENT_IMPLEMENTATION_READY` — the same constant, never a second
  predicate.
- D5C must implement `RunWorker.advance_multi_agent_runs` and the 3 step
  handlers (research/solve/review), enforcing `assert_call_budget` and
  `validate_role_transition` at each step, and must call
  `extract_cited_local_markers` + `validate_citation_markers_against_context`
  against the REAL sealed D2 context before ever calling
  `build_agent_artifact` with a citation list.
- D5C must implement the actual `multi_agent_max_active_runs_per_user`
  enforcement transactionally at submission time — D5B only validates the
  setting exists and is a positive int.
- Concurrency: this phase's lineage/uniqueness guarantees are proven against
  SQLite's real `IntegrityError`/`UniqueConstraint` behavior, matching every
  other RunEngine module's own stated posture — not independently
  re-verified against a live multi-worker PostgreSQL deployment.
- `[computer_action` remains a one-off hardcoded defensive regex, not yet
  part of the repository's own canonical `_MARKER_OPENS` list — flagged,
  not silently expanded into shared code, since doing so is out of this
  foundation-only phase's scope.

## 19. Confirmations

- No provider, network, tool, frontend, or dedicated multi-agent worker
  sweep was added.
- `run_multi_agent_enabled`, `run_engine_enabled`, and
  `run_messages_stream_durable_enabled` all remain `False`.
- Nothing was staged, committed, merged, pushed, reset, rebased, stashed,
  cleaned, discarded, or branch-switched; the pre-existing
  `stash@{0}: 7ab31a5` is untouched.

**D5B stops here. D5C (worker integration + activation) has not started.**
