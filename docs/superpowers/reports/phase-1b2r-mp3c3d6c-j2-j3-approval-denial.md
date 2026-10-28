# MP-3C3D6C-J2/J3 — MCP approval approve & deny browser journeys — COMPLETE

Date: 2026-09-17  
**Status: D6C-J2 and D6C-J3 COMPLETE — D6C IN PROGRESS (J1, J2, J3 complete; J4–J9 not started).**

Both journeys passed against real product code. Nothing below is a projected or
inferred result: every claim is backed by a live browser observation, a row read
out of the scenario's own SQLite database, or a focused test run.

## Scope and fidelity statement (read this first)

* The MCP server is a **deterministic test-only fake transport**, injected through
  the existing RunWorker constructor seam. No real MCP server, network socket or
  third-party tool was contacted.
* The LLM is the **deterministic test-only MarkerProvider** injected through the
  existing chat_provider seam. No paid or remote model was called.
* Everything else is real: real FastAPI lifespan, real RunWorkerSupervisor and
  RunWorker, real SQLite, real approval gateway, real approval route, real run
  engine, real React UI in a real Chromium.
* The Vite dev-proxy concurrency defect (D6B-R5) is **still OPEN** and is **not**
  fixed by this work. The smoke harness routes the browser straight to the backend
  via VITE_API_BASE_URL + CORS_ORIGINS. That is a **smoke workaround only**.

## Test-only scenario switch

THUNDER_SMOKE_SCENARIO is read **only** by tests/smoke/durable_smoke_app.py. It is
not in backend Settings, not in production environment parsing, not reachable from
any header, query, body, frontend request or normal startup. When it is unset the
worker is built exactly as before (chat_provider=CHAT_PROVIDER, no mcp_transport).

## J2 — Approve journey

Root: C:\\Users\\user\\AppData\\Local\\Temp\\thunder_d6b_81tfuah6
(evidence archived to C:\\deepseek-harness\\d6c-j2-evidence)
smoke.db SHA-256: 33dee5767e2b4d91f83e5a3bd8ea01a650ab77dacad0303c4d9010c6796ec6dd

Journey: login as d6b_smoke_user -> new session -> send "D6C-J2 approval probe" ->
wait for the real ApprovalDialog -> click **Approve once**.

### Browser evidence

| Observation | Result |
|---|---|
| ApprovalDialog rendered for the real pending approval | yes |
| Tool shown | mcp__smoke__echo |
| Risk shown | medium risk |
| Dialog details payload | redacted risk metadata only — no tool arguments |
| Raw marker [MCP_CALL ...] anywhere in the DOM | **absent** |
| Visible assistant text while parked at the gate | "Smoke approval fixture ready." |
| Final rendered answer | "Smoke approval fixture ready. SMOKE_TOOL_RESULT_ACKNOWLEDGED" |
| Final marker in the DOM | **absent** |

The dialog's DETAILS field contained exactly:
{"affected_resources":[],"credential_scope":null,"filesystem_scope":null,
"network_scope":null,"purpose":"","risk_level":"medium",
"risk_scope_flags":["external_tool"],"source_type":"mcp","tool_id":"mcp__smoke__echo"}
— i.e. the redacted envelope, not the call arguments.

### Run and approval state (same Run id throughout)

| Field | Value |
|---|---|
| Run id | 24b809c1-f9f7-4277-a5fc-a291269796f4 |
| Run kind / final status | chat_stream / **succeeded** |
| Run steps | 1 load_context succeeded, 2 tool_dispatch succeeded, 3 llm_generate succeeded |
| Run status event sequence | queued -> running -> waiting_for_approval -> running -> succeeded |
| PendingApproval | 9553f644-6f78-466c-9cbc-3d16e3c6cdd5, state **consumed**, consumed_at 2026-09-17 13:23:46 |
| RunSignal | approval_decided, consumed |
| ExecutionTurn | exactly 1 |
| ToolCall | exactly 1 |
| DispatchRecord | exactly 1, outcome **succeeded** |
| Messages | 2 (1 user, 1 assistant) |

