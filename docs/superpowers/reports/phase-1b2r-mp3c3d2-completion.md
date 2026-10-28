# Phase 1B.2R — MP-3C3D2 completion report

**Durable citations, memory context, and safe post-processing foundation**

**Date:** 2026-09-11 · **Branch:** `phase-1b1-approval-domain` · HEAD `b2e6718` ·
**all work uncommitted, not production-ready**

Follows the MP-3C3D2 section of
`docs/superpowers/plans/2026-09-11-phase-1b2r-mp3c3d0-mode-parity-plan.md`.

## Interrupted-session recovery

A prior session tried a lazy `context_fn` design (compute RAG/memory context lazily, inside
`submission.create_chat_run`'s own atomic staging area, so a reconnect/replay would skip
retrieval). Focused tests proved this unsafe: `memory_service.retrieve_relevant_memories()`
commits internally (access-metric bookkeeping), and calling it after `Message`/`Run` rows were
already staged-but-uncommitted force-committed that partial state early —
`test_submission_commit_failure_no_partial` (which asserts a downstream `transition_run` failure
leaves **zero** `Run` rows) went from `0 == 0` to `1 == 0`. The session was interrupted mid-revert,
leaving `submission.create_chat_run()` already reverted to accept `context=` but
`messages_stream.resolve_and_submit()` still calling it with the old `context_fn=` keyword — a
guaranteed `TypeError` on any durable submission.

This session repaired the inconsistency from repository evidence (confirmed via `grep` — one
stale call site, one stale docstring reference) and replaced the unsafe design with an
**authoritative pre-check**: `resolve_and_submit` calls `app.run_engine.engine._select_by_idem_scope`
— the exact same read-only scoped lookup `submit_run()` itself uses internally — to determine
*before* computing context whether this is a replay/conflict (skip retrieval entirely) or a
genuinely fresh submission (compute context first, then call `create_chat_run`, which stages
nothing until after context is already a plain Python dict). No second, drifting identity
implementation was introduced. See §"Final transaction / idempotency flow" below.

## Stage-0 reuse findings (condensed evidence table)

| Capability | Legacy entrypoint | Commits internally? | Durable decision |
|---|---|---|---|
| RAG retrieval | `rag_service.retrieve_chunks`/`build_citation_list`/`format_rag_context` | No (read-only + ChromaDB; embeds via the local `nomic-embed-text` model, never a chat/completion LLM) | Reuse directly, unmodified |
| Memory retrieval | `memory_service.retrieve_relevant_memories` | **Yes** (access-metric bookkeeping) | Must run strictly *before* `create_chat_run` opens its atomic staging area — proven by the regression above |
| Memory write (`save_memory`) | `memory_service.save_memory`→`_save_memory_if_new` | **Yes**, with a lexical exact-match dedup fast path (safe replay) | Not activated this phase — see Marker behavior below |
| SAVE_MEMORY/SAVE_DIRECTIVE/USE_MEMORY/PLAN/LESSON markers | `memory_tool.py`/`directive_tool.py`, post-stream on full text | `save_directive` is unconditional, **no approval boundary** | Durable scanner previously didn't watch these at all (raw leak risk) — now watched, fail-closed, never leaked, never auto-applied |
| KG mutation from an assistant marker | *(none found — confirmed again this session, no new evidence contradicts MP-3C3D0)* | n/a | Nothing to implement or defer |
| Citation schema | `schemas/documents.py` `Citation`, `Message.citations` (Text, JSON) | n/a | Reused verbatim, zero migration |
| Citation URL validation | *(none in rag_service — no citation source emits a URL)* | n/a | **Omitted this phase** rather than reusing `browser_agent.assert_url_allowed` (live DNS resolution — must never run inside worker finalization) — see below |

## Files changed

