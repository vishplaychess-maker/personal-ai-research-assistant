# MP-3C3D5C — durable multi-agent execution

Date: 2026-09-17

## Delivered behavior

Durable multi-agent runs execute the fixed Researcher → Solution Specialist → Reviewer sequence. Each role makes one attempt using the sealed provider/model and consumes validated, sealed prior artifacts. Intermediate output stays private. Only the validated reviewer response becomes the final assistant message, with citations derived from the sealed local context.

The dedicated worker sweep is connected to `poll_once`. Completed role artifacts are reused on recovery; an expired lease with an in-flight role becomes `outcome_unknown` rather than causing another provider call. Cancellation, session binding, lease/version checks, timeouts, and terminal transitions reuse the durable engine primitives.

`multi_agent_executable()` now has its code-owned implementation boundary enabled. Both public submission routes, settings capability, and worker use this predicate. All five deployment prerequisites and the server HMAC key remain required. **Deployment flags remain off by default; this change does not enable durable cutover on a running deployment.**

## Interruption-point corrections

- The stale frontend test no longer expects `agent_some_future_code` to reach `onAgentProgress`. Dispatch accepts only the six approved codes and ignores unknown codes, malformed values, non-object JSON, and duplicate event IDs.
- Production `App.handleSend` supplies `onAgentProgress`. `ChatArea` renders a bounded friendly label in an accessible status region. Start, completion, error, and cancellation clear the transient progress state. This completes the explicitly requested production UI path and supersedes D5A's hook-only UI deferral.
- Active-run admission moved into shared `create_chat_run`, behind a database write lock on the owning user row and held through the submission transaction. SQLite serializes writers; no process-local counter or unlocked count-then-insert remains in either route. Scoped replay and identity conflict retain their behavior at the limit. Rejection returns bounded HTTP 429 with no new run/message/step/payload rows.
- The worker calls `MarkerScanner.finish()` before accepting each role's output and rejects a held-back possible marker tail. Final artifact content is assembled only from scanner-approved text, without falling back to the unchecked provider response. Tests cover tails in all three roles, including a response consisting entirely of a marker prefix.

## Wire contract

Persisted `agent.progress` maps to SSE `agent_progress` with only `{code}`. The ordered codes are:

1. `agent_researching`
2. `agent_research_complete`
3. `agent_solving`
4. `agent_solution_complete`
5. `agent_reviewing`
6. `agent_finalizing`

These are D5B's approved Solution Specialist codes, superseding the older D5A `agent_coding` wording. Raw artifacts, prompts, secrets, citations, and exception text are not progress payloads.

## Validation

- Focused backend suite: 334 passed across multi-agent orchestration, artifact, readiness, submission and worker tests; settings capabilities; D3A mode contracts; MP-3A submission/SSE; shared submission; deep-research readiness; and marker-scanner closeout.
- Focused frontend suite: 91 passed across durable streaming, production App mode/progress integration, ChatArea, mode selector, approval dialog, and SSE parser.
- `npm run build`: TypeScript and Vite/PWA production build passed. Existing large-chunk and tooling deprecation warnings remain.
- Concurrency test uses two independent connections to file-backed SQLite at a cap of one: exactly one new submission succeeds, one is rejected, and replay remains accepted.
- Passing closeout checks cover the messages-stream submission adapter, rollback without consuming capacity, and persisted-progress-to-SSE mapping. `git diff --check` passed.

The first broad backend attempt was blocked by Windows temporary-directory permissions and tests using the configured database. The successful rerun used a disposable SQLite database and approved execution outside the sandbox. No live LLM was used by the focused worker tests; the repository's global pytest fixture still performs its existing best-effort local Ollama warmup.

## Boundaries

No migration, dependency, revision loop, fourth/router model call, live web acquisition, tool execution, or memory/directive write was added. PostgreSQL admission behavior was not integration-tested; deployment remains subject to the existing PostgreSQL/notifier gates. Final text-event publication and the final Message/run commit remain **separate transactions** — no atomicity across them is claimed. As of the closeout below, however, the crash window between them is closed by **exact-prefix continuation**, so a crash after any subset of the committed text chunks loses no text and duplicates none. MP-4 through MP-6 and deployment cutover remain deferred.

## D5C closeout verification (2026-09-17)

Two high-risk boundaries were re-verified against the submitted code rather than against this report. One real defect was found and fixed; the other held.

### A1 — serialized admission: PASS, no change

The required critical section is covered by ONE database write lock, not a process-local counter, and both public submission routes reach it through the same shared function:

