# Phase 1B.2R MP-4B — call-graph convergence: durable route never invokes legacy streaming helpers — COMPLETE

Date: 2026-09-17. Scope: **MP-4B only**. MP-4C, MP-4D, MP-5 and MP-6 are **NOT STARTED**.
This report exists because every required MP-4B gate was executed and produced evidence.

## 1. What MP-4B was

One narrowly-scoped convergence step: the durable run engine must not depend on the
legacy streaming module for SSE serialization. The canonical serializer was moved into
the run engine and the legacy module now re-exports it, so the dependency arrow points
legacy -> durable and never the reverse.

## 2. Files changed by MP-4B

**The initial MP-4B implementation changed FOUR files, not three. An earlier draft heading
said "Files changed (3)" while listing four; the correct count is four.** They are:

| # | File | Change |
|---|---|---|
| 1 | `backend/app/run_engine/sse.py` | canonical `format_sse` defined here (added after `_TERMINAL_TO_STATUSES`); byte-identical body `return f"event: {event}\ndata: {json.dumps(data)}\n\n"` |
| 2 | `backend/app/run_engine/messages_stream.py` | line 48 import switched from `app.services.streaming_service` to `app.run_engine.sse` — this was the ONLY `run_engine -> streaming_service` import in the codebase |
| 3 | `backend/app/services/streaming_service.py` | local `def format_sse` replaced by a true re-export `from app.run_engine.sse import format_sse  # noqa: E402,F401` |
| 4 | `tests/test_sse_serializer_parity.py` | NEW — 7 executable parity/purity tests (RED 7 failed -> GREEN 7 passed) |

This closeout added **one** further evidence file (not an implementation change):

| 5 | `tests/test_mp4b_durable_route_sentinel.py` | NEW — 15 executable route-level sentinel tests proving zero legacy invocation |

## 3. Gate 1 — executable sentinel call-count tests (PASS)

Command: `.venv/Scripts/python.exe -m pytest tests/test_sse_serializer_parity.py tests/test_mp4b_durable_route_sentinel.py -q`
Result: **22 passed, 0 failed** (7 serializer-parity + 15 sentinel route), 54.79s.

Design: the durable route was exercised through the **real FastAPI app**, a **real TestClient**,
**real register/login auth**, **real CSRF**, and **real SQLite persistence**. Every legacy helper
was replaced **at its actual route lookup site** — `app.routes.messages.*`, where the names are
bound at import time by `from app.services.streaming_service import (...)` — by a loud counting
sentinel that increments a counter **and raises** `LegacyInvoked`. Patching only the source module
would have been ineffective and was deliberately avoided.

What the 15 sentinel tests prove:

* **Zero legacy invocations for all four mode inputs** — omitted, explicit `chat`, `deep_research`, `multi_agent` — asserting the counter is empty after each.
* **Exactly one canonical submission per request** — `resolve_and_submit` is wrapped by a **call-through spy** (not a replacement), asserting it ran exactly once with the correct `session_id`/`message`, and that an omitted mode arrives as `None` while an explicit `"chat"` arrives as `"chat"` (the two are canonically distinct).
* **Correct durable identity and rows** — exactly one Run with kind `chat_stream` / `deep_research` / `multi_agent`, exactly one user Message, exactly one RunPayload, exactly one `load_context` RunStep with status `pending` (chat) or `skipped` (non-chat, the D6C-J9 contract).
* **Fail-before-persistence** — unsupported mode (422), unsupported image, deep-research-not-ready (422), multi-agent-not-ready (422), unready supervisor (503): in every case no Run, no Message, no `X-Run-Id`, and still zero legacy calls.
* **Idempotency** — identical replay returns the **same** Run with no duplicate Run/Message/Payload; changed semantics on the same key yields **409** with no second Run.
* **Negative control** — with the cutover flag OFF the legacy branch **is** taken and the counter **must** trip; it does (`LegacyInvoked` raised from `routes/messages.py:384`, `prepare_chat_context`), proving the sentinels are genuinely armed and that the suite cannot pass for the wrong reason.

