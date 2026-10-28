# Phase 1B.2R — MP-3C2 completion report

**Durable chat → MCP approval → same-run resume**

**Date:** 2026-09-11 · **Branch:** `phase-1b1-approval-domain` · HEAD `b2e6718` ·
**all work uncommitted, not production-ready**

MP-3C2 makes a durable `chat_stream` run that emits a valid `[MCP_CALL]` marker **suspend the
same run for approval**, and — on approval — **resume that same run**: the MCP call executes
at-most-once through the existing MP-2 gateway, its result is sealed, and generation continues
until a segment finishes with no marker and **exactly one** assistant `Message` is produced.

Backend-only. **No** lifespan wiring, **no** `/messages/stream` cutover, **no** SSE/frontend,
**no** Python/shell/browser/desktop execution, **no** reusable session grants, **no** scheduler.
Rides `run_chat_stream_enabled` (+ `run_engine_enabled`) — no new feature flag.

---

## Stage-0 architecture finding

MP-2's approval/dispatch spine is **run-kind-agnostic** and was reused unchanged:

* `app.approval.gateway.dispatch` — trusted-registry revalidation, `PendingApproval` creation +
  `proposal_dedupe_key`, one-time `consume_approval`, `dispatch_records` at-most-once
  (`UNIQUE(tool_call_id)` + started-before-execute CAS), sanitized audit. **Not modified.**
* `app.run_engine.mcp_flow.mirror_decision_signal` — already keys only on
  `run.pending_approval_id` + `run.status ∈ {created,queued,running,waiting_for_approval}`; it
  fires for a `chat_stream` run with zero changes. `open_mcp_args` / `default_mcp_transport` /
  `MCP_STATUS_REASONS` reused. **Not modified.**
* `RunWorker._mcp_first_pass` / `_mcp_on_decision` / `_mcp_dispatch` / `_mcp_finalize` /
  `_mcp_session_check` — drive a `tool_dispatch` `RunStep` + a `ClaimedRun`; a chat run's tool
  step is shaped **identically** to an `mcp_call` run's, so they were reused as-is (a single
  `claimed.kind == chat_stream` branch was added to `_mcp_finalize` to *continue* instead of
  *terminate*).
* `routes/approvals.py::_decide` — **zero changes**. It already flushes one
  `run_signals(approval_decided)` via `mirror_decision_signal` and never executes MCP, never
  calls the provider, never resumes the run inline.
* State machine — already has `running→waiting_for_approval→approved→resuming→running` plus every
  terminal. **No new run state.** `run.status` events narrate the whole flow — **no new control
  event.**
* Schema — `RunStep` already carries `seq` (`UNIQUE(run_id,seq)`), `execution_turn_id`,
  `tool_call_id`, `approval_id`, `input_payload_id`, `output_payload_id`; `Run.pending_approval_id`
  exists. **No migration.**

