# MP-3C3D6C-J9 — worker health, lifecycle, failure boundaries and D6C completion gate — COMPLETE

Date: 2026-09-17
**Status: D6C-J9 COMPLETE — D6C COMPLETE (J1-J9 complete).**

## Numbering reconciliation

| Current D6C label | D6A journey |
|---|---|
| D6C-J6 | D6A J7 - Deep Research |
| D6C-J7 | D6A J8 - Multi-Agent |
| D6C-J8 | D6A J9 - mandatory mobile viewport repeat |
| D6C-J9 | D6A J10 - worker-health gate |

Historical reports were not renamed.

## 1. The authorized lifecycle correction (the one production change)

**Old ambiguous behaviour:** for deep_research and multi_agent Runs the seq-1
load_context RunStep was created pending by the common submission path, and no
component owned or advanced it. Architecture section 43.3 lists that step as
identical to chat's own submission step and specifies transitions only for the
mode-owned steps, so ownership was genuinely undefined. It was safe - every sweep
selects Runs by status first and the DR/MA advanceable sets are {QUEUED, RUNNING},
so a terminal Run is never selected and the row could never be reclaimed - but it
advertised apparently-reclaimable work that nothing owned, and no test pinned it.

**Approved contract:**

    mode "chat"           -> seq-1 load_context = pending  (chat worker OWNS it)
    mode "deep_research"  -> seq-1 load_context = skipped  (compatibility placeholder)
    mode "multi_agent"    -> seq-1 load_context = skipped  (compatibility placeholder)

**Exact production correction**, in the single common submission path
(backend/app/run_engine/submission.py, where the row is created):

* one new import: from app.run_engine.state_machine import STEP_PENDING, STEP_SKIPPED
* the RunStep is created with
  status=(STEP_PENDING if submission.resolved_mode == "chat" else STEP_SKIPPED)
  and finished_at=(None if chat else _utcnow_aware())
* an explanatory source comment records the ownership decision.

Requirements honoured: reuses the already-resolved canonical validated mode
(submission.resolved_mode); adds NO second mode allowlist (a literal "chat"
comparison, matching the existing convention at submission.py:84); duplicates no
readiness logic; the Deep Research and Multi-Agent workers were NOT touched; no
migration was added (RunStep.status is an unconstrained documented string and
skipped is already in state_machine.STEP_TERMINAL); no existing smoke database was
backfilled; the module's existing terminal-timestamp helper (_utcnow_aware) is
used; no fake load_context execution is created; the protected architecture
document was not edited.

## 2. RED -> GREEN

New focused suite tests/test_submission_load_context_lifecycle.py.

RED (before the fix): **2 failed, 7 passed**, failing with
assert 'pending' == 'skipped' for BOTH deep_research and multi_agent - i.e. the
common submission path created the non-chat placeholder as pending.

GREEN (after the fix): **9 passed**.

Lifecycle matrix:

| Assertion | chat | deep_research | multi_agent |
|---|---|---|---|
| seq-1 kind | load_context | load_context | load_context |
| seq-1 status after submission | pending | skipped | skipped |
| finished_at | None | set | set |
| status in STEP_TERMINAL | no | yes | yes |
| owned executable steps | load_context (same row) | research_fetch_extract, deep_research_synthesis | agent_research, agent_solution, agent_review |
| exactly one seq-1 row | yes | yes | yes |
| idempotent replay mutates or duplicates it | no | no | no |
| omitted or default mode behaves as chat | yes | n/a | n/a |

## 3. Fresh service-backed proof (not only a unit test)

One fresh isolated deep_research smoke run: real FastAPI lifespan, real
RunWorkerSupervisor and worker thread, real SQLite, real browser, tripwire armed,
direct backend route.

Run 42f72f16-d6ac-45f0-9ec5-5595dcb4d979, kind deep_research, status **succeeded**.

| RunStep | Status |
|---|---|
| seq 1 load_context | **skipped** (finished_at set) |
| seq 2 research_fetch_extract | succeeded |
| seq 3 deep_research_synthesis | succeeded |