Honest bound on this harness: at lifespan time the durable flags are OFF so **no real worker
thread starts and no live provider is ever contacted**; at request time `app.state.run_worker_supervisor`
is a **stub** reporting the durable path ready. That stubs a deployment *liveness* signal only —
the route decision, mode validation, canonical submission, idempotency and every DB write are
the real implementations.

## 4. Gate 2 — three fresh isolated real-browser journeys (RUN; see caveats)

Each journey used a fresh temp root, a real backend on the real lifespan, the real production
Vite frontend, a real Chromium controller, and a deterministic offline fake provider.

| Journey | UI mode selected | raw request body captured | Run kind | Run status | Rendered answer |
|---|---|---|---|---|---|
| Chat | Chat (explicit) | `{"message":"...","mode":"chat"}` | `chat_stream` | succeeded | 8597 chars, **exact match** to expected |
| Deep Research | Deep Research | `{"message":"...","mode":"deep_research"}` | `deep_research` | succeeded | 146 chars `D6CJ6ANSWER ... [W1] ... [W3]` |
| Multi-Agent | Multi-Agent | `{"message":"...","mode":"multi_agent"}` | `multi_agent` | succeeded | 224 chars `D6CJ7REVIEWER Final answer: ...` |

All bodies were captured **before any reload**, via the MCP network recorder.

**Chat.** Two independent fresh runs produced the same result. Every browser check passed:
`browser_rendered_matches_expected` 8597 == 8597, `browser_console_errors_zero`,
`browser_page_errors_zero`, `browser_reached_completed_state`, `browser_observed_progressive_render`.
Progressive rendering was captured by an in-page MutationObserver installed **before** send:
observed lengths `[0, 2260, 4508, 6758, 9008, 9047]` (4 intermediate non-zero states, complete by t=4205ms).
DB invariants passed: exactly one run, one user message, one assistant message, run status succeeded,
persisted text reconstructs to 8597 chars over 5 events, final message equals reconstructed text,
`provider_selected_exactly_once` / `provider_invoked_exactly_once` = 1, **`no_outbound_network_attempt` = 0**,
canary absent from durable fields, UI and backend log.

Notable observation: the UI's **Auto** mode sends `{"message":"..."}` with **no** `mode` field
(the canonical omitted -> chat contract), so the explicit `mode":"chat"` body was captured by
selecting **Chat** in the real ModeSelector. The mode menu is readiness-driven: in the chat scenario
Deep Research and Multi-Agent render disabled with "Not yet available for durable chat."

**Deep Research.** Real durable `deep_research` run, status **succeeded**, 3 run steps.
Scenario-specific fixture evidence: 1 search (query hashed, 2 ranked results), 2 fetches
(`alpha.example.invalid` 2 passages, `beta.example.invalid` 3 passages), 1 synthesis provider
selection and 1 synthesis (message_count 2, prompt_len 930, `prompt_has_canary: true`) — and the
canary is confirmed **absent** from durable fields, the rendered UI and the backend log.

**Multi-Agent.** Real durable `multi_agent` run, status **succeeded**, 3 role calls in the locked
order, each prompt accumulating the previous output: researcher (output 159) ->
solution_specialist (prompt `has_researcher: true`, output 144) -> reviewer
(prompt `has_researcher: true`, `has_solution: true`, output 224). The reviewer output hash
`d556efe275dc3460` is **byte-identical** to the clean browser-rendered answer documented in the
accepted J7 report, independently reproducing that journey.

### Browser-journey caveats (disclosed, not hidden)

1. **The smoke harness's own verdict is `fail` for all three journeys**, because
   `check_database()` / `_check_provider()` / `_check_browser()` in `tests/smoke/run_smoke.py` are
   **hard-scoped to the chat scenario**: they demand `run_kind_is_chat_stream`, exactly one
   `load_context` step, and rendered text equal to `SMOKE_RESPONSE` (8597 chars). The
   `deep_research` and `multi_agent` scenarios reuse those assertions unchanged, so they cannot
   satisfy them by construction (`run_kind` observed `deep_research`/`multi_agent`, 3 run steps,
   146/224-char scenario answers). This is a **test-harness scoping limitation, not a product
   result**, and it is consistent with the accepted J6/J7 phases, which validated their journeys via
   scenario-specific artifacts rather than these chat-shaped assertions. **The harness was deliberately
   NOT modified** — changing assertions to fit evidence would be changing the gate.