Provider invocations: exactly **2** (invocation 1 emitted the visible prefix plus the
split marker; invocation 2 produced the acknowledgement after the tool result was
fed back). Transport invocation: exactly **1**.

### ActionLog chain (J2)

approval.requested -> approval.denied is not present; the J2 chain is:
approval.requested -> approval.approved -> approval.consumed ->
tool.execution.started -> tool.execution.succeeded (5 linked rows, single scope).

Verified two independent ways:

1. The production verifier app.approval.action_log.verify_chain over the scenario DB:
   **ok=True, count=5, break_at=None, detail="verified"**.
2. A raw-SQLite recomputation of every row_hash from the stored columns using
   app.approval.hashing.row_hash: **ok=True, link_ok=True, rows=5, genesis=000000000000**.

### Redaction and encryption (J2)

4 run_payloads, 2104 ciphertext bytes total. None of the following appear anywhere
in the stored ciphertext: "MCP_CALL", "ping", "SMOKE_ECHO_OK", "mcp__smoke__echo".

pending_approvals.args_hash == tool_calls.args_hash ==
99d096937055f4372b729971184f55607a947ba6c15b976de1f9dae42080647d.
The proposal's own display_summary and trusted_envelope_json contain identity and
scope metadata only — no tool arguments.

## J3 — Deny journey

Root: C:\\Users\\user\\AppData\\Local\\Temp\\thunder_d6b_ljesc5ta
(evidence archived to C:\\deepseek-harness\\d6c-j3-evidence)
smoke.db SHA-256: 2f923a688b4c2424f09928bd4d848eb8120969b4b9991d94d380415d98a1560a

Journey: fresh root, fresh browser -> login -> new session -> send
"D6C-J3 denial probe" -> wait for the real ApprovalDialog -> click **Deny once** ->
then replay deny, then attempt approve, from the same authenticated browser context.

### Browser evidence

Dialog rendered with the same redacted details and the same tool/risk line; the raw
marker was **absent** from the DOM while parked at the gate. After Deny the dialog
closed, no tool result text appeared anywhere in the page, and "SMOKE_ECHO_OK" and
"SMOKE_TOOL_RESULT_ACKNOWLEDGED" never appeared.

### Terminal state

| Field | Value |
|---|---|
| Run id | d67e5a4c-2596-4fba-9114-2ca334969d52 |
| Run kind / final status | chat_stream / **denied** |
| Run steps | 1 load_context succeeded, 2 tool_dispatch **blocked** (error_code approval_denied) |
| Run status event sequence | queued -> running -> waiting_for_approval -> **denied** |
| PendingApproval | 53ed50a8-8707-46e8-9cc9-015cfe19472f, state **denied**, decided_at 13:29:19.074554, consumed_at NULL |
| RunSignal | approval_decided, payload decision=denied, consumed |
| ExecutionTurn | exactly 1, assistant_message_id = **None** |
| ToolCall rows | 1 (recorded, never executed) |
| DispatchRecord rows | **0** |
| DispatchRecord rows with outcome='started' | **0** |
| Messages | **1** — the user message only; no assistant success message exists |
| ActionLog | approval.requested -> approval.denied (2 linked rows) |

Provider invocations: exactly **1** — there is no llm_generate step at all, so the
provider was never called again after the marker tripped. Transport invocations:
exactly **0**.

The transport is reachable only from inside app.approval.gateway.dispatch, and the
fake transport additionally fails closed if invoked before a started dispatch row
exists. With zero dispatch records of any outcome, the executed-tool count is zero
by construction as well as by observation.

### Replay and non-revival (authenticated browser context)

Performed from the page at http://127.0.0.1:60001 using the app's own auth model:
carry the HttpOnly refresh cookie, obtain a fresh access token from
POST /api/auth/refresh, then send the access token as Bearer plus the double-submit
X-CSRF-Token header echoed from the CSRF cookie.