Counts: runs 1, run_steps 3, run_payloads 2, run_events 9, messages 2 (one user, one
assistant), citations [W1] and [W3] unchanged, run_signals 0, pending_approvals 0,
dispatch_records 0, execution_turns 0, tool_calls 0. Search invoked 1 and fetch
invoked 2 - unchanged from the J6 baseline. Tripwire silent, no duplicate rows.
After a reload the conversation auto-hydrated without clicking (answer rendered,
counter "2 messages", citations preserved, one user bubble). Console: Errors 0, zero
syntax errors, zero manifest errors.

## 4. Worker / supervisor / health contract and the D6A J10 gate

D6A J10 acceptance text: GET /api/health/worker returns HTTP 200 with
durable_path_ready true while the smoke backend is running.

Observed directly, before and after the Run:

    STATUS 200
    {"durable_run":{"enabled":true,"started":true,"alive":true,"stopping":false,
     "last_successful_poll_at":"...","last_bounded_error_code":null,
     "consecutive_failure_count":0},"durable_path_ready":true}

No prompts, tool names/args/results, provider errors, API keys, user/session/run
ids, database URLs or filesystem paths are exposed.

Contract anchors:

* lifespan creates RunWorkerSupervisor() ONLY if one is not already pre-set (the
  smoke injection seam) and then awaits start() exactly once (main.py:1324-1326);
  shutdown takes the supervisor off app.state and clears it (main.py:1341-1344).
* the supervisor owns ONE threading.Thread(daemon=True) with an is_alive() handle
  and a bounded non-blocking join on stop; it is never force-killed.
* GET /api/health/worker reports {durable_run, durable_path_ready} and returns
  HTTP 503 when the durable path is not ready (main.py:1479-1503); with no
  supervisor it reports started/alive false and derives readiness from whether the
  public durable API is enabled.
* submission is refused before persistence when the durable path is not ready
  (messages_stream.assert_durable_stream_ready).
* a waiting-for-approval Run has dropped its lease and is not treated as a dead
  worker; health remains truthful.

## 5. Failure-boundary matrix (F1-F9)

Proven by the existing focused suites, all green in this pass (section 7), plus the
live evidence above. No hermetic result is presented as live-service data.

| ID | Boundary | Invariant proven |
|---|---|---|
| F1 | provider failure before completion | provider invoked at most once; Run never reports success; no assistant Message; no success complete event; bounded generic error reason; no raw exception, prompt or secret leaked |
| F2 | commit/finalization failure | commit failure never reports success; no complete event before durable success; Message, citations and terminal state follow the documented atomic boundary; replay does not duplicate the assistant Message |
| F3 | expired lease with safe pending work | only work durably known not to have invoked a provider is reclaimable; reclaim occurs once; no duplicate provider invocation; one terminal result |
| F4 | expired lease with provider-started work | provider is not invoked again; state becomes or remains outcome_unknown; no fabricated assistant Message; no success completion |
| F5 | cancellation | one cancel_requested signal; disconnect does not cancel; provider/generator closes exactly once; no assistant success Message; repeated cancel idempotent |
| F6 | approval waiting | worker drops its lease while waiting; a waiting Run is not treated as a dead worker; deny executes zero tools; approve resumes the same Run; health stays truthful |
| F7 | unsupported/reserved mode ownership | generic workers never claim unsupported or reserved modes and never convert a reserved queued Run into a false failure |
| F8 | worker unavailable/not ready | submission is refused before persistence while health truthfully reports not-ready (the documented policy; unchanged in J9) |
| F9 | notifier limitation | notifier wake-ups are process-local; worker polling provides eventual progress in the supported self-host single-process topology. Shared-store/multi-process convergence remains an MP-4/MP-6 hosted residual. |

## 6. Health-truthfulness matrix

The endpoint reports not-ready (HTTP 503) when the durable path cannot execute, and
ready (HTTP 200) only when the real worker is alive and able to claim work. Cases
covered by code and by the focused health/readiness suites:

