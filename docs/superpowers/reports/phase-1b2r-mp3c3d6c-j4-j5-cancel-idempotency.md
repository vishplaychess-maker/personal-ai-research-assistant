# MP-3C3D6C-J4/J5 — durable cancellation and semantic idempotency — COMPLETE

Date: 2026-09-17  
**Status: D6C-J4 and D6C-J5 COMPLETE — D6C IN PROGRESS (J1–J5 complete; J6–J9 not started).**

Two production defects were proven, authorised, fixed and closed with RED → GREEN
evidence, then re-proven end to end in a real browser against a fresh database.

## Fidelity statement (read this first)

* The LLM is the deterministic in-process test provider injected through the existing
  chat_provider seam (a cancellable fixture for J4, the D6B provider for J5). No paid
  or remote model was called; the outbound-network tripwire stayed silent.
* Everything else is real: real FastAPI lifespan, real supervisor/worker, real SQLite,
  real run engine and signal handling, real approval/idempotency services, real React UI
  in real Chromium, real auth (HttpOnly refresh cookie, Bearer access token,
  double-submit CSRF).
* The Vite dev-proxy concurrency defect (D6B-R5) is **still OPEN** and is **not** fixed
  here. The smoke routes the browser straight to the backend; that is a harness
  workaround only.

## Defect 1 — frontend initial-session hydration (PROVEN, FIXED)

**Boundary.** After a reload the DB held the user Message and
GET /api/sessions/1/messages returned 200 with it, but the conversation pane reported
"0 messages" and rendered no bubble until the session row was clicked.

**Root cause.** The mount effect in App.tsx selected the most recent session

    if (activeSessionId === null && sessions.length > 0 && isAuthenticated) {
      setActiveSessionId(sessions[0].id);      // selected, never hydrated
    }

while the manual path does setActiveSessionId(id) **and** loadMessages(id). Selection
without hydration is invisible whenever the message list is empty.

**Fix (frontend/src/App.tsx, minimal).**

1. The mount effect now hydrates the session it selects, through the SAME loadMessages
   path the manual click uses; the branch still only runs while activeSessionId === null,
   so an already-active session is never replaced.
2. hydratedSessionRef records the session already hydrated, so StrictMode's
   double-invoked mount effect cannot issue a second identical request.
3. messagesRequestRef adds newest-wins ordering inside loadMessages (mirroring the
   existing sessionListRequestRef idiom in loadSessions), so a slow initial hydration can
   never overwrite a newer manual selection.
4. Both refs are reset on the auth change effect, so a logout discards an in-flight load
   and a later login hydrates again.

**RED → GREEN.** frontend/src/__tests__/sessionHydration.test.tsx started as an
it.fails regression and is now 11 normal passing tests covering: hydrate and render with
no click; exactly one request under StrictMode; zero sessions → zero message request; an
already-active session is not replaced or re-hydrated; the manual click path still loads;
a late hydration response cannot overwrite a newer selection; an in-flight load is
discarded after logout. The extraction helper carries a control test because a naive
+/-500 character window silently swallowed handleSelectSession's own loadMessages call
and made the defect assertion pass spuriously.

## Defect 2 — durable text time-flush during a provider stall (PROVEN, FIXED)

**Boundary.** text_events.py documents TextEventBuffer as flushing when the buffer reaches
run_event_text_flush_bytes OR run_event_text_flush_ms elapse, and states tick() is the
time-based flush to call periodically from the producer's loop.

**Cadence analysis (measured, not assumed).**