2. **`provider_snapshot_recorded` fails for the chat journey** (`model: null`). Root cause is
   established, not guessed: `ResearchSession.model` is nullable **by design**
   (`# NULL = use config default`), and the real model picker in this deployment shows
   **"0 models"** and **"No providers configured. Add one in Settings."** — so no real user can select
   a concrete model here, and the check is unsatisfiable in a free/local-only, no-provider
   configuration. MP-4B changed only SSE serialization, so this cannot be an MP-4B regression.
3. **`saw_partial_render` honesty.** Chat: observed and quantified (above). Deep Research: recorded
   `false` — the observer was armed with the *chat* fixture needle while the DR scenario streams the
   research fixture, so growth sampling recorded only zeros; reported as NOT OBSERVED rather than claimed.
   Multi-Agent: recorded `false` with the raw sample sequence `1:0 | 3190:224 | 3238:0 | 3315:224` —
   the final answer bubble was never seen in an intermediate state (a re-render briefly reset it); what
   *was* observed progressively is the per-agent stage label ("Solution") while the final answer was absent.
4. **Pre-login console 401.** The single console error (`/api/auth/refresh` -> 401) occurred while logged
   **out** — the SPA's silent-refresh bootstrap probe — and is recorded verbatim in a separate
   `prelogin_console_errors_disclosed` field in each `browser_result.json`. No console or page error
   occurred during the authenticated journeys.
5. **Fakes are not live integrations.** These journeys prove the durable pipeline offline and
   deterministically; they do not exercise a real free/local provider.

## 5. Gate 3 — deferred frontend typecheck and production build (PASS)

* `npx tsc --noEmit -p tsconfig.json` -> **TSC_EXIT=0**
* `npm run build` -> **BUILD_EXIT=0** — 4052 modules transformed, built in 41.83s
  (`dist/index-Bw3q5fm-.js` 694.55 kB / 209.98 kB gzip, PWA service worker generated).
* `frontend/dist` is gitignored (`git check-ignore` exit 0, zero `dist/` entries in `git status`),
  so the build did not pollute the tracked tree.

## 6. Static, compile and source-control integrity gates

* `py_compile` on `run_engine/sse.py`, `run_engine/messages_stream.py`, `services/streaming_service.py`, `routes/messages.py` -> exit 0.
* `import app.main` -> `IMPORT_OK`, exit 0.
* AST guard (inside the parity suite): **zero** `streaming_service` imports anywhere under `run_engine/`, plus
  legacy re-export **identity** (`legacy_ss.format_sse is durable_sse.format_sse`) and byte-for-byte parity over
  6 payload cases including unicode round-trip.
* **Protected architecture document UNCHANGED** —
  `docs/superpowers/plans/2026-09-08-phase-1b2r-durable-run-resume-architecture.md`:
  `git hash-object` = `f4d0bbc24f20529addb32ac10bf12e197c7ad747` (exact match to the recorded value),
  byte length **268044** (exact match). Note for the record: a raw `Get-FileHash -Algorithm SHA1` of the
  same untouched file returns `a480e9b7...` and `Get-Content | Measure-Object -Line` returns 2814 — these are
  measurement artifacts (git blob hashing prepends `blob <size>\0`; `Measure-Object -Line` skips blank
  lines), **not** evidence of modification.
* `git diff --check` clean; HEAD still `b2e67183d4791cbbaed9d3d98e8833983510f3ff` on `phase-1b1-approval-domain`.
* **Nothing was committed, staged, reset, rebased, stashed, cleaned, discarded or branch-switched.**
  113 working-tree entries (84 untracked) — the intentionally dirty Phase 1B baseline.
  Note `run_engine/sse.py` and `run_engine/messages_stream.py` are **untracked** in this baseline, which is
  why `git diff HEAD` lists only `streaming_service.py`; their MP-4B content is present on disk and is
  covered by the passing tests.