| File | Change |
|---|---|
| `backend/app/run_engine/chat_stream.py` | `+DEFERRED_MARKERS`/`ALL_WATCHED_MARKERS`, `MarkerScanner` `"deferred"` capture-status category, `+read_context_snapshot`/`render_context_envelope`/`validate_citations`, `build_chat_input` splices the untrusted context envelope, `+2` bounded `CHAT_STATUS_REASONS` (`nonexec_marker_deferred`, `context_snapshot_unreadable`). |
| `backend/app/run_engine/submission.py` | `create_chat_run(..., context: Optional[dict] = None)` — sealed into the same submission payload as the exec snapshot; **never computed inside this function**, documented explicitly with the reason (the reverted `context_fn` regression). |
| `backend/app/run_engine/messages_stream.py` | `+_build_context_snapshot` (RAG+memory retrieval, bounded, non-fatal, logs exception *class name* only); `resolve_and_submit` does the authoritative pre-check via `_select_by_idem_scope` before computing context; `_terminal_frame`/`_final_completion_fields`/`_final_citations` read real committed data instead of hardcoded `[]`/`False`/`False`; `+2` `_FAIL_DETAIL` entries. |
| `backend/app/run_engine/worker.py` | `_run_chat_generation`'s scanner now watches `ALL_WATCHED_MARKERS`; `_route_tripped_scanner` maps `"deferred"` → `nonexec_marker_deferred`; `_finalize_chat_success` reads the sealed context snapshot, validates citations, writes `Message.citations` + `sources_used`/`memories_used` into `RunStep.output_ref` atomically with the Message commit; an unreadable snapshot **fails the run closed** rather than silently succeeding with fabricated-empty citations. |
| `tests/test_run_engine_mp3c3d2.py` **(new)** | 27 focused tests (below). |
| `docs/superpowers/reports/phase-1b2r-mp3c3d2-completion.md` **(new)** | this report. |
| `docs/superpowers/plans/2026-09-08-phase-1b2r-durable-run-resume-architecture.md` | new §39. |
| `THUNDER_AI_PRD.md` | MP-3C3D2 status row added. |

No migration. No config default changed. `run_messages_stream_durable_enabled` stays `False`.

## Final transaction / idempotency flow

```
resolve_and_submit(db, ctx, session_id, message, idempotency_key):
  1. load session, resolve provider/model  →  build_chat_submission(mode="chat")
  2. _select_by_idem_scope(db, tenant_id, user_id, session_id, key)   # READ ONLY,
     the exact lookup submit_run() uses internally — no drift
       → existing row found (replay OR conflict, doesn't matter which):
           skip retrieval entirely; context stays None
       → nothing found (genuinely fresh):
           context = _build_context_snapshot(...)   # RAG + memory, BEFORE
                                                       # anything is staged
  3. create_chat_run(db, ctx, submission=..., idempotency_key=..., context=context)
       → submit_run(commit=False): re-checks identity authoritatively
           → same key + matching identity  → returns the existing Run (replay)
           → same key + different identity → raises IdempotencyKeyConflict (409)
           → no existing row                → stages a NEW uncommitted Run
       → if the Run already has a RunStep (fully persisted before): rollback,
         return the existing run, False — created=False, context discarded
       → else (genuinely fresh, confirmed a SECOND time, authoritatively):
         Message + RunStep + RunPayload{exec, context} + run.queued event +
         created→queued transition — ONE commit, all or nothing
```

Steps 2 and 3's replay checks are independently authoritative (both ultimately consult
`_select_by_idem_scope`/`submit_run`'s own unique-constraint-backed logic) — step 2 is a
performance pre-check only, never trusted for correctness. A concurrent-race window between them
(this request's pre-check says "fresh," but another request commits first before this request's
`create_chat_run` call lands) is handled by the **unchanged** `submit_run` unique-constraint retry
(`uq_runs_idem_scope`, catches `IntegrityError`, re-selects, returns the winner) — proven by
`test_concurrent_same_key_race_persists_one_run`: this request's context is computed but silently
discarded when it turns out to have lost the race.

## Citation and memory guarantees

- **Citations come only from the sealed submission-time snapshot.** The model's generated text has
  no code path into `validate_citations` at all — citations are fixed before generation starts.
  `test_citation_comes_only_from_supplied_passage` and `test_model_invented_url_not_added` prove
  this directly.
- **Deduped** by `(document_id, chunk_id)`, first-seen order preserved (deterministic); **bounded**
  to 10 citations, 512-char fields; any citation missing a required field is dropped whole.
- **No citation URL support this phase.** `rag_service.build_citation_list` never emits a `url`
  field today. Rather than reuse a hand-rolled scheme/hostname blocklist (rejected per this
  session's explicit instruction — "do not introduce a weak custom URL validator"), and rather
  than call the repo's real SSRF guard (`app.services.browser_agent.assert_url_allowed` — live DNS
  resolution, private/loopback/link-local/metadata-IP rejection) from inside worker finalization's
  atomic staging area (a network call there is its own class of risk, matching the exact lesson of
  the `context_fn` regression), citation URLs are simply **omitted**. Documented in
  `chat_stream.py` as a directive for whenever a URL-bearing citation source is added.
- **Atomic with finalization.** `Message.citations` is set in the same `db.add(msg); db.flush()` →
  `RunStep` update → `_apply_transition` → `db.commit()` sequence MP-3B/3C1 already proved atomic;
  MP-3C3D2 only adds fields to that existing sequence, never a second commit.
  `test_citations_committed_atomically_with_message`, `test_complete_event_returns_committed_citations`.