No stop condition triggered (MP-2 reuses safely on a chat run; the linkage is server-owned;
no migration / state-machine redesign; at-most-once is the gateway's untouched authority).

## Files changed

**New**
- `backend/app/run_engine/chat_tools.py` — the MP-3C2 glue: `parse_one_mcp_call` (wraps the ONE
  canonical `app.tools.mcp_tool.extract_mcp_calls`; requires exactly one call),
  `normalized_args_json`, `open_segment_text` / `open_tool_output`, `tool_result_envelope`
  (wraps the ONE canonical `format_mcp_result`; middle-truncated to
  `run_chat_tool_result_max_bytes`), `tool_step_tool_id`, `CHAT_TOOL_STATUS_REASONS`,
  `ChatToolMarkerError`. No second grammar, no second approval/dispatch path.
- `tests/test_run_engine_mp3c2.py` — 27 tests (fake streaming provider + fake in-memory MCP
  transport; real approval gateway / trusted registry / `dispatch_records`; real
  `routes.approvals._decide`).
- `docs/superpowers/reports/phase-1b2r-mp3c2-completion.md` (this file).

**Modified**
- `backend/app/config.py` — `run_chat_tool_marker_max_bytes` (4096),
  `run_chat_max_tool_cycles` (3), `run_chat_tool_result_max_bytes` (8192); all validated `>= 1`.
- `backend/app/run_engine/chat_stream.py` — `MarkerScanner(+capture=, +max_capture_bytes=)`:
  opt-in confidential capture of a COMPLETE `[MCP_CALL]` line across arbitrary chunk splits
  (`captured` / `capture_status` ∈ `none|capturing|complete|overflow|incomplete|unsupported`);
  every other execution opener → `unsupported` (stays blocked); text after the marker's line is
  discarded. Default (`capture=False`) path is byte-for-byte unchanged.
- `backend/app/run_engine/worker.py` —
  `advance_chat_runs` now leases `waiting_for_approval|approved|resuming` chat runs too
  (`_CHAT_ADVANCEABLE`); `_advance_chat_run` routes an in-flight tool cycle to the new
  `_advance_chat_tool_run` and recovers on the **latest** step (not `seq=1`);
  `_run_chat_generation` uses a capture-enabled scanner, accumulates *clean* text separately from
  raw tokens, and routes a captured marker to `_chat_suspend_for_tool`;
  `_chat_suspend_for_tool` (seal segment + open turn/call + seal args + create `tool_dispatch`
  step, then MP-2 first pass → park); `_chat_continue_after_tool` (new continuation
  `llm_generate` step → resume); `_build_continuation_input` (rebuild provider input from durable
  state, snapshot preserved); `_finalize_chat_success` assembles the one `Message` from **every
  sealed segment**; `_mcp_finalize` gains a `chat_stream` branch that continues instead of
  terminating; `_route_tripped_scanner` maps the scanner to bounded reasons.
- `tests/test_run_engine_mp3b.py`, `tests/test_run_engine_mp3c1.py`,
  `tests/test_run_engine_mp3c1_closeout.py` — 3 tests updated for the **superseded** MP-3B/3C1
  "any `[MCP_CALL]` fails closed" contract: a valid marker now suspends for approval; an
  *unregistered* tool is still blocked fail-closed by the gateway (`unknown_tool`), still not
  executed, still never shown. Privacy assertions unchanged.

**No migration.** Every link uses existing MP-0 `run_steps` + Phase 1B.2 `pending_approvals` /
`dispatch_records` / `execution_turns` / `tool_calls` columns.

## Same-run state & step sequence

```
run:   created → queued → running ──(marker)──► waiting_for_approval ──► approved → resuming → running ──► … ──► succeeded
                                     (lease dropped)   (signal)          (fresh family + cancel re-checks)

steps: seq 1  load_context   pending→running→succeeded   sealed segment-1 clean text  (output_payload_id)
       seq 2  tool_dispatch  pending→waiting→running→succeeded/failed/incomplete
                             execution_turn_id · tool_call_id · approval_id · input_payload_id(tool_args)
                             · output_payload_id(tool_output) · dispatch_records(tool_call_id)
       seq 3  llm_generate   pending→running→succeeded   sealed segment-2 clean text
       seq 4  tool_dispatch  … (cycle 2, if segment 2 emits a marker)
       seq 5  llm_generate   … (continuation 2)
       …                     ≤ run_chat_max_tool_cycles (3) tool_dispatch steps
       last   llm_generate/load_context  → one Message(role=assistant) = ordered concat of every
                             sealed segment's clean text; run running→succeeded — ONE commit.
```

The generation segment that produced the marker is sealed and set **`succeeded`** in the *same
atomic commit* that creates the `tool_dispatch` step (`pending`) — so it is never regenerated
after a restart. The `tool_dispatch` step's `execution_turn_id` / `tool_call_id` are opened
(and committed) **before** that atomic commit, so a crash there only orphans harmless identity
rows (no proposal, no dispatch record) and the run recovers to `outcome_unknown`.

## Marker capture / parser behavior

* `MarkerScanner` (opt-in `capture=True`) detects `[mcp_call` across **any** chunk split (MP-3C1
  suffix-prefix hold), emits only the clean prefix, then privately buffers subsequent chunks
  until the marker's **line** closes (`\n`) or the stream ends with a `]` present → `captured`
  = that one line. `> run_chat_tool_marker_max_bytes` → `overflow`; stream ends mid-line with no
  `]` → `incomplete`; a non-MCP execution opener → `unsupported`. Text after the marker line
  from the same provider pass is dropped (never published, never trusted).
* `chat_tools.parse_one_mcp_call` runs the captured line through the repo's **one** parser
  (`app.tools.mcp_tool.extract_mcp_calls` — line-based regex + `json.loads`, JSON strings /
  escaping / closing `]` handled by that existing code) and requires **exactly one** call:
  0 / unparseable → `tool_marker_malformed`; `>1` → `tool_marker_multiple`.
* Normalized args are `canonicalize(args)` → sealed **once** in an encrypted `RunPayload`
  (`payload_type="tool_args"`). Raw marker text and raw args never enter a text event, a
  `run_steps` ref, `runs.status_reason`, a `run_signals` payload, or the logs. The
  server-owned `tool_id` **does** appear in `run_steps.input_ref` (an allowed non-secret id,
  exactly as MP-2).
* The provider iterator is always closed (`_adrive`'s `finally` runs `aclose()` after the
  `StreamAborted` raised on capture completion).
* Non-MCP execution markers (`[python_code` / `[proposed_command` / `[browser_action`) stay
  blocked with MP-3B/3C1's bounded `tool_resume_required`. One MCP call per suspension cycle.

## Approval → signal → dispatch → continuation flow

1. **Suspend** — `_chat_suspend_for_tool`: parse → cycle/one-active-proposal ceiling
   (durable `run_steps` count) → cancel/session re-check → `open_turn`/`open_tool_call` (commit)
   → **one atomic commit** {seal segment, gen step `succeeded`, seal args, `tool_dispatch` step
   `pending`} → `_mcp_first_pass` (MP-2, `execute_mcp=_forbidden`) → gateway returns
   `approval_required`, worker links `run.pending_approval_id` + `run_steps.approval_id`, step
   `waiting`, `running→waiting_for_approval` (**lease dropped**).
2. **Decide** — `POST /api/approvals/{id}/approve|deny` → `_decide` → `mirror_decision_signal`
   flushes one `run_signals(approval_decided,{approval_id,decision})` → `record_decision(commit=False)`
   → the tamper-evident ActionLog append commits decision + audit + signal **atomically**. No
   MCP, no provider, no worker, no run resume inline. No `/consume` API.
3. **Resume** — a later `advance_chat_runs` sweep leases the run (`_advance_chat_tool_run`) →
   `_mcp_on_decision`: fresh family check → cancel check → **consume the signal exactly once**
   (`UPDATE … WHERE consumed_at IS NULL`, rowcount 1) → `denied` ⇒ `waiting_for_approval→denied`
   (terminal, step `blocked/approval_denied`, no execution, no continuation); `approved` ⇒ fresh
   family check again → `waiting_for_approval→approved→resuming→running` (same commit as the
   consume) → `_mcp_dispatch`.
4. **Dispatch** — `_mcp_dispatch` → `dispatch_records` restart check → fresh family check → cancel
   check → `gateway.dispatch` (real transport): `consume_approval` (one-time CAS, envelope
   re-hash vs `action_hash`, user/tenant/session/run identity binding) → `_do_dispatch`
   (`DispatchRecord` CAS `started` committed → `execute_mcp()` **exactly once** → `succeeded|failed`
   committed). `_mcp_finalize` seals the result in an output `RunPayload` **before** any
   transition.
5. **Continue** — `_mcp_finalize`'s `chat_stream` branch commits {output sealed, step
   `succeeded/failed`} then `_chat_continue_after_tool`: fresh precheck + cancel →
   `_build_continuation_input` (submission snapshot **preserved** — provider/model/mode/resolved
   system prompt/options; prior sealed segments as `assistant`; each sealed tool result as a
   delimited, size-bounded **`user`** message — never the system prompt) → new `llm_generate`
   step `pending` (commit) → `running` (commit) → `_run_chat_generation`. No marker ⇒
   `_finalize_chat_success` (one Message); another marker ⇒ `_chat_suspend_for_tool` (next cycle).