* Teardown verified after every journey: `backend_dead: true`, `frontend_dead: true`, and no orphaned
  smoke `vite`/`uvicorn` processes (the only live `vite` on the host belongs to an unrelated project).

## 7. What MP-4B did NOT do (explicitly out of scope)

* **Nothing was deleted.** `streaming_service.format_sse`, `stream_chat_response`,
  `stream_multi_agent_response`, `run_research_workflow` and the graph `agent_*` nodes are all **RETAINED**
  (they still have callers and are the rollback path). Deletion is MP-4D, gated on a live free/local provider smoke.
* No adapter was invented: the evidence showed `resolve_and_submit`/`create_chat_run` already **is** the
  canonical boundary, so no `MultiAgentAdapter`/`DeepResearchAdapter` was added.
* **Unsafe legacy python execution remains open for MP-4C** (unchanged by MP-4B and NOT mitigated here):
  `services/streaming_service.py:853-857`, `services/streaming_service.py:535-540`,
  `services/langgraph_workflow.py:607` and `:1206` execute `[PYTHON_CODE:...]` with **no approval and no flag**;
  `run_engine_sandbox` does not exist.
* MP-4C (sandbox/memory-KG convergence), MP-4D (legacy retirement), MP-5 (graph/`run_research_workflow`
  retirement), MP-6 — **NOT STARTED**.

## 8. Residuals carried forward

1. Smoke harness assertions are chat-scoped (caveat 1 above) — MP-4C/MP-5 journeys will hit the same wall.
2. `provider_snapshot_recorded` unsatisfiable without a configured provider (caveat 2).
3. `tests/test_models.py` reports **49 setup ERRORS** (`httpx.ConnectError` WinError 10061 against a live
   server that was not started). **These are NOT counted as passing, NOT hidden, and NOT reported as
   "0 failures"**, and they are **not** classified as an MP-4B regression: the failure is environmental
   (an autouse fixture needs a running server). Reported separately and honestly.
4. Vite dev-proxy concurrent-SSE defect — still **OPEN**, not fixed, not claimed fixed. All journeys used
   the approved direct-backend (`VITE_API_BASE_URL`) route.
5. Legacy python execution paths listed in section 7 remain reachable.
6. No PostgreSQL concurrency matrix; no hosted multi-process verification; no live-provider run.
7. One historical intermittent mobile resume with root cause still unestablished.

## 9. Reproduction

`@
$env:PYTHONPATH="backend;."; .venv/Scripts/python.exe -m pytest tests/test_sse_serializer_parity.py tests/test_mp4b_durable_route_sentinel.py -q -p no:cacheprovider
$env:PYTHONPATH="backend";   .venv/Scripts/python.exe tests/smoke/run_smoke.py --keep-root --browser-timeout 900
$env:PYTHONPATH="backend";   .venv/Scripts/python.exe tests/smoke/run_smoke.py --scenario deep_research --keep-root
$env:PYTHONPATH="backend";   .venv/Scripts/python.exe tests/smoke/run_smoke.py --scenario multi_agent   --keep-root
cd frontend; npx tsc --noEmit -p tsconfig.json; npm run build
`@

Evidence roots kept for inspection (outside the repo, temp):
`C:\Users\user\AppData\Local\Temp\thunder_d6b_h4mzxhax`,
`...\thunder_d6b_21z6lk0l` (chat, two runs),
`...\thunder_d6b_unyrut8g` (deep research),
`...\thunder_d6b_6ob9dhl6` (multi-agent).

## 10. Verdict

**MP-4B — COMPLETE.** The durable route never invokes a legacy streaming helper (22 executable
tests, including a loud negative control), the dependency arrow is proven one-way at AST and runtime
level, all three real-browser journeys executed the durable route end-to-end for their mode with
succeeded runs and captured raw request bodies, and the deferred frontend typecheck and production
build are green. The two non-passing smoke assertions and the non-instrumented partial-render cases
are identified above with their established root causes as harness/deployment limitations rather than
product defects, and none of them is an MP-4B regression.