| Attempt | Result |
|---|---|
| POST /api/approvals/{id}/deny (replay) | **200** {"id":"53ed50a8-...","state":"denied","idempotent":true} |
| POST /api/approvals/{id}/approve (after denial) | **409** {"detail":"already denied"} |
| POST /api/approvals/{id}/deny (third time) | **200** {"state":"denied","idempotent":true} |

A denial cannot be revived into an approval, and repeating a denial is idempotent
rather than an error.

The database after all three attempts is **identical** to the database before them:

| Table | Before | After |
|---|---|---|
| runs | 1 (denied) | 1 (denied) |
| dispatch_records | 0 | 0 |
| run_signals | 1 | 1 |
| action_log | 2 | 2 |
| messages | 1 | 1 |
| run_events | 6 | 6 |

No new event, signal, dispatch, message or approval row was created by the replay
attempts.

## Focused verification run for this pass

| Suite | Result |
|---|---|
| tests/smoke/test_scenario_wiring.py (new, 7 tests) | 7 passed |
| tests/smoke/test_mcp_fakes.py (8 tests) | 8 passed |
| combined focused | **15 passed** |
| tests/smoke + tests/test_run_engine_mcp_resume.py + tests/test_approval_gateway.py | **95 passed, 1 skipped** |
| Frontend: ApprovalDialog, useStreaming, durableStreaming, streamSubmissionCount, useStreamingSafety, useStreamingStrictMode, manifestDeclaration | **67 passed (7 files)** |
| python -m py_compile over tests/smoke/*.py | exit 0 |
| git diff --check (worktree and index) | clean |

The wiring tests are structural (AST), so they assert the contract that makes the
journeys meaningful without importing the harness: the switch is child-env only and
absent from backend/app, the provider and transport are module-level singletons, the
default worker path is untouched (chat_provider=CHAT_PROVIDER, no mcp_transport),
the runner sets the scenario in the backend child env only, seeding strictly
precedes the manifest the external browser controller reads, seeding is idempotent
and preserves identity, and all seven durable production defaults are still False.

## Cleanup

After each scenario: 0 smoke descendant processes, 0 remaining thunder_d6b_* /
thunder_d6c_* temp roots, 0 listening ports on the scenario ports, no pending
worker or injector tasks, and no binary artifact added to the repository. Each
scenario's smoke.db, backend.log, frontend.log and manifest.json were archived
outside the repository before its temp root was deleted.

Repository state is unchanged by this pass: branch phase-1b1-approval-domain,
HEAD b2e67183d4791cbbaed9d3d98e8833983510f3ff, 0 staged files, stash@{0} untouched.
No commit, merge, push, stage, reset, rebase, stash, clean, discard, branch switch
or worktree operation was performed.

## Honest limitations and open items

1. **No screenshot of the J2 approval dialog.** J2's browser context and smoke root
   were torn down before screenshots were taken. J2's dialog evidence is therefore
   DOM text plus database rows, not an image. One screenshot of the J3 post-denial
   page exists at C:\\deepseek-harness\\d6c-j3-denied-terminal.png.
2. **UI observation, not investigated:** after the denied run, the conversation view
   shows "0 messages" even though the database holds the 1 persisted user message
   for the same session. Whether the user message should remain visible after a
   denied run is a product/UI question, not a security property. It does not affect
   any J3 acceptance result, which is established at the run, step, approval,
   signal, dispatch and message-row level. Recorded here as an open UI follow-up.
3. **The Vite dev-proxy concurrency defect remains OPEN.** It is not fixed and must
   not be described as fixed. The smoke bypass is a harness workaround.
4. **Transport invocation counts are established by construction:** zero dispatch
   records in J3 and exactly one succeeded dispatch record in J2, combined with the
   fake transport's own fail-closed precondition. No process-level counter was read
   out of the backend child.
5. **Fake MCP transport and deterministic provider.** These journeys prove the
   approval gate, binding, redaction, durable state machine and non-revival
   semantics. They do not prove behaviour against a real MCP server or a real model.
6. Physical network-loss behaviour and the remaining D6C journeys (J4–J9) are **not**
   covered by this report.