Provider attempts per segment = **1**; no paid fallback (`default_chat_provider`, MP-3C1, has no
fallback chain). A continuation invocation is a fresh bounded LLM attempt — never a repeat of the
MCP side effect.

## At-most-once & crash-window evidence

The gateway's `DispatchRecord(UNIQUE tool_call_id)` + started-before-execute CAS is the
unchanged at-most-once authority; each cycle opens its own server-owned `ExecutionTurn`/`ToolCall`.

| Crash point (restart = fresh `advance_chat_runs`) | Recovery | MCP re-invoked? | Message? |
|---|---|---|---|
| after clean text sealed, before the atomic tool-step commit | gen step still `running` (atomic commit didn't land) → `outcome_unknown` | no | no |
| after the atomic tool-step commit, before proposal | latest step `tool_dispatch/pending`, no `approval_id` → `_mcp_first_pass` replay; `proposal_dedupe_key` returns the SAME proposal | no | no |
| after proposal, before park | same as above → re-link + park | no | no |
| restart while `waiting_for_approval` | `_mcp_on_decision`, no signal → drop lease, stay parked | no | no |
| `approval_decided` persisted, not consumed | consumed once (rowcount-1 CAS); a racing second worker gets 0 rows | no | no |
| consumed, before `dispatch_records` `started` | `_mcp_dispatch` finds no record → dispatch once | yes, once | continues |
| `started`, no terminal `dispatch_records` row | `outcome_unknown` (`dispatch_incomplete`) | **no** | no |
| `succeeded` + result sealed, before continuation step | `duplicate_dispatch` + record `succeeded` + `output_payload_id` set → **reuse**, continue | **no** | continues → yes |
| result sealed, before continuation step creation | `_chat_continue_after_tool` (idempotent — creates the step) | no | continues |
| continuation step `pending`, before provider | latest step `llm_generate/pending` → reclaim + run | no | no |
| continuation step `running` at crash | latest step `llm_generate/running` + expired → `outcome_unknown` (no 2nd provider attempt) | no | no |
| Message inserted but the `running→succeeded` transition fails | `_apply_transition` raises → `db.rollback()` → **no Message**, run stays `running` | no | no |
| commit raises after the transition | `outcome_unknown` (`output_persist_failed`) — success never falsely reported | no | no |

A `succeeded` dispatch with a sealed output is reused; a `started`/`incomplete` dispatch without a
provable outcome becomes `outcome_unknown` and is never auto-re-executed. Final success requires
one committed `Message` (the `_apply_transition` lease + version CAS gates it; a re-entered
`_finalize_chat_success` sees `output_ref.message_id` and no-ops to `succeeded`).

## Cancellation / session / race behavior

* cancel before any proposal → `_chat_suspend_for_tool` checks the live signal first →
  `running→cancelling→cancelled`, **no proposal / turn / dispatch**.
* cancel while `waiting_for_approval` → `_mcp_on_decision` consumes cancel first (cancel wins);
  a later `approved` signal cannot dispatch (run terminal).
* cancel immediately before dispatch → `_mcp_dispatch`'s live-cancel check →
  `running→cancelling→cancelled`, no `DispatchRecord`.
* once `dispatch_records.started` commits → the run finishes on its **true** outcome; it never
  claims the external action was undone.
* denial → `waiting_for_approval→denied` (terminal); no execution, no continuation.
* session family **revoked / expired** before proposal, before signal-consume, before
  `→resuming/→running`, immediately before dispatch, or before the continuation provider call →
  fail closed (`session_revoked`); STRICT origin binding (no fallback family).
* duplicate approve/deny → idempotent (`idempotent_decision_state` + `uq_run_signals_live` nested
  savepoint) — one signal, one execution.
* cross-user / cross-session decision → `routes.approvals._owned_approval` 404 before any
  decision logic; the gateway's `consume_approval` re-binds user/tenant/session/run.
* stale worker / lost lease → `_apply_transition`'s `lease_owner` + `version` CAS matches 0 rows
  → `RunLeaseLost` → the worker abandons the run; a stale worker creates neither a second Message
  nor a second dispatch.
* terminal run + late `approval_decided` → `_advance_chat_run` returns `already_terminal`.

## Privacy evidence

Canaries seeded in the prompt, system prompt, MCP args, MCP output, and the post-marker trailing
text are asserted **absent** from every `run_events` row (control + text), every `run_steps`
`input_ref`/`output_ref`/`error_code`, `runs.status_reason`, every `run_signals` payload, and the
captured DEBUG logs — and **present only** inside `run_payloads.ciphertext` (sealed args /
sealed result / sealed segment text) and, for assistant-visible clean text, the text events +
the one final `Message`. The tool-result envelope (`=== MCP Tool Result … ===`) reaches the
provider input only; it is never persisted to a text event or the Message. `MarkerScanner` never
returns full or partial marker text. Worker logs are bounded class names / status slugs only.

## Focused test counts

| Check | Result |
|---|---|
| `compileall` — `config.py`, `chat_stream.py`, `chat_tools.py`, `worker.py`, changed tests | clean |
| `tests/test_run_engine_mp3c2.py` (new) | **27 passed** |
| `tests/test_run_engine_mp3c1_closeout.py` (MarkerScanner directly modified) | **21 passed** (1 updated for the superseded fail-closed contract) |
| `tests/test_run_engine_mp3c1.py` (chat_stream contract) | **21 passed** (1 updated) |
| `tests/test_run_engine_mp3b.py` (`_run_chat_generation` / `_finalize_chat_success`) | **21 passed** (1 updated) |
| `tests/test_run_engine_mcp_resume.py` (`_mcp_finalize` gained a chat branch) | **21 passed** (0 changes) |
| `import app.main` · `git diff --check` | OK · clean |

Not run (deferred to the final Phase 1B gate): the 119-test directly-affected group, the
613-test hermetic regression, the full suite, PostgreSQL, Docker, Ollama, ChromaDB, frontend,
live-provider.

## Remaining blockers

- **Lifespan wiring** — `RunWorker.run_forever` is still gated off (`run_engine_enabled` +
  `run_worker_enabled`); nothing starts the worker from the app lifespan. Deliberate (MP-3C+).
- **`/messages/stream` cutover** — the live chat path still shows "approval required" and
  executes nothing; the durable path is reachable only by the (feature-flagged, unwired) worker.
- **Frontend / SSE UI** — no client surfaces the durable chat-tool cycle.
- **PostgreSQL** — the lease/CAS/`dispatch_records`/partial-index approach is PG-safe by
  construction; the concurrency matrix + `SELECT … FOR UPDATE SKIP LOCKED` tightening remain the
  MP-6 / hosted-deployment gate (`EVAL_PG_TEST_URL` unset).
- **Broad regression** — full Phase 1B / 613-test hermetic, evals, Ollama/ChromaDB, Docker,
  frontend — deferred to the final phase gate.
- **No per-run attempt column** — provider attempts per segment and MCP invocations per cycle are
  hard-capped at 1 in code; tool cycles are capped at 3 from durable `run_steps`. A strict
  counter column is not required.

## Not started (confirmed)

Lifespan worker startup, live `/messages/stream` replacement, frontend/SSE integration,
Python/shell/browser/desktop/computer-use execution, reusable session grants, nested sub-agents,
scheduler work, the PostgreSQL matrix, Docker/service-backed tests, and broad regression are all
**not started**.

## Source control (confirmed)

Nothing was committed, merged, pushed, reset, stashed, cleaned, or rebased; no branch switch. The
pre-existing stash was not touched. The entire dirty working tree is preserved on
`phase-1b1-approval-domain` at HEAD `b2e6718`.