---

# MP-4B-R1 evidence closeout (2026-09-17) — full per-journey capture

This section records the R1 pass, which re-ran every gate and captured the complete
per-journey evidence matrix (POST counts, idempotency keys, X-Run-Id vs persisted Run ID,
SSE order, terminal counts, DB counts, fixture invocation counts, reload auto-hydration,
error collectors, tripwire, canary, teardown).

## R1-A. File accounting correction (restated)

The initial MP-4B implementation changed **FOUR** files, not three:
`run_engine/sse.py`, `run_engine/messages_stream.py`, `services/streaming_service.py` and the new
`tests/test_sse_serializer_parity.py`. The R1 pass added a fifth file,
`tests/test_mp4b_durable_route_sentinel.py`.

## R1-B. Test gates re-run

| Gate | Result |
|---|---|
| `tests/test_sse_serializer_parity.py` + `tests/test_mp4b_durable_route_sentinel.py` | **22 passed, 0 failed** |
| Related route/submission/readiness/worker suites (21 files) | **582 passed, 1 failed** |
| `tests/smoke` | **91 passed, 0 failed** |
| `tests/test_models.py` | **excluded** (49 environmental setup errors; not counted as passing) |

**The single failure is a stale pre-D5C assertion, not an MP-4B regression:**
`tests/test_run_engine_mp3c3d1.py::test_injected_mode_rejected_before_persistence` (line 212) still
loops `("multi_agent", ...)` expecting a generic `mode_unsupported_*` prefix, so it asserts
`multi_agent_not_executable.startswith("mode_unsupported")` and fails. The test's own comment
(lines 207-211) documents exactly this trade-off for `deep_research`, and the sibling
`test_run_engine_mp3c3b.py` was updated for the same D5C change — this file was missed. The producing
code (`messages_stream.unsupported_mode_reason`) is untouched by MP-4B, which changed only the serializer
import at `messages_stream.py:48`. Carried forward as a convergence item; deliberately **not** edited
during MP-4B.

## R1-C. Per-journey capture matrix

| | Chat | Deep Research | Multi-Agent |
|---|---|---|---|
| Mode selected in real ModeSelector | Chat | Deep Research | Multi-Agent |
| Exact raw request body | `{"message":"...2abce3e38b47","mode":"chat"}` | `{"message":"...511f3bc4d912","mode":"deep_research"}` | `{"message":"...54b5ce02d25d","mode":"multi_agent"}` |
| POST count to /messages/stream | 1 | 2 | 1 |
| Distinct Idempotency-Key count | 1 | **1** | 1 |
| X-Run-Id | `5130bede-…fafe` | `33a7a8f0-…fcf3` | `038bc0ed-…f4def4` |
| Persisted Run ID | `5130bede-…fafe` | `33a7a8f0-…fcf3` | `038bc0ed-…f4def4` |
| X-Run-Id == persisted Run ID | **yes** | **yes** | **yes** |
| SSE event order | `start, run_status, token x5, complete` | `start, run_status, research_progress x4, token` then `complete` | `start, run_status, agent_progress x5, token, agent_progress, complete` |
| SSE seq ids | 3-9 | 3-8 then 9 | 3-11 |
| Terminal complete count | 1 | 1 | 1 |
| SSE error count | 0 | 0 | 0 |
| Run kind / status | `chat_stream` / succeeded | `deep_research` / succeeded | `multi_agent` / succeeded |
| DB rows (runs/steps/payloads/messages/events) | 1/1/1/2/9 | 1/3/2/2/9 | 1/4/4/2/11 |
| approvals / tool_calls / dispatch / execution_turns | 0/0/0/0 | 0/0/0/0 | 0/0/0/0 |
| Messages (user/assistant chars) | 49 / 8597 | 49 / 146 | 49 / 224 |
| Fixture invocation counts | provider selected 1, invoked 1 | search 1, fetch 2, provider_selected 1, synthesis 1 | ma_role_call 3, ma_role_output 3 |
| Reload auto-hydration | yes (8597, 0 stream POSTs) | yes (146, 0 stream POSTs) | yes (224, 0 stream POSTs) |
| Console / page / unhandled-rejection errors | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| Tripwire (outbound attempts) | 0 | 0 | 0 |
| Canary absent (durable fields / UI / log) | yes / yes / yes | yes / yes / yes | yes / yes / yes |
| Independent teardown | backend_dead + frontend_dead | backend_dead + frontend_dead | backend_dead + frontend_dead |