| Quantity | Value |
|---|---|
| run_event_text_flush_ms | 100 ms |
| run_event_text_flush_bytes | 4096 bytes |
| run_worker_lease_ttl_s | 60 s |
| poll_interval passed to run_stream | max(1, 60 // 3) = **20 s** |
| where buf.tick() was called | **only inside on_token** |
| what the periodic callback _on_idle did | deadline + lease/cancel/session guards, **no tick** |

The gap between the documented bound (100 ms) and the only periodic hook (20 s) is 200x.
Raising the idle cadence was rejected: _on_idle performs DB-backed lease, cancellation and
session-binding checks, so calling it at the tick rate would be exactly the 10 Hz DB
polling loop the correction forbids. The two cadences were therefore separated.

**Fix.**

* chat_stream._adrive / run_stream take an optional on_tick / tick_interval hook. When
  present the wait timeout becomes min(poll_interval, tick_interval) and on_tick runs on
  each wake-up, while on_idle still runs at most once per poll_interval, measured on a
  monotonic clock — so the guards keep their existing rhythm and fail-closed behaviour
  and are never promoted to the tick rate.
* TextEventBuffer gains a flush_interval_s property (public view of flush_ms).
* worker.py passes on_tick=buf.tick, tick_interval=buf.flush_interval_s at BOTH
  run_stream call sites that own a TextEventBuffer (the chat driver and the second
  buffer-backed driver). The third run_stream call site has no buffer and is unchanged.
* When the buffer is empty, tick() returns immediately, so the extra wake-ups are an
  in-memory check, not a database round trip.

**RED → GREEN.** tests/smoke/test_text_flush_on_idle_regression.py began as a strict
xfail AST test and is now 7 normal passing behavioural tests: sub-threshold text becomes
durable during a stall within a measured bound; no empty text event; repeated ticks and
close after a flush do not duplicate text; UTF-8 content reconstructs exactly; the guard
cadence stays independent of the tick rate; no pending asyncio task survives; and both
buffer drivers are wired. The core test deliberately passes poll_interval = 20.0 s (the
production guard cadence) so ONLY the lightweight tick can satisfy the asserted bound.

**End-to-end before/after.** The browser fixture's prefix is deliberately smaller than
the 4096-byte size threshold, so only the time flush can ever write it:

| | Pre-fix (archived) | Post-fix |
|---|---|---|
| text RunEvents after a stall | **0** (3 control events only) | **1** |
| text rendered in the browser | none ("Generating…" only) | **817 chars** |

Post-fix timing: last provider chunk at 1789653907.2536, the text RunEvent persisted at
2026-09-17 14:05:07.356303 — **0.102 s later**, honouring flush_ms = 100 ms while the
provider was stalled.

## RunStep observation — read-only contract check, NO CHANGE

The cancelled Run reached terminal cancelled while run_steps[1] stayed running.

* The documented step vocabulary (models.py:1008) is
  pending|running|succeeded|failed|blocked|waiting|skipped|incomplete — there is
  **no cancelled step status** to move it to.
* advance_chat_runs (worker.py:1156-1158) documents that an expired-lease run is
  reclaimed ONLY "where durable state proves provider invocation / dispatch had not
  begun — otherwise outcome_unknown with NO second side effect", and
  tests/test_run_engine_mp3b.py::test_crash_after_invocation_is_outcome_unknown_no_retry
  pins that at-most-once behaviour.

**Conclusion: a lingering running step is intentional and load-bearing.** It is the
durable proof that provider invocation began, which forbids a retry. Clearing or closing
it on cancellation would destroy that evidence. No change was made, and no RunStep fix was
bundled into this correction.

## J4 — durable cancellation, fresh browser rerun (PASS)

Root C:\\Users\\user\\AppData\\Local\\Temp\\thunder_d6b_78cg0owc (archived to
C:\\deepseek-harness\\d6c-j4-postfix-evidence).

| Requirement | Evidence |
|---|---|
| partial text visibly rendered before Cancel | 817 chars, Stop control visible |
| elapsed from last provider chunk to persisted text | **0.102 s** |
| real Stop clicked exactly once | yes (App.tsx routes Stop to the durable cancel API) |
| one cancel_requested signal | run_signals = 1 |
| signal consumed once | consumed_at set |
| running → cancelling → cancelled | run.status events seq 6 and 7 |
| provider cancelled/closed exactly once | fixture cancelled:1 closed:1 |
| provider never completes | fixture completed:0; CANCELSMOKECOMPLETED never rendered |
| no assistant Message | messages = 1 (user only) |
| partial text events remain valid | text RunEvent seq 4 |
| repeated cancel = idempotent no-op | 200 already_terminal:true, cancellation_requested:false, twice |
| no new rows from repeated cancel | counts identical before/after |
| no approval/tool/dispatch rows | 0 / 0 / 0 |
| browser disconnect did not cancel | cancel came only from the explicit API call |

Post-reload acceptance (the Correction 1 proof):

| Layer | Result |
|---|---|
| DB messages | **1** user Message |
| GET /api/sessions/1/messages | **200, count 1** |
| frontend auto-selects the session | yes |
| frontend auto-requests its messages | **yes — GET /api/sessions/1/messages => 200 with no click** |
| user bubble renders | **yes** |
| UI message count | **1 message** (was "0 messages" before the fix) |
| cancelled status truthful | yes |
| no assistant success bubble | yes |

## J4 cancellation security

Auth and CSRF are required on the cancel route (require_csrf; 403 for missing/mismatched
tokens, observed during J3). Cross-user cancellation is covered by production tests —
tests/test_run_engine_cancellation.py asserts a different user (user_id=999), a different
session family and an invalid binding all fail closed with a non-disclosing RunNotFound —
and those tests pass in this correction's focused run. No additional browser adversarial
testing was performed.

## J5 — semantic idempotent replay and conflict (PASS)

Root C:\\Users\\user\\AppData\\Local\\Temp\\thunder_d6b_ofkrvies (archived to
C:\\deepseek-harness\\d6c-j5-evidence). Fixed key J5-FIXED-KEY-1, session 1, user
d6b_smoke_user (id 2).

| Case | Request | HTTP | Result |
|---|---|---|---|
| 1 initial | POST /api/runs, key K, prompt P | 201 | run 1a1cf082-4b0d-48ab-b1f5-a5e5695338b7, succeeded, created=true |
| 2 identical replay | same user/session/key/prompt/model/mode/provider | 201 | **same run id**, created=**false**, status succeeded |
| 3 semantic conflict | same key, prompt changed to a different one | **409** | bounded 62-byte body, no prompt and no key echoed, zero partial rows, original run unchanged |
| 4 isolation | genuine second user (id 3) with the same key and session | **404** | no run-id disclosure; GET run => 404; POST cancel => 404 |

Replay created **zero** new Run, Message, RunStep, RunPayload or RunEvent, and invoked the
provider **zero** additional times (provider_calls.jsonl stayed at one selected and one
invoked across all four cases).

## Exact database counts

J4 (after cancellation, repeated cancels and reload):

| Table | Count |
|---|---|
| runs | 1 (cancelled) |
| run_steps | 1 (load_context, running — see RunStep section) |
| run_payloads | 1 |
| run_events | 7 (3 control before generation, 1 text, run.cancel.requested, 2 status) |
| run_signals | 1 (cancel_requested, consumed) |
| messages | 1 (user only) |
| execution_turns | 0 |
| tool_calls | 0 |
| pending_approvals | 0 |
| dispatch_records | 0 |
| provider invocations | 1 (14 chunks, 1 stall, cancelled 1, closed 1, completed 0) |

J5 (identical after initial submission, identical replay, conflict and isolation):

| Table | After case 1 | After case 2 | After case 3 | After case 4 |
|---|---|---|---|---|
| runs | 1 | 1 | 1 | 1 |
| run_steps | 1 | 1 | 1 | 1 |
| run_payloads | 1 | 1 | 1 | 1 |
| run_events | 9 | 9 | 9 | 9 |
| run_signals | 0 | 0 | 0 | 0 |
| messages | 2 | 2 | 2 | 2 |
| execution_turns | 0 | 0 | 0 | 0 |
| tool_calls | 0 | 0 | 0 | 0 |
| pending_approvals | 0 | 0 | 0 | 0 |
| dispatch_records | 0 | 0 | 0 | 0 |
| provider invocations | 1 | **1** | **1** | **1** |

## Focused verification (this correction)

| Suite | Result |
|---|---|
| tests/smoke + mp3a + mp3b + cancellation + mp3c1 + mp3c2 + recovery + submit | **172 passed** |
| sessionHydration, ChatArea, useStreaming, durableStreaming, useStreamingStrictMode, useStreamingSafety, ApprovalDialog, streamSubmissionCount | **101 passed (8 files)** |
| tests/smoke/test_text_flush_on_idle_regression.py | 7 passed |
| python -m py_compile (changed files) | exit 0 |
| npx tsc --noEmit | exit 0 |
| git diff --check (worktree and index) | clean |

## Honest limitations and open items

1. **One console error, unattributed.** The current page reports one error,
   "Unexpected token ')'", with no source URL; it repeats in the console buffer. It cannot
   originate from these corrections (both are TypeScript-validated, tsc --noEmit clean,
   and unit-tested) and the D6B catalogue of pre-existing console errors does not list it.
   It is reported, not filtered, in the same spirit as D6B section 5. Open for J6–J9.
2. **App.test.tsx has pre-existing failures that these corrections did not cause.**
   The file rendered App without ThemeProvider, which main.tsx always mounts, so every
   authenticated render died before any assertion (Sidebar calls useTheme). That harness
   gap is repaired here and is documented in ModePropagation.test.tsx as pre-existing.
   Five remaining failures assert UI copy that no longer exists anywhere in production
   source ("Research Sessions", "Select or create a session", "✚ New" — zero occurrences
   outside test files). They are stale, unrelated to this work and were left alone. The
   404 test still asserts an error string that cannot render when messages is empty
   (the 404 path clears messages and ChatArea gates the error block on hasMessages); only
   the assertion this correction invalidated was updated, by keying the stub to the
   session id instead of call order.
3. **A J5 CASE 4 false alarm, reported deliberately.** The first isolation attempt used
   the token from POST /api/auth/refresh after POST /api/auth/register; register created
   user 3 but did NOT replace the existing refresh cookie, so the token still belonged to
   user 1 and the calls read and cancelled user 1's own Run. That looked like a critical
   cross-user violation. GET /api/auth/me proved the token was user 1's, and the case was
   redone with a real login for user 3, which returns non-disclosing 404s. **No cross-user
   access exists**; the alarming intermediate result was the harness's fault, not the
   product's.
4. **I cannot view images.** The running model declares no image input, so the screenshots
   below exist but were NOT visually inspected by me. The DOM probes and network captures
   are the evidence.
5. Screenshots taken (outside the repository):
   C:\\deepseek-harness\\d6c-j4-postreload-hydrated.png. The partial-text and cancelled
   states were captured as DOM probes rather than images during the live window.
6. Deterministic fake provider, no real MCP server and no real model. The Vite dev-proxy
   concurrency defect remains OPEN. J6–J9 and MP-4–MP-6 are not started.

## Cleanup

After each scenario: 0 smoke descendants, 0 waiting orchestrators, 0 bound ports, 0
thunder_d6b_*/thunder_d6c_* temp roots, 0 pending fixture tasks, and no binary artifact
added to the repository. Each scenario's smoke.db, logs, manifest and provider JSONL were
archived outside the repository before its temp root was deleted.

Repository state is unchanged by this pass: branch phase-1b1-approval-domain, HEAD
b2e67183d4791cbbaed9d3d98e8833983510f3ff, 0 staged, stash@{0} untouched. No commit, merge,
push, stage, reset, rebase, stash, clean, discard, branch switch or worktree operation was
performed.