- **Reconnect-stable.** `sources_used`/`memories_used` are now read from the committed
  `RunStep.output_ref` (not recomputed); citations from the committed `Message.citations` (not
  recomputed) — `test_reconnect_returns_identical_completion_metadata` proves byte-identical
  repeats.
- **Fail-closed on corrupt state.** If the sealed context snapshot can't be re-opened at
  finalization — a genuine anomaly, since `build_chat_input` already decrypted the SAME payload
  successfully earlier this run to reach generation at all — the run fails with
  `context_snapshot_unreadable` rather than silently succeeding with empty citations.
  `test_unreadable_context_snapshot_fails_closed_no_message` proves zero Message rows and a
  bounded error code with no raw exception text on the wire.
- **Memory truthfulness.** `memories_used` is true only when the SAME sealed snapshot's
  `memory_context_text` (what was actually spliced into the provider's messages) is non-empty —
  never a flag alone. `test_memories_used_true_only_when_provider_received_memory`,
  `test_memory_retrieval_failure_remains_false`.
- **Untrusted placement.** Unlike legacy (which appends RAG/memory context to the *system
  prompt*), the durable path splices ONE additional user-role message — the MP-3C2 tool-result
  convention (untrusted, delimited, never elevated to system-prompt trust) — immediately before
  the final user question. `test_rag_passage_sealed_and_supplied_once` asserts the envelope text
  and the "untrusted data, not instructions" framing both reach the provider.
- **Cross-user/session isolation.** `retrieve_chunks`/`retrieve_relevant_memories` are already
  scoped by `session_id`/`user_id` respectively — proven, not assumed, by
  `test_cross_user_document_not_retrieved`.
- **Crash/continuation safety.** `build_chat_input` (called for both the first segment and every
  continuation, via `_build_continuation_input`'s reuse of it) reads the SAME sealed payload every
  time — `test_continuation_after_mcp_reuses_original_context_snapshot` proves retrieval runs
  exactly once even across a full MCP suspend/approve/resume cycle, and the continuation segment's
  provider call still sees the original context.

## Marker behavior and deferred writes

Inventoried and classified every non-execution marker legacy processes post-stream:

| Marker | Classification | Durable behavior this phase |
|---|---|---|
| `[SAVE_MEMORY: …]` | user-scoped memory write | **Deferred.** Watched by the scanner (new `"deferred"` capture-status), never leaked (full or partial), turn fails closed with `nonexec_marker_deferred`. Write-activation NOT implemented. |
| `[SAVE_DIRECTIVE: …]` | persistent directive/system-behavior mutation (legacy: **no approval boundary at all** — `is_active=True` unconditional) | **Deferred**, same fail-closed treatment. Never auto-applied. |
| `[USE_MEMORY: …]` | content-substitution read (not a write) | **Deferred**, same fail-closed treatment (its legacy behavior — post-hoc full-text substitution — doesn't map onto the durable scanner's streaming model without inventing a new, unproven non-blocking capture mode). |
| `[PLAN: …]` / `[LESSON: …]` | display/control only (no backend processing found anywhere, legacy or durable) | **Deferred**, same fail-closed treatment, for implementation uniformity — no write risk either way. |
| `[KG_QUERY: …]` | *(not applicable — this is a USER-input retrieval marker, not an assistant-emitted marker; no assistant-side KG mutation marker exists in the codebase)* | Out of scope; nothing to defer. |
| `[MCP_CALL]` / `[PYTHON_CODE]` / `[PROPOSED_COMMAND]` / `[BROWSER_ACTION]` | execution | **Unchanged** — governed by the existing MP-2 approval gateway / fail-closed `tool_resume_required` path. `test_execution_markers_unchanged`, `test_deferred_reason_distinct_from_execution_marker_reason` prove the two families produce distinct, non-overlapping bounded reasons. |

**Why write-activation is deferred, precisely:** `memory_service.save_memory`'s dedup
(`_find_semantic_duplicate`'s lexical exact-match fast path) DOES give safe replay-idempotency for
a repeated write attempt with identical content. The gap is retry-*completeness*, not
duplication-safety: the run reaches `SUCCEEDED` (terminal) in the SAME commit that would need to
carry the memory write, and the existing worker's claiming model (`advance_chat_runs`) only ever
revisits **non-terminal** runs — there is no existing mechanism to durably retry a
best-effort post-commit side effect against an already-terminal run if the process crashes in that
narrow window. Building one would be a genuine worker-claiming-model extension, not a "smallest
adapter" change — exactly the fallback this task's own instructions anticipated ("implement only
secure capture, suppression, durable postprocess result... report memory-write activation as
blocked"). **Reported here as blocked**, not faked.

Streaming safety of the new `"deferred"` category reuses 100% of the proven chunk-boundary-safe
trip/stop machinery already used for execution markers — no new capture-buffer logic was added.
`test_deferred_marker_never_visible_and_split_across_chunks` (marker split across 4 arbitrary
chunk boundaries, never appears in any streamed token or the wire), `test_clean_prefix_before_deferred_marker_still_streamed`
(text before the marker is still delivered, in order).

## Failure/degradation policy (as implemented)

| Class | Examples | Behavior |
|---|---|---|
| Critical (fail closed) | corrupt/unreadable sealed context; finalization CAS failure (pre-existing, unchanged); ownership violation (structurally prevented, not just checked) | Run `FAILED`, bounded reason, **no** Message committed |
| Non-critical (degrade honestly) | no matching documents; no matching memories; RAG/memory retrieval raising an exception | `citations=[]`, `sources_used=False`/`memories_used=False`, generation proceeds normally — matches legacy's own "retrieval failure never breaks the chat" posture |

Retrieval failures never fall back to a paid or external provider (`test_no_direct_executor_or_paid_provider_call_during_submission`).

## Privacy evidence

`test_memory_contents_absent_from_refs_and_logs` and `test_canary_absent_from_refs_events_status_logs`
plant distinct canaries in the RAG passage text, memory content, and the raw user prompt, then
assert: none of `RunStep.input_ref`/`output_ref`/`error_code`, `Run.status_reason`, or captured
logs ever contain them — while the RAG passage snippet (the one thing that's *supposed* to be
user-visible) correctly reaches the authenticated `complete` event's `citations` field, proving the
test isn't just vacuously checking an empty channel. Exception logging in
`_build_context_snapshot` now logs `type(exc).__name__` only, never the raw exception string.

## Exact focused tests

`tests/test_run_engine_mp3c3d2.py` — **40/40 pass** (27 from the initial implementation + 13 added
in the closeout correction pass below). Also re-run for regression (all pass, zero failures):
- `tests/test_run_engine_mp3c3b.py` — **39/39** (full file — touched shared `chat_stream.py`/`worker.py`/`submission.py`/`messages_stream.py`)
- `tests/test_run_engine_mp3c2.py::{test_happy_path_same_run_tool_cycle_one_final_message,test_finalization_failure_leaves_no_duplicate_message,test_dispatch_success_reused_without_reexecution_on_restart}` — **3/3**
- `tests/test_run_engine_mp3c3d1.py` — **16/16** (full file — shares `messages_stream.py`)
- `tests/test_run_engine_submit.py` — **12/12**, `tests/test_run_engine_mp3a.py` — **24/24** (submission-path regression, given `submission.py` changes)

`py_compile` clean on all 4 changed backend files + the new test file. `git diff --check` clean.
`import app.main` — **OK** (a new import was added to `messages_stream.py`).

Not run (per scope): MP-3C3A's 41, the 119-test group, 613-test regression, frontend suite/build
(no frontend files touched), Docker, PostgreSQL, Ollama, ChromaDB, live providers, browser.

## Closeout correction pass (2026-09-11, post-review)

A follow-up review found four remaining gaps in the initial implementation, all fixed in this
pass without redesigning anything — the 121-test regression baseline was preserved throughout
(re-run selectively; see below).

**1. Unversioned context snapshot.** `read_context_snapshot()` silently converted any non-dict
`context` value to `{}` — a present-but-corrupt snapshot would have looked identical to "nothing
was ever retrieved," risking a successful answer with fabricated-empty citations. Fixed:
`chat_stream.CONTEXT_SNAPSHOT_VERSION = 1`; a new authoritative constructor,
`chat_stream.build_context_snapshot(...)`, is the ONE place a versioned snapshot is ever built
(called with no arguments it returns the canonical empty snapshot); `read_context_snapshot()` now
distinguishes three outcomes — genuinely absent (`"context"` key missing or `null`: honest
backward-compat, no error), present-but-malformed-or-wrong-version (raises the new
`chat_stream.ContextSnapshotError`, caught by the existing fail-closed wrapping in
`_finalize_chat_success`), or a valid versioned snapshot (returned as-is). **Both** chat
submission entry points were checked, not just `/messages/stream`: `routes/runs.py`'s direct
Run public API also calls `submission.create_chat_run` (with no `context` argument at all).
`create_chat_run` now normalizes ANY input — `None`, or a well-formed versioned dict — through
`build_context_snapshot`, so both entry points always seal a valid versioned object, never a bare
`None`.

**2. Passage/citation correspondence was not guaranteed.** The original implementation formatted
`rag_context_text` from the full retrieved-chunk list and built `citations` independently,
capping/deduping citations only at validation time — a chunk whose citation failed validation
would still have had its passage text sent to the provider with no corresponding citation. Fixed:
a new `messages_stream._select_cited_chunks` builds citations for the (already `rag_service.TOP_K`
-bounded) retrieved set, keeps only chunks whose citation validates via the new shared
`chat_stream._validate_one_citation`, dedupes by `(document_id, chunk_id)`, and — critically —
`rag_context_text`/`citations` are BOTH re-rendered from that SAME filtered chunk list (so
citation markers stay contiguous and correspond exactly). `build_context_snapshot`'s invariant
enforcement is a second, defensive gate: RAG text is kept only when at least one validated
citation survives; `sources_used`/`memories_used` can never be true without their text actually
present. Finalization's citation re-validation is unchanged and does not alter membership — it
was already correct once construction-time filtering is correct.

**3. Idempotency pre-check silently treated a DB failure as "fresh".** The prior version caught
every exception from `_select_by_idem_scope` and proceeded as if no existing run were found —
meaning a genuine database error would still trigger RAG/memory retrieval and a `create_chat_run`
attempt. Fixed: a `_select_by_idem_scope` failure now raises `DurableStreamError(503, "Durable
streaming is temporarily unavailable.")` immediately — the SAME bounded, generic phrasing
`assert_durable_stream_ready` already uses for internal failures — before any retrieval and before
`create_chat_run` is ever called. Only the exception's class name is logged, never raw text.

**4. Concurrency claim overstated.** The TOCTOU test (`test_toctou_race_simulation_persists_one_run`,
renamed from `test_concurrent_same_key_race_persists_one_run`) is a deterministic, single-threaded
simulation of a race window, not real parallel-thread/process/PostgreSQL concurrency. Its docstring
and this report now say so explicitly. Real concurrent-write verification remains deferred to the
MP-6/final Phase 1B PostgreSQL concurrency gate, unchanged from before.

**New/updated tests:** 13 new focused tests added to `tests/test_run_engine_mp3c3d2.py` (40 total,
up from 27) proving every numbered requirement of the correction pass — version sealing on every
fresh submission (both entry points), fail-closed on unknown-version and present-malformed
snapshots, narrow genuinely-absent backward compatibility, passage/citation membership exactness
(an uncited chunk's text never reaches the provider), duplicate-chunk determinism, the
`build_context_snapshot` truthfulness invariant directly, `Message.citations` matching the sealed
selected set exactly, zero retrieval on a pre-check DB failure (with zero rows persisted and no
raw exception text in logs), zero retrieval on a same-key conflict (409 preserved), and the
TOCTOU simulation. All 40 pass; regression re-run of `tests/test_run_engine_submit.py` (12),
`tests/test_run_engine_mp3a.py` (24), and
`tests/test_run_engine_mp3c3b.py::test_submission_commit_failure_no_partial` (the exact test that
caught the original atomicity regression) — 37 + 1, all green. `py_compile` clean; `git diff
--check` clean; `import app.main` OK (a new module-level import, `chat_stream`, was added to
`messages_stream.py`).

**Files changed this pass:** `chat_stream.py` (versioning, constructor, strict read validation,
`_validate_one_citation` extraction), `submission.py` (context normalization for both entry
points), `messages_stream.py` (passage/citation filtering, fail-closed pre-check, `chat_stream`
import), `worker.py` (comment accuracy only — the existing fail-closed wrapping already covered
the new exception type), `tests/test_run_engine_mp3c3d2.py` (+13 tests, 1 renamed/redocumented).

## Remaining blockers

None for MP-3C3D2's own scope. SAVE_MEMORY/SAVE_DIRECTIVE write-activation remains explicitly
deferred (see above) — a future mini-phase would need a genuine worker-claiming-model extension
for terminal-run postprocessing, which is out of scope for "smallest adapter" work.
Deep-research and multi-agent durable handlers (MP-3C3D4/D5 per the plan) are next.

## Confirmation

- `run_messages_stream_durable_enabled` remains `False`.
- No migration. No config default changed.
- No deep-research, multi-agent, image/vision, or mode-selector UX work performed.
- No hybrid RAG, new vector database, or non-MCP tool execution introduced.
- MP-3C3D3+ were not started.