### Durable reconnect is not a duplicate submission

Both the Deep Research and the first Multi-Agent journey issued **2 POSTs with 1 distinct
Idempotency-Key**. The second POST is `/messages/stream?after_seq=<n>` carrying the **same**
Idempotency-Key and returning the **same** X-Run-Id; the terminal `complete` is delivered exactly
once in total (stream 1 stops short of it, stream 2 resumes at the cursor and delivers it). This is
the designed durable resume contract, and it is the strongest available evidence that replay does
**not** duplicate a Run: the DB shows exactly one Run, one `load_context` step, and the expected
step/payload/event counts.

### Reload auto-hydration

After each journey the page was reloaded and the session re-selected: the answer re-rendered from
`GET /api/sessions/1/messages` (200) with **zero** new stream POSTs and no regeneration, proving the
terminal state is served from persistence.

### Controller correction (disclosed)

The first Multi-Agent journey in R1 typed a **stale prompt** copied from a previous run (manifest
expected `...8b24f1bb0919`, the controller typed `...1c1f3ce039b3`). The harness correctly failed
`user_message_matches_prompt`. That run was **discarded**, and the journey was re-run from a fresh
manifest with the prompt read from that manifest; `user_message_matches_prompt` then passed. The
discarded run is not cited as evidence anywhere above.

## R1-D. Static, AST and integrity gates

* `py_compile` (4 files) exit 0; `import app.main` -> `IMPORT_OK`.
* **AST dependency-direction guard: `RUN_ENGINE_TO_STREAMING_SERVICE_IMPORTS=0`** across all of
  `backend/app/run_engine/**`.
* **AST single-serializer-ownership guard: `FORMAT_SSE_DEFINITIONS=1`** — exactly one `def format_sse`
  in the whole backend, at `app/run_engine/sse.py:59`.
* Frontend: `npx tsc --noEmit` exit 0; `npm run build` exit 0 (built in 40.29s).
* `git diff --check` clean; HEAD `b2e67183` unchanged; architecture doc `git hash-object` =
  `f4d0bbc2…` @ 268044 bytes (unchanged).
* Smoke roots kept per journey; no orphaned backend/frontend processes after any run.

## R1-E. Residuals (unchanged, re-confirmed)

1. The smoke harness assertions remain **chat-scenario-scoped**, so the DR/MA scenario verdicts are
   dominated by chat-shaped expectations (`run_kind_is_chat_stream`, one `load_context`,
   `SMOKE_RESPONSE`). Every scenario-independent invariant in those runs passed (run status succeeded,
   exactly one run, exactly one user and one assistant message, final message equals reconstructed text,
   tripwire 0, canary absent, teardown clean).
2. `provider_snapshot_recorded` remains unsatisfiable without a configured provider (`model: null`;
   the picker shows 0 models / "No providers configured").
3. `browser_observed_progressive_render` is `true` only for Chat (growth
   `[0, 2260, 4508, 6758, 9008, 9047]`); DR and MA record `false` honestly — their short answers were
   never observed in an intermediate state, and only the per-agent stage label gave progressive evidence.
4. `tests/test_models.py` 49 setup errors (environmental, no live server) — reported separately.
5. Vite development-proxy concurrent-SSE defect still **OPEN**; not fixed, not claimed fixed. All
   journeys used the approved `VITE_API_BASE_URL` direct-backend path.
6. Ungated `[PYTHON_CODE:]` legacy execution paths remain open for MP-4C.
7. MP-4C, MP-4D, MP-5, MP-6 remain **NOT STARTED**.