- `backend/app/run_engine/submission.py::create_chat_run` acquires the lock (`db.execute(update(User).where(User.id == ctx.user_id).values(id=User.id))`) **before** admission and holds it until the submission commits.
- Inside that window: idempotency lookup + active-run count (`enforce_multi_agent_active_run_cap`, `submission.py:429-473`), `submit_run(commit=False)` (`submission.py:331-338`), and the commit that lands Message + payload + step + `run.queued` + the `created→queued` transition (`transition_run`, `submission.py:421-422`).
- Both routes call it: `backend/app/routes/runs.py:141` (`POST /api/runs`) and `backend/app/routes/messages.py:608` → `messages_stream.resolve_and_submit` → `submission.py:347` (`POST /messages/stream`). No route performs its own unlocked count-then-create; `enforce_multi_agent_active_run_cap` has exactly one caller.
- Evidence: `tests/test_multi_agent_submission.py::test_concurrent_admission_serializes_through_commit` uses two independent connections to a file-backed SQLite database at a cap of one — exactly one submission is created, the other is rejected, replay of the winner still returns `created=False`, and a changed-semantics replay still raises `IdempotencyKeyConflict`. Rollback-without-consuming-capacity, replay-at-cap, and 429-at-cap for the direct Run API are covered by the neighbouring tests in the same file.

**This is a single-process-verified guarantee.** SQLite serializes writers; on PostgreSQL the guarantee should follow from the per-user row lock, but that has **not** been integration-tested and no hosted/multi-process safety is claimed here.

### A2 — final Reviewer text recovery: DEFECT FOUND AND FIXED

The previous implementation used exactly the unsafe pattern:

```python
already_published = db.query(RunEvent.id).filter(
    RunEvent.run_id == claimed.run_id, RunEvent.category == "text",
).first() is not None
if not already_published:
    buf = TextEventBuffer(...); buf.publish(final_text); buf.close()
```

`text_events.TextEventBuffer._flush()` commits **each** capped chunk in its **own** session (`text_events.py:149-161`). A crash between two chunks therefore leaves a committed **partial prefix** — and the boolean guard then skipped publication entirely, permanently dropping the unpublished suffix while the run still finalized as `succeeded` with a full-content `Message`. Re-publishing from zero would have duplicated the prefix instead.

Fix — exact-prefix continuation, reusing existing primitives:

- New `backend/app/run_engine/text_events.py::read_persisted_text` reads back the committed `text` events for the run in `run_seq` order, and raises `ValueError` on a malformed payload so the caller fails closed.
- `RunWorker._finalize_multi_agent_success` (`backend/app/run_engine/worker.py`) now reads that prefix and publishes **only the exact missing suffix**. This is sound because this function is a `multi_agent` run's only text producer (no role output ever reaches a text event) and the validated Reviewer artifact is immutable, so the persisted text is always an exact prefix. Persisted text that is **not** a prefix fails closed (`text_recovery_mismatch`, no Message); an unreadable payload fails closed (`text_recovery_unreadable`).
- The lease/version CAS remains the sole "who finalizes" authority, the `output_ref.message_id` check remains the sole duplicate-Message guard, and no provider is invoked on this path — so a second Message, a second Reviewer provider call, and unvalidated output visibility all remain impossible.

Tests added to `tests/test_multi_agent_worker.py`:

- `test_partial_final_text_publish_recovers_exact_suffix_without_gap_or_duplicate` — drives a real three-role run, interrupts the **second** chunk write so a partial prefix is genuinely committed, then requires recovery to produce byte-identical replayed text, exactly one `Message` carrying the full validated text, and zero further provider calls. **Observed failing before the fix**: the run finalized `succeeded` but the replayed text was a strict prefix of the 12000-character validated artifact — the suffix was gone.
- `test_unexpected_persisted_text_fails_closed_without_a_message` — injects persisted text that is not a prefix and requires `failed` / `text_recovery_mismatch` with no `Message` and nothing further published. This one was **added with** the fix rather than proven against the old code (the old code had no such branch); it locks the new fail-closed behavior.

### Stale test reconciled (D5C drift)

`tests/test_run_engine_mp3c3b.py::test_unsupported_modes_fail_before_persistence` still asserted the pre-D5C generic slug `mode_unsupported_multi_agent`, but D5C gave `multi_agent` the dedicated config-and-implementation-dependent reason `multi_agent_not_executable` (`messages_stream.py:133-136` → `multi_agent_readiness.multi_agent_unavailable_reason`), mirroring deep research. The expectation was updated to the superseding code — the assertion is equally exact, not weakened. This drift went unnoticed because MP-3C3B was **not** part of the "334 passed" run quoted above; that figure covered the D4/D5-oriented set.

### Closeout test results (fresh)

- `tests/test_multi_agent_worker.py` — **37 passed** (35 pre-existing + 2 new).
- `tests/test_multi_agent_submission.py`, `test_run_engine_mp3a.py`, `test_run_engine_events.py`, `test_multi_agent_orchestration.py`, `test_multi_agent_artifacts.py`, `test_multi_agent_readiness.py` — **223 passed**.
- `tests/test_run_engine_mp3c3b.py`, `test_multi_agent_readiness.py` — **94 passed** (after the stale-expectation reconciliation).
- `tests/test_run_engine_mp3c1.py`, `test_run_engine_mp3c3b.py`, `test_deep_research_worker.py`, `test_run_engine_mp3c1_closeout.py` — 98 passed, **1 failed** pre-reconciliation (the stale MP-3C3B expectation above), 0 after.
- `py_compile` on every changed Python file and `git diff --check` — clean. All four deployment flags verified `False` from a fresh `Settings()`. Broad regression, PostgreSQL, Docker, live services, and the production frontend build were **not** re-run.
