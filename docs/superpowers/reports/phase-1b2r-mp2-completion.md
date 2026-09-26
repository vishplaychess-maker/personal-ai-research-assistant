# Phase 1B.2R — MP-2 completion report

**Durable one-time MCP approval resume + at-most-once dispatch**

**Date:** 2026-09-09 · **Branch:** `phase-1b1-approval-domain` · HEAD `b2e6718` ·
**all work uncommitted, not production-ready**

MP-2 connects the Phase 1B.2 approval gateway / trusted registry / `dispatch_records` ledger, the
durable RunEngine, the MP-0 encrypted `RunPayload` store, and the MP-1B `RunWorker` so that **one
approved MCP call resumes and executes exactly once through an internal, in-process durable flow**.
Internal only — no `/messages/stream` cutover, no SSE, no frontend, no public Run route, no
session grants.

---

## Files changed

**New**
- `backend/app/run_engine/mcp_flow.py` — `create_mcp_run` (internal service), `mirror_decision_signal`
  (approvals-route bridge), `open_mcp_args` / `step_tool_id` / `default_mcp_transport`, `MCP_STATUS_REASONS`.
- `tests/test_run_engine_mcp_resume.py` (21 tests, fake in-memory MCP transport).
- `docs/superpowers/reports/phase-1b2r-mp2-completion.md` (this file).

**Modified**
- `backend/app/run_engine/worker.py` — `RunWorker(+mcp_transport=)`; `advance_mcp_runs` +
  `_advance_mcp_run` / `_mcp_first_pass` / `_mcp_on_decision` / `_mcp_dispatch` / `_mcp_finalize` /
  `_mcp_session_check`; `poll_once` also advances MCP runs; `claim_next_queued` / `recover_expired`
  now **exclude** `kind == "mcp_call"`; `_consume_cancel(+frm=)`; `_finalize_terminal(+frm=)`;
  `_apply_transition` accepts the bounded MCP reason codes and drops the lease when parking at
  `waiting_for_approval`.
- `backend/app/run_engine/engine.py`, `.../events.py` — `"mcp_call"` added to `_RUN_KINDS`.
- `backend/app/run_engine/__init__.py` — export `create_mcp_run`, `mirror_decision_signal`.
- `backend/app/approval/state_machine.py` — `approve` / `deny` / `_decide` take `commit=False`.
- `backend/app/approval/service.py` — `record_decision(commit=True)` passthrough.
- `backend/app/routes/approvals.py` — `_decide` flushes the RunSignal, then
  `record_decision(commit=False)`; the tamper-evident ActionLog append issues the **single**
  commit — decision + audit event + signal persist or roll back together.
- `tests/run_engine_helpers.py` — `mk_engine` also creates `uq_pending_approvals_active_dedupe`.

**No migration.** Every identity link uses columns that already exist (MP-0 `run_steps` +
Phase 1B.2 `pending_approvals` / `dispatch_records`). No approval-domain table rewritten.

## Exact identity linkage (server-owned; never accepted from the client as authorization)

```
runs.id ─┬─ runs.auth_session_ref  (refresh_sessions.family_id — STRICT origin binding, 5C)
         ├─ runs.tenant_id / runs.user_id / runs.session_id
         └─ runs.pending_approval_id ──────────────► pending_approvals.id
run_steps (one, kind=tool_dispatch) ─┬─ run_id
         ├─ execution_turn_id ───────► execution_turns.id      (open_turn, server UUID)
         ├─ tool_call_id ────────────► tool_calls.id           (open_tool_call, bound to tool_id+args_hash)
         ├─ approval_id ─────────────► pending_approvals.id
         ├─ input_payload_id ────────► run_payloads (sealed normalized args; tool_args)
         └─ output_payload_id ───────► run_payloads (sealed result; tool_output)
pending_approvals.trusted_envelope_json  → tool_id, tool_version, tool_schema_hash,
         server_fingerprint, normalized action_hash, proposal_dedupe_key, policy_version
dispatch_records.tool_call_id (UNIQUE)   → the at-most-once authority + action_hash
```
The worker builds `gateway.GatewayContext` **only** from these run/step rows. `open_mcp_args`
supplies `open_payload`'s `expected_*` from the same trusted rows. Any mismatch → the gateway or
the payload store fails closed and nothing executes.

## Approval → signal flow (the approve/deny endpoint never executes anything)

1. `create_mcp_run` → run `created→queued`; one `tool_dispatch` step; normalized args
   (`canonicalize(args)`) sealed once in an encrypted `RunPayload`; server-owned turn + call.
2. worker `advance_mcp_runs` → **first gateway pass** with a `_forbidden` execute callback →
   `gateway.dispatch` returns `approval_required` (no `execute_mcp` call), creating the
   `PendingApproval`. Worker links `runs.pending_approval_id` + `run_steps.approval_id`, moves
   `running→waiting_for_approval`, **drops the lease**.
3. `POST /api/approvals/{id}/approve|deny` → `_decide`: owner/CSRF/state checks →
   `mirror_decision_signal` flushes one `run_signals(approval_decided,{approval_id,decision})`
   (idempotent via `uq_run_signals_live`, nested savepoint) → `record_decision(commit=False)` →
   the ActionLog append commits **decision + audit + signal atomically**. No MCP, no worker, no
   executor is touched inline.