| Condition | Reported |
|---|---|
| worker flag disabled | not ready |
| supervisor absent | started false, alive false; readiness derived from whether the public durable API is enabled |
| supervisor not started | not ready |
| worker thread dead | not ready |
| stopping or stopped | not ready |
| initialization failed | not ready (bounded last_bounded_error_code, no raw exception text) |
| real worker alive and claiming | **ready (observed HTTP 200, durable_path_ready true)** |

The endpoint was exercised live and returned 200 with durable_path_ready true both
before and after the Run, i.e. health remained truthful across the Run.

## 7. Verification

| Command | Result |
|---|---|
| focused backend set (smoke, new lifecycle suite, mp3a, mp3b, mp3c1, mp3c2, cancellation, recovery, atomicity, worker, deep-research worker, multi-agent worker / submission / orchestration / artifacts / readiness, research activation boundary, approvals api, submit) | **502 passed, 0 failed** |
| tests/test_submission_load_context_lifecycle.py (new) | 9 passed (RED was 2 failed / 7 passed) |
| npx tsc --noEmit -p tsconfig.json | exit 0 |
| npm run build | exit 0 |
| py_compile (changed python) | exit 0 |
| import app.main | IMPORT_OK |
| git diff --check | clean |

## 8. Cancelled provider-started running step - classification

Kept intentionally unchanged. running records durable evidence that provider
invocation began; RunStep.status has no cancelled member; the worker documents that
expired-lease reclaim happens only where durable state proves provider invocation
had not begun, otherwise outcome_unknown. The step is therefore not reclaimable as
safe pending work, the provider is never re-invoked, the Run stays terminal
cancelled, no late assistant success Message is produced, and no active-run
capacity is leaked. **Classified as intentional at-most-once evidence.**

## 9. Residual-risk register (preserved, none erased)

1. Vite development-proxy concurrent-SSE defect - OPEN, not fixed, not claimed fixed.
2. One historical intermittent mobile resume - root cause not established; not reproduced in two traced runs; no product defect claimed.
3. Long unbroken content is clipped internally though the page does not widen.
4. Mobile touch targets about 133x30 (mode) and 40x40 (send) remain below about 44x44.
5. Process-local notifier / shared-store limitation (MP-4/MP-6 hosted concern).
6. No PostgreSQL concurrency matrix yet.
7. No hosted multi-process verification yet.
8. DNS rebinding / TOCTOU residual risk in the fetch guard.
9. Fake providers do not prove live provider or network behaviour.
10. Intentionally non-terminal step states: chat load_context is worker-owned and reaches succeeded; a cancelled provider-started step remains running (durable at-most-once evidence). The former non-chat pending placeholder is now terminal skipped and is no longer a residual.

**Documentation-convergence item for MP-4/MP-6:** architecture section 43.3 should
state the seq-1 load_context ownership rule explicitly (chat-owned versus non-chat
compatibility placeholder created skipped). The protected architecture document was
deliberately NOT edited in J9.

## 10. Cleanup

After the run: 0 smoke processes, 0 waiting orchestrators, 0 bound ports,
0 thunder_d6* temp roots, 0 pending tasks, no repo-local binary artifacts and no
.playwright-mcp directory inside the repository. Evidence archived outside the
repository at C:/deepseek-harness/d6c-j9.

Repository state: branch phase-1b1-approval-domain, HEAD
b2e67183d4791cbbaed9d3d98e8833983510f3ff, 0 staged, stash@{0} untouched,
git diff --check clean. No commit, stage, merge, push, reset, rebase, stash, clean,
discard, branch switch or worktree operation was performed. The protected
architecture document is unmodified at 3332 lines / 268044 bytes /
f4d0bbc24f20529addb32ac10bf12e197c7ad747. A fresh Settings() instance still reports
all seven durable production defaults as False.

## 11. D6C completion

All D6C-J9 gates pass: worker health truthful; real supervisor/lifespan startup;
healthy Run; clean shutdown; F1-F9 boundaries; load_context lifecycle explicitly
classified and corrected; focused tests/typecheck/build green; teardown
independently clean; all seven defaults False.

**D6C is complete (J1-J9).** MP-4, MP-5 and MP-6 are NOT started.