4. worker `_mcp_on_decision`: fresh session-family check → cancel check → parse decision →
   **consume the signal exactly once** (`UPDATE … SET consumed_at WHERE consumed_at IS NULL`,
   rowcount 1) → `denied` ⇒ `waiting_for_approval→denied` (terminal); `approved` ⇒ fresh session
   check again → `waiting_for_approval→approved→resuming→running` (all in the same commit as the
   consume) → dispatch.

## At-most-once external dispatch sequence (`_mcp_dispatch` → `gateway.dispatch`, unchanged)

1. `dispatch_records` restart check (below). 2. fresh session-family check **immediately before
   invocation**. 3. live-cancel check (cancel wins → `running→cancelling→cancelled`, no dispatch
   record). 4. `open_mcp_args` (trusted expected context). 5. `gateway.dispatch`:
   trusted-registry re-resolve (lock/enable/schema-hash/fingerprint/policy-version) → prior-denied
   check → prior-dispatch check → `consume_approval` (one-time CAS `approved→consumed`, envelope
   re-hash vs `action_hash`, user/tenant/session/run identity binding) → `_do_dispatch`:
   **DispatchRecord CAS `started` committed → then `execute_mcp()` invoked exactly once → outcome
   `succeeded|failed` committed**. 6. worker seals the result in an output `RunPayload`
   **before** the run's terminal transition; `succeeded`/`failed` accordingly.
   Max automatic MCP invocation attempts = **1**; no retry loop; a lease expiry / restart never
   creates a second invocation.

## Crash-window behavior (restart = a fresh `RunWorker.poll_once`)

| `dispatch_records` state on restart | run / step outcome | MCP re-invoked? |
|---|---|---|
| none | continue normal validation + dispatch | yes, once (first real invocation) |
| `started` / `incomplete` (no terminal) | `outcome_unknown` / step `incomplete` | **no** |
| `succeeded` | `succeeded` (reuse; output payload kept) | **no** |
| `failed` | `failed` (`tool_errored`) | **no** |
| gateway `blocked: dispatch_incomplete` | `outcome_unknown` | no |
| gateway `blocked: duplicate_dispatch` | reuse if record `succeeded` + payload sealed, else `outcome_unknown` | no |
| output-payload seal fails after a real `executed` | `outcome_unknown` (`output_persist_failed`) — success is never reported | no |
Cancel after `started` commits: the run finishes on its true outcome; it never claims the
external action was cancelled/undone. Denial never writes a `started` record and is terminal.

## Payload confidentiality

Normalized args and tool results live **only** in `run_payloads` (context-bound Fernet envelope,
MP-0). `runs` / `run_steps` refs / `run_events` / `run_signals` / `action_log` / `dispatch_records`
carry only IDs, states, hashes, and bounded reason/error codes. A test seeds a secret canary in
the args and the result and asserts it is absent from every ordinary column and present only
inside `run_payloads.ciphertext`. Worker logs bounded codes only — never args/results/exception
text.

## Focused test counts (per the mini-phase instruction — no broad regression)

| Check | Result |
|---|---|
| `python -m compileall` (all changed files) | clean |
| `tests/test_run_engine_mcp_resume.py` | **21 pass** |
| approval gateway/API + approval domain + MCP dispatch/tool + RunWorker + RunEngine cancellation/session-binding/transitions/events (directly affected) | **296 pass / 3 skip** |
| `git diff --check` · `import app.main` | clean · OK |

Required cases covered: approve → one signal, no inline exec; duplicate approve → no duplicate
signal; denial never invokes MCP + terminal; approved action → fake MCP once; restart after
success → 0 extra invocations; crash after `started` → `outcome_unknown`; crash before `started`
→ safe resume; altered action hash → `envelope_mismatch` blocked; tampered sealed args → fail
closed; locked/changed registry → blocked; revoked session before dispatch → blocked; different
active family of the same user → not accepted; cancel before dispatch → no invocation; cancel
after `started` → no false undo; two workers race → one invocation; encrypted args/result
round-trip; secret canary absent from ordinary fields; approval decision + signal roll back
together; output-persistence failure → not reported as success; no public route / stream change.

## Remaining blockers

- **Not wired into the app lifespan / `/messages/stream`** — deliberate (MP-3). `run_forever`
  still gated off by `run_engine_enabled` + `run_worker_enabled`.
- **`default_mcp_transport`** resolves the real MCP server via `tool_registry` + `mcp_service`
  and is exercised only by injected fakes here; the live wiring lands with MP-3.
- **PostgreSQL unverified** — the lease/CAS/`dispatch_records`/partial-index approach is PG-safe
  by construction; the concurrency matrix + `SELECT … FOR UPDATE SKIP LOCKED` tightening are MP-6
  (`EVAL_PG_TEST_URL` unset). Hosted-deployment blocker stands.
- **No per-run attempt column** — MCP external attempts are hard-capped at 1 in code; a strict
  counter is not required for MP-2.
- **Broad regression deferred** — full Phase 1B / 613-test hermetic, evals, Ollama/ChromaDB,
  Docker, frontend — all deferred to the final phase gate.

## Not started (confirmed)

MP-3 (streaming persistence / reconnect), MP-4, MP-5, MP-6, and Phase 1B.3 are **not started**.
No `/messages/stream` change, SSE, frontend, scheduler integration, reusable session grants,
Python/shell/browser/desktop execution, LLM calls, or nested sub-agents.

## Source control (confirmed)

Nothing was committed, merged, pushed, reset, stashed, cleaned, or rebased. The entire dirty
working tree is preserved. All Phase 1B / 1B.2R work remains uncommitted on
`phase-1b1-approval-domain` (HEAD `b2e6718`).
