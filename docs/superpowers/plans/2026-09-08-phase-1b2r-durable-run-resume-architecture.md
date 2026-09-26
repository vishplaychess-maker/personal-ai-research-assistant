# Phase 1B.2R — Durable Run, Approval Resume & Unified Execution Architecture

**Date:** 2026-09-08 (design) · 2026-09-08 (MP-0 decision-lock + schema foundation)
**Status:** Architecture **conditionally approved** (D1–D10 locked below). **MP-0 implemented**
(schema + state machine + encrypted payload store + allowlisted signals + migration + tests) —
awaiting user review. MP-1 and later NOT started. Phase 1B.3 NOT started.
**Branch:** `phase-1b1-approval-domain` (HEAD `b2e6718`). Phase 1B.1 + 1B.2 + 1B.2R MP-0 all
uncommitted.
**Gate:** NOT approved to commit / merge / push / release / begin broad tool wiring / claim
production-ready durable execution.

> Source-control: nothing committed, merged, pushed, reset, stashed, cleaned, or rebased. No
> Phase 1B.1 / 1B.2 table or model rewritten. MP-0 is additive-only.

MP-0 as-built detail: see **§27** (schema, encryption contract, state machine, migration) and the
completion report `docs/superpowers/reports/phase-1b2r-mp0-completion.md`.

---

## 0. Locked decisions (D1–D10) and the secure-payload contract

The user conditionally approved the custom SQLAlchemy-backed `RunEngine` (this document, §6) and
locked the following. MP-0 implements only the schema foundation consistent with them.

### D1 — orchestration foundation
Custom database-backed `RunEngine` now. LangGraph durable checkpointers **deferred** to a possible
later consolidation of graph-shaped subflows. Rules: **one** authoritative durable store per run
(the `runs` / `run_steps` / `run_events` ledger); a future LangGraph subflow may run behind
`run_steps.engine = "langgraph"` but stays **subordinate** to RunEngine identity / approval /
dispatch / audit state; **no** `langgraph` upgrade and **no** `langgraph-checkpoint-sqlite` /
`-postgres` added in MP-0; **no** Temporal / DBOS / Prefect. The earlier "≈80% of a durable
engine" phrasing is withdrawn — replaced by the evidence list in §27.4 (primitives already
available vs capabilities still missing).

### D2 — worker topology
**Self-host:** one `RunWorker` may run as an `asyncio` lifespan task; SQLite ⇒ single application
process / single worker; startup must reject or clearly block an unsupported multi-worker SQLite
configuration; worker state must never live only in memory. **Hosted:** PostgreSQL required; a
dedicated worker process/service is mandatory for production; API processes must not each start an
uncontrolled embedded worker; claims use DB leases + PostgreSQL-safe locking; PG multi-worker
verification remains a hosted-release blocker. **No worker exists in MP-0** — topology is recorded
as config seams (`run_engine_enabled`, `runs.lease_owner`/`lease_expires_at`,
`run_steps.lease_owner`/`lease_expires_at`) only.

### D3 — request idempotency
Client-generated opaque `Idempotency-Key` is the primary retry identity. It is **not** an
authorization credential. Uniqueness scoped by **`(tenant_id, user_id, session_id,
idempotency_key)`** — MP-0: `runs.idempotency_key` + a partial-UNIQUE index
`uq_runs_idem_scope` on `(COALESCE(tenant_id,''), user_id, COALESCE(session_id,-1),
idempotency_key) WHERE idempotency_key IS NOT NULL`. `runs.idempotency_identity_hash` holds a
canonical, non-secret hash of the **normalized request identity** (never a hash of the raw prompt
alone) so "same key + same identity ⇒ reuse run" / "same key + different identity ⇒ deterministic
conflict" can be enforced by the future submit path. Regenerate ⇒ new key ⇒ new run. Absent key
during a compat window ⇒ server may create a fresh run/key but must **not** claim transport-retry
dedup. The frontend generates/persists the key in a later mini-phase. `/messages/stream` is
**not** touched in MP-0.

### D4 — disconnect & cancellation
Network / SSE disconnect must **not** cancel a durable run. Cancellation is an explicit
authenticated state transition. Future rule set (encoded in the MP-0 state machine, not yet wired
to any route/worker): waiting→cancelled; queued→cancelled; running-cancellable-step→cancelling→
cancelled after acknowledgement; an irreversible external tool already in flight may end
`outcome_unknown`; cancellation must not falsely claim an external side effect was stopped;
duplicate cancel is idempotent (`uq_run_signals_live` — one live `cancel_requested` per run);
cross-user/tenant cancel is rejected. MP-0 = schema + state-machine support + tests only.

### D5 — session revocation
Session/revocation validation is **mandatory** before approval resume or tool dispatch (no
"skip and document"). Evidence: `RefreshSession` (`family_id` indexed, `revoked_at` NULL == active,
`expires_at`); `refresh_token_service._revoke_family()` clears a whole family on logout / reuse.
Binding: `runs.auth_session_ref` holds a `refresh_sessions.family_id` — **never** a bearer/refresh
token or token hash. `app.run_engine.session_binding.session_ref_is_active()` is the fail-closed
predicate (`None` / unknown / revoked / expired ⇒ `False`). **Known gap (MP-1 auth seam,
documented):** `create_access_token` currently mints only `{"sub", "exp", "iat"}` — no
`family_id` claim — so a request handler cannot yet populate `auth_session_ref`. Until that seam
lands, `auth_session_ref` stays NULL and a future resume/dispatch **fails closed**. MP-0 ships the
column + predicate + tests; it does **not** invent a revocation state or resume path.

### D6 — migration order
MP-0 schema → MP-1 RunEngine/worker core (no route cutover) → **MP-2 MCP one-time approval
resume** (first behavioural flow) → MP-3 streaming persistence/reconnect → MP-4
multi-agent/deep-research/Python convergence → MP-5 non-stream `/messages` + scheduler → MP-6
hardening, reconciliation, PostgreSQL matrix, full regression. Do **not** migrate the whole
streaming pipeline first.

### D7 — run-event granularity
No row-per-token. A future writer batches text deltas by configurable byte size **or** max
elapsed time — initial target ≈ 4 KB or 100 ms, whichever first (`run_event_text_flush_bytes` =
4096, `run_event_text_flush_ms` = 100, both validated ≥ 1). Control / security / state events
(`run_events.category = "control"` — approval-required, approval-resolved, transitions,
cancellation, tool lifecycle, terminal, errors) are persisted **immediately** and never wait for
a text batch. Per-run monotonic order via `UNIQUE(run_id, run_seq)`. MP-0 = schema (`category`
column + the unique constraint) + config validation only; no batching logic.

### D8 — Python sandbox
Python sandbox execution must **eventually** route through `PendingApproval` + the Phase 1B.2
gateway + the durable runner. **Not before MP-4.** Nothing in MP-0.

### D9 — scheduler
Scheduled tasks must **eventually** submit durable runs via `RunEngine` instead of invoking the
legacy workflow directly. **Not before MP-5.** Nothing in MP-0.

### D10 — roadmap documentation
The additive Phase 1B status block is **approved** for `THUNDER_AI_PRD.md`. It distinguishes:
implemented · approved-for-progression · design-only · blocked · unverified · deployment-blocker ·
committed/uncommitted. MP-0 existing does **not** make Phase 1B.2R "implemented".

### Secure payload persistence contract (mandatory correction — resolved)

**The apparent contradiction** ("secrets never enter run state" vs "tool arguments are re-parsed
from persisted text") is resolved as:

1. **RunEngine creates no new plaintext copy of tool arguments.** The `[MCP_CALL: … {json args}]`
   marker already lives in the assistant `messages.content` row (the *existing* Message privacy
   model, unchanged). At dispatch (MP-2+) the worker re-parses the args from that Message text and
   re-hashes them — it does **not** copy them into `run_steps` / `run_events`. Duplication &
   retention of assistant/user text stays governed by the `messages` table; RunEngine adds none.
2. **Non-secret references + hashes only** in `runs` / `run_steps` / `run_events` / `run_signals`
   / `action_log` / `trusted_envelope_json`. `run_steps.input_ref` / `output_ref` carry JSON like
   `{message_id, marker_index, tool_id, args_sha256}` / `{output_sha256, output_len, message_id}`.
   Never raw args, credentials, tokens, cookies, headers, private keys, provider configs, or
   unrestricted tool output.
3. **`run_payloads`** is the **only** sanctioned home for run-scoped bytes that (a) are *not*
   already in a `messages` row and (b) could be sensitive. It is sealed with a **versioned,
   context-bound encrypted envelope**. Before encryption, `seal_payload` builds one canonical
   JSON envelope — `{version, tenant_id, user_id, run_id, step_id, payload_type, action_hash,
   payload_encoding:"utf-8", payload}` — serialized with the repo's RFC 8785 canonicalizer
   (`app.approval.canonical`) and encrypted **exactly once** with the raising Fernet cipher
   (`app.services.encryption.get_cipher()` — `cryptography.fernet.Fernet`, AES-128-CBC +
   HMAC-SHA256, authenticated). **Every context field is inside the authenticated ciphertext.**
   On open, `open_payload` decrypts once and fails closed unless the envelope `version` and
   every context field (`tenant_id, user_id, run_id, step_id, payload_type, action_hash`) and
   `payload_encoding` match **trusted server-supplied** expected values (never client input).
   **There is deliberately NO adjacent plaintext digest** — an unkeyed `sha256(plaintext)` next
   to the ciphertext would let an attacker with DB read access offline-guess a low-entropy
   payload; it is unnecessary given Fernet's HMAC and it was **removed**. **Verified limits of
   the encryption service** (checked, not assumed): single 32-byte key from `ENCRYPTION_KEY` env
   / `settings.encryption_key`; raises `RuntimeError` when unset; rotation via `MultiFernet`
   only (re-encrypt under a new primary, decrypt with the old key as fallback) — **no per-row
   key-version**. `run_payloads.enc_scheme` names the *cipher scheme* (`"fernet.v1"`), a
   forward-compat seam — **not** a key-version claim. `app.run_engine.payloads` deliberately does
   **not** reuse `encryption_service.decrypt_key` (which swallows errors and returns `""`); it
   uses the raising cipher directly. **Scope: UTF-8 strings / canonical JSON only** — not
   arbitrary binary.
4. **Provider / MCP credentials** stay where they already live —
   `user_providers.encrypted_api_key` (Fernet) and `mcp_servers.env_json` — referenced from run
   state by id (`config_ref_id`-style), never copied.

**`run_payloads` contract (implemented in MP-0):**

| Property | Value |
|---|---|
| Context binding | a canonical JSON envelope (`version, tenant_id, user_id, run_id, step_id, payload_type, action_hash, payload_encoding, payload`) is encrypted **once**; on open every field is re-checked against TRUSTED server-supplied expected values. Swapping a ciphertext between rows — even together with every mutable column — fails closed (`RunPayloadContextMismatch` / `RunPayloadIntegrityError`). |
| Encryption | `cryptography.fernet.Fernet` via `get_cipher()`; **encrypt-exactly-once, decrypt-exactly-once** (spy-cipher test). Envelope bytes from `app.approval.canonical.canonicalize` (RFC 8785). |
| Integrity | Fernet HMAC-SHA256 over the whole authenticated envelope. **No adjacent unkeyed digest** — removed (offline-guessing risk for low-entropy payloads). |
| Empty payload | `plaintext == ""` → the envelope's `payload` is `""` — no `"\x00"` / whitespace / sentinel; round-trips to `""`. |
| Key version / rotation | MultiFernet primary re-encrypt + old-key fallback (repo's only mechanism; test-proven). `enc_scheme` = cipher-scheme tag, not a key version. |
| Action binding | envelope `action_hash` must equal the trusted `expected_action_hash` — both `None` is a match; any other difference ⇒ `RunPayloadActionHashMismatch` (constant-time compare). |
| `tenant_id` normalization | `""` and `None` both normalize to `null` in the envelope (real owner ids are never empty), so a null owner and an empty owner cannot be confused; a real tenant vs expected-null still fails closed. |
| Size limit | `byte_len` (payload UTF-8 length) `≤ settings.run_payload_max_bytes` (65536) enforced **before** any encryption. `byte_len` is a column OUTSIDE the ciphertext — it **intentionally reveals payload length** and is used only for the size cap + operational reporting; **not confidential**. |
| Expiry / retention | `expires_at` past ⇒ `RunPayloadExpired`; a future sweeper prunes. |
| Deletion | `deleted_at` soft-flag ⇒ `RunPayloadAccessError`; `purge_payload()` hard-deletes (crypto-shred). |
| Failure behaviour (all typed, fail closed) | missing key ⇒ `RunPayloadEncryptionUnavailable`; wrong key / tampered / truncated / malformed token ⇒ `RunPayloadIntegrityError`; non-object / bad JSON / missing or extra field / wrong field type ⇒ `RunPayloadEnvelopeError`; unsupported `version` ⇒ `RunPayloadEnvelopeVersionError`; `payload_encoding != "utf-8"` or invalid UTF-8 ⇒ `RunPayloadEncodingError`; context field mismatch ⇒ `RunPayloadContextMismatch`; action-hash mismatch ⇒ `RunPayloadActionHashMismatch`; unknown `enc_scheme` ⇒ `RunPayloadSchemeError`; expired ⇒ `RunPayloadExpired`; deleted / not found / fast owner pre-check ⇒ `RunPayloadAccessError`. **Never** returns `""`/`None`/partial. A legitimately sealed empty string still round-trips. |
| Redaction / logging | the module never logs ciphertext, plaintext, decrypted-envelope data, args, or secret-bearing exception text (log-capture canary test). |
| Structural guarantee | no `plaintext_sha256` / `*sha*` / `*hash*` column in the clean schema (`action_hash` — a hash of trusted non-secret fields — is the only exception); a secret canary is never present in any `runs`/`run_steps`/`run_events`/`run_signals`/`run_payloads`/`action_log` column after a full lifecycle, nor hashable to any stored value; ciphertext-swap adversarial tests (run/step/user/tenant/tenant-null; action-hash null-in-both and equal-in-both). |
| Obsolete round-1 dev schema | a local dev DB from the earlier MP-0 iteration has `run_payloads` **with** the obsolete `plaintext_sha256 NOT NULL` column, which would reject authoritative inserts — it is **not** inert. `_reconcile_run_payloads_obsolete_column()` (run first, every startup, idempotent): **EMPTY** stale table ⇒ atomically rebuilt to the authoritative 13-column shape (SQLite: explicit `BEGIN IMMEDIATE` — RESERVED lock + rollback-safe DDL; PostgreSQL: transactional `engine.begin()`); indexes restored; `_verify_run_engine_schema()` then hard-fails if any obsolete column survived. **NON-EMPTY** stale table ⇒ **fail fast at startup** with a non-secret, actionable error (old rows' ciphertext predates the envelope format and are never transformed / deleted / copied); the table and every row are left unchanged; deterministic on re-run. Manual drop/recreate is only an operator recovery option after the fail-fast, never the sole protection. **No old payload row is ever silently migrated into the envelope format.** PostgreSQL stale-schema repair is **unverified** (no PG env). |

### `run_signals.payload` allowlist (mandatory correction — resolved)

`run_signals.payload` is **not** an unrestricted JSON field. `app.run_engine.signals` is the only
sanctioned builder/parser. Allowed: `approval_id` (opaque id), `decision` ∈ {`approved`,
`denied`}, `reason_code` (`^[a-z][a-z0-9_]{0,47}$`), `server_ts` (short ISO-8601). Rejected:
raw prompts, raw tool args, credentials, tokens, cookies, headers, unrestricted error text,
nested structures, unknown fields, oversized (> 512 chars), non-object JSON. `parse_signal_payload`
re-validates on read (defence in depth against a row written by a broken path).

---

## 1. Executive decision

**Recommended architecture: Option 2 — a custom, database-backed durable run engine that wraps
and reuses the Phase 1B.2 primitives, introduced via a strangler migration behind fail-safe
feature flags.**

The user-facing chat path (`streaming_service.py`) is a hand-rolled async generator that never
touches LangGraph; the LangGraph path (`langgraph_workflow.py`) compiles **without a
checkpointer**, so its `interrupt()` / `Command(resume=…)` cannot durably resume — "resume" today
is a full graph re-invoke with pseudo-state reconstructed from a message's `citations` JSON.
Phase 1B.2 already built the hard parts of durability: server-owned execution identity
(`execution_turns`, `tool_calls`), at-most-once local dispatch (`dispatch_records` — `UNIQUE` +
CAS), envelope reconstruction (`pending_approvals.trusted_envelope_json`), a tamper-evident
`action_log`, and the trusted gateway with an internal `dispatch_mcp_call_via_gateway` entry.
Option 2 adds a thin `runs` / `run_steps` / `run_events` ledger + a resume worker on top of
those, with **zero new runtime dependencies** and a clean SQLite→PostgreSQL path.

**Rejected now, revisited later:** Option 1 (LangGraph-native durable checkpointer) — it requires
adding `langgraph-checkpoint-sqlite` + `langgraph-checkpoint-postgres` (and, for the PG saver,
`psycopg` v3 alongside the pinned `psycopg2-binary` v2) against a deliberately tightly-pinned
stack built around `langgraph==0.3.2`, `mcp==1.12.4`, `pydantic==2.9.0`, `starlette<0.39`; it
still needs the `dispatch_records` ledger for external at-most-once (LangGraph replays the
interrupt node on resume), so it becomes a *second* durable-execution system rather than a
replacement; and it does not by itself convert the streaming path. It is retained as a **future
consolidation target for the graph-shaped multi-agent flow** once the stack is modernized — the
`run_steps` abstraction is designed not to preclude it.

**Rejected:** Option 3 (Temporal / DBOS / Prefect) — repo evidence shows SQLAlchemy + one worker
loop satisfies every stated requirement; a second orchestration platform adds a server/DB/SDK/ops
surface and lock-in disproportionate to a single-box, self-host-first product.

### Roadmap / gate status (Project Control Center — no dedicated file exists; recorded here and appended to `THUNDER_AI_PRD.md` §Status)

- **Phase 1B.2** — approved for progression (self-host-first scope). Not committed / merged / pushed.
- **Phase 1B.2R** — this design gate. Phase 1B.3 broad tool wiring is **blocked** until the
  durable-run + resume architecture below is approved.
- **Before any Phase 1B commit/push** — the full service-backed backend regression suite
  (live Ollama + ChromaDB, ~23 min) must be run and reported.
- **Before hosted (multi-user) deployment** — PostgreSQL verification of the 1B.2 partial-unique
  dedupe index, ActionLog `SELECT … FOR UPDATE` serialization, dispatch idempotency, the
  active-proposal caps, **and** the new run-lease / resume-signal concurrency is a mandatory
  blocker.

---

## 2. Repository evidence (Stage 0)

All paths relative to repo root. Symbols cited from the working tree at `b2e6718` + uncommitted
Phase 1B.1/1B.2.

### 2.1 User-facing chat request lifecycle

| # | Concern | Evidence (path : symbol / line) | Fact |
|---|---|---|---|
| 1 | Chat request lifecycle | `backend/app/routes/messages.py :: stream_chat` (`@router.post("/api/sessions/{session_id}/messages/stream")`, line 301) | Synchronous setup (`prepare_chat_context`) → returns a `StreamingResponse(event_generator())`. The generator holds ALL run state; nothing durable is written until the terminal `complete`. |
| 2 | `/messages/stream` request contract | `messages.py:305-352`; body `ChatRequest{message, image_url}` (`app/schemas/documents.py`) | **No idempotency key, no turn/run token, no `Last-Event-ID`.** Validations: `session_id > 0`, `len(message) ≤ 10000`. Auth: `get_current_user`; CSRF: `require_csrf`. |
| 3 | SSE / event-stream behavior | `messages.py:357-487`; `streaming_service.format_sse` (`event: <type>\ndata: <json>\n\n`) | Event types: `start`, `plan`, `token`, `agent_status`, `complete`, `error`, `cancelled`. **No `id:` field. No monotonic event id. No replay.** Headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`. Disconnect check: `await request.is_disconnected()` between events → emits `cancelled` and returns. |
| 4 | Message persistence timing | `streaming_service.prepare_chat_context:138-146` (user msg saved up-front); `messages.py:429-453` — assistant `Message` created by the **route**, only after the generator yields `complete`; `message_id` is `None` in every `complete` emitted by the service (`streaming_service.py:642, 748, 884`). `messages.py:329` docstring: "On cancellation, partial responses are NOT saved." | **No persisted assistant-turn / generation record exists during streaming.** A transport retry = a fresh POST that re-runs the LLM. |
| 5 | Session / conversation ownership | `models.py :: ResearchSession` (line 116): `id INTEGER PK`, `user_id INTEGER FK users.id`, `model`, `system_prompt`, naive `created_at/updated_at`. `messages.py :: _get_session_or_404` filters `ResearchSession.user_id == user_id`. | Ownership is `user_id` only. **No `tenant_id` anywhere in the app.** |
| 6 | `streaming_service.py` execution path | `stream_chat_response` (line 660): a hand-rolled `async` generator — `start` → optional `plan` → multi-agent router → CAG semantic-cache short-circuit → provider fallback chain `generate_stream_async` → on `done`: inline `extract_python_code`→`run_python_code_async`; inline `extract_mcp_calls`→`run_mcp_calls` with a fresh `SessionLocal()`; skill-marker load; CAG store; `evaluate_response`; yield `complete`. **Not a LangGraph graph.** `mcp_retry` counter at `streaming_service.py:805-823` is a single pass (no real loop). |
| 7 | `langgraph_workflow.py` graph | `build_workflow()` (line 1628): `StateGraph(WorkflowState)`, 19 nodes, `set_entry_point("load_context")`, linear head → `route_task` conditional (`single_agent`→`generate_answer` / `multi_agent`→`agent_research`), terminal HITL branch `generate_answer`→`ask_terminal_approval`→`execute_terminal`/`skip_terminal`→`regenerate_answer`→`self_evaluate`→`save_output`→`extract_memory`→`END`. **`return workflow.compile()` at line 1741 — NO `checkpointer=` argument.** Compiled singleton `_workflow_app` (line 1745). |
| 8 | Existing `interrupt()` usage | `langgraph_workflow.py :: ask_terminal_approval` (line 1234): `user_decision = interrupt("I'd like to run this command …")`. Comment (line 23): "interrupt() — pause graph, return approval request". | Only one `interrupt()`. It is on the non-streaming `/messages` path. |
| 9 | Existing fake / manual resume | `run_research_workflow(resume_from=…)` (line 1751): reconstructs `pending_command_from_db` from `last_msg.citations` JSON (`meta["pending_approval"]`, `meta["pending_command"]`, line 1794-1799); builds a fresh `initial_state`; calls `_workflow_app.invoke(initial_state, config={"resume_from": Command(resume=resume_from), "recursion_limit": …})` (line 1910). Catches `langgraph.errors.GraphInterrupt` (line 1925) and also detects "interrupt via state" (`pending_command` set & `command_approved is None`, line 1946). `routes/messages.py:139-216` drives it: `_check_pending_approval` reads the last assistant message's `citations`; `_is_approval_response` maps `yes/no`; re-invokes `run_research_workflow`. | **"Resume" re-invokes the WHOLE graph from `load_context`** with reconstructed pseudo-state. `config={"resume_from": Command(resume=…)}` is a **non-standard key**; LangGraph reads `Command` only as the first positional `invoke()` arg, and `Command(resume=…)` **raises `RuntimeError` without a checkpointer** (see §4). So the `Command(resume=…)` here is effectively dead — the real resume is the state reconstruction + node guards (`state.get("regenerate")`, `state.get("pending_command")`). The LLM is re-run. |
| 10 | MCP marker parsing & dispatch | `app/tools/mcp_tool.py`: `extract_mcp_calls(text)` — line-scan regex `_MARKER_RE`, cap `settings.max_mcp_calls_per_response` (3). `run_mcp_calls(...)` — **Phase 1B.2: never auto-executes**; returns a structured blocked / approval-required text block. `dispatch_mcp_call_via_gateway(...)` — the trusted internal path: `open_turn` + `open_tool_call` → `gateway.dispatch(ctx, mcp_name, args, execute_mcp=mcp_service.call_tool)`. Call sites of `run_mcp_calls`: `streaming_service.py:814`, `langgraph_workflow.py:1108, 1366`. | The live path shows "approval required" and executes nothing. Only `dispatch_mcp_call_via_gateway` executes, and it is **not reachable from the live UI** (by design, 1B.2). |
| 11 | Terminal approval | `langgraph_workflow.py :: ask_terminal_approval` (`interrupt()`, non-streaming); `execute_terminal` (line 1268) → `run_command(command)` (`app/tools/terminal_executor.py:80`, `timeout=COMMAND_TIMEOUT_SECONDS`, `shell=…`, inspection allowlist). Streaming path: `[PROPOSED_COMMAND]` markers are **not executed** in `streaming_service.py`. | Two different mechanisms; the durable one (`interrupt`) is not durable (no checkpointer). |
| 12 | Python sandbox | `app/tools/python_sandbox.py :: run_python_code_async` — `asyncio.create_subprocess_exec`, `asyncio.wait_for(timeout=CODE_TIMEOUT_SECONDS≈10s)`, Linux `RLIMIT_AS`/`RLIMIT_CPU` via `preexec_fn`, `os.setsid` process-group kill-tree. Called inline by `streaming_service.stream_chat_response` (line 802) and `stream_multi_agent_response._run_sandbox` (line 503). | Fire-and-forget, no approval, bounded by wall-clock timeout only. Runs inside the request/generator. |
| 13 | Browser-action approval | `langgraph_workflow.py:1128-1175`: `browser_agent.extract_browser_actions`; risky actions (`classify_risky_action`) emit `[BROWSER_APPROVAL_REQUIRED]` + `state["browser_pending_approval"]`; executed only on a follow-up turn whose user message `is_approval(...)`. Comment: "no graph interrupt … turn-based approval". | A third approval style (turn-based marker in message text), non-durable, not wired to Phase 1B. |
| 14 | Deep-research & multi-agent | `streaming_service.stream_multi_agent_response` (line 380) — a **separate** hand-rolled Researcher→Coder→Reviewer pipeline (`_llm` via `asyncio.to_thread(generate_response)`, bounded `MAX_REVIEW_RETRIES`, `MAX_TOKENS_PER_TASK` char budget). `langgraph_workflow` has its own `agent_research`/`agent_code`/`agent_review` nodes. Deep research: `app/tools/deep_research.run_deep_research` invoked in `prepare_chat_context:333` and a `deep_research` graph node. | The multi-agent + deep-research logic is **implemented twice** (once per pipeline) with drift. |
| 15 | Background jobs / scheduler | `app/services/scheduler_service.py`: `AsyncIOScheduler()` with **no jobstore** → default in-memory `MemoryJobStore`. Task *definitions* are durable in `scheduled_tasks` (SQLite) and reloaded on `start_scheduler()` via `load_scheduled_tasks()`. Jobs call `run_research_workflow` (the sync non-streaming path). | Schedule is durable; in-flight job state is not. No general run ledger. |
| 16 | Cancellation / timeouts | Streaming: `request.is_disconnected()` between events (cooperative; a blocked provider call is not interrupted). Sandbox: 10s wall-clock. Terminal: `COMMAND_TIMEOUT_SECONDS`. LangGraph: `config={"recursion_limit": MAX_AGENT_STEPS}`. Char budget: `MAX_TOKENS_PER_TASK=10000`. | No first-class cancel; no run-level timeout; no way to cancel a waiting approval. |
| 17 | DB models & migration strategy | `models.py`: legacy tables use `Integer` PK + **naive** `datetime.utcnow`; Phase 1B tables use `String(36)` UUID PK + tz-aware `_utcnow_aware` + nullable `tenant_id` (no FK) + `user_id` `Integer` (no FK). Migrations: hand-rolled `_migrate_*()` in `app/main.py`, run in `lifespan` via `_migrate_database()`; `create_all()` only creates missing tables; every new column needs a guarded `ALTER TABLE ADD COLUMN`. `_migrate_approval_domain()` / `_migrate_approval_domain_1b2()` are `CREATE TABLE/INDEX IF NOT EXISTS` + guarded `ALTER`, idempotent, SQLite + PG. | Additive, idempotent, dialect-agnostic migrations are the established pattern. No Alembic. |
| 18 | LangGraph / checkpoint deps | `backend/requirements.txt`: `langgraph==0.3.2`, `langgraph-checkpoint==2.0.16`, `langchain-core==0.3.41`, `sqlalchemy==2.0.35`, `psycopg2-binary==2.9.10` (v2), `fastapi==0.115.0` (`starlette<0.39`), `pydantic==2.9.0`, `apscheduler==3.10.4`. **Installed probe:** `langgraph.checkpoint.memory.MemorySaver` present; `langgraph.checkpoint.sqlite` and `langgraph.checkpoint.postgres` **`ModuleNotFoundError`** (separate packages, not installed). `langgraph.func` (`entrypoint`, `task`) and `langgraph.types` (`interrupt`, `Command`) present. No `aiosqlite`, no `asyncpg`, no `psycopg` v3. | The installed set has **no durable checkpointer**. |
| 19 | Phase 1B tables | `models.py` (uncommitted 1B.1/1B.2): `PendingApproval` (+`proposal_dedupe_key`, `execution_turn_id`, `tool_call_id`, `trusted_envelope_json`), `SessionGrant`, `ActionLogEntry`, `ActionLogHead`, `TrustedTool`, `ExecutionTurn` (`id`, `tenant_id?`, `user_id`, `session_id?`, `run_id?`, `assistant_message_id?`, `kind`, `created_at`), `ToolCall` (`UNIQUE(execution_turn_id, call_index)`), `DispatchRecord` (`UNIQUE(tool_call_id)`, `outcome ∈ started|succeeded|failed|incomplete`). Gateway: `app/approval/gateway.py :: dispatch` + `BLOCK_REASONS` + `tool.execution.blocked` + active-proposal caps. | The durable "logical tool call" + "at-most-once dispatch" + "reconstructable envelope" + "tamper-evident audit" layers **already exist**. `execution_turns.run_id` is a nullable free-text column with **no `runs` table behind it yet**. |
| 20 | Frontend streaming consumer | `frontend/src/useStreaming.ts`: `fetch(POST /api/sessions/{id}/messages/stream)` + `response.body.getReader()` + manual `parseSSEBuffer` (`event:` / `data:` lines only). **Not `EventSource`** → no browser auto-reconnect, no `Last-Event-ID`. No reconnect/replay logic. "Prevent double-submission" via a per-hook `abortRef` (does not cover multi-tab). Handles a `tool_approval_required` event (`onToolApproval`) that the backend does **not** currently emit. On network error → `onError({code:"NETWORK_ERROR"})`, done. `cancelStream()` = `AbortController.abort()`. |

### 2.2 Do the two pipelines still differ? — YES.

`/messages/stream` (React default) → `streaming_service.stream_chat_response` (hand-rolled
generator, no LangGraph). `/messages` (non-streaming, backward-compat + scheduler) →
`langgraph_workflow.run_research_workflow` (LangGraph, no checkpointer). They **duplicate** plan
preview, memory/RAG/skill/KG/web/deep-research injection, multi-agent orchestration, python
sandbox, MCP handling, CAG caching, and self-evaluation — each implemented separately, with
observable drift (e.g. streaming multi-agent is a `while` loop in `stream_multi_agent_response`;
graph multi-agent is `agent_*` nodes + conditional edges).

### 2.3 Capability matrix (verified)

| Capability | `/messages/stream` (streaming_service) | `/messages` (langgraph_workflow) | Scheduler |
|---|---|---|---|
| Orchestrator | hand-rolled async generator | LangGraph `StateGraph` | calls `/messages` path |
| Durable checkpointer | none | **none** (`compile()` bare) | n/a |
| Persistent run/turn id before generation | **no** | no (`assistant_message_id=None` until `save_output`) | no |
| Idempotency key on request | **no** | no | job id (in-memory) |
| Token streaming to client | yes (SSE) | no (blocking return) | no |
| SSE event ids / replay | **no** | n/a | n/a |
| HITL approval mechanism | none (MCP shows "approval required" text) | `interrupt()` (needs checkpointer it lacks) | none |
| Cross-request approval resume | **impossible** | pseudo (full graph re-invoke, LLM re-run) | n/a |
| Crash recovery of an in-flight run | none | none | reload schedule only |
| First-class cancellation | cooperative disconnect only | `recursion_limit` only | scheduler stop |
| Retry (side-effect-safe) | none | ad-hoc "Hermes" retry flags | none |
| MCP execution | blocked (1B.2) | blocked (1B.2) | blocked (1B.2) |
| At-most-once tool dispatch | `dispatch_records` (only via `dispatch_mcp_call_via_gateway`, not reachable live) | same | same |
| Multi-agent | `stream_multi_agent_response` (separate) | `agent_*` nodes (separate) | via graph |
| Python sandbox | inline, no approval | inline, no approval | via graph |
| Browser approval | n/a | turn-based marker (non-durable) | n/a |
| Authoritative state holder | the HTTP generator | the in-process graph invocation | the process |

---

## 3. Current pipeline comparison (narrative)

1. **Streaming path is the product.** It is what the React UI calls; it streams tokens; it runs
   python inline; it *would* run MCP inline if 1B.2 hadn't gated it. Its state lives entirely in
   the `event_generator` closure + a `ChatContext` of scalars. Kill the process mid-generation
   and the turn is gone; the user's message is already persisted but no assistant reply, no
   partial output, no "it was working on X".
2. **LangGraph path is a well-structured graph with the *shape* of durability** (nodes, edges,
   one `interrupt()`), **but no persistence**. Because it compiles without a checkpointer:
   `interrupt()` throws `GraphInterrupt` and unwinds; there is no saved state to resume into; the
   "resume" re-runs `load_context → generate_plan → … → generate_answer → ask_terminal_approval`
   from scratch, re-charging the LLM and re-deriving RAG/memory/web context, then re-hitting the
   approval node — now carrying `pending_command` reconstructed from a message's `citations`
   JSON. It works for a trivial `yes/no` terminal command because the command string round-trips
   through the DB; it would **not** correctly resume "the exact approved MCP action with its
   original `action_nonce`" — that is precisely the Phase 1B.2 gap.
3. **Everything hard is already done once, in Phase 1B.2, for the reference path.** `open_turn`
   → `open_tool_call` → `gateway.dispatch` gives a server-owned `execution_turn_id` +
   `tool_call_id`, a reconstructable `ActionEnvelope`, `dispatch_records` at-most-once, and a
   sanitized `tool.execution.*` audit trail. What is missing is (a) a `runs` ledger that owns a
   turn across HTTP requests and process restarts, (b) a worker that advances a run after an
   approval decision without re-running the LLM, and (c) a stream that replays from the DB
   instead of from a live generator.

---

## 4. External official research (primary sources only)

Consulted: **LangGraph** — `/langchain-ai/langgraph` (versions incl. `0.2.74`, matching the
`0.3.2` era) and `/websites/langchain_oss_python_langgraph`; **SQLAlchemy 2.0** docs (row
locking, `with_for_update`); **APScheduler 3.x** docs (jobstores). No non-official sources used.
No paid platform docs consulted beyond a factual comparison note (§5, Option 3), and only because
the audit surfaced the LangGraph dependency cost.

### 4.1 LangGraph — verified facts relevant to this design

| Fact | Source | Implication |
|---|---|---|
| `Command(resume=…)` **raises `RuntimeError("Cannot use Command(resume=...) without checkpointer")`**. | `langgraph/pregel/_loop.py :: _first` (`if not self.checkpointer: raise RuntimeError`) | The repo's current "resume" cannot be LangGraph resume. Any LangGraph-native durable design **requires** a persistent checkpointer. |
| A graph compiled with **no checkpointer** needs no `configurable` and cannot resume; a graph **with** a checkpointer **requires** `configurable.thread_id` (± `checkpoint_ns`, `checkpoint_id`). | `langgraph/pregel/main.py :: Pregel._defaults`; `checkpoint requires … thread_id` | A durable LangGraph design needs a **stable server-owned `thread_id`** per run. |
| Resume after `interrupt()` = `graph.invoke(None, config)` **or** `graph.invoke(Command(resume=value), config)` with the same `thread_id`. | `human_in_the_loop` how-tos; `_first` resume detection (`None` / `Command` / same `run_id`) | Resume is a first-class call, not a re-invoke of the initial input. |
| **On resume, the node containing `interrupt()` re-executes from its beginning.** Guidance: put side effects (`api_call(answer)`) **after** `interrupt()`. | `concepts/human_in_the_loop.md` ("All the code from the beginning of the node to the interrupt will be re-executed") | Even the LangGraph-native path needs the Phase 1B.2 `dispatch_records` ledger to guarantee external at-most-once. A checkpoint alone does **not** give exactly-once side effects. |
| Durable savers: `SqliteSaver` / `AsyncSqliteSaver` in `langgraph-checkpoint-sqlite`; `PostgresSaver` / `AsyncPostgresSaver` in `langgraph-checkpoint-postgres`. `langgraph-checkpoint` (installed) ships only `MemorySaver` (non-durable, in-process). | `libs/checkpoint-sqlite/README.md`; `how-tos/persistence_postgres.ipynb`; installed-package probe | Durable LangGraph needs **new dependencies**. `langgraph-checkpoint-postgres` uses `psycopg` (v3); the repo pins `psycopg2-binary` (v2). |
| Streaming while the graph runs: `graph.stream(input, config, stream_mode=…)` / `astream(...)` with `stream_mode ∈ {"values","updates","messages","custom","debug"}`; resume-streaming = `graph.stream(None, config, stream_mode="values")`. | `wait-user-input.ipynb`; `subgraphs-manage-state.ipynb` | Token streaming from a graph is possible but is a different emit model than the current provider `generate_stream_async`; adopting it is a streaming-path rewrite. |
| `langgraph.func` (`@entrypoint`, `@task`, functional API) exists in the installed `0.3.2`. `@task` results are checkpointed and **not** re-run on resume. | installed probe; `functional_api` docs | If Option 1 were chosen, the functional API (not `StateGraph`) is the lower-friction way to wrap the existing generator — but it still needs a durable saver. |

### 4.2 SQLAlchemy 2.0 — verified facts

- `select(...).with_for_update(skip_locked=True)` (PostgreSQL `SELECT … FOR UPDATE SKIP LOCKED`)
  is the standard primitive for a worker to atomically claim one queued row without contention.
  SQLite has no row locks; `BEGIN IMMEDIATE` (already used by `app/approval/action_log.py ::
  _ChainLock`) serializes writers, which is sufficient for the single-writer self-host case.
- Partial / expression indexes with `WHERE` are already used and verified on both engines in 1B.2
  (`uq_pending_approvals_active_dedupe`).

### 4.3 APScheduler 3.x — verified facts

- `AsyncIOScheduler` defaults to `MemoryJobStore`. A `SQLAlchemyJobStore(url=…)` gives durable
  jobs but pickles the callable and is a poor fit for "resume this specific run step". Better:
  keep APScheduler for *cron scheduling only*, and run the durable-run worker as a dedicated
  `asyncio` task in the FastAPI `lifespan` (or a separate process for hosted).

### 4.4 Comparison note — Temporal / DBOS / Prefect (official docs, factual only)

| System | Durability model | Fit against repo evidence |
|---|---|---|
| **Temporal** | External server (its own DB) + workers; deterministic workflow replay. | Adds a server + DB + SDK + ops; deterministic-replay constraints clash with the existing non-deterministic, side-effecting generator. Disproportionate for single-box self-host. Lock-in. |
| **DBOS Transact** | Library (no server); **Postgres-only** durable steps via a system schema. | Closest fit (library, not a server). But: Postgres-only durability breaks the SQLite self-host dev/default (`database_url = "sqlite:////data/app.db"`); a second execution/decorator model; still needs the 1B.2 ledger for tool at-most-once. |
| **Prefect** | Orchestration server + agents; flows/tasks. | Server + agents; batch-workflow ergonomics, not request-scoped chat turns with token streaming. Overkill + lock-in. |

None is adopted. Option 2's requirements (durable state, approval wait, resume worker, leases,
retry, cancellation, SSE replay, SQLite + PG) are all met by SQLAlchemy tables + one worker loop
+ the existing `action_log` locking pattern.

### 4.5 Verified fact / researched capability / recommendation / assumption / open decision

- **Verified repository fact:** everything in §2 and §3; the LangGraph path has no checkpointer;
  the streaming path is not LangGraph; `dispatch_records` / `execution_turns` / `tool_calls` /
  `trusted_envelope_json` / `action_log` exist; deps as in §2.1-18.
- **Researched framework capability:** §4.1-4.3 (LangGraph `interrupt`/`Command(resume)`/saver
  requirements & node-replay; SQLAlchemy `with_for_update`; APScheduler jobstores).
- **Recommendation:** Option 2 (§1, §6). Strangler migration starting with the MCP-approval flow.
- **Assumption (flagged):** self-host runs a single backend process/worker (the current Docker
  compose model); multi-worker is a hosted/PG concern. The single-tenant `tenant_id = NULL`
  convention from Phase 1B continues.
- **Open decision (user):** see §23. Foremost: adopt a LangGraph durable checkpointer now (add
  `langgraph-checkpoint-sqlite` + `-postgres` + `psycopg` v3) **or** build the custom runner now
  and keep LangGraph checkpointing as a later consolidation. This design assumes the latter.

---

## 5. Options matrix

| Dimension | **Option 1 — LangGraph-native durable** | **Option 2 — custom DB-backed runner (RECOMMENDED)** | **Option 3 — external engine (Temporal/DBOS/Prefect)** |
|---|---|---|---|
| Repository fit | Low today. Streaming path is not a graph; graph path would need a rewrite to stream + to move all side effects after `interrupt()`. | High. Extends the 1B.2 tables + `action_log` locking pattern; both pipelines become thin adapters. | Low. Second orchestration model; deterministic-replay or decorator constraints alien to the current code. |
| New runtime deps | `langgraph-checkpoint-sqlite`, `langgraph-checkpoint-postgres`, `psycopg` v3, `aiosqlite`; likely a `langgraph` bump (0.3.2 is old) → cascade risk with `mcp==1.12.4` / `pydantic==2.9.0` / `starlette<0.39`. | **None.** SQLAlchemy + stdlib `asyncio`. | A server + its client SDK (Temporal/Prefect) or Postgres-only (DBOS). |
| Implementation complexity | High (rewrite streaming emit model; interrupt-node hygiene; two persistence systems to reconcile). | Medium (new tables + state machine + worker + SSE replay; ~6 mini-phases). | High (stand up + operate the platform; port flows; still need the 1B.2 ledger). |
| Migration effort | Large — the graph must be re-authored for durability; streaming rewritten. | Incremental — strangler: MCP-approval flow first behind a flag, other flows migrate one at a time. | Large + parallel infra. |
| Self-host cost | +2-4 pip deps; PG saver drags `psycopg` v3; SQLite saver adds `aiosqlite`; keeps working on one box. | ~0. One extra `asyncio` task in `lifespan`. SQLite stays the default. | New service(s) to run on the self-host box (or Postgres-only). Unacceptable for the "download and run" story. |
| Hosted scaling path | LangGraph PG checkpointer scales with Postgres; multi-worker via `thread_id` routing. | Runner tables on Postgres; workers claim rows via `SELECT … FOR UPDATE SKIP LOCKED`; N workers. Clean. | Scales well but is a separate system to scale/operate. |
| Operational burden | Two schemas to back up / migrate / reason about (checkpoints + 1B.2 ledger). | One additive schema; same migration mechanism (`_migrate_*`), same `action_log` audit. | Highest — a distributed system + its observability. |
| Failure recovery | Checkpoint per super-step; interrupt node replays. Still need `dispatch_records` for tool at-most-once. | Explicit `run_steps` with `status` + `lease`; a restarted worker requeues `running`+expired-lease steps; `dispatch_records` unchanged. | Engine-managed; well understood but opaque. |
| Approval / HITL | Native `interrupt()` — but only after adding a durable saver; approve API still must not execute. | `run` enters `waiting_for_approval`; the approve API (unchanged) signals the run row; the worker resumes. No `interrupt()` needed on the streaming path. | Engine "signal"/"await" — new API surface. |
| Streaming support | Requires switching to `graph.astream(stream_mode="messages"/"custom")`. | `run_events` table + a `GET …/events?after=<id>` replay endpoint; provider `generate_stream_async` output is appended as events. Minimal change to token production. |  Engine-agnostic; you still build the SSE replay. |
| Cancellation | `graph` has no first-class per-run cancel in 0.3.2; cooperative flags. | `cancellation_requests` row + worker checks a cooperative cancel flag between steps and before dispatch; a waiting run cancels immediately. | Native cancel signal. |
| Idempotency | `thread_id` de-dupes a resume; initial submit needs your own key anyway. | Client `Idempotency-Key` → `runs.idempotency_key UNIQUE(user_id, key)`; resume keyed on `run_id` + approval decision; dispatch keyed on `tool_call_id`. | Native workflow-id idempotency + your dispatch ledger. |
| Multi-agent / sub-agent | Subgraphs / `Send` — native, but the streaming multi-agent is not a graph today. | A run can spawn child `run_steps` of kind `sub_run`; the multi-agent loop becomes steps. Or keep it in a graph adapter that the runner drives as one step. | Child workflows — native. |
| SQLite compatibility | `SqliteSaver` works for dev; not recommended for concurrent production. | Full. `BEGIN IMMEDIATE` serialization (already proven in 1B.2). | DBOS: **no** (Postgres-only). Temporal/Prefect: n/a (external DB). |
| PostgreSQL compatibility | `PostgresSaver` — good, needs `psycopg` v3. | Full. `SELECT … FOR UPDATE SKIP LOCKED` for worker claim. Same partial-index pattern as 1B.2. | Native. |
| Vendor lock-in | Medium (LangGraph-specific checkpoint schema + APIs). | **None** (plain tables + your code). | High. |
| Key risks | Dep cascade on a pinned stack; two durability systems; streaming rewrite; `0.3.2` durability ergonomics are immature vs `0.4+`. | You own the state machine + worker correctness; concurrency bugs are yours to test (mitigated by reusing the `action_log` lock pattern + a full concurrency test matrix). | New infra to run + secure + observe; onboarding cost; migration risk. |
| **Recommendation** | Defer. Adopt for the graph-shaped multi-agent flow after a deliberate stack modernization. | **Adopt now.** Smallest thing that satisfies every requirement on the current stack. | Reject. |

---

## 6. Recommended architecture

### 6.1 One canonical orchestration path — the `RunEngine`

```
                       ┌─────────────────────────────────────────────┐
  POST /api/sessions/{id}/messages/stream                            │
  (adapter: streaming_service)  ─────────────►  RunEngine.submit()   │
  POST /api/sessions/{id}/messages                                   │
  (adapter: langgraph_workflow) ─────────────►  RunEngine.submit()   │
  scheduler_service job         ─────────────►  RunEngine.submit()   │
                                                       │             │
                       ┌───────────────────────────────┼───────────┐ │
                       ▼                               ▼           │ │
                  runs / run_steps / run_events   PendingApproval  │ │
                  (durable ledger)                (Phase 1B.1)     │ │
                       │                               ▲           │ │
                       ▼                               │           │ │
                 RunWorker (asyncio task in lifespan;  │ approve   │ │
                 claims steps via lease; advances FSM) ─┘ API      │ │
                       │                                (unchanged)│ │
                       ▼                                           │ │
          ┌────────────┴───────────────┐                           │ │
          │ step: llm_generate  ──────────────►  provider.generate_stream_async
          │ step: tool_dispatch ──────────────►  gateway.dispatch()  (Phase 1B.2)
          │ step: sub_run (multi-agent, deep research, …)             │
          └────────────────────────────┘                             │
                       │                                             │
                       ▼                                             │
        run_events (monotonic id per run)  ◄── GET …/runs/{run_id}/events?after=<id>
                                               (SSE replay; DB is authoritative)
```

- **`RunEngine.submit(session_id, user_id, kind, input_ref, idempotency_key) -> run_id`** — the
  single entry. Creates a `runs` row in `created`, an `execution_turns` row (reused from 1B.2,
  now `run_id`-linked), enqueues, returns the opaque `run_id`. Idempotent on
  `(user_id, idempotency_key)`.
- **`RunWorker`** — one `asyncio` task started in the FastAPI `lifespan` (self-host); a pool of N
  for hosted. It claims the next actionable step under a lease (`SELECT … FOR UPDATE SKIP LOCKED`
  on PG; `BEGIN IMMEDIATE` on SQLite), executes it, appends `run_events`, advances the FSM,
  renews/releases the lease. It NEVER runs inside an HTTP request.
- **Streaming endpoint** becomes a thin **event tail**: `submit()` then loop
  `GET run_events WHERE run_id=? AND id > cursor` and forward as SSE until a terminal event.
  Reconnect = re-open the tail with the last seen `id`. The generator holds **no run state**.
- **The approve API is unchanged** (`/api/approvals/{id}/approve|deny`, `extra="forbid"`, never
  executes). It additionally writes a `run_signals` row (or sets `runs.approval_signal`) that the
  worker observes. **No public `/consume`. No LLM regeneration as resume.**
- **The gateway is unchanged.** A `tool_dispatch` step calls `dispatch_mcp_call_via_gateway`
  (now driven by the worker, not a test) with the persisted `execution_turn_id` + `tool_call_id`;
  `dispatch_records` gives at-most-once; `trusted_envelope_json` gives exact envelope
  reconstruction with the original `action_nonce`.

### 6.2 Why it fits Thunder AI

- **Self-host-first, zero new deps.** One `asyncio` task; SQLite stays the default; the Docker
  build model is untouched.
- **Reuses the security spine.** Approval semantics, envelope hashing, at-most-once dispatch, and
  the tamper-evident `action_log` are the Phase 1B.1/1B.2 code, unmodified. This design is a
  *consumer* of those guarantees, so it cannot weaken them.
- **Converges the two pipelines** without a big-bang rewrite — the strangler (§14) migrates one
  flow at a time behind flags that fail *closed* (flag off ⇒ no MCP execution, exactly as 1B.2
  already ships).
- **Clean hosted path.** Same tables on Postgres; worker pool with `SKIP LOCKED`; the PG
  verification gate already exists for 1B.2 and extends naturally.
- **No lock-in, no second execution model, no second durable store.**

---

## 7. Rejected alternatives (detail)

1. **Option 1 — LangGraph-native durable execution (deferred, not rejected forever).**
   *Rejected as the near-term path because:* (a) it needs new deps against a stack whose pins are
   load-bearing (`mcp 1.12.4` ↔ `pydantic 2.9` ↔ `starlette <0.39` ↔ `fastapi 0.115`), and
   `langgraph 0.3.2` predates the mature `durability=` / task-durability ergonomics of
   `0.4+`/`0.6`; (b) the *product* path (`streaming_service`) is not a graph, so Option 1 still
   leaves it un-durable unless it too is re-authored onto `graph.astream`; (c) LangGraph replays
   the interrupt node, so `dispatch_records` is still required — Option 1 adds a checkpoint
   schema *on top of* the ledger rather than replacing it; (d) two durable stores to migrate,
   back up, and reason about. *Kept for later:* once the stack is modernized, the graph-shaped
   `agent_research/agent_code/agent_review` sub-flow is a good candidate to run under a real
   checkpointer, invoked by the `RunEngine` as a single `sub_run` step. `run_steps.kind` and
   `run_steps.engine` are designed to allow `engine = "langgraph"` for exactly this.

2. **Option 3 — external durable workflow engine.** Rejected: no requirement here cannot be met
   by SQLAlchemy + a worker; every candidate adds an operational/lock-in surface disproportionate
   to a single-box, self-host-first product. DBOS's Postgres-only durability directly breaks the
   SQLite default. Documented in §4.4.

3. **"Just add a checkpointer to the existing graph and call it done."** Rejected: it would make
   `/messages` (non-streaming, backward-compat, scheduler) durable but not the product streaming
   path, deepening the divergence the task asks to eliminate; and it re-runs the whole graph head
   (memory/RAG/web/LLM) on every resume unless nodes are split — a large refactor of a 2000-line
   file — for a worse outcome than Option 2.

4. **Execute the tool from the approve endpoint / add a `/consume` route / in-memory callback.**
   Explicitly forbidden by the Phase 1B gate and by this task. The `RunEngine` design keeps
   authorization (approve API) and execution (worker `tool_dispatch` step) on opposite sides of
   the durable ledger.

---

## 8. Durable state machine

### 8.1 `runs.status`

```
                 ┌──────────────► cancelling ──────────► cancelled
                 │                    ▲                      ▲
 created ─► queued ─► running ─┬──────┼──────────────────────┤
                     ▲   │     │      │                      │
                     │   │     ▼      │                      │
             resuming│   │  waiting_for_approval ─► approved ─┘ (→ resuming → running)
                     │   │     │  │            └─► denied ────────────► failed(reason=denied)
                     └───┘     │  └─────────────► (expired) ──────────► failed(reason=approval_expired)
                               │
   running ──────────────────► succeeded
   running ──────────────────► failed
   running ──────────────────► timed_out
   running/resuming ─────────► outcome_unknown   (crash after external side-effect, pre-persist)
```

**States:** `created`, `queued`, `running`, `waiting_for_approval`, `approved`, `denied`,
`resuming`, `cancelling`, `cancelled`, `succeeded`, `failed`, `timed_out`, `outcome_unknown`.
Terminal: `cancelled`, `denied`, `succeeded`, `failed`, `timed_out`, `outcome_unknown`.

**Legal transitions**

| From | To | Trigger |
|---|---|---|
| `created` | `queued` | `submit()` commits |
| `queued` | `running` | worker claims the run's first step (lease acquired) |
| `queued` | `cancelling` | cancel requested before pickup |
| `running` | `waiting_for_approval` | a step needs a `PendingApproval` and none is authorized (`gateway.dispatch` → `approval_required`) |
| `running` | `succeeded` \| `failed` \| `timed_out` | final step outcome |
| `running` | `cancelling` | cancel requested |
| `running` | `outcome_unknown` | worker crash detected after `tool.execution.started` but before an outcome event (lease expiry + `dispatch_records.outcome ∈ {started, incomplete}`) |
| `waiting_for_approval` | `approved` | approve API writes the signal; worker observes it and re-validates (§15) |
| `waiting_for_approval` | `denied` | deny API signal |
| `waiting_for_approval` | `failed` (`reason=approval_expired`) | server clock passes `PendingApproval.expires_at` (worker sweep) |
| `waiting_for_approval` | `cancelling` | cancel requested while waiting |
| `approved` | `resuming` | worker picks the run back up |
| `resuming` | `running` | resume step re-hydrated; envelope reconstructed + re-validated |
| `resuming` | `failed` (`reason ∈ {policy_changed, tool_relocked, schema_mismatch, session_revoked, owner_mismatch, envelope_mismatch}`) | a §15 re-validation fails at resume |
| `denied` | `failed` (`reason=denied`) | denial is terminal; no execution ever |
| `cancelling` | `cancelled` | worker acknowledges; no pending `tool.execution.started` without an outcome |
| `cancelling` | `outcome_unknown` | cancel raced a dispatch that had already started externally |

**Invalid transitions (must raise / be rejected, tested):** any terminal → non-terminal;
`denied → approved` (or any re-approval of a denied run); `waiting_for_approval → running`
without going through `approved → resuming`; `queued → succeeded` (no work done);
`cancelled → running`; `outcome_unknown → succeeded` (only human reconciliation may move it, and
only to `failed`/`succeeded` with an explicit `reconciled_by`); resuming a run whose
`PendingApproval` is `consumed` by a *different* `tool_call_id`.

### 8.2 `run_steps.status`

`pending` → `running` → (`succeeded` | `failed` | `blocked` | `waiting` | `skipped` |
`incomplete`). `waiting` = the step surfaced an approval requirement (its run is
`waiting_for_approval`). `incomplete` mirrors `dispatch_records.incomplete` (crash mid-dispatch).
A `running` step whose lease expired is eligible for one requeue **only if** it is
side-effect-free (`llm_generate`, `retrieve_context`, …); a `tool_dispatch` step whose
`dispatch_records` row is `started`/`incomplete` goes to `incomplete` and its run to
`outcome_unknown` (no auto-retry).

---

## 9. Data model

New tables (additive; UUID `String(36)` PKs; tz-aware `_utcnow_aware`; `tenant_id VARCHAR(64)
NULL` no-FK; `user_id INTEGER` no-FK — matching the Phase 1B conventions and its migration
helper). **No Phase 1B.2 table is dropped or rewritten.** `execution_turns` is *extended* (guarded
`ALTER ADD COLUMN`), not duplicated.

### 9.1 `runs`

| Column | Type | Notes |
|---|---|---|
| `id` | `String(36)` PK | opaque public `run_id` (uuid4) |
| `tenant_id` | `String(64)` NULL | 1B convention; NULL self-host |
| `user_id` | `Integer` NOT NULL | owner; code-enforced |
| `session_id` | `Integer` NULL | `research_sessions.id` (no FK, matches 1B) |
| `thread_id` | `String(64)` NOT NULL | server-derived (`run:<id>`); the LangGraph `thread_id` seam for a future Option-1 `sub_run` |
| `kind` | `String(24)` NOT NULL | `chat_stream` \| `chat_sync` \| `scheduled` \| `sub_run` |
| `status` | `String(24)` NOT NULL | §8.1; default `created` |
| `status_reason` | `String(64)` NULL | e.g. `denied`, `approval_expired`, `policy_changed` |
| `idempotency_key` | `String(80)` NULL | client `Idempotency-Key`; server-hashed if absent |
| `parent_run_id` | `String(36)` NULL | for `sub_run` |
| `input_ref` | `Text` NOT NULL | JSON of **non-secret** references only: `{user_message_id, image_ref, model_name, provider_ref}` — **never** raw provider config / API keys / full history |
| `current_step_id` | `String(36)` NULL | the step the worker is on / blocked at |
| `pending_approval_id` | `String(36)` NULL | the `PendingApproval` this run waits on |
| `lease_owner` | `String(64)` NULL | worker id holding the run |
| `lease_expires_at` | `DateTime(tz)` NULL | heartbeat deadline |
| `run_deadline_at` | `DateTime(tz)` NULL | hard run-level timeout |
| `last_event_id` | `Integer` NOT NULL | high-water mark for `run_events` (monotonic per run) |
| `created_at` / `updated_at` / `started_at` / `finished_at` | `DateTime(tz)` | |
| `reconciled_by` / `reconciled_at` | `String(64)` / `DateTime(tz)` NULL | for `outcome_unknown` closure |

Indexes: `UNIQUE(user_id, idempotency_key)` **partial** `WHERE idempotency_key IS NOT NULL`;
`ix(status, lease_expires_at)` (worker claim + sweep); `ix(user_id, session_id, created_at)`
(listing); `ix(pending_approval_id)`; `ix(parent_run_id)`; `ix(tenant_id, status)`.
Retention: terminal runs pruned with the `action_log` retention policy (operational, later); no
TTL enforced in code. SQLite: claim/sweep under `BEGIN IMMEDIATE`. PostgreSQL: `SELECT id FROM
runs WHERE status IN ('queued','approved') AND (lease_expires_at IS NULL OR lease_expires_at <
now()) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1`.

### 9.2 `run_steps`

| Column | Type | Notes |
|---|---|---|
| `id` | `String(36)` PK | `step_id` |
| `run_id` | `String(36)` NOT NULL | |
| `tenant_id` / `user_id` / `session_id` | as `runs` | denormalized for owner-scoped queries |
| `seq` | `Integer` NOT NULL | 1-based position within the run |
| `kind` | `String(32)` NOT NULL | `load_context` \| `llm_generate` \| `parse_tools` \| `tool_dispatch` \| `sub_run` \| `finalize` \| … |
| `engine` | `String(16)` NOT NULL | `native` (default) \| `langgraph` (future) |
| `status` | `String(16)` NOT NULL | §8.2 |
| `execution_turn_id` | `String(36)` NULL | FK-by-convention to 1B.2 `execution_turns` |
| `tool_call_id` | `String(36)` NULL | FK-by-convention to 1B.2 `tool_calls` (set for `tool_dispatch`) |
| `approval_id` | `String(36)` NULL | the `PendingApproval` for this step |
| `attempt` | `Integer` NOT NULL | retry counter (side-effect-free steps only) |
| `input_hash` | `String(64)` NULL | canonical hash of the step's trusted inputs (idempotency within the run) |
| `output_ref` | `Text` NULL | JSON of **non-secret** result refs (`{text_len, output_sha256, message_id}`); raw text is streamed as events, not stored here |
| `error_code` | `String(48)` NULL | sanitized; from `gateway.BLOCK_REASONS` where a dispatch was blocked |
| `lease_owner` / `lease_expires_at` | as `runs` | step-level lease for fine-grained recovery |
| `started_at` / `finished_at` / `created_at` | `DateTime(tz)` | |

Indexes: `UNIQUE(run_id, seq)`; `ix(status, lease_expires_at)`; `ix(tool_call_id)`;
`ix(run_id)`; `ix(user_id, session_id)`.

### 9.3 `run_events`

| Column | Type | Notes |
|---|---|---|
| `id` | `BigInteger` PK autoincrement (global) | globally monotonic |
| `run_id` | `String(36)` NOT NULL | |
| `run_seq` | `Integer` NOT NULL | **per-run** monotonic (1,2,3…), assigned under the run lease — this is the client-facing `Last-Event-ID` |
| `tenant_id` / `user_id` | as `runs` | for scope checks on the replay endpoint |
| `type` | `String(32)` NOT NULL | `status` \| `token` \| `plan` \| `agent_status` \| `tool_approval_required` \| `tool_execution_started` \| `tool_execution_succeeded` \| `tool_execution_failed` \| `tool_execution_blocked` \| `run_completed` \| `run_failed` \| `run_cancelled` |
| `data` | `Text` NOT NULL | JSON; **sanitized** — token text and status only; no raw tool output (hash+len), no args, no secrets |
| `created_at` | `DateTime(tz)` | |

Indexes: `UNIQUE(run_id, run_seq)`; `ix(run_id, id)` (replay `WHERE run_id=? AND run_seq > ?`).
Retention: token events are bulky — prune aggressively (e.g. keep 24-48h after the run is
terminal, or collapse to the final `Message` and drop `token` rows). `status`/lifecycle events
kept with the run. **Bounded replay:** the replay endpoint caps `limit` (≤ 2000 events / request)
and refuses `after` values older than the retained window (returns `410 Gone` → client re-fetches
the final `Message`).

### 9.4 `run_signals`

| Column | Type | Notes |
|---|---|---|
| `id` | `String(36)` PK | |
| `run_id` | `String(36)` NOT NULL | |
| `kind` | `String(24)` NOT NULL | `approval_decided` \| `cancel_requested` |
| `payload` | `Text` NULL | `{approval_id, decision}` — decision is `approved`/`denied` only; **no hashes, no envelope** |
| `created_by_user_id` | `Integer` NOT NULL | must equal `runs.user_id` (re-checked by the worker) |
| `consumed_at` | `DateTime(tz)` NULL | worker marks it consumed exactly once |
| `created_at` | `DateTime(tz)` | |

Indexes: `UNIQUE(run_id, kind) WHERE consumed_at IS NULL` (one live signal of a kind per run);
`ix(run_id)`. The approve/deny API and the cancel API each `INSERT … ON CONFLICT DO NOTHING`
(idempotent). Rationale for a table (not a column): keeps the signal producer (request thread)
and consumer (worker) decoupled, gives an audit row, and makes "duplicate approval / duplicate
resume" naturally idempotent.

### 9.5 Extensions to existing Phase 1B.2 tables (guarded `ALTER ADD COLUMN`, nullable)

- `execution_turns` **+`run_id VARCHAR(36)`** (link a turn to its run; existing free-text
  `run_id` stays as the LLM/run correlation string), **+`thread_id VARCHAR(64)`**,
  **+`idempotency_key VARCHAR(80)`**. `ix(run_id)`.
- No change to `tool_calls`, `dispatch_records`, `pending_approvals`, `trusted_tools`,
  `action_log*`. `pending_approvals` already carries `execution_turn_id` + `tool_call_id` +
  `trusted_envelope_json` — the worker reads them, does not add columns.

### 9.6 Per-entity summary

| Entity | PK | Ownership | Lifecycle | Idempotency key | Timestamps | Retention | Key indexes | SQLite | PostgreSQL | Recovery |
|---|---|---|---|---|---|---|---|---|---|---|
| `runs` | uuid | `tenant?,user,session?` | §8.1 | `UNIQUE(user_id, idempotency_key)` partial | created/updated/started/finished | prune terminal w/ audit policy | `(status,lease_expires_at)`, `(user,session,created)` | claim/sweep via `BEGIN IMMEDIATE` | `FOR UPDATE SKIP LOCKED` | expired lease + non-terminal ⇒ requeue (side-effect-free) or `outcome_unknown` |
| `run_steps` | uuid | denormalized from run | §8.2 | `UNIQUE(run_id,seq)`, `input_hash` | started/finished/created | with the run | `(status,lease_expires_at)`, `(tool_call_id)` | same | same | side-effect-free step requeues; `tool_dispatch` defers to `dispatch_records` |
| `run_events` | bigint | `tenant?,user` | append-only | `UNIQUE(run_id,run_seq)` | created | short (tokens), long (status) | `(run_id,id)` | serialized append under run lease | same | replay is read-only; nothing to recover |
| `run_signals` | uuid | `user` (== run.user) | insert→consumed | `UNIQUE(run_id,kind) WHERE consumed_at IS NULL` | created/consumed | with the run | `(run_id)` | insert-or-ignore | `ON CONFLICT DO NOTHING` | unconsumed signal re-observed on worker restart |
| `execution_turns` (ext) | uuid (existing) | existing | existing + `run_id` link | existing | existing | existing | `+ix(run_id)` | existing | existing | existing |

---

## 10. Identity hierarchy (final, server-owned)

```
tenant_id            (NULL self-host; String(64); no FK; from auth context only)
  └─ user_id         (Integer; from get_current_user; the ownership root)
      └─ session_id / conversation_id   (research_sessions.id; user-scoped)
          └─ thread_id                  ("run:<run_id>"; server-derived; the LangGraph seam)
              └─ run_id                 (uuid4; public opaque; one durable unit of work)
                  ├─ execution_turn_id  (1B.2; uuid4; internal; one generation turn)
                  │   └─ tool_call_id   (1B.2; uuid4; internal; one logical tool call;
                  │        identity = the tool_calls row UUID, NOT call_index)
                  │        └─ dispatch_attempt_id  (= dispatch_records.id; 1B.2;
                  │             UNIQUE(tool_call_id) ⇒ at-most-once local)
                  ├─ step_id            (run_steps.id; uuid4; internal)
                  ├─ approval_id        (pending_approvals.id; 1B.1; referenced by the client
                  │                      only as an opaque id it already owns; never trusted
                  │                      for hashes/envelope)
                  ├─ checkpoint_id      (RESERVED — only exists if a future sub_run uses a
                  │                      LangGraph checkpointer; keyed under thread_id)
                  └─ stream_event_id    (run_events.run_seq; per-run monotonic; the
                                         client-facing Last-Event-ID)
```

| Id | Public opaque ref? | Internal only? | Accepted from client? | Always server-derived? |
|---|---|---|---|---|
| `tenant_id` | no (from token) | — | no | yes (auth context) |
| `user_id` | no | — | no | yes (`get_current_user`) |
| `session_id` | yes (already in URLs) | — | as a reference, ownership re-checked every request | — |
| `run_id` | **yes** | — | as a reference (resume/cancel/stream), ownership re-checked | yes (uuid4 at `submit()`) |
| `thread_id` | no | yes | no | yes (`"run:<run_id>"`) |
| `execution_turn_id` | no | yes | no | yes (`open_turn`) |
| `step_id` | no | yes | no | yes |
| `tool_call_id` | no | yes | no | yes (`open_tool_call`) |
| `dispatch_attempt_id` | no | yes | no | yes (`gateway._do_dispatch`) |
| `approval_id` | yes (client already lists its own) | — | as a reference on the approve/deny API (owner-filtered → 404) | — |
| `checkpoint_id` | no | yes | no | yes (future) |
| `stream_event_id` (`run_seq`) | **yes** (as `Last-Event-ID`) | — | as an integer cursor on the replay endpoint, clamped | yes (assigned under the run lease) |

**Ownership verification.** Every run-addressed endpoint loads `runs` by `id` and rejects
(`404`, non-disclosing) unless `run.user_id == current_user.id` **and**
`(run.tenant_id or None) == (auth tenant or None)`. The worker re-checks `run_signals.created_by_user_id
== run.user_id` before consuming a signal. A resume re-runs the full §15 revalidation set.

**Transport retry reuses the same logical run.** The client sends `Idempotency-Key` on the
initial POST. A retried POST with the same `(user_id, key)` returns the existing `run_id` and its
current event cursor — it does **not** create a second run or re-run the LLM. If the key is
absent, the server derives one from a canonical hash of `(user_id, session_id, user_message_id)`
so an immediate accidental double-submit still de-dupes (documented as best-effort — a genuinely
new message with identical text in the same session within the dedupe window is treated as the
same run; the window is short and configurable).

**A regenerated response is a different run.** "Regenerate" from the UI is a new POST **without**
the prior `Idempotency-Key` (or with an explicit `regenerate=true` that forces a fresh key) →
new `run_id`, new `execution_turn_id`. The old run stays terminal. `proposal_dedupe_key` (1B.2)
already binds `run_id` + `execution_turn_id` + `tool_call_id`, so an approval for the old run
cannot authorize the new one.

**Multi-tab / duplicate submit.** Two tabs, same message, same `Idempotency-Key` (the key is
derived per user action, stored with the optimistic UI message) → both attach to one `run_id` and
both tail the same `run_events`. Two tabs, *different* actions → two runs, isolated. Without a
shared key (older client), the server-derived hash still collapses an accidental rapid double
POST; a deliberate "send the same text twice" makes two runs (acceptable — it is two user
actions).

**Cross-tenant / cross-user resume is impossible.** The resume/cancel/stream endpoints 404 on a
non-owned `run_id`; the worker's signal-consume re-checks the owner; the gateway's
`consume_approval` (1B.2) independently re-checks user/tenant/session/run and refuses a
cross-boundary `approval_id`.

---

## 11. Approval-resume sequence (end-to-end)

```
CLIENT                         API (request thread)                 DB (authoritative)                 RUN WORKER (asyncio)                 GATEWAY (1B.2)
  │  POST /messages/stream                                                                                                                     │
  │  Idempotency-Key: K       ─────────►  RunEngine.submit()                                                                                    │
  │                                       ├─ upsert runs(status=created, idem=K)  ──►  runs row                                                  │
  │                                       ├─ open_turn() ─────────────────────────►  execution_turns(run_id)                                     │
  │                                       └─ runs.status=queued, return run_id                                                                  │
  │  ◄── 200; body = SSE tail of run_events?after=0                                                                                             │
  │                                                                            ◄── claim run (lease)  ──  worker: runs.status=running           │
  │                                                                                                       step llm_generate:                    │
  │  ◄── event token …            (tail forwards run_events)  ◄── run_events(token) ◄──  provider.generate_stream_async  (worker appends)        │
  │                                                                                                       step parse_tools: extract_mcp_calls   │
  │                                                                                                       step tool_dispatch:                   │
  │                                                                                                         open_tool_call(turn, idx, tool, h) ─┼─► tool_calls
  │                                                                                                         gateway.dispatch(ctx, name, args)   │
  │                                                                                                           registry.resolve_trusted OK       │
  │                                                                                                           build+persist ActionEnvelope ─────┼─► pending_approvals(.trusted_envelope_json, .action_nonce)
  │                                                                                                           request_approval (dedupe) ────────┼─► PendingApproval(state=requested)
  │                                                                                                           no approval, no grant             │
  │                                                                                                         ◄── returns approval_required       │
  │                                                                                                       worker: runs.status=waiting_for_approval,
  │                                                                                                               runs.pending_approval_id=A     │
  │  ◄── event tool_approval_required {approval_id:A}                                                                                            │
  │  ◄── event run_completed? NO — tail stays open OR client may close; DB holds state                                                           │
  │  (client may disconnect here — nothing is lost)                                                                                             │
  │                                                                                                                                            │
  │  GET /api/approvals  (lists A, owner-filtered)                                                                                              │
  │  POST /api/approvals/A/approve  (unchanged; extra=forbid; NEVER executes)                                                                   │
  │                              ├─ record_decision(A, approve)  ──►  A.state=approved + action_log(approval.approved)                            │
  │                              └─ INSERT run_signals(run_id, kind=approval_decided, {A, approved})  ON CONFLICT DO NOTHING                      │
  │  ◄── 200 {state: approved}                                                                                                                  │
  │                                                                            ◄── worker sweep sees run_signals / A.state                       │
  │                                                                                worker: re-check owner==run.user_id;                          │
  │                                                                                        runs.status=approved → resuming                       │
  │                                                                                        §15 revalidation:                                     │
  │                                                                                          - authenticated owner still valid? (session store)  │
  │                                                                                          - A.state==approved, not expired?                    │
  │                                                                                          - trusted_tools row still enabled+unlocked?          │
  │                                                                                          - tool_schema_hash / server_fingerprint unchanged?   │
  │                                                                                          - policy_version unchanged?                          │
  │                                                                                          - reconstruct_envelope(A) → action_hash matches      │
  │                                                                                        any fail ⇒ runs.status=failed(reason=…), event, stop   │
  │                                                                                resume the SAME tool_dispatch step (same step_id/tool_call_id) │
  │                                                                                gateway.dispatch(ctx, name, args)  ────────────────────────────┼─►
  │                                                                                  A.state==approved →                                          │
  │                                                                                    reconstruct+re-hash envelope from action_nonce             │
  │                                                                                    consume_approval (CAS: approved→consumed) ────────────────┼─► action_log(approval.consumed)
  │                                                                                    DispatchRecord CAS insert (UNIQUE tool_call_id) ──────────┼─► dispatch_records(outcome=started)
  │                                                                                    action_log(tool.execution.started)                         │
  │                                                                                    execute_mcp()  ← the ONLY tool invocation                  │
  │                                                                                    action_log(tool.execution.succeeded|failed) + hash/len     │
  │                                                                                  ◄── GatewayResult(executed)                                  │
  │                                                                                worker: append run_events(tool_execution_succeeded),           │
  │                                                                                        continue run (next llm_generate w/ tool result),       │
  │                                                                                        … → finalize → save Message → runs.status=succeeded     │
  │  (client reconnects) GET /api/sessions/{id}/runs/{run_id}/events?after=<last_seen_run_seq>                                                    │
  │  ◄── replays token/status events from the DB, then run_completed {message_id}                                                                │
```

**Step numbering against the task's 16-point list:** 1 = `submit()` + `Idempotency-Key`;
2 = `open_turn()` before `llm_generate`; 3 = `llm_generate` step; 4 = `registry.resolve_trusted`
+ `_classify`; 5 = `request_approval` (dedupe); 6 = `runs.status=waiting_for_approval`;
7 = client disconnect is a no-op; 8 = approve/deny API (unchanged); 9 = `run_signals` +
`A.state` observed by the worker (internal, not an execute call); 10 = worker resumes the **same
`step_id` / `execution_turn_id` / `tool_call_id`**; 11 = `reconstruct_envelope` + re-hash;
12 = `consume_approval` (CAS, internal); 13 = `execute_mcp` via `gateway.dispatch`;
14 = `tool.execution.*` + `run_events`; 15 = run continues to a terminal state; 16 = client
reconnects to the replay endpoint.

**Rules honored:** approve API never invokes a tool (it writes a signal row); no `/consume`
route; no LLM regeneration presented as resume (the resume re-enters the *same* persisted step);
no in-memory callback (the worker reads the DB); no client-controlled hashes/envelope (the worker
reconstructs from `trusted_envelope_json` + `action_nonce`); a backend restart mid-wait loses
nothing (`runs` + `run_signals` are durable); a denial is terminal (`runs.status=denied → failed`;
`A.state=denied`; the `tool_call_id` also carries a sticky DENIED in the gateway); an expired
approval durably blocks (worker sweep → `failed(reason=approval_expired)`; `gateway.dispatch`
independently blocks on expiry); resume is tenant/user/session scoped (owner re-check + gateway
identity binding).

---

## 12. Side-effect & idempotency contract

**No global exactly-once is claimed.** The guarantee is **local at-most-once per persisted
`tool_call_id`**, provided by Phase 1B.2 unchanged (`dispatch_records UNIQUE(tool_call_id)` +
CAS insert + the early gateway check), now driven by the durable worker instead of a test.

### 12.1 Crash / race scenarios and required behavior

| Scenario | Detection | Required behavior |
|---|---|---|
| Crash **before** `consume_approval` | run stuck `resuming`/`approved`, lease expired; `A.state=approved`; no `dispatch_records` row | Requeue the resume step (side-effect-free so far). `consume_approval` CAS still available. |
| Crash **after** `consume_approval`, **before** `dispatch_records` insert | `A.state=consumed`, no `dispatch_records` row | The run's `tool_call_id` is bound to a consumed approval but not dispatched. Worker on requeue: `gateway.dispatch` sees `A.state=consumed` (not `approved`) and **no** dispatch record → it must **not** re-consume and must **not** silently execute; it returns `blocked(approval_consumed)` and the run goes to `outcome_unknown` (the authorization was spent; whether the tool ran is unknown). Human reconciliation. *(This is a narrow window; §24 Mini-phase 3 tightens it by making the `dispatch_records` insert part of the same transaction as `consume_approval`'s CAS where the DB allows.)* |
| Crash **after** `dispatch_records.started`, **before** the tool returns | lease expired; `dispatch_records.outcome ∈ {started, incomplete}` | `run_steps` → `incomplete`; `runs` → `outcome_unknown`. **Never auto-retried.** `run_events(tool_execution_failed, reason=outcome_unknown)`. Reconciliation workflow (§12.4). |
| Crash **after** the external side effect, **before** the success event | same as above (`dispatch_records.started`) | Same: `outcome_unknown`. The design cannot know the tool succeeded. Honest state, not `succeeded`. |
| Duplicate worker (two workers claim the same run) | lease `UNIQUE`/`FOR UPDATE`; second claim fails | Loser backs off. On SQLite the single-writer model + `BEGIN IMMEDIATE` prevents it; on PG `SKIP LOCKED` + `lease_owner` CAS. |
| Duplicate resume signal | `run_signals UNIQUE(run_id, kind) WHERE consumed_at IS NULL` | Second insert is a no-op; worker consumes once (`consumed_at` CAS). |
| Duplicate approval request (client re-POSTs approve) | `record_decision` is idempotent (1B.2: `{idempotent:true}`); `run_signals` upsert is `ON CONFLICT DO NOTHING` | No effect. |
| Stale worker lease | `lease_expires_at < now()` on a `running` run/step | Sweeper requeues side-effect-free work; defers `tool_dispatch` to `dispatch_records` truth. |
| Timeout during tool execution | worker per-step timeout wraps `gateway.dispatch`; `run_deadline_at` for the whole run | Step → `failed`/`incomplete` (if a dispatch had started → `incomplete` + `outcome_unknown`); else `timed_out`. |
| Provider retry (LLM 5xx) | `llm_generate` step is side-effect-free | Auto-retry up to `max_llm_attempts` (config) with backoff; each attempt appends fresh `token` events with a new `attempt` tag; the client tail shows a `status` event `regenerating`. |
| Application restart | `lifespan` starts the worker; sweeper runs on boot | `queued`/`approved` runs resume; `running` with expired lease per the rules above; `waiting_for_approval` untouched. |
| DB reconnect | SQLAlchemy pool handles it; the worker retries the claim loop | No run lost (state is in the DB). |
| SSE reconnect | client re-opens the replay endpoint with `Last-Event-ID` | Bounded replay from `run_events`; see §13. |
| Cancel racing approval | both write `run_signals`; worker processes `cancel_requested` with priority | If not yet dispatched → `cancelled`. If `dispatch_records.started` → `outcome_unknown` (cancel cannot un-ring a started side effect). |
| Cancel racing `tool_dispatch` | worker checks a cooperative cancel flag **immediately before** `gateway.dispatch`; the gateway call itself is not interruptible | Pre-dispatch → `cancelled`. Post-`started` → `outcome_unknown`. |

### 12.2 Which operations may be auto-retried

- **Auto-retry (bounded, backoff):** `load_context`, `llm_generate`, `parse_tools`,
  `retrieve_*`, `finalize`, and any `run_step` with no external side effect and an expired lease.
- **Never auto-retry:** any `tool_dispatch` whose `dispatch_records.outcome ∈ {started, failed,
  incomplete}`; a resume past a spent `consume_approval`. These require human reconciliation.

### 12.3 External idempotency-key propagation

When the trusted registry marks a tool as `idempotency_capable` (a new **non-secret**
`trusted_tools.risk_metadata_json` field, operator-set), the worker passes a derived
`Idempotency-Key = sha256("thunder.tool_dispatch" || tool_call_id)` to `execute_mcp` via a
gateway pass-through arg. The gateway does **not** interpret it; the MCP tool/server may. This is
best-effort and advertised as such; the local `dispatch_records` guarantee stands regardless.

### 12.4 Reconciliation workflow (`outcome_unknown`)

1. `runs.status = outcome_unknown`; `run_events(run_failed, reason=outcome_unknown)`;
   `action_log` already holds `tool.execution.started` with `execution_turn_id` / `tool_call_id`
   / `action_hash` / `mcp_server_id`.
2. A new **owner-scoped** endpoint `GET /api/sessions/{id}/runs?status=outcome_unknown` lists
   them with the non-sensitive correlation fields.
3. `POST /api/sessions/{id}/runs/{run_id}/reconcile {resolution: succeeded|failed, note}` —
   requires the owner; writes `runs.reconciled_by/at`, `run_steps` final status, and an
   `action_log` `run.reconciled` event. It **does not** re-execute anything. (Design-only here;
   built in Mini-phase 5.)
4. Metric: `runs_outcome_unknown_total` (§19) — a spike is an operational alarm.

### 12.5 Lease / heartbeat

- `lease_ttl` (config, default 60s). The worker renews `runs.lease_expires_at` and
  `run_steps.lease_expires_at` every `lease_ttl/3` while a step runs.
- A `tool_dispatch` step's lease is renewed *around* the `gateway.dispatch` call on a background
  timer; if the process dies, the lease simply expires and the sweeper applies §12.1.
- The sweeper runs every `sweep_interval` (config, default 30s) and on boot.

---

## 13. Streaming / reconnect design

- **Persisted event sequence.** Every client-visible event is a `run_events` row with a per-run
  monotonic `run_seq` assigned **while the worker holds the run lease** (so no gaps, no
  reordering — same discipline as the `action_log` chain's `UNIQUE(chain_scope_id, seq)`).
- **Monotonic ids.** `run_seq` is the client-facing id. The global `id BIGINT` is for efficient
  range scans and cross-run ordering only.
- **Transport.** Keep the current `fetch` + `ReadableStream` POST (no switch to `EventSource`).
  The POST response is the live tail. Add a **separate** `GET
  /api/sessions/{id}/runs/{run_id}/events?after=<run_seq>&wait=<ms>` long-poll/SSE replay
  endpoint for reconnect. The frontend sends the last `run_seq` it rendered as `after` (a
  `Last-Event-ID`-equivalent it manages itself, since it is not using `EventSource`).
- **Bounded replay.** `after` older than the retained token window → `410 Gone`; the client then
  `GET /api/sessions/{id}/messages` to load the finalized `Message` and any `status` events still
  retained. `limit` per replay call ≤ 2000.
- **Backpressure.** The tail reads `run_events` in pages; if the client is slow, the worker is
  unaffected (it writes to the DB, not to the socket). The tail drops nothing — it just lags.
- **Disconnect behavior.** The POST tail closing does **not** cancel the run (unlike today's
  `request.is_disconnected()` → `cancelled`). The run continues on the worker; the user can
  reconnect or read the final `Message`. Explicit cancel is a separate `POST
  …/runs/{run_id}/cancel`.
- **Partial assistant-output persistence.** `token` events ARE the partial output, in the DB. On
  `finalize`, the worker concatenates them into the `Message` (content) and marks the run
  terminal. A crash mid-generation leaves the `token` rows; on restart the worker either resumes
  `llm_generate` (if the provider call is safely re-runnable — it re-streams, and the tail
  de-dupes by `run_seq`) or, if generation had completed, moves to `finalize`.
- **Final message materialization.** Exactly one `Message` row per run, written by `finalize`
  under the run lease, id emitted in `run_completed`. `run_events` `token` rows may then be
  pruned.
- **Duplicate event suppression.** The client tracks the max `run_seq` rendered and ignores any
  `run_seq ≤ that` (covers a reconnect overlap).
- **Cancellation.** `run_cancelled` is a terminal event; the tail forwards it and closes.
- **Client-visible status events.** `status` events carry `{phase: queued|running|
  waiting_for_approval|resuming|cancelling|finalizing, detail}` so the UI can show "waiting for
  your approval" without inventing it from token text.
- **Approval events.** `tool_approval_required {approval_id}` (the frontend already has
  `onToolApproval`), then after resume `tool_execution_started` / `_succeeded` / `_failed` /
  `_blocked` (sanitized — reason code + hashes/len only).
- **Terminal run event.** Exactly one of `run_completed` / `run_failed` / `run_cancelled`, always
  last.

**The DB is authoritative.** The HTTP generator holds no run state; it is a windowed reader of
`run_events`. Killing every open connection loses nothing.

---

## 14. Pipeline-convergence plan (strangler)

**Canonical path:** `RunEngine` + `RunWorker` + `run_steps`. `streaming_service` and
`langgraph_workflow` become **adapters** that (a) build the step list for a run and (b) expose
the existing token-production / node logic as step bodies.

### 14.1 Adapter boundary

```python
# conceptual — NOT code to write in this phase
class RunAdapter(Protocol):
    def plan_steps(self, run: Run) -> list[StepSpec]: ...
    def run_step(self, run: Run, step: RunStep) -> StepResult: ...   # side-effect-free where possible
```

- `ChatStreamAdapter` wraps the current `stream_chat_response` body: `plan_steps` →
  `[load_context, llm_generate, parse_tools, tool_dispatch*, finalize]`; `run_step("llm_generate")`
  calls the existing provider fallback chain and appends `token` events; `run_step("tool_dispatch")`
  calls `gateway.dispatch`.
- `ChatSyncAdapter` / `langgraph_workflow` → initially unchanged (still runs the graph in one
  step of kind `sub_run`, `engine="native"`), so `/messages` keeps working during migration.
- `MultiAgentAdapter` / `DeepResearchAdapter` → later, each becomes a `sub_run` or a set of
  steps; the duplicated logic is deleted from `streaming_service` once the adapter is the only
  caller.

### 14.2 Migration order

1. **MCP-approval flow on `/messages/stream`** (the actual gap). Flag `run_engine_mcp` (default
   **off** ⇒ current 1B.2 "approval required, no execution" behavior — fail-closed). On ⇒ the
   MCP `tool_dispatch` step runs through the worker and can be resumed.
2. **All of `/messages/stream`** through the `RunEngine` (token streaming via `run_events`). Flag
   `run_engine_stream`. Fail-closed: off ⇒ today's generator.
3. **Python sandbox** as a `tool_dispatch`-style step behind the gateway pattern (an approval
   surface for code execution; today it has none). Flag `run_engine_sandbox`.
4. **Multi-agent + deep research** collapse to one implementation (adapter/sub_run); delete the
   `streaming_service` copies.
5. **`/messages` (non-streaming)** and the **scheduler** re-pointed at `RunEngine.submit()` +
   wait-for-terminal; delete `run_research_workflow`'s bespoke resume.
6. **Browser automation** approval routed through `PendingApproval` + a `tool_dispatch` step;
   delete the turn-based marker path.

### 14.3 Compatibility period, gates, removal criteria

- Each flag ships **off**. A flag turns on only after its Mini-phase test gate (§18) is green on
  SQLite, the eval tasks (§17) for that flow pass, and (for hosted) the PG matrix passes.
- Legacy path removal criterion per flow: flag on by default for ≥ 1 release with no
  `outcome_unknown` spike and no regression in the eval suite ⇒ delete the legacy branch and its
  duplicated logic in the next release.
- **No indefinite dual sources of truth:** the design forbids a state where both the generator
  and `run_events` are authoritative for the same flow — once a flow's flag is on, the generator
  for that flow is a pure reader.

---

## 15. Security threat model

| Threat | Vector | Mitigation (design) |
|---|---|---|
| Cross-tenant / cross-user run or checkpoint access | guess/enumerate `run_id`; call resume/cancel/stream for another user | `run_id` is uuid4 (unguessable); every run-addressed endpoint 404s unless `run.user_id == current_user.id` and tenant matches; worker re-checks `run_signals.created_by_user_id`; `run_events` replay filters by owner. No LangGraph checkpoint in the recommended design ⇒ no checkpoint-namespace attack surface; if a future `sub_run` adds one, `thread_id = "run:<run_id>"` inherits the same 404 gate and the checkpointer is never queried with a client-supplied `thread_id`. |
| `thread_id` guessing | derive another run's `thread_id` | `thread_id` is `"run:<uuid4>"`, internal only, never accepted from the client. |
| Forged resume request | POST a fabricated resume / signal | There is no "resume" endpoint that takes trusted state — resume is triggered only by `record_decision` (owner-checked, CSRF) writing `run_signals`; the worker independently re-checks owner and re-runs §15 revalidation. |
| Duplicate resume | replay the approve POST | `record_decision` idempotent; `run_signals UNIQUE(run_id, kind) WHERE consumed_at IS NULL`; `consume_approval` CAS `approved→consumed`; `dispatch_records UNIQUE(tool_call_id)`. |
| Stale approval | approve, wait past expiry, then the worker resumes | Worker revalidation checks `A.expires_at` (server clock); `gateway.dispatch` independently blocks `approval_expired`; a sweeper moves long-waiting runs to `failed(reason=approval_expired)`. |
| Checkpoint poisoning | tamper stored run/step/checkpoint state to smuggle args/authorization | Recommended design stores **no** executable state — `run_steps.input_ref` / `output_ref` are non-secret reference JSON; the tool's real args are re-parsed from the persisted assistant text + re-hashed at dispatch; authorization comes only from `PendingApproval` + `trusted_envelope_json` (hash-bound, 1B.2). Tampering `run_steps` cannot change what executes because the envelope is reconstructed and re-hashed against `A.action_hash`. |
| Replay of an old `ActionEnvelope` | resubmit a stale envelope | 1B.2 `consume_approval` re-canonicalizes and compares `action_hash`; `action_nonce` is single-use; `dispatch_records` blocks a second dispatch of the same `tool_call_id`. |
| Privilege / policy change while a run waits | operator disables a tool, bumps `policy_version`, re-locks after a schema change, while `A` is `approved` | **Every resume re-validates:** `trusted_tools` row still `enabled` and `locked=False`; `tool_schema_hash` and `server_fingerprint` unchanged vs. `A`; `A.policy_version` unchanged vs. current; else `runs.status=failed(reason ∈ {tool_disabled, tool_relocked, schema_mismatch, fingerprint_mismatch, policy_changed})` + a `tool.execution.blocked` audit event. (1B.2 `gateway.dispatch` already blocks these; the worker checks them *before* calling the gateway so the run fails cleanly with a precise reason.) |
| Session logout / token revocation while waiting | user logs out / session revoked, then the (already-persisted) approval decision resumes | Worker revalidation calls an auth-service check that the run's owner still has an active session (see §23 open decision — the repo has refresh-token revocation via `refresh_token_service`; the check reuses it). If the owner's session is revoked ⇒ `runs.status=failed(reason=session_revoked)`; no execution. |
| Secret leakage via run/checkpoint state or logs | provider API keys, `provider_config`, raw args, raw tool output land in `runs`/`run_steps`/`run_events`/`action_log` | Hard rule: those tables store only non-secret references and hashes. `run_events` token data is model output text only (already user-visible); tool output is **never** in an event — only `output_sha256` + `output_len` (1B.2 pattern). `input_ref` names a `provider_ref` (a pointer to the user's stored, encrypted provider config — `encryption_service`), never the config itself. A test asserts no `sk-`/`Bearer`/`api_key` pattern in any of the four tables after a full lifecycle. |
| Raw tool output in logs | worker logs the `GatewayResult.tool_output` | The worker logs only `reason_code` + hashes; `GatewayResult.tool_output` is forwarded to the run's `finalize` step as model context and to the `Message`, not to logs. |
| Unbounded run creation (DoS) | script POSTs thousands of runs | New per-user caps (config, validated ≥ 1, not client-settable): `max_active_runs_per_user`, `max_active_runs_per_session`, `max_queued_runs_per_user`. `submit()` fails closed with `run_rejected(reason=run_limit)` + a bounded audit signal — mirrors the 1B.2 active-proposal cap design. The 1B.2 `max_active_proposals_per_*` caps still bound approvals underneath. |
| Unbounded event replay (DoS) | GET `events?after=0` on a huge run repeatedly | `limit ≤ 2000` per call; `after` clamped to the retained window (`410` past it); replay endpoint is owner-scoped and rate-limited by the existing `rate_limiter`. |
| DoS via abandoned waiting runs | create runs that wait forever on approval | `waiting_for_approval` runs inherit `A.expires_at`; a sweeper fails them at expiry; `max_active_runs_per_user` counts `waiting_for_approval`; a configurable `max_wait_ttl` caps it regardless of `A`. |
| Worker as a confused deputy | a step executes with more authority than the requesting user | The worker never elevates: it acts strictly with `run.user_id` / `run.tenant_id`; `gateway.GatewayContext` is built from the run row, not from any client input. |
| Cancellation abuse | cancel another user's run | `POST …/runs/{run_id}/cancel` is owner-checked (404 otherwise) + CSRF. |

**Every resume revalidates, in order, before the worker calls `gateway.dispatch`:**
1. authenticated owner still valid (session not revoked);
2. `run.user_id` / `run.tenant_id` unchanged and match the run;
3. `run.session_id` still exists and is owned;
4. `PendingApproval.state == approved` and `expires_at > now` (server clock);
5. `PendingApproval.policy_version == current DEFAULT_POLICY_VERSION`;
6. `trusted_tools` row exists, `enabled`, `locked == False`;
7. `trusted_tools.tool_schema_hash == A.tool_schema_hash` and
   `trusted_tools.server_fingerprint == A.server_fingerprint`;
8. `reconstruct_envelope(A).action_hash() == A.action_hash`;
9. no prior `dispatch_records` row for `A`'s `tool_call_id` (else block per §12).
Any failure ⇒ terminal `failed` with a precise `status_reason` + a `tool.execution.blocked`
audit event; the gateway is not called.

---

## 16. Migration plan

All additive, idempotent, dialect-agnostic — following `_migrate_approval_domain_1b2()`.

- **`_migrate_run_engine()`** in `app/main.py`, called from `_migrate_database()` **after**
  `_migrate_approval_domain_1b2()`:
  - `CREATE TABLE IF NOT EXISTS runs (...)` + indexes (incl. the partial `UNIQUE(user_id,
    idempotency_key) WHERE idempotency_key IS NOT NULL` — the 1B.2 `COALESCE`/`WHERE` partial
    index pattern is proven on SQLite ≥ 3.8 + PG).
  - `CREATE TABLE IF NOT EXISTS run_steps (...)`, `run_events (...)`, `run_signals (...)` +
    indexes.
  - Guarded `ALTER TABLE execution_turns ADD COLUMN run_id / thread_id / idempotency_key`
    (nullable) + `ix(run_id)` — a straddling DB without those columns gets them; a fresh DB gets
    them from `create_all`. No non-empty-table hazard (all new columns nullable).
  - Post-create `_verify_run_engine_schema()` (mirrors `_verify_session_grants_schema()`): asserts
    every required column + the `runs` idempotency partial-unique + the `run_events`
    `UNIQUE(run_id, run_seq)` + `run_signals` partial-unique; raises `RuntimeError` (halts
    startup) on any gap.
- **No destructive change.** No `DROP`, no data rewrite, no column type change. `Message`,
  `ResearchSession`, and every Phase 1B table keep their exact shape.
- **ORM models** added to `models.py` (`Run`, `RunStep`, `RunEvent`, `RunSignal`) using the 1B
  conventions block; `create_all` must agree with the raw DDL (a `test_run_engine_migrations.py`
  asserts it, like `test_approval_migrations.py`).
- **Rollback of the migration** = code rollback; the tables stay, inert, retaining audit data
  (same property as the 1B tables). A run in flight when the code is rolled back is left in its
  last durable state; on roll-forward the worker resumes it.
- **PostgreSQL:** the same DDL; `runs`/`run_steps` claim uses `FOR UPDATE SKIP LOCKED`;
  `run_events` append uses the run lease (held row) — no extra locking primitive needed beyond
  what 1B.2 already validated.

---

## 17. Evaluation tasks (proposed IDs — dataset NOT modified in this phase)

`evals/dataset.jsonl` is unchanged here. Proposed additions for a later phase, each a
`product_inprocess` target driving `RunEngine` on an in-memory / file SQLite DB with a mock
`execute_mcp`:

| ID | Scenario | Acceptance criteria |
|---|---|---|
| **T46** | Durable approval wait | `submit()` → run reaches `waiting_for_approval`; `runs` + `PendingApproval` rows persist; `execution_attempted == False`; 0 `dispatch_records`. |
| **T47** | Process restart while waiting | after T46, drop & rebuild the `RunEngine` (new worker) from the same DB → run still `waiting_for_approval`, same `run_id` / `execution_turn_id` / `tool_call_id`; no duplicate rows. |
| **T48** | Exact resume, no LLM regeneration | approve → worker resumes the **same** `step_id`; the `llm_generate` step is **not** re-run (assert its `attempt` unchanged, no new `token` events before the tool result); `execute_mcp` called exactly once; `dispatch_records.outcome == succeeded`. |
| **T49** | Denied run never executes | deny → `runs.status` → `denied`→`failed(reason=denied)`; `execute_mcp` call count 0; a later spurious `run_signals(approval_decided, approved)` is ignored (approval already `denied`). |
| **T50** | Expired approval never executes | approve, advance the injected clock past `A.expires_at`, worker sweep → `failed(reason=approval_expired)`; `execute_mcp` count 0; `tool.execution.blocked` event with `reason_code=approval_expired`. |
| **T51** | Duplicate approval idempotency | approve twice (+ a third via `run_signals` upsert) → one resume, one `execute_mcp`, one `approval.consumed`. |
| **T52** | Duplicate resume signal idempotency | inject two `run_signals(approval_decided)` before the worker runs → exactly one resume, `consumed_at` set once. |
| **T53** | Cross-tenant / cross-user resume rejection | user B approves user A's `approval_id` (owner filter → 404) AND a forged `run_signals` row with `created_by_user_id = B` on A's run → worker refuses (owner mismatch), run stays `waiting_for_approval`, `execute_mcp` count 0. |
| **T54** | Crash-before-dispatch recovery | kill the worker after `consume_approval` but before `dispatch_records` insert (monkeypatched) → on restart the run → `outcome_unknown`; `execute_mcp` count 0; no auto-retry. |
| **T55** | Crash-after-dispatch → `outcome_unknown` | kill after `dispatch_records.started`, before the outcome event → run `outcome_unknown`; `run_steps` `incomplete`; a later attempt is `blocked(dispatch_incomplete)`; `reconcile` endpoint moves it to `failed`/`succeeded` with `reconciled_by` and writes `run.reconciled`. |
| **T56** | Cancellation while waiting | cancel a `waiting_for_approval` run → `cancelled`; `execute_mcp` count 0; a subsequent approve is a no-op. |
| **T57** | Cancellation while running (pre-dispatch) | cancel during `llm_generate` → run `cancelled` at the next cooperative check; partial `token` events retained; no tool dispatched. |
| **T58** | SSE reconnect + event replay | tail to `run_seq = k`, disconnect, reconnect `after=k` → receives `k+1…terminal` exactly once; `after` past the retained window → `410`, then the finalized `Message` is available. |
| **T59** | Same run does not duplicate a side effect | resume the same `tool_call_id` twice (second via a stale `run_signals`) → `execute_mcp` count 1; second attempt `blocked(duplicate_dispatch)`. |
| **T60** | Pipeline convergence | with `run_engine_stream` on, a plain chat turn produces a `Message` identical (modulo ids/timestamps) to the legacy generator path for the same fixture; only `run_events` is authoritative. |
| **T61** | Service restart (mid-generation) | kill during `llm_generate` (no tool) → restart → run completes; the `Message` content equals a single clean generation (token rows de-duped by `run_seq`); exactly one `Message`. |
| **T62** | PostgreSQL multi-worker | (PG gate) 2 workers, 20 runs, 5 needing approval → each run advanced by exactly one worker at a time (`lease_owner` never flaps mid-step); 0 duplicate `execute_mcp`; `run_events` `UNIQUE(run_id, run_seq)` never violated. |
| **T63** | Regenerate is a different run | submit run X (waits on approval), then "regenerate" (fresh key) → run Y with a new `execution_turn_id`; approving X's `approval_id` cannot authorize Y (`proposal_dedupe_key` differs); X and Y terminal states independent. |

T44 stays `blocked_by: "1B"`. T45 stays a real `product_inprocess` eval.

---

## 18. Test matrix (future)

| Layer | Coverage | Blocks self-host release | Blocks hosted release | Blocks Phase 1B commit/push | Blocks "durable autonomous work manager" claim |
|---|---|---|---|---|---|
| **Unit** | `RunEngine.submit` idempotency; step planner; envelope-reconstruct revalidation set; event serializer sanitization | ✅ | ✅ | ✅ | ✅ |
| **State-transition** | every legal transition in §8.1/§8.2; every invalid transition raises | ✅ | ✅ | ✅ | ✅ |
| **Database** | DDL ↔ ORM agreement; partial-unique indexes present (SQLite + PG DDL text); retention prune | ✅ | ✅ | ✅ | — |
| **Migration** | clean DB; upgrade from 1B.2 schema; ×3 idempotency; `execution_turns` ALTER on a populated table; `_verify_run_engine_schema` fail path | ✅ | ✅ | ✅ | — |
| **Concurrency** | worker claim under `BEGIN IMMEDIATE` (SQLite, N threads); `run_signals` dedupe; lease expiry requeue; duplicate-worker (mock) | ✅ | ✅ | ✅ | ✅ |
| **Restart / recovery** | T47, T54, T55, T61; sweeper on boot | ✅ | ✅ | ✅ | ✅ |
| **API** | resume via approve (owner/CSRF/404); cancel; replay endpoint bounds (`410`, `limit`); `run_rejected` caps | ✅ | ✅ | ✅ | — |
| **SSE** | tail forwards; reconnect dedupe (T58); disconnect ≠ cancel; backpressure (slow reader) | ✅ | ✅ | ✅ | ✅ |
| **Approval integration** | T46, T48–T52; `PendingApproval` state coupling; `tool.execution.blocked` reasons | ✅ | ✅ | ✅ | ✅ |
| **Tool-dispatch integration** | worker → `gateway.dispatch`; `dispatch_records` at-most-once under the worker; T59 | ✅ | ✅ | ✅ | ✅ |
| **Tenant isolation** | T53; `run_events` replay owner filter; forged `run_signals` | ✅ | ✅ | ✅ | ✅ |
| **Cancellation** | T56, T57; cancel-racing-approval; cancel-racing-dispatch → `outcome_unknown` | ✅ | ✅ | ✅ | ✅ |
| **Timeout** | per-step timeout; `run_deadline_at`; timeout-during-dispatch → `incomplete`/`outcome_unknown` | ✅ | ✅ | ✅ | ✅ |
| **Failure injection** | ActionLog write failure at each transition; DB disconnect mid-step; provider 5xx retry cap; monkeypatched crash points | ✅ | ✅ | ✅ | ✅ |
| **SQLite** | full matrix on `sqlite://` + file SQLite | ✅ | — | ✅ | ✅ |
| **PostgreSQL** | T62 + the full concurrency/lease/replay matrix on PG (`EVAL_PG_TEST_URL`) | — | ✅ | ✅ (report status; may be a documented blocker if PG still unavailable) | ✅ |
| **End-to-end browser** | Playwright: send → approval prompt → approve in UI → tokens resume → reconnect after a forced socket drop → final message | — | ✅ | — | ✅ |
| **Full service-backed regression** | the ~23-min live-Ollama/ChromaDB suite, green | — | ✅ | ✅ | ✅ |

---

## 19. Operational requirements

- **Worker lifecycle.** Self-host: one `RunWorker` `asyncio` task in the FastAPI `lifespan`,
  stopped on shutdown (drain: stop claiming, let the current step finish or lease-expire).
  Hosted: `RUN_WORKER_CONCURRENCY` (config) workers; optionally a dedicated worker process
  (`python -m app.run_worker`) so API restarts don't interrupt runs.
- **Config (all validated ≥ 1 / sane at startup, not client-settable):** `run_engine_enabled`
  (master, default off), `run_engine_mcp` / `run_engine_stream` / `run_engine_sandbox` (flags,
  off), `run_worker_concurrency` (1), `run_lease_ttl_s` (60), `run_sweep_interval_s` (30),
  `run_deadline_s` (900), `run_step_timeout_s` (300), `max_llm_attempts` (3),
  `max_active_runs_per_user` (10), `max_active_runs_per_session` (3),
  `max_queued_runs_per_user` (20), `run_event_token_retention_h` (48),
  `run_replay_max_events` (2000), `run_wait_ttl_s` (equal to `PendingApproval` TTL by default).
- **Metrics (log-based; the repo has no Prometheus):** `runs_total{status}`,
  `runs_outcome_unknown_total` (alarm), `run_resume_latency_seconds`,
  `run_lease_expiry_requeue_total`, `run_worker_claim_contention_total`,
  `run_events_replayed_total`, `run_rejected_total{reason}`.
- **Observability.** Every run transition writes an `action_log` event
  (`run.created|queued|running|waiting|resumed|succeeded|failed|cancelled|reconciled`) with the
  same sanitization rules — one audit chain for approvals *and* runs.
- **Backups / retention.** `runs` / `run_steps` / `run_signals` / lifecycle `run_events` are
  audit-grade — back them up with `action_log`. `token` `run_events` are ephemeral — exclude from
  long-term backup; prune on the retention job.
- **Deployment note.** SQLite + multiple uvicorn workers is unsafe for the run engine (multiple
  writers, no `SKIP LOCKED`). Self-host stays single-worker (already the case). Hosted ⇒
  PostgreSQL (already the plan).
- **Runbook (design stub):** how to list `outcome_unknown` runs, how to reconcile, how to drain
  the worker for a deploy, how to force-fail a stuck run (owner or admin, audited).

---

## 20. Cost / complexity implications

- **New runtime dependencies:** **none** for Option 2. (Option 1 would add ≥ 3 and risk a
  `langgraph` bump cascade — see §5.)
- **New code (estimate, future phases):** `app/run_engine/` (~6 modules: `engine.py`,
  `worker.py`, `state_machine.py`, `steps.py`, `events.py`, `signals.py`) ≈ 900–1300 LOC;
  `app/models/models.py` +4 models ≈ 200 LOC; `app/main.py` `_migrate_run_engine` +
  `_verify_run_engine_schema` ≈ 180 LOC; 2 new routes (`runs` list/get/cancel/reconcile,
  `runs/{id}/events` replay) ≈ 200 LOC; `streaming_service` adapter refactor ≈ net −150 to +250
  LOC depending on how much duplicated logic is deleted; tests ≈ 1500–2500 LOC across the §18
  matrix.
- **Runtime cost.** One extra always-on `asyncio` task (idle poll every `sweep_interval`).
  `run_events` write volume ≈ one row per streamed token chunk — bounded by `MAX_TOKENS_PER_TASK`
  (10000) per run and pruned within `run_event_token_retention_h`. SQLite write amplification is
  the main cost; mitigate by batching token events (e.g. flush every N tokens / 200ms) — a config
  knob, not a correctness concern.
- **Operational cost.** One more schema to migrate/back up (but same mechanism), one more audit
  event family (same chain), a reconciliation runbook. No new service to run self-host.
- **Migration cost.** Strangler ⇒ spread over ~6 mini-phases; each is independently shippable and
  flag-gated; no big-bang.
- **Hosted cost.** PostgreSQL is already required for multi-user (1B.2). Worker pool is threads/
  processes on the same box class; no new infra tier.

---

## 21. Known limitations

- **Not built in this phase — design only.** No code. The gap (live one-time approval resume)
  remains open until the mini-phases land.
- **`outcome_unknown` is a real terminal state, not an error to hide.** A crash between
  `consume_approval` and a persisted outcome yields `outcome_unknown` requiring human
  reconciliation. The design shrinks the window (single-transaction CAS + dispatch-record insert
  where the DB allows) but cannot eliminate it — a `SIGKILL` after an external HTTP side effect
  is fundamentally unknowable locally.
- **No global exactly-once.** Only local at-most-once per `tool_call_id`. External idempotency is
  best-effort key propagation for tools that opt in.
- **Token-level resume of `llm_generate` re-streams from the start of that step.** If the process
  dies mid-generation, the resumed step produces a fresh generation; the client tail de-dupes by
  `run_seq` but the *content* may differ from the pre-crash partial (LLM nondeterminism). The
  `Message` is always one clean generation. True token-offset resume is out of scope.
- **SQLite self-host is single-worker by construction.** Concurrency guarantees for the run
  engine at multi-worker scale are a PostgreSQL claim, gated on the PG verification.
- **Streaming transport stays POST + `ReadableStream`.** No `EventSource`; the client manages its
  own `Last-Event-ID`-equivalent cursor. This is deliberate (auth headers on the initial POST)
  but means the browser's native reconnect is not used.
- **LangGraph path is not converted in the first mini-phases.** `/messages` and the scheduler
  keep running `run_research_workflow` (wrapped as one `sub_run` step) until Mini-phase 5.
- **Session-revocation check on resume** depends on `refresh_token_service` semantics being
  suitable for a synchronous "is this user's session still valid" query — see §23.

---

## 22. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Concurrency bug in the worker claim / lease / signal path | Medium | High (double execution or stuck runs) | Reuse the `action_log` `_ChainLock` pattern; full §18 concurrency matrix on SQLite + PG; `dispatch_records` UNIQUE is the last-line backstop against double `execute_mcp`. |
| `run_events` write volume degrades SQLite | Medium | Medium (latency) | Batch token events; aggressive retention; measure in Mini-phase 2 with a benchmark task before flag-on. |
| Strangler stalls — two pipelines persist for months | Medium | Medium (the divergence the task wants gone) | Removal criteria in §14.3 are explicit and gate-checked; each mini-phase deletes legacy code as its exit condition. |
| Resume revalidation misses a case (e.g. a new 1B.3 policy dimension) | Low | High (stale authorization executes) | The worker calls `gateway.dispatch` which *independently* re-checks everything (1B.2) — the worker's pre-checks are for clean failure messaging, not the security boundary. |
| `outcome_unknown` runs pile up unnoticed | Low | Medium | Metric + alarm + a listing endpoint + a runbook. |
| Hosted PG multi-worker never actually verified (env unavailable) | Medium | High for hosted | Same posture as 1B.2: an explicit, documented hosted-deployment blocker; T62 + the PG matrix must pass first. |
| Frontend reconnect logic is non-trivial and could double-render | Medium | Low | `run_seq` monotonic + client max-seen filter (T58); Playwright e2e covers a forced drop. |
| Dependency drift if Option 1 is chosen later | Low (deferred) | Medium | `run_steps.engine` seam keeps it optional and isolated to `sub_run`; a stack-modernization spike precedes it. |
| Scope creep into a full workflow engine | Medium | Medium | YAGNI gate: the engine does chat turns + approval resume + sub_run. No DAG editor, no arbitrary retries policy language, no cross-run orchestration in 1B.2R. |

---

## 23. Open decisions requiring user approval

| # | Decision | Options | Recommendation | Why it matters |
|---|---|---|---|---|
| **D1** | Durable execution substrate | (a) Custom `RunEngine` now, LangGraph checkpointer deferred to a later `sub_run` consolidation; (b) Adopt LangGraph durable checkpointer now — add `langgraph-checkpoint-sqlite` + `langgraph-checkpoint-postgres` + `psycopg` v3, likely bump `langgraph` | **(a)** | Infrastructure, dependency surface, and migration shape all fork here. (b) risks the pinned-stack cascade (`mcp`/`pydantic`/`starlette`). |
| **D2** | Worker deployment | (a) `asyncio` task inside the API `lifespan` (self-host) + worker pool same-process (hosted); (b) always a dedicated worker process (`python -m app.run_worker`) | **(a)** self-host, **(b)** optional for hosted | Ops complexity; API-restart resilience. |
| **D3** | Idempotency key source | (a) client sends `Idempotency-Key` per user action (new frontend work); (b) server-derives from `(user, session, user_message_id)` only | **(a) primary, (b) fallback** | Multi-tab / regenerate / retry semantics (§10) depend on it. Pure (b) makes "send identical text twice deliberately" ambiguous. |
| **D4** | Disconnect semantics | (a) POST tail close does **not** cancel the run (explicit `cancel` only); (b) preserve today's behavior (disconnect ⇒ `cancelled`) | **(a)** | (a) is the point of durability; but it changes observable behavior — a user closing a tab leaves a run advancing. Cost bounded by `run_deadline_s` + caps. |
| **D5** | Session-revocation check on resume | (a) query `refresh_token_service` / active-session store synchronously during worker revalidation; (b) skip it (rely only on approval ownership + gateway identity binding) | **(a) if the repo's session model supports a cheap check; else (b) + documented limitation** | Threat "logout while waiting". Needs a look at `refresh_token_service` capabilities. |
| **D6** | Migration order start point | (a) MCP-approval flow first (the actual gap); (b) whole streaming path first | **(a)** | (a) delivers the missing capability fastest with the smallest blast radius. |
| **D7** | `token` event persistence granularity | (a) one `run_events` row per provider chunk; (b) batch every N tokens / T ms | **(b)** with N/T config | SQLite write amplification vs. reconnect fidelity. |
| **D8** | Python sandbox as an approval surface | (a) route sandbox execution through `PendingApproval` + a `tool_dispatch` step (Mini-phase 3); (b) leave it inline/unapproved | **(a)** | Today code execution has no approval at all — a real security gap, but expanding scope. |
| **D9** | Scheduler runs | (a) re-point scheduled tasks at `RunEngine.submit()` (Mini-phase 5); (b) leave the scheduler on the legacy `run_research_workflow` indefinitely | **(a)** | Convergence completeness; a scheduled autonomous task that needs approval currently cannot get it. |
| **D10** | `THUNDER_AI_PRD.md` §Status update | (a) append the §1 roadmap block to the PRD Status section now (additive, documentation-only); (b) keep roadmap only in this plan doc | **(a)** — proposed as a diff for approval (the PRD is the source of truth; not editing it silently) | The task asks to update roadmap docs "if present"; the PRD Status section is the only roadmap artifact. |

---

## 24. Exact implementation mini-phases (for a later, approved phase)

Each ships behind a fail-closed flag, with its §18 test slice green on SQLite and its §17 eval
tasks passing, before the flag defaults on. Nothing here is executed in 1B.2R.

- **MP-0 — Schema + models + migration (no behavior change).**
  `models.py` +`Run`/`RunStep`/`RunEvent`/`RunSignal`; `_migrate_run_engine()` +
  `_verify_run_engine_schema()`; `execution_turns` ALTER. Tests: `test_run_engine_migrations.py`
  (clean / 1B.2-upgrade / ×3 idempotent / populated-`execution_turns` ALTER / verify-fail).
  Exit: DDL ↔ ORM agree on SQLite + PG DDL text; `import app.main` OK.

- **MP-1 — `RunEngine` + `RunWorker` core (in-process, no route wiring).**
  `app/run_engine/{engine,worker,state_machine,steps,events,signals}.py`. `submit()`, the claim/
  lease/sweep loop, the state machine, `run_events` append under lease, `run_signals` consume.
  A `NativeChatAdapter` that runs a single `llm_generate` step calling the existing provider
  chain. No approval yet. Tests: state-transition (all legal + invalid), concurrency (SQLite
  `BEGIN IMMEDIATE`, N threads), restart/sweeper, event serializer sanitization. Eval: T60/T61
  (behind an internal switch). Exit: a chat turn completes end-to-end through the engine in a
  test, `Message` identical to the legacy path.

- **MP-2 — MCP approval resume through the engine.** Flag `run_engine_mcp` (off).
  Add `parse_tools` + `tool_dispatch` steps; `tool_dispatch` calls
  `dispatch_mcp_call_via_gateway` semantics (worker builds `GatewayContext` from the run row).
  `waiting_for_approval` ⇄ `run_signals(approval_decided)` wired to `record_decision` (a small,
  additive hook in the approvals route: after a successful decide, `INSERT run_signals … ON
  CONFLICT DO NOTHING`). Worker revalidation set (§15). `outcome_unknown` handling (§12).
  Tests: T46–T55, T59, T63; failure-injection at every §12.1 crash point; tenant isolation T53.
  Exit: all listed evals pass on SQLite; `run_engine_mcp` can be turned on for `/messages/stream`
  and an approved MCP call executes exactly once without LLM regeneration.

- **MP-3 — Streaming path on the engine + reconnect.** Flag `run_engine_stream` (off).
  `/messages/stream` becomes: `submit()` → tail `run_events`. New `GET
  …/runs/{run_id}/events?after=&wait=` replay endpoint (owner-scoped, bounded, `410` past
  retention). Frontend: `useStreaming.ts` gains a reconnect path that re-opens the replay
  endpoint with the last `run_seq`; disconnect no longer cancels (D4). Token event batching (D7).
  Tests: T58, SSE matrix, backpressure, e2e Playwright (drop + reconnect). Exit: parity with the
  legacy generator for the eval fixtures; reconnect after a forced socket drop resumes tokens.

- **MP-4 — Convergence: multi-agent + deep research + python sandbox.**
  `MultiAgentAdapter` / `DeepResearchAdapter` as `sub_run` or step sequences; delete the
  `streaming_service` duplicates once the adapter is the sole caller. `run_engine_sandbox` (off):
  route python execution through `PendingApproval` + a `tool_dispatch` step (D8). Tests: existing
  multi-agent/deep-research/sandbox suites re-pointed; convergence test (one implementation).
  Exit: `stream_multi_agent_response` and the graph `agent_*` nodes no longer both exist for the
  same purpose.

- **MP-5 — Non-streaming path + scheduler on the engine.**
  `/messages` and `scheduler_service` call `RunEngine.submit()` + wait-for-terminal (sync
  adapter). Delete `run_research_workflow`'s bespoke `resume_from` reconstruction and the
  `citations`-JSON approval encoding. Browser automation approval routed through `PendingApproval`
  (D9-adjacent). Tests: `/messages` contract unchanged; scheduled task that needs approval now
  reaches `waiting_for_approval` and can be approved. Exit: one canonical orchestration path;
  `langgraph_workflow` either deleted or reduced to a `sub_run` step body.

- **MP-6 — Hardening + hosted verification.**
  `outcome_unknown` reconcile endpoint + runbook; per-user run caps + `run_rejected` audit;
  metrics; the full PG matrix (T62 + concurrency/lease/replay on `EVAL_PG_TEST_URL`); the
  ~23-min service-backed regression. Exit: hosted-deployment blocker list is either cleared or
  explicitly, accurately documented.

---

## 25. Rollback strategy

- **Per mini-phase:** the flag is the rollback. Flag off ⇒ the legacy code path runs; the engine
  tables sit inert with their audit data (same property as the 1B tables). No migration is
  reverted.
- **Mid-run rollback:** a run in flight when a flag is turned off (or code is rolled back)
  finishes on whatever worker last held it, or lease-expires and is swept per §12; new requests
  take the legacy path. No data loss, no half-migrated state (the engine tables are additive).
- **Schema rollback:** not required — additive tables + nullable `ALTER`s. If a table must go,
  `DROP TABLE IF EXISTS runs/run_steps/run_events/run_signals` in a follow-up migration; the
  `execution_turns` columns stay (nullable, harmless).
- **Emergency stop:** `run_engine_enabled = false` (master flag) ⇒ `submit()` refuses, the worker
  drains, everything falls back to legacy. Documented in the runbook.
- **Data safety:** no rollback path deletes `PendingApproval`, `dispatch_records`, `action_log`,
  `Message`, or `ResearchSession` rows.

---

## 26. Definition of done (for the eventual implementation phase — not this one)

A mini-phase is done when:
1. its §18 test slice is green on `sqlite://` **and** file SQLite, with the concurrency and
   failure-injection cases included;
2. its §17 eval tasks pass as `product_inprocess`;
3. `python -m compileall` clean on all changed files; `import app.main` OK; `git diff --check`
   clean;
4. the migration is idempotent ×3 and `_verify_run_engine_schema` passes on a clean DB and on a
   DB upgraded from the current 1B.2 schema;
5. no `sk-` / `Bearer` / `api_key` / provider-config pattern appears in `runs` / `run_steps` /
   `run_events` / `run_signals` / `action_log` after a full lifecycle (automated assertion);
6. the flag ships **off**; turning it on is a separate, documented step gated on 1–5.

The **phase** (1B.3-durable / 1B.2R implementation) is done when:
- one canonical orchestration path exists; `streaming_service` and `langgraph_workflow` are
  adapters or deleted; no flow has two authoritative state holders;
- a live end-to-end flow works: user sends a message → MCP action proposed → run
  `waiting_for_approval` → user approves in the UI → the **original** action executes exactly
  once (no LLM regeneration) → tokens resume → the client can disconnect and reconnect and see
  the finished result;
- a backend restart at any point in that flow loses nothing;
- denied / expired / cross-tenant / duplicate / crash cases behave per §8, §12, §15 with tests;
- the PostgreSQL matrix (T62 + concurrency/lease/replay) passes, or the hosted-deployment blocker
  is explicitly and accurately documented;
- the full service-backed regression suite is green;
- documentation (this plan + a completion report + `THUNDER_AI_PRD.md` §Status) is updated;
- nothing was committed/merged/pushed without explicit approval.

Only then may Thunder AI be described as a **durable autonomous work manager**.

---

## Stop-condition check (performed during this design task)

| Stop condition | Triggered? | Handling |
|---|---|---|
| Repo evidence contradicts the Phase 1B reports | **No** — it corroborates them (no checkpointer; streaming ≠ LangGraph; reference dispatch works but live resume does not). | — |
| Installed LangGraph/checkpoint versions cannot support the recommended design | **No for Option 2** (needs no checkpointer). **Yes for Option 1** — no durable saver installed; would need new deps. | Recorded as **D1** (open decision); Option 2 recommended precisely to avoid it. |
| Durable resume requires a new paid external platform | **No** — SQLAlchemy + one worker. | — |
| A destructive migration appears necessary | **No** — additive tables + nullable `ALTER`s only. | — |
| The design would weaken Phase 1B approval guarantees | **No** — it is a consumer of the 1B.1/1B.2 boundary (approve API never executes, no `/consume`, envelope hash-bound, `dispatch_records` at-most-once). | — |
| Tenant/session ownership cannot be derived safely | **No** — `get_current_user` + `ResearchSession.user_id`; `tenant_id` nullable per 1B. | — |
| Secrets would need to be stored in checkpoint/run state | **No** — the recommended design stores only non-secret references + hashes; §15 makes this a hard rule with a test. (This is a *reason* Option 2 is safer than a LangGraph checkpoint holding `provider_config` in graph state.) | Baked into the data model (§9) and threat model (§15). |
| Choosing between two options materially changes infra/cost/deployment | **Yes** — Option 1 vs 2 (deps, second store) and D2 (worker process). | Surfaced as **D1**/**D2** open decisions; this doc recommends but does not commit. |

No stop condition blocks delivering this design. **D1 and D2 are genuine gates for the
implementation phase and are called out for explicit user approval.**

---

## 27. MP-0 — as-built schema foundation (2026-09-08)

MP-0 implements **only**: ORM models, additive migration, schema verification, the pure state
machine, the encrypted payload store, the allowlisted signal schema, the fail-closed
session-binding predicate, config validation, and focused tests. **No worker, run loop, route,
`/messages/stream` change, SSE, approval resume, MCP execution, frontend change, scheduler
integration, MP-1+, or Phase 1B.3.**

### 27.1 New tables (5) — additive, no existing table altered

| Table | PK | Purpose | Key constraints |
|---|---|---|---|
| `runs` | `id VARCHAR(36)` uuid4 | the durable unit of work; authoritative state | partial-UNIQUE `uq_runs_idem_scope` (D3); ix on `(status,lease_expires_at)`, `(user_id,session_id,created_at)`, `(pending_approval_id)`, `(parent_run_id)`, `(tenant_id,status)`, `(tenant_id,user_id,session_id,idempotency_key)` |
| `run_steps` | `id VARCHAR(36)` | one step of a run's plan; `engine ∈ {native, langgraph}` | `UNIQUE(run_id, seq)`; ix on `(status,lease_expires_at)`, `(tool_call_id)`, `(user_id,session_id)` |
| `run_events` | `id VARCHAR(36)` | persisted replayable client events; `category ∈ {control, text}` | `UNIQUE(run_id, run_seq)`; ix on `(run_id,created_at)`, `(user_id)` |
| `run_signals` | `id VARCHAR(36)` | out-of-band approve/deny/cancel instruction for the future worker | partial-UNIQUE `uq_run_signals_live` `(run_id, kind) WHERE consumed_at IS NULL`; ix on `(run_id,kind)` |
| `run_payloads` | `id VARCHAR(36)` | **context-bound encrypted envelope** for sensitive run-scoped bytes (no `plaintext_sha256`) | ix on `(run_id)`, `(step_id)`, `(user_id)`, `(expires_at)` |

`run_events.id` is a `VARCHAR(36)` uuid4 (not a global autoincrement) — dialect-safe on SQLite +
PostgreSQL with the raw `CREATE TABLE IF NOT EXISTS` migration; per-run ordering is
`UNIQUE(run_id, run_seq)`. `TIMESTAMP` in the raw DDL vs `DateTime(timezone=True)` in the ORM
matches the established Phase 1B pattern (tz-awareness comes from `_utcnow_aware` in Python;
PostgreSQL column-type parity is part of the unverified hosted gate).

### 27.2 `execution_turns` — NOT changed

MP-0 adds **no column** to `execution_turns`. Its existing Phase 1B.2 `run_id VARCHAR(64)` column
is **reused** as the link to `runs.id` in the RunEngine era (documented in the model + migration).
No `thread_id` / `idempotency_key` column is added there — `runs.thread_id` and
`runs.idempotency_key` are the home for those, reachable from a turn via `run_id`. This keeps
MP-0's blast radius to "5 new tables, zero `ALTER`".

### 27.3 State machine — `app/run_engine/state_machine.py` (pure)

Run states: `created, queued, running, waiting_for_approval, approved, denied, resuming,
cancelling, cancelled, succeeded, failed, timed_out, outcome_unknown`. Terminal:
`{succeeded, failed, timed_out, cancelled, denied, outcome_unknown}`.

| From | Legal → |
|---|---|
| `created` | `queued`, `cancelling` |
| `queued` | `running`, `cancelling`, `cancelled` |
| `running` | `waiting_for_approval`, `succeeded`, `failed`, `timed_out`, `cancelling`, `outcome_unknown` |
| `waiting_for_approval` | `approved`, `denied`, `failed` (approval expired / revalidation fail), `cancelling` |
| `approved` | `resuming`, `cancelling`, `failed` |
| `resuming` | `running`, `failed`, `cancelling`, `outcome_unknown` |
| `cancelling` | `cancelled`, `outcome_unknown` |
| `denied` / `succeeded` / `failed` / `timed_out` / `cancelled` | *(terminal — none)* |
| `outcome_unknown` | *(terminal; `→ succeeded` / `→ failed` **only** with `allow_reconcile=True`)* |

Step states: `pending → running → {succeeded, failed, blocked, waiting, incomplete}`;
`pending → skipped`; `waiting → {running, blocked, skipped}`. `waiting` = the step surfaced an
approval requirement.

Helpers: `assert_run_transition` / `assert_step_transition` (raise `InvalidRunTransition`),
`run_can_transition` / `step_can_transition`, `is_run_terminal` / `is_step_terminal`,
`race_winner(approval_pending, cancel_pending)` → **cancel always wins**,
`idempotent_decision_state(current, decision)` → `None` if still actionable, `current` if already
settled the same way (deterministic no-op), raises on a conflicting or undecidable state. A test
(`test_state_machine_module_is_pure`) asserts the module imports no `sqlalchemy` / `app.models` /
`asyncio` / socket / file I/O.

### 27.4 Primitives already available vs capabilities still missing (replaces the "≈80%" claim)

**Available and reused unchanged** — server-owned execution identity (`execution_turns`,
`tool_calls`), local at-most-once dispatch (`dispatch_records` `UNIQUE(tool_call_id)` + CAS),
reconstructable hash-bound envelope (`pending_approvals.trusted_envelope_json` + `action_nonce`),
tamper-evident audit chain (`action_log*`), the trusted gateway with the internal
`dispatch_mcp_call_via_gateway` entry, active-proposal caps, `BLOCK_REASONS` + `tool.execution.*`
events, a verified Fernet encryption service with `MultiFernet` rotation, a session/revocation
store (`refresh_sessions`).

**Still missing (built across MP-0 … MP-6)** — a `runs` ledger that owns a turn across HTTP
requests and restarts *(MP-0: schema only)*; a worker that advances a run after an approval
without re-running the LLM *(MP-1/2)*; a `family_id` claim on the access token so
`auth_session_ref` can be populated *(MP-1 auth seam)*; durable `run_events` replay + SSE
reconnect *(MP-3)*; convergence of the two pipelines + sandbox approval *(MP-4)*; scheduler +
non-stream `/messages` on the engine *(MP-5)*; `outcome_unknown` reconciliation + the PostgreSQL
concurrency matrix + the full service-backed regression *(MP-6)*.

### 27.5 Migration & verification

`app/main.py :: _migrate_run_engine()` — `CREATE TABLE / INDEX IF NOT EXISTS` for the 5 tables +
the two partial-UNIQUE indexes; dialect-agnostic; **no `ALTER`, no `DROP`, no data move**; runs
inside `_migrate_database()` right after `_migrate_approval_domain_1b2()`, on both SQLite and
PostgreSQL. `_verify_run_engine_schema()` runs immediately after and **halts startup**
(`RuntimeError`) on any missing table, missing required column, missing `run_steps` /
`run_events` uniqueness, or (SQLite) missing partial-UNIQUE index. Idempotent — verified ×3.
`Base.metadata.create_all` column/index/unique sets match the raw DDL for all 5 tables
(the two partial-UNIQUE indexes are migration-only, exactly like 1B.2's
`uq_pending_approvals_active_dedupe`). Existing `pending_approvals` / `dispatch_records` rows are
byte-for-byte unchanged across the migration (test). PostgreSQL execution is **unverified** here
(`EVAL_PG_TEST_URL` unset) — an enumerated hosted-deployment blocker.

### 27.6 Files (MP-0)

New: `backend/app/run_engine/{__init__,errors,state_machine,payloads,signals,session_binding}.py`;
`tests/test_run_engine_state_machine.py`, `tests/test_run_payloads.py`, `tests/test_run_signals.py`,
`tests/test_run_engine_models.py`, `tests/test_run_engine_migrations.py`;
`docs/superpowers/reports/phase-1b2r-mp0-completion.md`.
Modified: `backend/app/models/models.py` (+5 models, conventions block), `backend/app/main.py`
(+`_migrate_run_engine` / `_verify_run_engine_schema` + wiring), `backend/app/config.py`
(+`run_engine_enabled`, `run_payload_max_bytes`, `run_event_text_flush_bytes/ms` + validator),
`THUNDER_AI_PRD.md` (§Status), this document (§0, §27).

---

## 28. MP-1A — as-built: auth session binding + core transactional services (2026-09-08)

**Correction round 2 applied. Status: BLOCKED — DECISION REQUIRED** on three items (see §28.8).
Not a worker, not a route cutover, not MP-1B/MP-2.
Plan: `docs/superpowers/plans/2026-09-08-phase-1b2r-mp1a-auth-and-core-services.md`.
Report: `docs/superpowers/reports/phase-1b2r-mp1a-completion.md` — **§12 is authoritative**.

**Round-2 corrections (report §12):**
* **Fresh DB-backed authorization.** `AuthContext.durable_session_binding_valid` is diagnostic
  only. Every RunEngine read and mutation calls
  `session_binding.validate_session_binding(db, ctx, run=…)` inside its own transaction —
  owner-constrained family lookup (`family_id` **and** `user_id`, not revoked, not expired),
  run owner/tenant match, and (STRICT default) run-family match. Re-checked immediately before
  every `commit()`, and on the idempotent-replay / race-recovery return.
* **Closed per-event schemas.** `events.CONTROL_EVENT_SCHEMAS` — exact required/optional keys and
  a closed enum per field for the 5 MP-1A event types (`run.created`, `run.queued`, `run.status`,
  `run.cancel.requested`, `run.error`). `_sanitize_event_data` (the "non-secret scalar" sieve)
  is deleted. `run.error` carries a bounded `error_code`, never raw exception text.
* **Identity tag relabelled.** `idempotency_identity_hash` is an explicitly *structural*,
  domain-separated (`"structural-1"`) tag — NOT semantic. The client `request_fingerprint`
  parameter is removed. A server-keyed semantic HMAC is a pending decision (§28.8).
* **Login orphan cleanup.** A token-signing failure after the refresh session is persisted now
  revokes that session before re-raising.

### 28.1 Auth seam — the `sid` claim closes the D5 gap

The access token now carries **`sid`** = the login session's `refresh_sessions.family_id`
(OpenID-Connect-style session id; a private RFC 7519 claim). Never a token / hash / cookie /
header / API key / user-controlled id. `create_access_token(data=…)` merges a caller dict, so
this is additive and no client contract breaks (no test asserts the JWT claim set; the frontend
treats `access_token` as opaque).

* **login** (`routes/auth.py`) reordered — the refresh session is created **first**, its
  `family_id` resolved (`refresh_token_service.family_id_for_raw_token`, one indexed lookup),
  then the access token is minted with `sid`. Exactly one family per login (unchanged).
* **refresh** re-mints the access token with the same `sid` — `rotate_refresh_token` preserves
  `family_id` across rotation, so the claim is continuous for the life of the login. No change
  to `rotate_refresh_token` / `create_refresh_session` return arities (a test unpacks the former
  as a 2-tuple).
* A **legacy** access token with no `sid` still authenticates but yields
  `durable_session_binding_valid = False` → every RunEngine mutation fails closed. No
  content-derived or user-id-derived fallback.
* Unchanged: JWT verification, refresh rotation, reuse detection, `_revoke_family`, logout, CSRF.

### 28.2 `AuthContext` (`run_engine/context.py`)

`AuthContext(user, user_id, tenant_id, auth_session_ref, durable_session_binding_valid)`.
`build_auth_context(db, *, user, token_payload)` normalizes `sid`/`tenant` and sets
`durable_session_binding_valid = bool(sid) and session_ref_is_active(db, sid)` (the MP-0
fail-closed predicate, unchanged). `get_auth_context` is a FastAPI dependency; **no MP-1A route
consumes it** — it is the seam MP-2 wires in. `tenant_id` is always `None` today (no tenant
source in the repo); the isolation checks are in place for when one exists.
`auth_service.resolve_user_from_payload` extracted so the `sub → int → User` rule lives once.

### 28.3 Core services (`run_engine/engine.py`, `run_engine/events.py`) — NOT in a route / streaming / model method

| Function | Contract |
|---|---|
| `submit_run` | Scope = normalized `(tenant_id, user_id, session_id, idempotency_key)`. Server generates `run_id` (uuid4). `idempotency_identity_hash` = `sha256(RFC-8785(identity tuple + kind + client-supplied thread_id + optional non-secret `request_fingerprint`))` — **never the raw prompt or the prompt hash alone**; a server-derived `thread_id` is not an identity input. Pre-`SELECT` mirrors the `uq_runs_idem_scope` partial index (`COALESCE` semantics); on `INSERT` the index is the **final authority** — an `IntegrityError` whose constraint is exactly `uq_runs_idem_scope` → rollback + bounded retry (3) → return the winner. Any other `IntegrityError` re-raises (no broad catch — `run_engine/_dberr.py` matches the exact constraint on SQLite *and* PostgreSQL). Replay (same key + same identity) returns the existing run with **no** new `run.created` event and **no** new step. Same key + any identity difference → `IdempotencyKeyConflict`. Initial `run.created` control event is written in the **same transaction** as the run insert. `idempotency_key` is required in MP-1A; the no-key compat window is deferred. |
| `get_run` | Owner-scoped read; `RunNotFound` (non-disclosing) for absent / other-owner / other-tenant. |
| `transition_run` | Pure MP-0 state machine (`assert_run_transition`) + a single conditional `UPDATE … WHERE id AND version = :expected AND status = :frm` — `version` bumps **exactly once**. Terminal source protected (`RunAlreadyTerminal`; a no-op re-assertion of the same terminal state is allowed). Cancellation wins races: a fresh `cancelling`/`cancelled` read, or a lost CAS that finds the run now cancelling, → `CancellationInProgress`. Stale CAS → `RunStateConflict(expected, actual)`. No in-memory locks, no worker. `started_at`/`finished_at` set on entry to `running` / any terminal. |
| `append_control_event` | Closed `CONTROL_EVENT_TYPES` allowlist (lifecycle + `run.cancel.requested` + `run.status`/`run.error`); disallowed → `DisallowedControlEventType`. `data` sanitized to non-secret scalars only (no nested structures, no raw output/args/headers/prompts), capped at `run_control_event_max_bytes`. Seq allocation = conditional `UPDATE runs SET last_event_seq = :cur+1 WHERE last_event_seq = :cur` **plus** the row insert in **one transaction**, so a failed insert (`uq_run_events_run_seq`) rolls the high-water mark back — it never permanently advances. Bounded retry (5) → `ControlEventContention`. |
| `read_control_events` | Owner-scoped, `run_seq > after_seq`, ascending, `limit` clamped to (and defaulting to) `run_event_read_max_page`. No SSE/polling route, no `text`-category rows. |
| `request_cancellation` | Owner + session-family check (`SessionBindingMismatch` when the run's `auth_session_ref` and the caller's differ). Terminal run → deterministic `CancellationResult(already_terminal=True)`, **no live signal, run not mutated, no control event**. Otherwise idempotent: a live `run_signals(cancel_requested)` (unique via `uq_run_signals_live`) → `already_requested`; else insert the signal + append `run.cancel.requested` **once**, in one transaction. Never sets `consumed_at`, never sets `status = cancelled` — the worker (MP-1B+) does both. |

### 28.4 Config (server-only safety limits; client cannot raise or disable; validated `>= 1`)

`run_idempotency_key_max_bytes = 80` (== `runs.idempotency_key VARCHAR(80)`),
`run_control_event_max_bytes = 4096` (== `run_event_text_flush_bytes`),
`run_event_read_max_page = 200`. **No** active-run product cap — §24 assigns per-user run caps to
MP-6; MP-1A adds only abuse-resistance bounds.

### 28.5 Migrations

**None.** All 5 tables, every column (incl. `runs.auth_session_ref`), and all three unique
indexes already exist from MP-0. The `sid` claim needs no schema change.

### 28.6 Known limitations carried forward

* **Revoke-between-validation-and-commit (residual):** after round 2 the family is re-validated
  in the same transaction immediately before every `commit()` (and after, for `transition_run`).
  On a single SQLite connection there is no true concurrent revoke in that window. On PostgreSQL
  a revoke committed by another connection between the pre-commit check and `commit()` would
  still be missed by this op — the MP-1B/MP-2 worker MUST re-validate immediately before any
  external side effect. No row-level lock added (unverified locking is out of scope; no PG test).
* **PostgreSQL:** the CAS + unique-index + `validate_session_binding` approach is PG-safe by
  construction, but unverified here (`EVAL_PG_TEST_URL` unset) — hosted-deployment blocker stands.
* **No worker** consumes signals or advances runs — `request_cancellation` records intent only.

### 28.7 Files (MP-1A, incl. round 2)

New: `backend/app/run_engine/{context,engine,events,_dberr}.py`;
`tests/{run_engine_helpers,test_run_engine_auth_context,test_run_engine_submit,
test_run_engine_transitions,test_run_engine_events,test_run_engine_cancellation,
test_run_engine_session_binding,test_run_engine_atomicity}.py`;
`docs/superpowers/plans/2026-09-08-phase-1b2r-mp1a-auth-and-core-services.md`;
`docs/superpowers/reports/phase-1b2r-mp1a-completion.md`.
Modified: `backend/app/run_engine/{__init__,errors,session_binding,events,engine,context}.py`,
`backend/app/services/{auth_service,refresh_token_service}.py`,
`backend/app/routes/auth.py`, `backend/app/config.py`,
`THUNDER_AI_PRD.md` (§Status), this document (§28).

### 28.8 Pending decisions — RESOLVED (2026-09-09, user)

* **5A — semantic-identity HMAC key → dedicated `RUN_IDEMPOTENCY_HMAC_KEY`.** Used only when
  semantic idempotency is implemented. NOT `JWT_SECRET`, NOT `ENCRYPTION_KEY`. Server-side, **no
  hardcoded default**, fails closed when the semantic / public Run path is enabled without it.
  **Not** implemented in MP-1B (no canonical public submission contract exists yet).
* **5B — canonical semantic-submission contract → deferred to MP-3 public route wiring.** Until
  then: the structural identity tag is **internal-only and documented incomplete**; no public Run
  submission endpoint may ship; no client fingerprint is authoritative; MP-3 must cover every
  behavior-changing request field **without** storing raw prompt content or an unkeyed prompt
  hash.
* **5C — same-user cross-session run visibility → STRICT origin-session binding CONFIRMED.**
  `session_binding._SESSION_BOUND_RUN_ACCESS = True` stays. A run can be read / mutated /
  cancelled / resumed / approved / executed **only** by its originating active refresh-token
  family; a different active login family of the same user gets `SessionBindingMismatch`.
  Cross-device transfer / adoption is deferred to a **separately approved explicit
  re-authentication flow** — the policy is not to be silently weakened.

**MP-1A is therefore APPROVED FOR PROGRESSION** (uncommitted, not production-ready). Only the
documentation / config-status updates the decisions require were applied; the 613-test MP-1A
regression was **not** re-run.

---

## 29. MP-1B — as-built: RunWorker + claim / lease + cancellation / recovery core (2026-09-09)

Decisions 5A / 5B / 5C locked (§28.8). **Mechanism only — nothing wired into the app lifespan.**
Not MP-2 (no approval resume), not a route / SSE / frontend / scheduler change, not Phase 1B.3.
Report: `docs/superpowers/reports/phase-1b2r-mp1b-completion.md`.

### 29.1 Files

New: `backend/app/run_engine/worker.py`; `tests/test_run_engine_worker.py`,
`tests/test_run_engine_recovery.py`; this section; the completion report.
Modified: `backend/app/run_engine/{__init__,errors}.py` (+`RunLeaseLost`, +worker exports),
`backend/app/config.py` (+7 `run_worker_*` knobs; 5 ints validated `>= 1`),
`tests/run_engine_helpers.py` (+`Clock`, +fresh-session read helpers),
`THUNDER_AI_PRD.md` (§Status),
`docs/superpowers/reports/phase-1b2r-mp1a-completion.md` (decisions-resolved note).
**No migration** — every column used (`runs.lease_owner`, `lease_expires_at`, `run_deadline_at`,
`version`, `last_event_seq`, `run_signals.consumed_at`) exists from MP-0. **`events.py`
unchanged** — worker error reasons live in `runs.status_reason` (a bounded module constant set),
leaving the closed control-event schema registry untouched.

### 29.2 `RunWorker` behaviour

- **Service boundary.** A class owning its own DB sessions (injected zero-arg `session_factory`)
  and lease identity. Public surface: `claim_next_queued`, `execute`, `recover_expired`,
  `renew_lease`, `poll_once`, `async run_forever`, `stop`. Started nowhere in MP-1B; MP-3 adds
  one `await RunWorker(sf).run_forever()` line to `lifespan`.
- **Atomic claim.** Conditional `UPDATE runs SET status='running', version=version+1,
  lease_owner=:lo, lease_expires_at=:exp, run_deadline_at=COALESCE(run_deadline_at,:dl),
  started_at=COALESCE(started_at,:now) WHERE id=:id AND version=:v AND status='queued' AND
  (lease_expires_at IS NULL OR lease_expires_at < :now)`. `rowcount == 1` is the sole winner —
  two racing claimers yield exactly one lease owner; the loser re-polls. One `run.status`
  (`queued` to `running`) control event, same transaction.
- **Lease owner / token.** `lease_owner` is `"<worker_id>/<uuid4-hex>"` — an identity and an
  unforgeable per-claim token (`worker_id` clamped to 24 chars; fits `VARCHAR(64)`). Every
  worker mutation UPDATE carries `WHERE lease_owner = :mine AND version = :v`; a stale token or
  lost race matches 0 rows to `RunLeaseLost`, and the worker abandons the run for this sweep.
- **Lease renewal.** `renew_lease(run_id, lease_owner)` extends only a non-terminal run held by
  the current owner; a stale / wrong owner returns `False`.
- **CAS / version.** `runs.version` bumps exactly once per worker state change; every transition
  UPDATE is version-guarded; reuses the pure MP-0 `assert_run_transition`.
- **Stale-worker rejection + fresh session validation** are one gate, `_precheck`, run when
  handling begins **and** again immediately before invocation: `expire_all` + re-read the run;
  `RunLeaseLost` unless it is still `running` under exactly this `lease_owner` and `version`;
  then `validate_session_binding` against the LIVE DB with an `AuthContext` built **from the run
  row** (owner id, tenant, `auth_session_ref`) — never a request. The cached
  `durable_session_binding_valid` flag is passed `False` and never trusted. A revoked / expired
  / mismatched family to `running` to `failed` (`status_reason = "session_revoked"`); the
  handler never runs.
- **Cancellation, consumed exactly once.** A live `run_signals(cancel_requested)` is consumed
  with `UPDATE ... SET consumed_at = :now WHERE id = :id AND consumed_at IS NULL` — `rowcount ==
  1` is the once-guarantee. The consume + two `run.status` events + `running` to `cancelling` to
  `cancelled` commit **together**; any failure rolls the whole unit back (signal stays
  unconsumed, run stays `running`). Cancel is checked before handler invocation (blocks it) and
  again after a side-effect-free handler returns.
- **Handler registry.** `register_handler(kind, fn, *, retry_safe)` / `unregister_handler` /
  `clear_handlers` / `resolve_handler`. **Ships empty.** Unknown `run.kind` to `running` to
  `failed` (`status_reason = "unknown_handler"`) — fail closed. `HandlerContext` carries only
  `run_id` / `kind` / `user_id` / `tenant_id` / `attempt` — no DB session, no auth token, no
  prompt / args. A raising handler to `status_reason = "handler_error"` (**never** the exception
  text); `ok=False` to a bounded code (`handler_rejected`, else collapsed to `handler_error`).
- **Bounded attempts / timeouts.** `run_worker_handler_timeout_s` per-run wall clock (exceeded
  to `timed_out`). Recovery is bounded by `run_deadline_at`, set at first claim to
  `now + handler_timeout_s x run_worker_max_attempts` (no per-run attempt column to no
  migration); past it a retry-safe run recovers to `failed` (`deadline_exceeded`) rather than
  re-running. `run_worker_recover_batch` bounds runs per sweep.
- **Retry-safe recovery.** `recover_expired` sweeps `status='running' AND lease_expires_at <
  now`. A registered **`retry_safe`** handler within the deadline to atomic reclaim (new lease
  token, `version++`) then `execute`. Anything else — unknown handler, non-retry-safe handler —
  to `running` to `outcome_unknown` (**never** auto-retried). A fresh `RunWorker` calling
  `recover_expired` is a restart: only retry-safe kinds resume.
- **Disabled by default.** `run_worker_enabled = False` and `run_engine_enabled = False`;
  `run_forever` returns immediately unless **both** are true. A sweep exception is logged as its
  class name only (no traceback / message); the loop continues.

### 29.3 Focused verification (per the mini-phase instruction — no broad regression)

- `python -m compileall` — clean on every changed file.
- `tests/test_run_engine_worker.py` (14) + `tests/test_run_engine_recovery.py` (6) — **20 pass**.
- Directly-affected existing suites re-run green: `test_run_engine_session_binding.py`,
  `test_run_engine_events.py`, `test_run_engine_cancellation.py`,
  `test_run_engine_transitions.py` — **44 pass**.
- `git diff --check` clean; `import app.main` OK.
- **Deferred to the final phase gate (not run):** full Phase 1B / 613-test hermetic regression,
  approval / MCP / eval suites, PostgreSQL, Ollama / ChromaDB service-backed tests, Docker /
  frontend builds.

### 29.4 Known limitations carried into MP-2+

- **No lifespan wiring** — inert until MP-3 starts it (an always-on loop with an empty registry
  would be a production no-op).
- **No per-run attempt counter** — recovery is `run_deadline_at` wall-clock bounded; a strict
  integer cap is a `runs` column (migration) if a later phase needs one.
- **`resuming`-state recovery** is out of scope (no approval-resume path exists yet — MP-2).
- **PostgreSQL** — the claim / lease / recovery CAS is PG-safe by construction (`SELECT … FOR
  UPDATE SKIP LOCKED` tightening is MP-6), unverified here (`EVAL_PG_TEST_URL` unset).
- **SQLite `DateTime(timezone=True)` round-trip drops `tzinfo`** — the worker normalizes
  DB-read datetimes to aware-UTC before any Python-side comparison (`_aware`).

---

## 30. MP-2 — as-built: durable one-time MCP approval resume + at-most-once dispatch (2026-09-09)

**Internal / in-process only.** No `/messages/stream` cutover, SSE, frontend, scheduler, reusable
session grants, or public Run route. Full detail:
`docs/superpowers/reports/phase-1b2r-mp2-completion.md`.

### 30.1 Files

New: `backend/app/run_engine/mcp_flow.py`; `tests/test_run_engine_mcp_resume.py` (21); this
section; the completion report.
Modified: `backend/app/run_engine/{worker,engine,events,__init__}.py`
(`+"mcp_call"` run kind; `RunWorker` MCP driver — `advance_mcp_runs` / `_advance_mcp_run` /
`_mcp_first_pass` / `_mcp_on_decision` / `_mcp_dispatch` / `_mcp_finalize`; `poll_once` advances
MCP runs; generic claim/recover exclude `mcp_call`; `_consume_cancel`/`_finalize_terminal` take
`frm`; parking at `waiting_for_approval` drops the lease),
`backend/app/approval/{state_machine,service}.py` (`+commit=False` on `approve`/`deny`/`_decide`/
`record_decision`), `backend/app/routes/approvals.py` (`_decide` commits the decision + audit +
`run_signals(approval_decided)` atomically via the ActionLog append),
`tests/run_engine_helpers.py` (`+uq_pending_approvals_active_dedupe`),
`THUNDER_AI_PRD.md` (§Status).
**No migration** — the identity linkage fits the existing MP-0 `run_steps` + Phase 1B.2
`pending_approvals` / `dispatch_records` columns.

### 30.2 Mandatory invariants — where each is enforced

| # | Invariant | Enforcement |
|---|---|---|
| 1 | approval API never executes | `_decide` only validates + flushes one allowlisted `run_signals` row + `record_decision(commit=False)`; never calls the gateway / worker / an executor. First worker pass uses a `_forbidden` `execute_mcp`. |
| 2 | server-owned identity binding | `GatewayContext` + `open_payload` `expected_*` built ONLY from `runs` / `run_steps` rows; `pending_approvals.trusted_envelope_json` carries tool_id/version/schema-hash/fingerprint/action-hash/dedupe-key; any mismatch → gateway `_block` / `RunPayload*` fail closed. |
| 3 | one-time approval only | `gateway.consume_approval` CAS `approved→consumed`; no `resolve_session_grant` path reached (none created); duplicate approve/deny → idempotent `_decide` early-return, `uq_run_signals_live` stops a duplicate signal; conflicting decision → existing 409. |
| 4 | trusted-registry revalidation | `gateway.dispatch` re-runs `registry.resolve_reason` immediately before dispatch (unknown/locked/disabled/schema-mismatch/fingerprint-mismatch/tenant-mismatch → `_block`); the worker maps the bounded `reason_code` to `runs.status_reason`. |
| 5 | fresh session validation | `_mcp_session_check` (live DB `validate_session_binding`, ctx from the run row, STRICT family) before consuming the signal, before `→resuming/running`, and immediately before the external invocation. Revoked/expired/other-family → `failed(session_revoked)`; no fallback family. |
| 6 | cancel wins before dispatch | live `cancel_requested` checked in `_mcp_on_decision` and again in `_mcp_dispatch` before `gateway.dispatch`; `→cancelling→cancelled`, no dispatch record, `approval_decided` signal left unconsumed. After `started` commits the run finishes on its true outcome — never a false undo. |
| 7 | at-most-once external dispatch | `dispatch_records` (`UNIQUE(tool_call_id)` + CAS `started` committed **before** `execute_mcp()`), unchanged. Worker reads the ledger on entry: succeeded→reuse, failed→failed, started/incomplete→`outcome_unknown`, none→proceed. Never an in-memory-only flag. |
| 8 | payload confidentiality | args + results only in `run_payloads` (context-bound Fernet envelope). Canary test: absent from every ordinary column, present only in `ciphertext`. |
| 9 | result handling | success → seal output payload → link to step → mark step + dispatch → **then** terminal run transition; seal failure → `outcome_unknown(output_persist_failed)`, never "succeeded"; ambiguous post-call → `outcome_unknown` with non-secret ids retained. |
| 10 | denial | no `started` record, no MCP call, decision consumed once, `waiting_for_approval→denied` terminal, cannot later be approved. |
| 11 | attempts | MCP external auto-invocation attempts hard-capped at 1; lease expiry / restart never creates a second invocation; no MCP retry loop; no per-run attempt migration. |

### 30.3 Focused verification

`compileall` clean · `test_run_engine_mcp_resume.py` **21 pass** · directly-affected
approval-gateway / approvals-API / approval-domain / MCP-dispatch / MCP-tool / RunWorker /
RunEngine-cancellation / -session-binding / -transitions / -events suites **296 pass / 3 skip** ·
`git diff --check` clean · `import app.main` OK. Broad Phase 1B / 613-test / eval / PostgreSQL /
Ollama / ChromaDB / Docker / frontend regression **deferred to the final phase gate**.

### 30.4 Carried into MP-3+

No lifespan / `/messages/stream` wiring (MP-3). `default_mcp_transport` unexercised against a
live MCP server (fakes only). PostgreSQL concurrency matrix + `FOR UPDATE SKIP LOCKED` = MP-6.
The stale unconsumed `cancel_requested` signal on a run that finished a real dispatch is left in
place (harmless; a terminal-run cancel is a deterministic no-op).

---

## 31. MP-3A — as-built: durable Run API contract + persisted text events + SSE reconnect (2026-09-09)

Additive, **feature-flagged off** (`run_public_api_enabled`). **No `/messages/stream` change, no
LLM generation, no frontend, no browser/Python/terminal execution, no reusable grants, no
scheduler.** MP-2 guarantees untouched. Full detail:
`docs/superpowers/reports/phase-1b2r-mp3a-completion.md`.

### 31.1 Files

New: `backend/app/run_engine/{submission,text_events,sse}.py`, `backend/app/routes/runs.py`;
`tests/test_run_engine_mp3a.py` (24); this section; the report.
Modified: `backend/app/run_engine/engine.py` (`submit_run(+identity_hash=,+commit=)` — an
explicit 64-hex hash is the authoritative run identity, replacing the interim structural tag;
`commit=False` for caller-owned submission atomicity), `.../events.py`
(`_try_append_event(+category=)`, `+read_run_events` — all categories, `run_seq`-ordered,
page-bounded), `.../__init__.py` (exports), `backend/app/config.py` (`+run_public_api_enabled`
off, `+run_sse_poll_interval_s/heartbeat_s/max_seconds`, `+run_text_event_max_bytes`;
`RUN_IDEMPOTENCY_HMAC_KEY` is **not** a Settings field), `backend/app/main.py` (register
`runs_router`; boot fails closed if the flag is on without the HMAC key),
`tests/test_run_engine_mcp_resume.py` (one MP-2 boundary assertion relaxed).
**No migration.**

### 31.2 Semantic-idempotency HMAC (decision 5A/5B)

`RUN_IDEMPOTENCY_HMAC_KEY` — env-only, no default, min-16 chars, required only when the flag is
on, fails closed (503 request / RuntimeError boot), never returned or logged.
`semantic_identity = HMAC-SHA256(key, "thunder-ai/run-semantic-identity/v1" || 0x00 ||
RFC-8785(canonical submission))`, 64-hex, fed to `submit_run(identity_hash=…)` as the
authoritative `(scope, key)` identity. The canonical `chat-run/v1` submission covers
`session_id, message, image_url, model` (server-resolved), `mode` (server-resolved + allowlisted
`chat|multi_agent|deep_research`), `system_prompt` (as a **keyed** HMAC under a separate domain,
never raw), and a sorted scalar `options` map. Raw prompt = an HMAC input only (keyed ⇒ not an
offline-dictionary target); never stored as identity metadata / `input_ref` / event / log. The
request body forbids extra fields, so a client cannot inject a fingerprint. Same key + same
semantic ⇒ same run; any behaviour-changing change ⇒ `IdempotencyKeyConflict` (409).

### 31.3 API + submission atomicity

`POST /api/runs` (Idempotency-Key required, CSRF), `GET /api/runs/{id}`,
`POST /api/runs/{id}/cancel` (CSRF, `request_cancellation` — intent only), `GET
/api/runs/{id}/events` (SSE). `require_run_public_api()` gates every route: 404 off, 503
misconfigured. `RunNotFound` **and** `SessionBindingMismatch` → 404 (non-disclosing);
`SessionBindingRequired` → 401. **No execution in any route.** `create_chat_run` persists — in
**one commit** (via `submit_run(commit=False)` + `transition_run`) — the user `Message`, the
`Run` (`created→queued`), the sealed input `RunPayload`, the initial `RunStep` (ids-only
`input_ref`), and the `run.created` + `run.queued` events. Replay adds nothing.

### 31.4 Text events + SSE

`run_events.category="text"` (`data={"text":…}`) share the per-run `run_seq`; control stays
immediate. `TextEventBuffer` batches a Python `str` by `run_event_text_flush_bytes` /
`_flush_ms`, `close()` flushes the remainder, over-cap flushes split on codepoint boundaries
(never mid-character); `run_text_event_max_bytes` caps a single event. `stream_run_events`:
DB-backed per-`run_seq` replay, `id:` lines, `Last-Event-ID` / `?after_seq` (never re-sends ≤
cursor), bounded 200-row pages, `: heartbeat` comments (0 ⇒ every idle poll, never a fake
event), refresh-family revalidation **every** poll (revoked ⇒ `event: error` + close), disconnect
closes the connection only (no cancel/mutation), `run_sse_max_seconds` cap, redacted errors, DB
as source of truth (no in-memory notifier yet).

### 31.5 Carried into MP-3B/MP-3C

Real streaming producer + live in-memory notifier + `/messages/stream` cutover = MP-3B/3C.
`create_chat_run` only queues — no chat handler is registered and the worker is still not
lifespan-wired. PostgreSQL concurrency matrix = MP-6.

---

## 32. MP-3B — as-built: worker-backed durable chat generation + durable text streaming (2026-09-09)

Behind `run_chat_stream_enabled` (off) **and** `run_engine_enabled`. Adds a **RunWorker chat
driver** — NOT lifespan-wired, NOT a `/messages/stream` cutover, NO frontend, NO fallback chain,
NO tool execution, NO reusable grants, NO scheduler.

### 32.1 Files

New `backend/app/run_engine/chat_stream.py` — smallest provider seam: `build_chat_input`
(opens the MP-3A sealed submission via `open_payload` with run/step-row expected context,
rebuilds session `messages` from the `Message` rows `<= message_id`; `system_prompt=None` —
MP-3A only sealed a keyed HMAC), `default_chat_provider` (**reuses `get_provider`**, pinned to
the sealed model snapshot, deployment `llm_provider` — never per-user settings),
`run_stream` (`asyncio.run` bridge over `generate_stream_async`, `aclose()`s the iterator),
`first_marker_index`, `CHAT_STATUS_REASONS`, typed `ProviderStream{Error,Timeout}` /
`StreamAborted` (no provider text). `worker.py`: `RunWorker(+chat_provider=)`, `_ChatHeartbeat`,
`advance_chat_runs` / `_advance_chat_run` / `_run_chat_generation` / `_finalize_chat_success`,
`poll_once` extended; `claim_next_queued` / `recover_expired` exclude `chat_stream` **only when
the flag is on**. `config.py` `+run_chat_stream_enabled`. **No migration**; `events.py` unchanged.

### 32.2 Invariants — where each is enforced

| Invariant | Enforcement |
|---|---|
| Provider attempts = 1; no auto re-invocation after start | step non-`pending` = durable "invocation began" proof; expired lease + `running` step → `outcome_unknown`, never re-invoked |
| Sealed input never in refs/events/logs/exceptions | `open_payload` (context-bound); driver writes only bounded `status_reason` / `error_code`; `raise ProviderStreamError() from None` |
| Streamed text = model-visible only, exact UTF-8, no markers | `TextEventBuffer` (codepoint-safe); `first_marker_index` publishes only the clean prefix, then fails closed `tool_resume_required` |
| Lease renewed only by owner; loss stops output + finalize | `_ChatHeartbeat.beat` → `renew_lease` (CAS on `lease_owner`); `_guard()` also re-checks row `lease_owner`/`version` every 8 tokens |
| No second worker reclaims a healthy generation | `advance_chat_runs` skips `running` + unexpired lease (`in_progress`) |
| Cancel checked before invocation / mid-stream / pre-finalise; cancel wins | `_advance_chat_run` pre-gate, `_guard()`, `_finalize_chat_success`; `_consume_cancel(frm=running)`; provider iterator `aclose()`d; partial text kept, no Message |
| Exactly one assistant Message; insert+step+run atomic | one `commit` in `_finalize_chat_success`; `_apply_transition` lease+`version` CAS is the sole finaliser authority; `output_ref` set ⇒ prior finalise won |
| Persistence failure ⇒ run non-successful | `_apply_transition` raise → rollback, no Message, stays `running`; `commit` raise → rollback → `outcome_unknown` (`output_persist_failed`) |

### 32.3 Focused verification

21 new tests (`tests/test_run_engine_mp3b.py`, fake providers) + 109 directly-affected
(`test_run_engine_{mp3a,worker,recovery,session_binding,cancellation,mcp_resume,transitions,events}`)
green; `compileall` + `git diff --check` clean. Broad / 613 / eval / PG / service-backed / Docker
deferred to the final phase gate.

### 32.4 Carried into MP-3C

Durable chat → MP-2 one-time-approval tool suspend/resume; `/messages/stream` cutover
(`submit()` + tail, disconnect ≠ cancel); durable system-prompt + provider snapshot in
`chat-run/v1`; free-tier fallback chain in the durable path; live in-memory notifier. Nothing
committed / merged / pushed.

---

## 33. MP-3C1 — as-built: durable chat parity + stateful stream safety + feature-flag hardening (2026-09-09)

Hardens MP-3B. Still NO `/messages/stream` cutover, lifespan wiring, frontend, chat→MCP resume,
or tool execution.

### 33.1 Files

`chat_stream.py` **rewritten**: `build_exec_snapshot` / `EXEC_SNAPSHOT_VERSION` (versioned
snapshot: provider, model, mode, resolved system prompt, sorted options — **never a key**);
`build_chat_input` reads the snapshot and **fails closed** on missing/unknown version (no
deployment-provider / `system_prompt=None` fallback); `default_chat_provider(chat_input, *, db,
user_id)` — provider+model from the snapshot, live credential applied only on a provider match,
no `build_fallback_chain`; `MarkerScanner` (bounded held-back suffix, chunk-boundary-safe, never
emits full/partial marker text); `run_stream` loop-safe (dedicated thread + own loop under a
running loop) + stall-safe (`asyncio.wait(timeout=poll_interval)` → `on_idle()` each idle tick).
`submission.py`: `chat-run/v2`, `+provider` in `ChatRunSubmission` / `_canonical_bytes` /
`semantic_identity`; `create_chat_run` seals `{message,image_url,exec:<snapshot>}`;
`+RunFeatureNotExecutable` + `assert_chat_stream_executable()`. `routes/runs.py`: submit handler
calls `assert_chat_stream_executable()` **before persistence** (503, no queued run) and resolves
the provider server-side into the submission+snapshot. `worker.py`: `claim_next_queued` /
`recover_expired` **always** exclude `chat_stream`; `_run_chat_generation` uses `MarkerScanner`,
the new `run_stream` signature, `on_idle`, and flushes the scanner tail on a clean end. **No
migration** (snapshot rides the MP-3A sealed payload; poll interval reuses
`run_worker_lease_ttl_s // 3`).

### 33.2 Invariant deltas vs §32.2

| Invariant | Enforcement |
|---|---|
| Execution snapshot authoritative; no substitution | `build_chat_input` raises `chat_input_error` on a missing/unknown snapshot; provider+model+system prompt come only from it |
| Semantic HMAC covers `provider` | `_canonical_bytes` (`chat-run/v2`); changed provider + same key ⇒ `IdempotencyKeyConflict` |
| Snapshot never carries credentials | api key resolved live by `user_id` at exec time, applied only on a provider match; absent from the submission + sealed snapshot |
| Marker scan chunk-boundary-safe, bounded, no partial leak | `MarkerScanner` holds `< max-marker-length`; `feed()` returns safe prefix only; `""` after a trip |
| Provider bridge loop-safe | `run_stream` → dedicated thread when `get_running_loop()` succeeds |
| Liveness checks under a stall | `_adrive` idle-tick `on_idle()` → deadline ⇒ `provider_timeout`, else `_guard()` |
| `chat_stream` always worker-reserved; invalid combos fail closed | unconditional exclusion; `assert_chat_stream_executable()` at the submit route |
| No implicit paid fallback | single snapshot provider; `build_fallback_chain` unused (asserted) |

### 33.3 Focused verification

21 new tests (`tests/test_run_engine_mp3c1.py`) + 21 MP-3B + 24 MP-3A + 119 directly-affected
(`test_run_engine_{worker,recovery,session_binding,cancellation,transitions,events,mcp_resume,submit}`)
green; `compileall` + `git diff --check` clean; `import app.main` OK. Broad / 613 / eval / PG /
service-backed / Docker deferred to the final phase gate.

### 33.4 Carried into MP-3C

`/messages/stream` cutover, lifespan wiring, frontend, durable chat → MP-2 tool suspend/resume,
budget-guarded fallback chain, live notifier, PG matrix. A run stranded `running` by a
mid-flight flag flip = MP-6 reconcile. Nothing committed / merged / pushed.

---

## 34. MP-3C2 — as-built: durable chat → MCP approval → same-run resume (2026-09-11)

Rides `run_chat_stream_enabled` (+ `run_engine_enabled`) — **no new flag**. Still NO lifespan
wiring, NO `/messages/stream` cutover, NO frontend, NO Python/shell/browser/desktop execution,
NO reusable session grants, NO scheduler.

### 34.1 What was reused unchanged

The MP-2 spine is **run-kind-agnostic**; MP-3C2 adds glue, not a second implementation:

* `app.approval.gateway.dispatch` — trusted-registry revalidation, `proposal_dedupe_key`,
  one-time `consume_approval`, `dispatch_records` at-most-once (`UNIQUE(tool_call_id)` +
  started-before-execute CAS), sanitized audit. **Untouched.**
* `mcp_flow.mirror_decision_signal` — keys on `run.pending_approval_id` + `run.status ∈
  {created,queued,running,waiting_for_approval}`; fires for a `chat_stream` run as-is.
  `open_mcp_args` / `default_mcp_transport` / `MCP_STATUS_REASONS` reused. **Untouched.**
* `routes/approvals.py::_decide` — **zero changes** (one `run_signals(approval_decided)` flushed
  atomically with the decision; no MCP / provider / inline resume; no `/consume` API).
* `RunWorker._mcp_first_pass` / `_mcp_on_decision` / `_mcp_dispatch` / `_mcp_finalize` /
  `_mcp_session_check` — a chat run's `tool_dispatch` `RunStep` is shaped identically to an
  `mcp_call` run's; the only delta is a `claimed.kind == chat_stream` branch in `_mcp_finalize`
  that **continues generation** instead of terminating.
* State machine — `running→waiting_for_approval→approved→resuming→running` + terminals already
  exist. **No new run state.** `run.status` events narrate the flow. **No new control event.**
* Schema — `RunStep{seq (UNIQUE(run_id,seq)), execution_turn_id, tool_call_id, approval_id,
  input_payload_id, output_payload_id}` + `Run.pending_approval_id` already exist.
  **No migration.**

### 34.2 Files

New: `backend/app/run_engine/chat_tools.py`, `tests/test_run_engine_mp3c2.py`,
`docs/superpowers/reports/phase-1b2r-mp3c2-completion.md`.
Modified: `backend/app/config.py` (`+run_chat_tool_marker_max_bytes`,
`+run_chat_max_tool_cycles`, `+run_chat_tool_result_max_bytes`, all validated `>= 1`);
`backend/app/run_engine/chat_stream.py` (`MarkerScanner(+capture=,+max_capture_bytes=)`);
`backend/app/run_engine/worker.py` (`_CHAT_ADVANCEABLE`; `advance_chat_runs` /
`_advance_chat_run` restructure keyed on the latest step; `_advance_chat_tool_run`,
`_chat_suspend_for_tool`, `_chat_continue_after_tool`, `_build_continuation_input`,
`_route_tripped_scanner`; `_finalize_chat_success` multi-segment concat; `_mcp_finalize` chat
branch); `tests/test_run_engine_{mp3b,mp3c1,mp3c1_closeout}.py` (3 tests updated for the
superseded "any `[MCP_CALL]` fails closed" contract).

### 34.3 Same-run state / step flow

```
run:   created → queued → running ──(complete [MCP_CALL] captured)──►
       waiting_for_approval  (lease dropped, run.pending_approval_id set)
   ──(POST /api/approvals/{id}/approve → run_signals(approval_decided), NOTHING executed)──►
       approved → resuming → running   (signal consumed once; fresh family + cancel re-checks)
   ──(gateway.dispatch real transport, execute_mcp once; result sealed)──►
       running  (continuation llm_generate segment)
   ──► … ≤ 3 tool cycles …  ──► succeeded   (ONE assistant Message; running→succeeded, one commit)
   denied ⇒ waiting_for_approval→denied (terminal; no execution, no continuation)

steps: seq1 load_context   pending→running→succeeded  sealed clean segment-1 text
       seq2 tool_dispatch  pending→waiting→running→succeeded/failed/incomplete
                           execution_turn_id·tool_call_id·approval_id·input_payload_id(tool_args)
                           ·output_payload_id(tool_output)·dispatch_records(tool_call_id)
       seq3 llm_generate   pending→running→succeeded  sealed clean segment-2 text
       seq4 tool_dispatch  … (cycle 2)     seq5 llm_generate … (continuation 2)
       last llm_generate   → Message(assistant) = ordered concat of every sealed segment
```

`_chat_suspend_for_tool` opens the server-owned `ExecutionTurn`/`ToolCall` first (they commit
on their own — a crash there only orphans harmless identity rows), then does **ONE atomic
commit**: {seal segment-N clean text → `output_payload_id`; gen step `running→succeeded`; seal
`canonicalize(args)` → `tool_args` `RunPayload`; create `tool_dispatch` step `pending` with
turn/call/args ids}. After that commit the marker request is fully durable and a restart replays
the gateway first pass dedupe-safe. `_build_continuation_input` rebuilds the provider input from
durable state only: MP-3C1 execution snapshot (provider / model / mode / resolved system prompt /
options — **preserved**) + each prior sealed segment as an `assistant` message + each sealed tool
result wrapped by the canonical `format_mcp_result` envelope as an **untrusted, size-bounded
`user` message** (never the system prompt).

### 34.4 Marker capture

`MarkerScanner(capture=True)` — MP-3C1 chunk-split detection, emits only the clean prefix, then
privately buffers post-opener chunks until the marker **line** closes (`\n`, or EOF with a `]`
present) → `captured` = that one line. `> run_chat_tool_marker_max_bytes` → `overflow`; EOF
mid-line, no `]` → `incomplete`; a non-MCP execution opener → `unsupported` (stays blocked with
`tool_resume_required`). Post-marker text on the same pass is discarded. The provider iterator is
always `aclose()`d (`_adrive` `finally`). `chat_tools.parse_one_mcp_call` runs the captured line
through the repo's ONE parser (`app.tools.mcp_tool.extract_mcp_calls`) and requires **exactly
one** call (0 → `tool_marker_malformed`, `>1` → `tool_marker_multiple`). One MCP call per
suspension cycle.

### 34.5 Limits (durable, not in-memory)

`_chat_suspend_for_tool` counts `tool_dispatch` `run_steps` on the run: `≥ run_chat_max_tool_cycles`
(3) → `tool_cycle_limit` fail-closed with **no** new proposal / turn / dispatch; any non-terminal
`tool_dispatch` step present → same (one active proposal per run). The gateway's per-turn /
per-session / per-user active-proposal ceilings apply unchanged. Provider attempts per segment =
1; `default_chat_provider` has no fallback chain (a paid model is never an implicit fallback).

### 34.6 Crash-window table (restart = fresh `advance_chat_runs`)

| Crash point | Recovery | MCP re-invoked? | Message? |
|---|---|---|---|
| clean text sealed, before the atomic tool-step commit | gen step still `running` → `outcome_unknown` | no | no |
| atomic tool-step commit landed, before proposal | latest step `tool_dispatch/pending` no `approval_id` → `_mcp_first_pass` replay; `proposal_dedupe_key` → SAME proposal | no | no |
| proposal created, before park | as above → re-link + park | no | no |
| restart while `waiting_for_approval` | `_mcp_on_decision`, no signal → drop lease, stay parked | no | no |
| `approval_decided` persisted, not consumed | consumed once (rowcount-1 CAS); racing worker gets 0 rows | no | no |
| consumed, before `dispatch_records.started` | `_mcp_dispatch` → dispatch once | yes, once | continues |
| `started`, no terminal dispatch row | `outcome_unknown` (`dispatch_incomplete`) | no | no |
| `succeeded` + result sealed, before continuation step | `duplicate_dispatch` + record `succeeded` + `output_payload_id` → **reuse**, continue | no | continues → yes |
| result sealed, before continuation step creation | `_chat_continue_after_tool` recreates the step | no | continues |
| continuation step `pending`, before provider | latest `llm_generate/pending` → reclaim + run | no | no |
| continuation step `running` at crash | latest `llm_generate/running` + expired → `outcome_unknown` | no | no |
| Message inserted, `running→succeeded` transition fails | `_apply_transition` raises → rollback → no Message, run `running` | no | no |
| commit raises after the transition | `outcome_unknown` (`output_persist_failed`) | no | no |

Final success requires one committed `Message` — the `_apply_transition` lease + `version` CAS is
the sole finaliser authority; a re-entered `_finalize_chat_success` sees `output_ref.message_id`
and no-ops to `succeeded`.

### 34.7 Focused verification

27 new tests (`tests/test_run_engine_mp3c2.py`) + 84 directly-affected
(`test_run_engine_{mp3c1_closeout,mp3c1,mp3b,mcp_resume}` — 3 updated for the superseded
"any `[MCP_CALL]` fails closed" contract) green; `compileall` + `git diff --check` clean;
`import app.main` OK. 119-group / 613 / eval / PG / service-backed / Docker / frontend /
live-provider deferred to the final Phase 1B gate.

### 34.8 Carried into MP-3C3+

`/messages/stream` cutover, lifespan worker startup, frontend/SSE UI, live notifier, PG
concurrency matrix (`SELECT … FOR UPDATE SKIP LOCKED`), budget-guarded fallback chain. Nothing
committed / merged / pushed.

---

## 35. MP-3C3A — as-built: FastAPI worker lifespan + operational safety (2026-09-11)

Wires the MP-1B `RunWorker` into the one canonical FastAPI lifespan through a thin
`RunWorkerSupervisor`. Still NO `/messages/stream` cutover, NO frontend/SSE change, NO chat-UX
change, NO new run state / control event / migration.

### 35.1 Files

New: `backend/app/run_engine/worker_supervisor.py`, `tests/test_run_engine_mp3c3a.py`,
`docs/superpowers/reports/phase-1b2r-mp3c3a-completion.md`.
Modified: `backend/app/config.py` (`+run_worker_web_concurrency=1`,
`+run_worker_shutdown_grace_s=30`, `+run_worker_backoff_min_s=2`, `+run_worker_backoff_max_s=60`,
all validated `>=1`); `backend/app/main.py` (lifespan wiring + `GET /api/health/worker`;
`/api/health` unchanged); `backend/app/run_engine/worker.py`
(`RunWorker.attach_stop`/`_stopping()` + stop checks in `claim_next_queued`/`advance_mcp_runs`/
`advance_chat_runs`/`_run_chat_generation._guard()`/`run_forever` — all additive, inert without a
supervisor); `backend/app/run_engine/__init__.py` (exports).

### 35.2 Startup ordering

```
lifespan enter → dirs · JWT-secret · ephemeral-SQLite warn · _migrate_knowledge_graph
  → init_db() → _migrate_database() (all _migrate_* + _verify_run_engine_schema — HALTS on gap)
  → _create_default_user() → assert_hmac_key_configured() (iff run_public_api_enabled)
  → start_scheduler()
  → app.state.run_worker_supervisor = RunWorkerSupervisor() (only if absent); await .start()
        .start(): no-op unless run_engine_enabled AND run_worker_enabled;
                  assert_embedded_topology_ok() raises SupervisorFatalError (HALTS startup) if
                  effective web-process count > 1 AND database_url is sqlite;
                  build RunWorker(SessionLocal), attach_stop(stop_event.is_set),
                  threading.Thread(target=_run_loop, name="run-worker", daemon=True).start()
yield
lifespan exit → await supervisor.stop() ; app.state.run_worker_supervisor = None ; shutdown_scheduler()
```

> **Correction (lifecycle close-out, 2026-09-11):** the original §35 draft ran the sweep loop as
> an `asyncio.Task` doing `await asyncio.to_thread(worker.poll_once)` and claimed the executor
> thread was a daemon that "ends with the process". **Both are false** — a runtime probe (CPython
> 3.11.15) showed `asyncio.to_thread` threads are `daemon=False`, cancelling the awaiting task
> does not stop the running function, and `loop.shutdown_default_executor()` *joins* it. The
> execution model is now **one explicitly owned `threading.Thread(name="run-worker", daemon=True)`**
> — see §35.11.

The worker thread is created ONLY inside `.start()`, ONLY after every boot check succeeds. Nothing
starts a thread at import. `.start()` is idempotent (`self._started`); repeated lifespan entry /
`TestClient(app)` never duplicates the thread.

### 35.3 Thread / session ownership  *(see §35.11 for the corrected model)*

| Concern | Owner |
|---|---|
| sweep + backoff loop | one owned `threading.Thread` **`run-worker`, `daemon=True`** running `_run_loop` |
| every DB `Session` | opened+closed inside `RunWorker._session()` / `TextEventBuffer` / `_ChatHeartbeat`, each on the thread that opened it — **no Session crosses a thread** |
| provider coroutine | driven **inline** on the worker thread (`run_stream` sees no running loop) — no `chat-provider-bridge` thread in the deployed path |
| lease heartbeat | `chat-lease-hb` thread, own short-lived Session, joins on stop |
| cooperative stop | `threading.Event` owned by the supervisor; `worker.attach_stop(event.is_set)` — read on the worker thread, set on the loop thread |

`_run_loop` reuses `worker.poll_once` **unchanged**; it does not reuse `run_forever` (which
swallows errors, has no backoff/fatal path/status and no stalled-provider shutdown seam).
`run_forever` is retained for standalone/MP-1B use.

### 35.4 Shutdown

`stop()` → set the Event → `claim_next_queued`/`advance_mcp_runs`/`advance_chat_runs` accept no
new work; an in-flight `_run_chat_generation._guard()` (every 8 tokens) / `_on_idle()` (every
`lease_ttl//3` s while stalled) sees the Event → `StreamAborted` with `state["abort"]="shutdown"`
→ returns `RunOutcome(abandoned,"worker_shutdown")`: **NO Message, NO terminal transition** — run
stays `running`, lease expires, next boot recovery ⇒ `outcome_unknown` (never a false success,
never a false cancel). The `run-worker` loop exits (~stop-Event granularity). `stop()` waits up
to `run_worker_shutdown_grace_s` (30 s) for the thread to finish via a **non-blocking
`await asyncio.sleep(0.05)` poll of `is_alive()`** — no `join`, no `task.cancel()`. On timeout the
thread is **left running** (`daemon=True` ⇒ it cannot block process exit) and the status reports
`shutdown_deadline_exceeded` while staying `stopping`/`alive` — see §35.11. A
`dispatch_records.started` MCP dispatch is untouched ⇒ next boot `outcome_unknown`, never
re-executed / never claimed undone.

| Run state at shutdown | Outcome |
|---|---|
| `queued` / `waiting_for_approval` | unchanged — resumable next boot |
| step still `pending` (not invoked) | lease expires → reclaimed next boot |
| generation already started, interrupted | stays `running` → `outcome_unknown` next boot; never `succeeded` |
| MCP dispatch `started`, no terminal | `outcome_unknown` next boot; never re-executed |
| committed final Message + `succeeded` | terminal, untouched |

### 35.5 Failure / backoff

`_run_loop` wraps each `poll_once` in **`except Exception`** (operational only) plus a dedicated
`except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit)` that does minimal
cleanup + **re-raises** (`worker_interrupted`, never counted / classified / backed off — see
§35.11). `_classify(exc: Exception) → (bounded_code, is_fatal)`: schema-shaped
`OperationalError/ProgrammingError` (`no such table` / `no such column` / `does not exist`) →
`("schema_error", True)`; `OperationalError/InterfaceError/DBAPIError/TimeoutError` →
`("db_unavailable", False)`; `SupervisorFatalError` → `(code, True)`; else `("sweep_error",
False)`. Recoverable ⇒ `consecutive_failure_count++`, bounded exp backoff
`backoff_min → backoff_max` (interrupted promptly by the stop Event), **reset after any clean
sweep**. Fatal, or `consecutive_failure_count >= 12` (`max_consecutive_failures`) ⇒ log
`critical` + stop the task (no retry-forever, no spin). `last_bounded_error_code` is a fixed
slug; no raw exception text / secret is ever stored or logged.

### 35.6 Health / readiness

`GET /api/health/worker` → `{"durable_run": <status>, "durable_path_ready": <bool>}`. `status`
keys exactly: `enabled, started, alive, stopping, last_successful_poll_at (ISO|null),
last_bounded_error_code (slug|null), consecutive_failure_count`. **Never** a prompt / tool name /
args / result / provider error / API key / user id / session id / run id / DB URL.
`durable_path_ready` = True when `run_public_api_enabled` is off; else True **only** when the
worker is enabled, `alive`, and not in a fatal state — otherwise the endpoint returns **HTTP
503**. `/api/health` (legacy) is byte-for-byte unchanged with the durable flags off or on.

### 35.7 Deployment topology

Supported: self-host, **exactly one web process**, SQLite or PostgreSQL; the worker runs inside
that process's event loop. Only one web process may enable the embedded worker before the MP-6
PostgreSQL verification. `run_worker_web_concurrency` (operator-declared count) plus a best-effort
env read (`WEB_CONCURRENCY` / `GUNICORN_WORKERS` / `UVICORN_WORKERS` / `GUNICORN_CMD_ARGS -w N`);
`> 1` + SQLite + worker enabled ⇒ **startup halted** (`SupervisorFatalError`). PostgreSQL
multi-process is PG-safe by construction but the concurrency matrix + `FOR UPDATE SKIP LOCKED`
are the MP-6 gate — this phase claims no hosted/multi-process safety. A future dedicated worker
process reuses `RunWorker(session_factory)` + `RunWorkerSupervisor(worker_factory=…)` unchanged.
No Redis / Celery / scheduler framework / queue introduced.

### 35.8 Marker adversarial result

No parser weakness — no parser change. `MarkerScanner` captures one line;
`extract_mcp_calls`'s greedy-with-backtracking `\{.*\}` + `json.loads` locks onto the first
`}]`-terminated body that validates and ignores trailing text; internal `]`/`}` inside JSON
strings/arrays/objects are handled by `json.loads`; a multi-line or never-valid body ⇒ zero calls
⇒ `ChatToolMarkerError` (`tool_marker_malformed`) ⇒ fail closed. Clean prefix always preserved;
marker/args/trailing text never emitted.

### 35.9 Focused verification

31 new tests (`tests/test_run_engine_mp3c3a.py`) + `test_run_engine_{worker,recovery,mp3b,mp3a}`
+ MP-3C2 happy-path/plain-chat spot checks (98 total) green; `compileall` + `git diff --check`
clean; `import app.main` OK. 119-group / full 111-test MP-3C2 / 613 / PG / Docker / service-backed
/ frontend / live-provider deferred to the final Phase 1B gate.

### 35.10 Carried into MP-3C3B+

`/messages/stream` cutover, frontend/SSE UI, live notifier, budget-guarded fallback chain, PG
concurrency matrix, dedicated worker process. Nothing committed / merged / pushed.

### 35.11 Lifecycle close-out (2026-09-11) — corrected execution & shutdown model

**Proven at runtime (CPython 3.11.15), correcting the original §35 draft:**
`asyncio.to_thread` threads are `daemon=False`; `future.cancel()` does not stop the running
function; `loop.shutdown_default_executor()` (run by `asyncio.run` on close) *joins* a still-running
`to_thread` call (~0.6 s measured). ⇒ an uncooperative in-flight call under the old model could
keep FastAPI shutdown alive past the grace period.

**Corrected model — one owned `threading.Thread(name="run-worker", daemon=True)`:**
`start()` builds the worker, `attach_stop(self._stop.is_set)`, and starts the thread running
`_run_loop` (the poll + classify + backoff loop; `threading.Event.wait(timeout)` is the
stop-interruptible sleep — no asyncio on the thread). `daemon=True` ⇒ a stuck thread can **never**
block `asyncio.run()` / process exit; the abrupt daemon kill is a *safety net only*, never the
normal path (cooperative stop is). `alive` = `self._thread.is_alive()` (the truth). `stop()` sets
the Event and polls `is_alive()` with `await asyncio.sleep(0.05)` up to
`run_worker_shutdown_grace_s`; **no `join`, no force-kill**.

**Exception boundary:** `_run_loop`'s sweep-failure catch is `except Exception` (operational only);
a dedicated `except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit)` does
minimal cleanup (`last_bounded_error_code = "worker_interrupted"`, **no** count bump, **no**
backoff) then **re-raises** so the thread ends. `_classify` is typed `(exc: Exception)` — the four
interrupt classes never reach it and are never `sweep_error`/`db_unavailable`.

**Stuck-stopping is honest:** if the grace expires while `is_alive()`, `stop()` keeps
`started=True` / `stopping=True`, sets `last_bounded_error_code="shutdown_deadline_exceeded"`, and
`alive` keeps tracking `is_alive()` — it never claims the worker stopped. `is_durable_path_ready()`
returns **False** whenever `stopping` or `self._stop.is_set()`, plus the fatal-code set now
includes `shutdown_deadline_exceeded`. A later `stop()` (or the thread finishing) completes
cleanup. `/api/health/worker` then shows `alive:true, stopping:true, durable_path_ready:false`,
HTTP 503.

**Web-concurrency guard:** `assert_embedded_topology_ok()` uses
`max(run_worker_web_concurrency, detected)` (`WEB_CONCURRENCY`/`GUNICORN_WORKERS`/`UVICORN_WORKERS`/
`GUNICORN_CMD_ARGS -w N`); a **disagreement is logged (WARNING)**. **Only** SQLite + effective
count > 1 is auto-blocked (`SupervisorFatalError`, halts startup). **PostgreSQL multi-process is
not auto-protected** (MP-6 gate) — `run_worker_web_concurrency` must be set to the real count.

**Residual limitation (accepted):** a truly uncooperative *synchronous* third-party call inside
`poll_once` cannot be stopped without force-killing a thread. Embedded self-host stays honest &
safe (no process-exit hang, truthful status, no falsified run, atomic+crash-recoverable
sub-operations). The only fully-bounded fix is a **separate worker process** — a future MP-6
production topology, **not built here**; the `RunWorker` / `RunWorkerSupervisor(worker_factory=…)`
seams already support it.

**Tests:** `tests/test_run_engine_mp3c3a.py` = **41 passed** (31 + 10 close-out — cancellation/
BaseException boundary ×5, truthful thread shutdown ×3, `alive` tracks thread ×1, web-concurrency
mismatch warning ×1). `compileall` + `git diff --check` clean; `import app.main` OK.

---

## 36. MP-3C3B — as-built: durable `/messages/stream` cutover + DB-authoritative notifier (2026-09-11)

Feature-flagged (`run_messages_stream_durable_enabled`, OFF by default). Backend-only — NO
frontend change, legacy pipeline retained, flag in no default/deployment config, no production
cutover claimed. NO migration / new run state / new control event.

### 36.1 Files

New: `backend/app/run_engine/notifier.py`, `backend/app/run_engine/messages_stream.py`,
`tests/test_run_engine_mp3c3b.py`, `docs/superpowers/reports/phase-1b2r-mp3c3b-completion.md`.
Modified: `backend/app/config.py` (`+run_messages_stream_durable_enabled`,
`+run_messages_stream_max_concurrent_per_user=4`, `+run_messages_stream_submits_per_min=30`);
`backend/app/routes/messages.py` (`stream_chat` +optional `_bearer=Depends(bearer_scheme)` and a
first-line `if settings.run_messages_stream_durable_enabled: return await _stream_chat_durable(...)`
— legacy body otherwise unchanged; new `_stream_chat_durable`); `backend/app/main.py` (lifespan
`run_notifier.bind_loop()` / `unbind_loop()`); `backend/app/run_engine/__init__.py` (exports).

### 36.2 Request / wire compatibility matrix

`ChatRequest {message(1..10000), image_url?}` — the **only** legacy body fields; both map into
`chat-run/v2` (`image_url` set ⇒ rejected pre-persistence). Wire: `text/event-stream` + the legacy
`Cache-Control`/`X-Accel-Buffering`/`Connection` headers + `X-Run-Id` (frontend ignores unknown
headers). Framing `event: <n>\ndata: <json>\n\n` (via `streaming_service.format_sse`) + an
SSE-standard `id: <run_seq>` line (`useStreaming.ts` `parseSingleEvent` reads only `event:`/`data:`
lines ⇒ `id:`/`:` skipped; `switch(event.type)` has no default ⇒ unknown events ignored).
Events: `start` (once, cursor 0), `token {"token"}`, `run_status {"status"}` (new, ignored),
`tool_approval_required {"approval_id","tool_id"}` (**redacted**), `complete {"message_id",
"citations":[], "sources_used":false, "memories_used":false}`, `error {"code","detail"}`,
`cancelled {"detail"}`. `plan` / `agent_status` / citations / confidence / deep-research progress:
**never emitted / never fabricated** (those modes are rejected). One assistant `Message`
(worker's `_finalize_chat_success`, atomic with `run→succeeded`). `Last-Event-ID` / `?after_seq`
replay supported backend-side (never re-sends `≤ cursor`); the current frontend does not use it.
Disconnect closes the connection only — the run is **not** cancelled and its `Message` is still
persisted (a deliberate difference from legacy `cancelled`+no-save).

### 36.3 Route decision flow (`_stream_chat_durable`)

```
flag off ─────────────────────────────► exact legacy stream_chat body (unchanged)
flag on:
  assert_durable_stream_ready(app.state.run_worker_supervisor)   → 503 (bounded) before persistence
     requires: run_engine_enabled ∧ run_worker_enabled ∧ run_chat_stream_enabled
             ∧ run_public_api_enabled ∧ valid RUN_IDEMPOTENCY_HMAC_KEY
             ∧ assert_chat_stream_executable ∧ supervisor.is_durable_path_ready()
  session_id/length validation (mirrors legacy)
  unsupported_mode_code(has_image):  image | enable_deep_research | enable_multi_agent → 422 before persistence
  Idempotency-Key header required + MP-1A charset/length         → 422
  bearer token required → build_auth_context (sid family)        → 401
  submit_rate_limited(user_id)  (rate_limiter seam)              → 429
  resolve_and_submit → build_chat_submission (server-resolved provider/model/mode/system_prompt)
                     → create_chat_run  (MP-3A semantic HMAC; NO executor)   → 409 on conflict / 404 owner
  acquire_stream_slot(user_id)  (per-user concurrency cap)       → 429
  StreamingResponse(stream_legacy_wire(...), headers={…, X-Run-Id: run.id})
```

### 36.4 Event / reconnect flow (`stream_legacy_wire`)

`subscribe(run_id)` → loop: `is_disconnected()`? / `run_sse_max_seconds`? / open a short session →
`validate_session_binding` (STRICT family, per poll; failure ⇒ generic `error` `stream_closed`) →
`read_run_events(after_seq=cursor, limit=run_event_read_max_page)` → map each row to a legacy
frame (§36.2), advance `cursor`, stop on a terminal `run.status`; a reconnect whose cursor is
past a terminal event still gets a synthesized `_terminal_frame`. When no new rows: emit a
`: heartbeat` (never persisted), then `await asyncio.wait_for(ev.wait(), timeout=run_sse_poll_interval_s)`
then `ev.clear()`. `finally: unsubscribe`. DB is authoritative — the wake only shortens the wait.

### 36.5 Notifier semantics

`RunNotifier` (one/process, single-process only). `notify(run_id)` is safe from any thread
(`loop.call_soon_threadsafe`); `_wake` sets each `asyncio.Event` on the app loop. Publication =
global ORM `Session` hook: `before_flush` accumulates `RunEvent`/`Run` `run_id`s onto
`session.info` across every flush; `after_commit` fires **one** `notify` per `run_id`;
`after_rollback` clears ⇒ wake **only after commit**. Bounded: ≤64 waiters/run, ≤4096 runs (over
cap ⇒ unregistered Event ⇒ poll-only fallback). Payload ⊆ `{run_id}` — no prompt/text/args/
result/provider output/user id/session id/secret. Missed / early / duplicate wake ⇒ the next DB
re-query still returns the row exactly once; broken notifier ⇒ bounded polling.

### 36.6 Unsupported-mode boundary

`enable_deep_research` / `enable_multi_agent` default **True** ⇒ the durable flag is usable only
when an operator also turns both off (documented dependency). Images always rejected. All
rejections are 422 **before** `create_chat_run` — no partial persistence, no fallback to the
legacy pipeline once the durable branch is taken.

### 36.7 Failure / race table

| Situation | Behavior |
|---|---|
| submission commit fails | `create_chat_run` is one txn ⇒ no partial Run/Message/RunStep/payload |
| disconnect right after submit | generator returns; run continues; Message still persisted |
| reconnect during generation | cursor-resumed; no `run_seq ≤ cursor` re-sent ⇒ no duplicate text |
| event committed between query and wait | wait times out (or `after_commit` wake) ⇒ next query returns it |
| wake before subscriber waits | next DB re-query finds it (DB authoritative) |
| broken notifier | falls back to bounded polling (tested) |
| worker dies / shuts down mid-stream | readiness checked at submit; a stuck run heartbeats to `run_sse_max_seconds`; MP-3C3A never falsely cancels; interrupted generation ⇒ `outcome_unknown` ⇒ `error`, never a false `complete` |
| terminal failure | cannot become `succeeded`; terminal `error` is final |
| late approval on denied/cancelled run | cannot resurrect it; no dispatch |
| cross-user stream read | `validate_session_binding` fails ⇒ generic `stream_closed`; no run data leaked |

### 36.8 Verification

`tests/test_run_engine_mp3c3b.py` = **39 passed** (flag/compat 6, submission 5, streaming/wire
10, notifier 6, approval journey 4, failure/race 6, privacy 2). `test_run_engine_mp3a.py` **24
passed**, `test_run_engine_mp3c3a.py` **41 passed** (lifespan touched). `compileall` +
`git diff --check` clean; `import app.main` OK. 111-test MP-3C2 / 119-group / 613 / PG / Docker /
frontend / live-provider deferred to the final Phase 1B gate.

### 36.9 Carried into MP-3C3B+ / frontend

Frontend `useStreaming.ts` cutover (send `Idempotency-Key`, read `X-Run-Id`, reconnect via
`Last-Event-ID`), deep-research / multi-agent / image / citation mode parity, and multi-process
notifier delivery (MP-6 PG gate). Nothing committed / merged / pushed.

## 37. MP-3C3C — as-built: frontend durable stream, reconnect, and MCP approval UX (2026-09-11)

**implemented, uncommitted, not production-ready.** Frontend only + one additive CORS
allow/expose entry. `run_messages_stream_durable_enabled` stays OFF; with it off the frontend
path is byte-for-byte the legacy stream. Supports **plain chat + durable MCP approval/resume
inside plain chat** only. Out of scope (unchanged): deep-research / multi-agent / image / media
on the durable path, citation & confidence parity, enabling the flag, deleting legacy code,
browser/terminal/Python execution, reusable grants, paid providers.

### 37.1 Files

| File | Change |
|---|---|
| `frontend/src/lib/sseParser.ts` (new) | `SSEStreamParser` — incremental, dual wire. `feedBytes`/`feed`/`flush`, `remainder`. Normalises CRLF→LF, splits on `\n\n`, buffers the trailing incomplete frame, bounds it to `MAX_SSE_BUFFER` (1 MiB), `TextDecoder(stream:true)` for split UTF-8. `parseFrame` reads `event`/`data`/`id`, strips one leading SP, skips `:`-comments, `id` accepted only as a non-negative safe integer. Legacy `parseSSEBuffer(buffer)` shim kept byte-identical (JSON-parse each frame, drop on failure, return `{events,remainder}`). |
| `frontend/src/useStreaming.ts` (rewritten) | Dual-mode hook. Legacy behaviour preserved when the response has **no `X-Run-Id`**. Durable additions below. |
| `frontend/src/api.ts` | `+getApproval / approveRun / denyRun / cancelRun` (all via the existing `request<T>` wrapper → bearer + CSRF + 401-refresh-retry). |
| `frontend/src/types.ts` | `+ApprovalDetail / ApprovalDecision / RunCancelResult`. |
| `frontend/src/components/ApprovalDialog.tsx` (new) | Accessible Radix modal; fetches the redacted detail, explicit one-shot Approve/Deny. |
| `frontend/src/App.tsx` | Wires `durableRunId` / `phase` / `cancelDurableRun` / `resumeAfterApproval`; renders `ApprovalDialog` + a non-blocking `role="status"` reconnect/resume/cancel toast; routes the Stop button to `cancelDurableRun` when a run id exists. |
| `backend/app/main.py` | CORS: `expose_headers=["X-Run-Id"]` **and** `allow_headers += ["Idempotency-Key","Last-Event-ID"]` — the smallest non-wildcard change that lets a cross-origin frontend send the durable request headers and read the run id. Nothing else touched. |

### 37.2 Frontend state machine (`phase`, durable only — `null` on the legacy path)

```
        startStream (X-Run-Id seen)
  null ─────────────────────────────► streaming
                                        │  │  ▲
              token/run_status=running  │  │  │ run_status=running
                                        ▼  │  │
     tool_approval_required ──► waiting_approval        (stream closed by server;
                                        │                NO auto-reconnect while
        resumeAfterApproval()           │                paused — human think-time)
                                        ▼
                                    resuming ──► (reconnect) ──► streaming
   stream drop / clean close (no terminal, run id present, not paused)
        streaming ──► reconnecting ──(backoff)──► streaming
   cancelDurableRun() ──► cancelling ──► (await server-persisted `cancelled`)
   complete | error | cancelled | reconnect_exhausted ──► done ──► null (refs cleared)
```

* Only these phases exist: `streaming | waiting_approval | reconnecting | resuming | cancelling | done`.
* `done` is transient — `finish()` clears `runIdRef/cursorRef/idemKeyRef/submissionRef`, sets `durableRunId=null`, `phase=null`.
* A non-terminal durable run **blocks another `startStream`** (`abortRef`/`startPendingRef`/`isStreaming` guard).

### 37.3 Idempotency-Key lifecycle

* One `crypto.randomUUID()` (RFC-4122 v4 fallback via `getRandomValues`) minted **per explicit user send**, stored in `idemKeyRef`.
* **Reused unchanged** on every reconnect / resume / post-cancel re-open of that send.
* **New** key on the next explicit send (never derived from prompt text; not user-editable; never logged; not persisted).
* Sent as the `Idempotency-Key` request header on the initial POST and every reconnect POST.

### 37.4 Reconnect contract

* Trigger: the streaming response ends (drop or clean close) **without** a terminal event, a run id is known, and the hook is not paused on an approval gate, not cancelled, not unmounted.
* Re-issues the **same** `POST /api/sessions/{id}/messages/stream` with the same `Idempotency-Key`, plus `Last-Event-ID: <cursor>` header and `?after_seq=<cursor>` query, where `cursor` = the highest **accepted** numeric SSE `id` (`cursorRef`).
* No restart-from-zero, **no new run** (same key ⇒ backend returns the same run), no second parser vocabulary.
* Dedupe: any frame whose `id ≤ cursorRef` is parsed but produces **no side effect** (no duplicated token / callback). `cursorRef` advances only on a strictly-greater id. Legacy frames (no `id`) are always fresh.
* Backoff: `min(500ms · 2^attempt, 8s)` ± 30% jitter. Bounds: **6 attempts** or **60 s** elapsed since the pass started, whichever first.
* Exhaustion ⇒ terminal `onError({code:"reconnect_exhausted", detail:"Lost connection to the response. Please retry."})` — a **recoverable** error (the app may offer Retry; it does not set a hard failure banner for this code or for `RATE_LIMITED`/`DURABLE_UNAVAILABLE`).
* Stops immediately on a terminal event, `401/403`-class HTTP error, `cancelStream()`/`cancelDurableRun()`, or unmount. `useEffect` cleanup aborts the in-flight fetch and clears the pending backoff timer.
* Never persists raw prompts or tokens anywhere; the cursor and run id live only in refs for the lifetime of the run.

### 37.5 Message reconciliation (in `App.tsx`, unchanged store)

* Optimistic user message on send; streamed assistant draft rendered from `streamedContent`.
* `complete` carries `message_id` (from the persisted `RunStep.output_ref`) → the draft is replaced by the one committed `Message`; the post-run session refresh then finds that same id ⇒ **no duplicate**.
* `error` / `cancelled` / `reconnect_exhausted` clear the draft and show an honest state; a browser disconnect is **not** shown as a backend cancel (the durable run keeps going server-side and still persists its Message).
* Marker text, tool args, envelope fields, provider-error text, and control metadata are never rendered (the backend wire already redacts; the frontend additionally only reads the known fields).

### 37.6 MCP approval UX

* On `event: tool_approval_required {approval_id, tool_id}` the hook enters `waiting_approval` and calls `onApprovalRequired`. **No auto-reconnect while paused** — resume is user-driven.
* `ApprovalDialog` fetches `GET /api/approvals/{id}` (authenticated; bearer + CSRF via `request<T>`) and renders **only** server-returned redacted fields: `tool_id`, `risk_level` (spelled out — "high risk" — not colour-only), `purpose_redacted` / `display_summary`, `created_at`, `state`. Never renders or logs API keys, action hashes, session-family ids, raw provider metadata, or hidden envelope fields.
* Explicit separate **Approve** / **Deny** buttons; no countdown, no auto-approve, no "auto" mode. `decidedRef` + `disabled` ⇒ exactly one decision even under a double click. Approve → `POST /api/approvals/{id}/approve`; Deny → `.../deny`. The approval API call **never executes MCP in the browser**.
* After Approve the app calls `resumeAfterApproval()` → phase `resuming` → the hook re-opens the stream (same key + cursor) for the continuation; status line shows "Approved — resuming…". Deny shows a terminal "Denied. The tool call was not run." Stale / consumed / already-decided / fetch-failed ⇒ a safe non-actionable "error" state with a Close button; the stream still delivers the true outcome.
* a11y: Radix `role="dialog"` + `aria-modal`, `DialogTitle`/`DialogDescription` wire the accessible name/description, focus moves into the dialog and is restored on close, Escape/outside-click do not dismiss while a decision is pending, a `role="status" aria-live="polite"` region narrates every state.

### 37.7 Auth / security

* Reuses the existing in-memory access token + `getAuthHeadersAsync()` (streaming path) and `request<T>` (approval/cancel path) — **no second auth store**. Tokens never placed in a URL, never logged.
* `getReader()` is guarded: a disturbed/locked body fails **closed** (durable ⇒ bounded reconnect; legacy ⇒ `onError`), never a hanging pass or unhandled rejection.
* `401/403` on a durable pass ⇒ terminal `onError`, reconnect stops (no retry loop against a revoked session family).
* CSRF double-submit header attached to every state-changing call (approve/deny/cancel/stream POST).

### 37.8 Unsupported-mode behaviour

The durable backend returns these **before any persistence**; the frontend surfaces them and **does not silently retry through the legacy path**, and fabricates no citations / progress / confidence / agent status:

| HTTP | `onError.code` | UX |
|---|---|---|
| 422 | `VALIDATION_ERROR` | generic "this request isn't supported on the durable path" (image / deep-research / multi-agent / bad Idempotency-Key) |
| 503 | `DURABLE_UNAVAILABLE` | "the durable response path is unavailable" — recoverable, offer Retry |
| 429 | `RATE_LIMITED` | "too many requests / streams" — recoverable |
| 404 | `SESSION_NOT_FOUND` | session gone |
| other non-2xx | `HTTP_ERROR` | generic |

### 37.9 React correctness / performance

* Token updates go straight to `contentRef` + a single `setStreamedContent` per token (jsdom test throughput is fine; no rAF batching added — YAGNI until a real profile shows jank).
* All reconnect / cancel / approval callbacks read `cbRef` / `runIdRef` / `cursorRef` / `terminalRef` / `awaitingApprovalRef` from refs — no stale closures; `useCallback` deps minimal; functional `setState` where prior state matters.
* Transient run identity (`runIdRef`, `cursorRef`, `idemKeyRef`) lives only in refs. Cleanup effect clears the backoff timer and aborts the fetch on unmount.
* No new global dependency, state library, or bundle bloat — production build unchanged bar the hook/parser/dialog modules (`tsc && vite build` green).

### 37.10 Verification

* New/changed frontend tests: `sseParser.test.ts` **13**, `durableStreaming.test.ts` **12**, `ApprovalDialog.test.tsx` **6**, `useStreaming.test.ts` **29** (all pass — the pre-existing 17 failures + 1 in `RetryIntegration` were a stale-mock / signature-detection bug now fixed). `npx tsc --noEmit` clean. `npm run build` green.
* Full frontend suite: **230 passed / 48 failed** (down from a pristine-HEAD **181 / 66**). Every remaining failure is `useTheme must be used within a ThemeProvider` in `Sidebar` / `ModelSelector` / `ChatArea` / `App` test files — a **pre-existing** harness gap in components MP-3C3C did not touch (identical set at HEAD).
* Backend (CORS only): `tests/test_auth.py::TestPhase7CCORS` **5 passed** (2 new: durable-header preflight + `X-Run-Id` expose), `tests/test_run_engine_mp3c3b.py` **39 passed** (contract intact). MP-3C3A's 41 and MP-3C3B's 39 not re-run — their contracts are unchanged.
* Rendered validation: **no real browser** — repo has no Playwright/E2E and none was installed. The rendered surface is the **jsdom** DOM (`@testing-library/react`, vitest 4.1.10): `ApprovalDialog.test.tsx` mounts the real Radix modal and asserts roles, accessible name, `role="status"`, focusable one-shot buttons, and that sensitive fields never reach `document.body.innerHTML`. Backend mocked (`vi.mock("../api")`, mocked `fetch`) and labelled as such. `git diff --check` clean.

### 37.11 Carried into MP-3C3D+ / MP-6

Deep-research / multi-agent / image / citation & confidence parity on the durable path; enabling
`run_messages_stream_durable_enabled`; retiring the legacy stream; multi-process notifier
delivery. Nothing committed / merged / pushed.

## 38. MP-3C3D1 — as-built: server-owned durable mode routing (2026-09-11)

Fixes a defect found during MP-3C3D0's evidence-first planning pass (see
`docs/superpowers/plans/2026-09-11-phase-1b2r-mp3c3d0-mode-parity-plan.md` §1): the durable
`/messages/stream` unsupported-mode gate rejected every request whenever the **global**
`enable_deep_research` / `enable_multi_agent` settings were on, regardless of what the individual
request actually asked for. Both default `True` — so any self-host deployment at defaults would
have durable chat 422 on *every* message, including plain chat, the moment the cutover flag turned
on. This section states the **final per-run routing rule** those global flags no longer govern.

### 38.1 The rule

`/messages/stream`'s request body (`ChatRequest`) carries no field a client can use to select a
run mode — every request the route accepts executes as plain chat. Routing is therefore governed
by exactly **one server-owned constant**, defined once and shared by both the unsupported-mode
gate and the submission call so they cannot drift apart:

```python
# backend/app/run_engine/messages_stream.py
DURABLE_STREAM_MODE = "chat"
_SUPPORTED_DURABLE_MODES = frozenset({DURABLE_STREAM_MODE})
```

`unsupported_mode_code(*, has_image, mode=DURABLE_STREAM_MODE)` checks `has_image` first
(unconditional 422, unchanged), then checks `mode` against `_SUPPORTED_DURABLE_MODES` — **never**
`settings.enable_deep_research` / `settings.enable_multi_agent`. Those two settings remain
meaningful only to the **legacy** pipeline (`streaming_service.py`), which continues to read them
exactly as before; they are not part of the durable engine's routing decision at all.

`submission.py`'s `_ALLOWED_MODES = {"chat", "multi_agent", "deep_research"}` is the durable
engine's full *future* vocabulary (validated by `resolve_mode()`, feeds the semantic-identity
HMAC). `_SUPPORTED_DURABLE_MODES` here is this **route's own, narrower** business rule — which of
those the route can *actually* serve *today*. A client-adjacent value of `"deep_research"` or
`"multi_agent"` is therefore a recognized-but-not-yet-executable mode: rejected with the same
bounded, sanitized `mode_unsupported_<slug>` code as any garbage string, never silently downgraded
to chat and never silently accepted.

### 38.2 Mode reaches the worker unchanged (no new plumbing needed)

The mode-preservation plumbing this rule relies on already existed end-to-end before this fix —
confirmed by direct inspection, not assumed:

* `submission.build_chat_submission(mode=...)` → `ChatRunSubmission.resolved_mode` (defaults
  `None`→`"chat"` via `resolve_mode`).
* `_canonical_bytes()` includes `"mode": sub.resolved_mode` → `semantic_identity()`'s HMAC — a
  request submitted with a different mode under the same `Idempotency-Key` deterministically
  raises `IdempotencyKeyConflict`, never silently reuses the prior run.
* `create_chat_run()` seals `mode` inside `chat_stream.build_exec_snapshot(...)`, stored in the
  encrypted `RunPayload`, **and** in the non-secret `RunStep.input_ref` (`{"mode": ...}`, ids
  only — never a second plaintext copy).
* `chat_stream.build_chat_input()` reads the sealed snapshot back into `ChatInput.mode`, which the
  worker's chat driver receives on every invocation.

This mini-phase's only change was the **gate** (`unsupported_mode_code`) and making the
already-correct default (`mode=None`→`"chat"`) explicit at the two call sites
(`resolve_and_submit`, `routes/messages.py`) — proven behavior-preserving for existing runs:
`semantic_identity()` over an explicit `mode="chat"` submission equals `semantic_identity()` over
the prior implicit `mode=None` submission (`resolve_mode` normalizes both to `"chat"` identically).

### 38.3 What this does not change

* `run_messages_stream_durable_enabled` stays `False`.
* Durable mode support is still **chat only** — no deep-research or multi-agent execution exists
  on the durable path after this fix, only the *routing bug* that would have blocked chat
  regardless of those modes' real implementation status is fixed.
* No new request field, no public API redesign — `ChatRequest` is unchanged; pydantic's default
  `extra="ignore"` already means an injected `"mode"` body field is silently dropped before it
  ever reaches route code.
* Citations, memory, and the memory/directive/KG marker-postprocessing gap flagged in MP-3C3D0
  §1/§5 remain unaddressed — that is MP-3C3D2's scope, not this one's.

### 38.4 Verification

16 new focused tests (`tests/test_run_engine_mp3c3d1.py`) + one updated MP-3C3B test
(`test_unsupported_modes_fail_before_persistence`) — see
`docs/superpowers/reports/phase-1b2r-mp3c3d1-completion.md` for the exact list and results.
`py_compile` clean on all four changed files; `git diff --check` clean; no config/import/main
change, so `import app.main` was not re-run this pass.

## 39. MP-3C3D2 — as-built: durable citations, memory context, safe post-processing (2026-09-11)

Adds truthful citation/memory-context support to durable plain chat, and a fail-closed
post-processing boundary for the persistence/planning markers (`[save_memory]`, `[save_directive]`,
`[use_memory]`, `[plan]`, `[lesson]`) that were previously unwatched — and therefore able to leak
raw — on the durable path. See `docs/superpowers/reports/phase-1b2r-mp3c3d2-completion.md` for the
full evidence table, test list, and the interrupted-session recovery this section's transaction
flow reflects.

### 39.1 Context-enrichment step (no new RunStep, no new run state)

The plan's suggested sequence ("context_enrichment step, llm_generate step, …") is realized
**without** a new `RunStep` kind or seq-numbering change — a deliberate deviation, chosen after
concluding that inserting a new step into `_advance_chat_run`'s crash-recovery branch logic would
be higher-risk than necessary. Instead:

* `messages_stream._build_context_snapshot(db, user_id, session_id, message)` reuses
  `rag_service.retrieve_chunks`/`build_citation_list`/`format_rag_context` and
  `memory_service.retrieve_relevant_memories`/`format_memories_for_prompt` — bounded, non-fatal on
  failure (matches legacy), no new LLM call (RAG embeds via the same local embedding model legacy
  already uses).
* The result — `{"rag_context_text", "citations", "sources_used", "memory_context_text",
  "memories_used"}` — is sealed into the SAME encrypted `RunPayload` as the MP-3C1 execution
  snapshot (`payload_type="other"`, no new payload type, no migration), at the existing seq=1
  submission step created by `submission.create_chat_run`.
* `chat_stream.build_chat_input` reads it back and, when non-empty, splices **one** additional
  untrusted user-role message — `render_context_envelope`'s delimited block — immediately before
  the final (current-turn) user message. **Unlike legacy** (which appends RAG/memory context to
  the *system prompt*), this reuses the MP-3C2 tool-result convention: untrusted external content
  is a delimited **user**-role message, never elevated to system-prompt trust.
* `_build_continuation_input` (worker.py) already calls `build_chat_input` for its base messages —
  the context envelope survives an MCP suspend/approve/resume cycle for free, with **zero**
  additional retrieval (proven: `test_continuation_after_mcp_reuses_original_context_snapshot`
  asserts `retrieve_chunks` is called exactly once across a full tool cycle).

### 39.2 Transaction / idempotency flow (post-recovery)

An earlier attempt within this same phase computed context *inside* `create_chat_run`'s atomic
staging area via a lazy `context_fn` callable, gated on `create_chat_run`'s own internal replay
check. This was proven unsafe and reverted: `memory_service.retrieve_relevant_memories` commits
internally (access-metric bookkeeping), and firing that commit after `Message`/`Run` rows were
already `db.add()`-ed-but-uncommitted force-committed that partial state — breaking
`test_submission_commit_failure_no_partial`'s all-or-nothing guarantee (a downstream
`transition_run` failure went from leaving 0 `Run` rows to leaving 1).

The corrected flow — `messages_stream.resolve_and_submit` — performs an **authoritative
pre-check** before computing context:

```
_select_by_idem_scope(db, tenant_id, user_id, session_id, key)   # the EXACT read-only
                                                                    lookup submit_run() uses
  → existing row found (replay OR identity conflict)  →  skip retrieval, context stays None
  → nothing found (genuinely fresh)                    →  context = _build_context_snapshot(...)
                                                            BEFORE create_chat_run is even called
create_chat_run(..., context=context)   # stages nothing until context is already a plain dict
```

No second, drifting identity implementation — `_select_by_idem_scope` (`app.run_engine.engine`) is
imported and reused verbatim. A concurrent-race window between the pre-check and the real
`create_chat_run` call (another request commits first) is handled by the **unchanged**
`submit_run` unique-constraint retry (`uq_runs_idem_scope`); this request's wastefully-computed
context is silently discarded on the replay path — proven by
`test_concurrent_same_key_race_persists_one_run`.

### 39.3 Citation provenance

`chat_stream.validate_citations` bounds (≤10), dedupes (`document_id`+`chunk_id`), truncates
(512-char fields), and drops any citation missing a required field — reusing the canonical
`Citation` shape (`marker`, `document_id`, `filename`, `page_number`, `chunk_id`, `snippet`) with
**no `url` field this phase**: `rag_service` never emits one, and rather than ship a weak
hand-rolled scheme/hostname blocklist, or call the repo's real SSRF guard
(`browser_agent.assert_url_allowed` — live DNS resolution) from inside worker finalization's
atomic staging area, citation URLs are simply omitted pending a future citation source that
actually needs them. The model's generated text has no code path into citation construction at
all — citations are fixed before generation begins.

### 39.4 Atomic finalization + fail-closed unreadable context

`RunWorker._finalize_chat_success` now: reads the sealed context snapshot from the run's seq=1
step (the SAME payload `build_chat_input` already decrypted successfully earlier this run);
validates citations; writes `Message.citations` and `sources_used`/`memories_used` (added to
`RunStep.output_ref`, non-secret booleans) in the **same** `db.add(msg); db.flush()` →
`RunStep` update → `_apply_transition` CAS → `db.commit()` sequence MP-3B/3C1 already proved
atomic — no second commit. If the sealed snapshot cannot be re-opened (a genuine anomaly, not "no
context was ever sealed"), the run **fails closed** with `context_snapshot_unreadable` — no
Message is created, never a silent success with fabricated-empty citations.
`messages_stream._terminal_frame` reads `sources_used`/`memories_used` from the committed
`RunStep.output_ref` and citations from the committed `Message.citations` — never recomputed, so
reconnect/replay returns byte-identical completion metadata.

### 39.5 Marker policy

| Marker | Class | Durable behavior |
|---|---|---|
| `[save_memory]` | user-scoped memory write | watched, captured-and-suppressed, turn fails closed (`nonexec_marker_deferred`); write-activation deferred |
| `[save_directive]` | persistent directive mutation (legacy: **no approval boundary**) | same — never auto-applied |
| `[use_memory]` | content-substitution read | same (not write-related, but its post-hoc-substitution semantics don't map onto the scanner's streaming model without a new capture mode) |
| `[plan]` / `[lesson]` | display/control only | same, for uniformity — no backend write exists for either, legacy or durable |
| `[kg_query]` | *(n/a — user-input retrieval marker, not assistant-emitted)* | out of scope |
| `[mcp_call]` / `[python_code]` / `[proposed_command]` / `[browser_action]` | execution | **unchanged** — MP-2 gateway / `tool_resume_required` |

`chat_stream.MarkerScanner` gained one new capture-status value, `"deferred"`, reusing 100% of the
existing chunk-boundary-safe trip/stop machinery (no new capture-buffer logic). Write-activation
for `[save_memory]` is explicitly **not implemented**: `memory_service.save_memory`'s lexical
exact-match dedup fast path makes a *repeated* write safe, but the run reaches `SUCCEEDED`
(terminal) in the same commit a write would need to ride along with, and the worker's claiming
model (`advance_chat_runs`) only ever revisits non-terminal runs — durably retrying a best-effort
post-commit side effect against an already-terminal run would need a genuine claiming-model
extension, reported as blocked rather than faked.

### 39.6 Crash / replay behavior

* Crash before context is sealed (mid-retrieval, pre-`create_chat_run`): nothing persisted; the
  next request attempt (same Idempotency-Key) re-runs `resolve_and_submit` from scratch — safe,
  since nothing was ever staged.
* Crash after context is sealed but before the run advances: the worker's existing
  attempt-1/lease/version-CAS machinery (MP-3B/3C1, unchanged) governs recovery exactly as before;
  the sealed context snapshot survives untouched and is re-read on the next attempt.
* Crash around finalization: the existing `_apply_transition` CAS + `output_persist_failed`
  handling (unchanged) still applies; MP-3C3D2 only adds citation/flag fields to an
  already-proven-atomic commit, never a second commit point.
* An unreadable context snapshot at finalization time is now its own **critical**, fail-closed
  outcome (`context_snapshot_unreadable`) rather than silently degrading to empty citations.

### 39.7 Verification

27 new focused tests (`tests/test_run_engine_mp3c3d2.py`) + full regression runs of
`test_run_engine_mp3c3b.py` (39), three MP-3C2 finalization tests, `test_run_engine_mp3c3d1.py`
(16), and `test_run_engine_submit.py` + `test_run_engine_mp3a.py` (36) — all green (121 tests
total across the regression set). `py_compile` clean; `git diff --check` clean; `import app.main`
OK (a new import was added). See the completion report for the full list and the exact defect this
session repaired.

### 39.8 Closeout correction pass (2026-09-11)

A follow-up review found and fixed four remaining gaps, all without a redesign:

* **Context snapshot versioning.** `chat_stream.CONTEXT_SNAPSHOT_VERSION = 1`; the ONE
  authoritative constructor, `chat_stream.build_context_snapshot(...)` (no-args ⇒ the canonical
  empty snapshot), is used by every path that seals a context — `messages_stream.
  _build_context_snapshot` for `/messages/stream`, and `submission.create_chat_run`'s own
  normalization (`context if ... version matches ... else build_context_snapshot()`) for the
  **direct Run public API** (`routes/runs.py`, which calls `create_chat_run` with no `context`
  argument at all — the second chat-submission entry point, previously unchecked). `chat_stream.
  read_context_snapshot` now distinguishes: genuinely absent (`"context"` key missing or `null` —
  honest backward compatibility, canonical empty snapshot, no error) vs. present-but-malformed-or-
  wrong-version (raises `chat_stream.ContextSnapshotError`, caught by `_finalize_chat_success`'s
  existing fail-closed wrapping — no code change needed there, it was already broad enough).
* **Passage/citation one-to-one correspondence.** New `messages_stream._select_cited_chunks`
  builds citations for the (already `rag_service.TOP_K`-bounded) retrieved set via the new shared
  `chat_stream._validate_one_citation`, keeps only chunks whose citation validates, dedupes by
  `(document_id, chunk_id)`, and `rag_context_text`/`citations` are BOTH re-rendered from that SAME
  filtered list — an uncited chunk's passage text can never reach the provider.
  `build_context_snapshot`'s invariant enforcement (§39.1) is the defensive second gate.
* **Fail-closed idempotency pre-check.** `resolve_and_submit`'s `_select_by_idem_scope` pre-check
  (§39.2) previously caught every exception and proceeded as "fresh" — now a lookup failure raises
  `DurableStreamError(503, "Durable streaming is temporarily unavailable.")` (the same bounded
  phrasing `assert_durable_stream_ready` already uses) immediately, before any retrieval and before
  `create_chat_run` is ever called. Only the exception class name is logged.
* **Concurrency claim corrected.** The TOCTOU test
  (`test_toctou_race_simulation_persists_one_run`) is explicitly documented, in its docstring and
  the completion report, as a deterministic single-threaded simulation — **not** real
  multi-threaded/multi-process/PostgreSQL concurrency verification, which remains deferred to the
  MP-6/final Phase 1B gate.

13 new tests (40 total in `tests/test_run_engine_mp3c3d2.py`); regression re-run of
`test_run_engine_submit.py` (12), `test_run_engine_mp3a.py` (24), and
`test_run_engine_mp3c3b.py::test_submission_commit_failure_no_partial` (the exact atomicity test
the original regression broke) — all green. `py_compile` clean; `git diff --check` clean;
`import app.main` OK.

## 40. MP-3C3D3A — as-built: explicit request-level mode contract and routing (2026-09-12)

Replaces ambiguous global-boolean-driven mode selection with one explicit, validated,
server-resolved per-request mode contract (`chat` / `deep_research` / `multi_agent`) — backend
contract and routing only; no durable deep-research/multi-agent handler and no frontend selector
yet. See `docs/superpowers/reports/phase-1b2r-mp3c3d3a-completion.md` for the full routing truth
table, URL/deep-research interaction evidence, and test list.

### 40.1 The canonical contract

* `ChatRequest.mode: Optional[str]` (new, additive) — `None` when omitted, otherwise the raw
  client string, bounded by pydantic (`max_length=32`) before it reaches any handler code.
* `submission.validate_request_mode(raw)` is the ONE authoritative request-level validator.
  **Strict exact-match** — no case-folding, no whitespace-stripping: `"CHAT"`/`" chat "` are
  rejected, not silently canonicalized. `None` (omitted) is returned as `None`, deliberately
  distinguishable from explicit `"chat"` — legacy routing needs that distinction to choose
  heuristic-vs-forced behavior. `/messages/stream` (both legacy and durable branches) calls it
  directly. `/api/runs` calls it **indirectly**, through `resolve_mode(raw)` — see §40.6: an
  initial version of this phase left `resolve_mode` as its own separate, case/whitespace-tolerant
  check (mirroring its PRE-EXISTING MP-3A/3C1 behavior), which meant `/api/runs` was NOT actually
  covered by the strict contract despite this section's original wording — a real asymmetry,
  corrected in a follow-up pass by making `resolve_mode` delegate to `validate_request_mode`
  rather than duplicate its logic.
* `messages_stream.unsupported_mode_reason(mode)` is the separate EXECUTABILITY gate — an
  already-validated mode checked against `DURABLE_STREAM_MODE`/`_SUPPORTED_DURABLE_MODES`
  (currently `{"chat"}`). `unsupported_mode_code` (the existing `/messages/stream` durable gate,
  MP-3C3D1) now delegates to it internally; `/api/runs` calls it directly. One allowlist
  (`submission._ALLOWED_MODES`), one executability set — no second, drifting definition anywhere.

### 40.2 Legacy routing (streaming_service.py)

`ChatContext` gained a `mode` field; `prepare_chat_context(mode=...)` and
`stream_chat_response`'s multi-agent check both branch on it identically:

```
mode == "chat"            -> neither deep-research injection nor multi-agent routing
mode == "deep_research"   -> forces deep-research injection (bypasses detect_research_task),
                              UNLESS the message already produced web-scraped context (the
                              pre-existing `not web_scraped` guard — unchanged, still honored)
mode == "multi_agent"     -> forces stream_multi_agent_response (bypasses detect_complex_task)
mode is None (omitted)    -> EXACT original behavior: settings.enable_deep_research +
                              detect_research_task decide the injection; settings.enable_multi_agent
                              + detect_complex_task decide the routing — independently, in their
                              original order, with their original composition (both can fire on
                              the same omitted-mode turn, exactly as before)
```

An explicit mode is always exclusive — deep-research injection and multi-agent routing are never
composed when the client asked for one specifically, unlike the omitted-mode case which can
compose both (unchanged legacy precedent). The legacy capability gate
(`enable_deep_research`/`enable_multi_agent` must be `True` for the matching explicit mode) runs
in the ROUTE, before `prepare_chat_context` is even called — that function's own step 2 saves the
user `Message` immediately, so the gate must precede it to be truthfully "before persistence".

### 40.3 Durable routing (messages_stream.py / routes/messages.py)

`stream_chat`'s route handler now validates/resolves mode ONCE, before the durable-vs-legacy
branch decision, so an invalid mode fails identically (422) regardless of the cutover flag.
`_stream_chat_durable` receives the resolved mode and passes it into the existing
`unsupported_mode_code` gate (unchanged position in the function — after readiness checks, before
the Idempotency-Key/bearer checks) instead of the previously-hardcoded `DURABLE_STREAM_MODE`
constant. Recognized-but-unimplemented modes (`deep_research`/`multi_agent`) still fail with the
exact same bounded 422 MP-3C3D1 already proved — now honestly driven by what the client actually
requested rather than an unreachable code path (no request could previously express anything but
implicit "chat").

### 40.4 Direct Run API (routes/runs.py) — closed gap

Found and fixed a real pre-existing gap: `SubmitChatRunBody.mode` already existed and
`build_chat_submission`'s `resolve_mode` already validated it against the full durable-engine
vocabulary (`chat`/`deep_research`/`multi_agent`) — but nothing rejected the latter two before
`create_chat_run`, meaning a `mode=deep_research` submission via `/api/runs` would have been
persisted as a queued `chat_stream` run that `advance_chat_runs` would pick up and silently run
through ordinary plain-chat generation, ignoring the requested mode entirely. Fixed with the same
`unsupported_mode_reason` gate `/messages/stream` uses, inserted after `build_chat_submission`
(mode already normalized) and before `create_chat_run` — zero new allowlist, zero broadening of
the route's other behavior (image handling on `/api/runs` was and remains untouched — out of this
phase's scope).

### 40.5 Verification

38 new focused tests (`tests/test_run_engine_mp3c3d3a.py`) proving exact mocked-entrypoint call
counts for every legacy routing claim, real-TestClient-route proofs (not bare gate-function calls)
for every durable and direct-Run-API rejection/acceptance claim, the URL/deep-research
short-circuit, idempotency (mode already part of the MP-3A semantic identity — unchanged, reused),
and privacy. One MP-3C3D1 test updated (its "ChatRequest has no mode field" premise was
intentionally superseded). Regression: `test_run_engine_mp3c3d1.py` (16), `test_run_engine_submit.py`
(12), `test_run_engine_mp3a.py` (24), `test_run_engine_mp3c3b.py` (39),
`test_run_engine_mp3c3d2.py` (40) — all green, 169 tests total across the focused set.
`py_compile` clean; `git diff --check` clean; `import app.main` OK. No frontend file changed; no
migration; `run_messages_stream_durable_enabled` stays `False`.

## 41. MP-3C3D3B — as-built: capability-aware frontend mode selector (2026-09-12)

### 41.1 The capability contract

No new route. `routes/settings.py`'s existing, authenticated (`get_current_user`)
`GET`/`PUT /api/settings` gained one additive field, `chat_modes:
List[ChatModeCapability]`, where `ChatModeCapability = {mode, available,
reason?}` — exactly three bounded, public fields, never an env value, secret,
internal slug, worker name, or session-family id. Computed by
`_compute_chat_mode_capabilities()`, which reuses the SAME two signals D3A's
routes already gate on: `messages_stream._SUPPORTED_DURABLE_MODES` (durable
executability) and `settings.enable_deep_research` / `enable_multi_agent`
(the legacy deployment-capability gate in `routes/messages.py`). No second
allowlist; capability advertising can never drift from what the routing
layer would then accept — "chat" is always available on either path; "auto"
is never a backend mode and never appears in this list (it is the
frontend's own "omit `mode`" choice).

### 41.2 The frontend mode model

`types.ts`: `BackendChatMode = "chat" | "deep_research" | "multi_agent"`
(must match `submission._ALLOWED_MODES` exactly); `UIChatMode = "auto" |
BackendChatMode`. Serialization: `useStreaming.ts`'s POST body is built as
`JSON.stringify({ message, image_url, mode: sub.mode })` — when `sub.mode`
is `undefined` (Auto), `JSON.stringify` drops the key entirely, so the wire
body is byte-identical to the pre-D3B body. Never `mode: null`, never
`mode: "auto"`, never a display label.

### 41.3 Immutability across reconnect/resume

`startStream(sessionId, message, imageUrlOrCallbacks, callbacks?, mode?)`
gained one optional trailing parameter (no existing call site needed to
change — all omit trailing args). `App.tsx`'s `handleSend` reads the
mutable `chatMode` UI state exactly once, synchronously, at send time, and
passes the resolved value in. `useStreaming`'s pre-existing `submissionRef`
(already the single immutable per-send snapshot for `message`/`imageUrl`)
now also carries `mode` — every reconnect pass and every post-approval
resume re-reads that same ref, never live selector state. A later,
independent `startStream()` call (the next message) mints its own fresh
`idemKeyRef` and `submissionRef`, so a mode change only ever affects the
NEXT send.

### 41.4 The selector component

`frontend/src/ModeSelector.tsx` — built on the already-vendored
`components/ui/dropdown-menu.tsx` (`@radix-ui/react-dropdown-menu`, already
used by `ApprovalDialog.tsx` and the chat-export menu). `DropdownMenuRadioGroup`
+ `DropdownMenuRadioItem` give native keyboard support (Enter/Space/ArrowDown
to open, roving focus that skips `disabled` items, Escape to close) and
Radix-managed `role="menuitemradio"` / `aria-checked` / `data-disabled` —
no hand-rolled listbox/ARIA needed. Unavailable modes stay visible,
`disabled`, with a short bounded reason (never an internal slug like
`mode_unsupported_deep_research`). The trigger is a real `<button
type="button">` rendered in its own toolbar row above the composer
`<textarea>` (not squeezed into the existing icon-button row), so it can
never shrink the message input on a narrow screen, and it never submits the
form. `ChatArea` passes `disabled={isStreaming}` into it, freezing the
selector while a send is in flight — the same pattern already used on the
textarea/attach buttons. A capability-fetch failure or a refresh that
invalidates the current explicit selection both fail safe: the former keeps
the hardcoded default (`chat` available, the two advanced modes disabled),
the latter resets the selection to `auto` and shows a small, auto-dismissing
notice — neither path ever speculatively enables an advanced mode.

### 41.5 Verification

Backend: 9 new focused tests (`tests/test_settings_mode_capability.py`) +
regression (`test_run_engine_mp3c3d3a.py` 46, `test_run_engine_mp3c3d1.py`
16) — 71 total, green; `py_compile` clean; `git diff --check` clean;
`import app.main` OK. Frontend: 9 new `ModeSelector.test.tsx` + 8 new
serialization/reconnect tests appended to `durableStreaming.test.ts`, run
alongside the full pre-existing `useStreaming`/`durableStreaming`/
`sseParser`/`RetryIntegration`/`ApprovalDialog` suites — **initial result:
108 total, 99 passed, 9 failed in `ChatArea.test.tsx`**. **Closeout
correction (same day):** each of the 9 was individually investigated
against a verbose run and the real `git diff` of `ChatArea.tsx` (which
touches only the mode-selector insertion, never any of the 9 failing
assertions' target regions) — all 9 were genuine pre-existing test/product
copy drift (emoji removed, wording changed, a visible memory-status label
replaced by an icon+title, elsewhere in this same uncommitted tree, unrelated
to mode selection) and were repaired to assert the current, correct product
text/accessible queries (never loosened, never skipped). A new
`ModePropagation.test.tsx` (11 tests) additionally proves the mode selection
reaches the real `App.handleSend`→`useStreaming.startStream` boundary
through a real `<App/>` render — closing the one proof gap the isolated
`ModeSelector`/hook-level tests left open. Corrected total: **119 tests,
119 passed, 0 failed** (the original 108 now all green + 11 new). `npx tsc --noEmit` clean. No migration; no new
dependency; `run_messages_stream_durable_enabled` and `run_engine_enabled`
stay `False`;
no durable deep-research/multi-agent handler exists.

## 42. MP-3C3D4B — as-built: safe deep-research acquisition + immutable structured-source snapshot foundation (2026-09-12)

**Not durable Deep Research.** No synthesis, no public route, no worker
sweep, no capability activation exists after this phase — only the
acquisition/persistence foundation D4C will build on.

### 42.1 Module boundary

Five new modules, each a single concern: `research_sources.py` (pure
schema/normalization/bounds/marker-neutralization, no DB/network),
`research_search.py` (search adapter wrapping the unmodified free
`web_search.search_web`), `research_fetch.py` (guarded fetch/extract
adapter), `research_acquisition.py` (orchestrator wiring adapters +
`research_sources` behind injectable cancel/session/deadline/heartbeat/
progress seams — zero DB commit, zero LLM/MCP/tool/KG/memory/directive
call), `research_snapshot.py` (the encrypted persistence primitive).

### 42.2 Transport decision

`httpx` single-hop-at-a-time HTTP GET, NOT Playwright — Chromium follows
redirects/loads subresources internally with no request interception
anywhere in this repository, and building genuine per-request validation
over that surface was assessed as the "material new infrastructure" this
phase was told to avoid. A bare GET has no subresource problem at all.
Residual, accepted: no JS execution → sparse extraction on client-rendered
pages (an extraction-quality tradeoff, not a security gap).

### 42.3 SSRF/redirect closure (D4-DECISION-2)

Every redirect hop — not just the initial URL — is normalized then
validated via the existing, proven `browser_agent.assert_url_allowed`
(real DNS resolution + IP classification) before being followed;
`follow_redirects=False`; loop detection; a small redirect-count cap; the
final effective URL is itself validated and is what gets persisted. Proven
structurally that no subresource/JS/download path exists in the transport
(one GET per hop, verified by test). Response bytes are capped
INCREMENTALLY during `iter_bytes()` streaming (which also yields
already-decompressed content, closing the decompression-bomb concern for
free) — never after full buffering. DNS-rebinding/TOCTOU residual risk is
recorded honestly: `httpx` performs its own independent DNS resolution at
connect time, separate from the validation-time resolution — the SAME
gap `browser_agent.py`'s own `BrowserSession.navigate` already has, no
better, no worse; true pinning is not implemented anywhere in this
repository.

### 42.4 Structured snapshot + citation contract

`RESEARCH_SOURCE_SNAPSHOT_VERSION = 1` — versioned, bounded
(`RESEARCH_MAX_SOURCES`, `RESEARCH_MAX_PASSAGES_PER_PAGE`,
`RESEARCH_MAX_PASSAGE_CHARS`, `RESEARCH_MAX_TOTAL_PASSAGE_BYTES`, plus
new deployer-tunable `config.py` bounds for queries/redirects/timeouts/
response bytes/extracted chars — all validated positive, with
`research_fetch_timeout_s < research_total_deadline_s` enforced as a
cross-field invariant). Every surviving passage gets exactly one citation
marker, assigned only after dedup/truncation; an invalid-citation removal
drops exactly its matching passage; content hashes are computed on the
exact neutralized+truncated text that gets sealed. Three-layer
deduplication (identical URL, identical cross-source passage hash, and
orchestrator-level pre-fetch URL dedup), documented and tested.
`truncated` reflects only a REAL configured bound being reached — a failed
fetch is never mistaken for truncation.

**Untrusted-input marker neutralization** — the gap D4A identified
(`chat_stream.MarkerScanner` scans only MODEL OUTPUT, never retrieved
INPUT) is closed here: every reserved-marker opener
(`app.skills.loader._MARKER_OPENS`, the same list the output scanner uses)
is broken with an inserted zero-width space, preserving the original
text's exact casing (a case-insensitive regex substitution with a FIXED
replacement string was found, during test-writing, to silently rewrite
casing — fixed with a replacement callback instead) and every other
character, verified against all 7 execution-marker openers.

### 42.5 Persistence — real at-most-one authority

`RunPayload` itself has NO database uniqueness constraint on
`(run_id, step_id, payload_type)`. Authority is instead anchored to
`RunStep`'s existing, real `UniqueConstraint("run_id", "seq")` — standard
ANSI SQL, identical under SQLite and PostgreSQL. `seal_research_snapshot`
seals the payload and inserts a new `RunStep` at `(run_id, seq)` in one
caller-owned, uncommitted transaction (`commit=False`); a concurrent
second seal for the same `(run_id, seq)` is rejected by a genuine
`IntegrityError` at commit — proven directly by test, including that
rollback leaves zero orphan `RunStep`/`RunPayload` rows and the winner is
completely unaffected. Session-binding is explicitly NOT this primitive's
job (the payload envelope has no `session_id` field, matching every other
`seal_payload` caller in the repo) — it remains the orchestrator's
`check_session_binding()` seam's responsibility, unchanged from every other
durable step's existing pattern.

### 42.6 Verification

132 new focused tests (config 33, search/fetch 36, sources 33, acquisition
11, persistence 11, activation-boundary 8) + 110 directly-affected
regression (`test_run_payloads.py`, `test_settings_mode_capability.py`,
`test_run_engine_mp3c3d3a.py`, `test_run_engine_mp3c3d1.py`) — 242 total,
green. `py_compile` clean on all 7 changed/new backend files;
`git diff --check` clean; `import app.main` OK. No migration; no new
runtime dependency (`httpx` already pinned, matches installed version
exactly); no frontend file touched; no worker sweep registered; no public
route added; capability response unchanged (`deep_research` still
unavailable under durable cutover); `run_messages_stream_durable_enabled`
and `run_engine_enabled` stay `False`.

## 43. MP-3C3D4C — as-built: end-to-end durable Deep Research (2026-09-12)

**OFF by default** (`run_deep_research_enabled=False`). Reuses D4B's
acquisition/persistence foundation unmodified except one order-of-events
correction (§43.2).

### 43.1 Readiness gate

One authoritative predicate, `deep_research_readiness.deep_research_executable()`:
`run_engine_enabled ∧ run_worker_enabled ∧ run_chat_stream_enabled ∧
run_public_api_enabled ∧ run_deep_research_enabled ∧ RUN_IDEMPOTENCY_HMAC_KEY
configured`. `run_chat_stream_enabled` is a genuine prerequisite — synthesis
reuses the identical generation bridge as durable chat. Reused unchanged by
`messages_stream.unsupported_mode_reason` (mode routing on both
`/messages/stream` and `/api/runs` — their existing call sites needed zero
changes, since both already call this function generically for any mode),
`routes/settings._compute_chat_mode_capabilities`, and
`RunWorker.advance_deep_research_runs`'s own self-gate.

### 43.2 Integration corrections found during this phase

1. Acquisition and synthesis must complete in ONE worker call — a design
   that returned early after sealing the snapshot (expecting a later sweep
   to pick up synthesis) never actually resumed, because the run's lease
   was freshly minted by the same sweep and would not appear expired again
   for a full lease TTL. Fixed by chaining directly into synthesis inside
   `_run_deep_research_acquisition`, mirroring `advance_chat_runs`'s own
   single-call-drives-the-whole-generation pattern.
2. D4B's `research_acquisition.run_research_acquisition` originally emitted
   its own `research_sources_ready` progress event — before the caller
   even attempted to seal the snapshot, which is both redundant (the
   worker also emits it, correctly, after persistence) and order-wrong (the
   requirement is "only after authoritative persistence commits," which
   the orchestrator — having no DB access — cannot itself guarantee).
   Removed from the orchestrator; the worker's post-seal emission is now
   the only source.

#### 43.2b Resume-and-closeout corrections (2026-09-13)

The session that wrote §43.1–43.7 hit its limit immediately after
beginning the PRD update, before its own claims below were independently
re-verified. A separate resume pass re-audited the interrupted state
against the repository itself (never trusting this document's own prior
text) and found/fixed three further defects, and proved one previously
source-inspection-only claim at the real HTTP-route level:

1. `submission.py` defined `assert_deep_research_executable()`, mirroring
   `assert_chat_stream_executable()` — but it was never called from any
   route (`grep -rn "assert_deep_research_executable" backend/app/
   tests/` found only its own definition). Unlike chat readiness, deep-
   research readiness cannot be checked mode-agnostically/early, since
   both public routes only know the request's `mode` after parsing the
   body — by which point `messages_stream.unsupported_mode_reason`
   already gates it (the SAME `deep_research_executable()` predicate).
   Removed the dead function rather than wire it into a second, duplicate
   call site.
2. `research_synthesizing` existed only in the `research.progress` schema
   (§43.6) and `_map_event`'s dispatch table — no code path ever allocated
   one. Fixed: allocated atomically in the SAME commit as the synthesis
   `RunStep`'s `pending`→`running` transition (worker.py,
   `_run_deep_research_synthesis`), i.e. before the provider is invoked.
3. `_run_deep_research_synthesis` caught `Exception` around
   `chat_stream.read_context_snapshot` and silently defaulted to
   `local_citations=[]`, continuing to a normal success — contradicting
   that function's own documented fail-closed contract and the plain-chat
   finalization code in the same file, which correctly fails the run
   closed (`context_snapshot_unreadable`) on the identical exception.
   Fixed to match the chat path.
4. Added a real `TestClient`-level test proving `/api/runs` ACCEPTS and
   queues `mode="deep_research"` when every prerequisite is genuinely met
   (`test_deep_research_readiness.py::test_direct_run_api_accepts_deep_research_when_fully_ready`)
   — the rejection side already had real-route proof
   (`test_run_engine_mp3c3d3a.py`); only the acceptance side had never
   been proven above the level of `inspect.getsource()` string-matching.

One item was investigated and found correct as designed, not a defect:
local `[N]` citations are (deliberately, per the pre-existing D2 chat
contract) filtered by construction-time context-snapshot membership, not
by whether `final_text` references the marker — a DIFFERENT, and
independently already-approved, rule from web `[W#]` citations'
reference-time filtering. See the completion report's "Citation filtering
policy" section for the full evidence.

### 43.3 Run/step model

`Run.kind="deep_research"` (server-derived from `resolved_mode`, never
client input — `submission.create_chat_run`'s one-line change). Two
RunStep kinds (not D4A's originally-proposed three — the smallest set
D4B's ACTUAL persistence primitive truthfully supports, since it only ever
builds one sealed acquisition step): `load_context` (seq 1, identical to
chat's own submission step) → `research_fetch_extract` (seq 2, sealed
atomically by D4B's `seal_research_snapshot`, always inserted already
`succeeded` — no pending/running intermediate state is structurally
possible) → `deep_research_synthesis` (seq 3, `pending`→`running`
[attempt-1 durable proof, committed before any provider call]→
`succeeded`/`failed`/`incomplete`). Bounded new failure codes:
`research_no_sources`, `citation_validation_failed`,
`research_citations_missing`; every other failure mode is REUSED from
`CHAT_STATUS_REASONS` (synthesis shares the identical generation bridge).

### 43.4 Citation composition

Local/document citations keep their existing `[1]`/`[2]` scheme,
unmodified (`chat_stream.validate_citations`, reading the same sealed D2
context snapshot every chat run already reads). Web-research citations
get a deterministic, collision-free `[W1]`, `[W2]`, ... namespace, assigned
once in the sealed snapshot's own immutable passage order (stable across
every restart/reconnect — `deep_research_citations.build_web_marker_map`).
Server-authoritative validation: a `[W#]` marker in the model's final text
not present in the sealed map fails closed
(`citation_validation_failed`); zero valid web citations in an otherwise
non-empty generation fails closed (`research_citations_missing`) rather
than silently claiming a researched answer; citations persist in
first-reference order, deduplicated, with the model's own generated text
never itself becoming an authoritative URL/citation source. Both citation
classes share ONE dict shape (`document_id`/`chunk_id` present but `None`
for a web source) so `Message.citations` stays one homogeneous list.

### 43.5 Worker integration

`RunWorker` gained `advance_deep_research_runs`/`_advance_deep_research_run`/
`_run_deep_research_acquisition`/`_run_deep_research_synthesis`/
`_finalize_deep_research_success`, plus injectable
`research_search_adapter`/`research_fetch_adapter` constructor parameters
(mirrors the existing `chat_provider` injection exactly). `deep_research`
is excluded from the generic `claim_next_queued`/`recover_expired` queries
(same pattern as `_MCP_RUN_KIND`/`_CHAT_RUN_KIND`) — only the dedicated
sweep may claim it. Wired into `poll_once()` — the ONE integration point
the existing `RunWorkerSupervisor` already calls, so **zero supervisor
changes were needed**.

### 43.6 SSE/frontend

New `"research.progress"` control-event schema (closed `code` enum:
`research_searching`/`research_fetching`/`research_sources_ready`/
`research_synthesizing` — no `research_planning`) mapped by
`messages_stream._map_event` into an additive SSE event
`research_progress`. **Closeout correction (§43.2b)**:
`research_synthesizing` is now actually allocated by the worker (atomically
with the synthesis `RunStep`'s `pending`→`running` transition, before the
provider is invoked) — it previously existed only in this schema/mapping.
`useStreaming.ts` gained one dispatch case + an
optional `onResearchProgress` callback — an unaware older frontend build
already safely ignores the unrecognized event name via its existing
`default:` fallthrough. No change to `ModeSelector.tsx`/`ChatArea.tsx`/
`App.tsx` — the existing D3B selector already sends
`mode:"deep_research"` and already reads capability from the existing
`GET /api/settings` response.

### 43.7 Verification

**Closeout correction (§43.2b)**: the counts originally recorded here
(345/124/71) were never independently re-verified by the interrupted
session and are internally inconsistent on their face (345's own cited
sub-totals sum to 268, not 345). Replaced below with fresh command output
from the resume-and-closeout pass only, scoped to what that pass's
corrections could affect — the broader worker-integration regression
suite and the frontend suite were NOT re-run (neither was touched) and
their prior counts should be treated as unverified, not reasserted.

```
PYTHONPATH=backend .venv/Scripts/python.exe -m pytest \
  tests/test_deep_research_readiness.py tests/test_deep_research_worker.py \
  tests/test_research_activation_boundary.py tests/test_run_engine_mp3c3d3a.py \
  tests/test_research_snapshot_persistence.py tests/test_research_acquisition.py \
  tests/test_settings_mode_capability.py -q
```
→ **115 passed, 0 failed, 0 skipped** (13 readiness [12 + 1 new real-route
positive-path test] + 18 worker [17 + 1 new fail-closed context-snapshot
test, 1 corrected for the `research_synthesizing` fix] + 7 D4B
activation-boundary + 46 D3A mode-routing + 11 D4B snapshot-persistence +
11 D4B acquisition + 9 settings-capability). `py_compile` clean on every
file touched this pass (`submission.py`, `worker.py`, and the two test
files above); `git diff --check` clean; `import app.main` OK (fresh).
Frontend: not re-run this session (no frontend file touched by this
pass's corrections) — the prior "71 passed"/`tsc --noEmit` claims are
carried forward UNVERIFIED. No migration; no new dependency; no
KG/memory/directive write anywhere in the deep-research path (confirmed
by source inspection); no multi-agent work started;
`run_deep_research_enabled`/`run_messages_stream_durable_enabled`/
`run_engine_enabled` all stay `False` (re-confirmed fresh this pass, not
reused).

## 44. MP-3C3D5B — as-built: sealed role-artifact foundation for durable Multi-Agent (2026-09-13)

Foundation only, per the MP-3C3D5A plan. **OFF by default**
(`run_multi_agent_enabled=False`); no worker wiring, no public-route
change, no capability activation. Full detail:
`docs/superpowers/reports/phase-1b2r-mp3c3d5b-completion.md`.

### 44.1 Canonical contracts

* `app.run_engine.multi_agent_orchestration` — the ONE role graph:
  `ROLE_ORDER = (researcher, solution_specialist, reviewer)` (locked;
  "solution_specialist", not the legacy "Coder"), depth-1
  lineage/transition/budget as pure functions, `MULTI_AGENT_RUN_KIND =
  "multi_agent"`, and the canonical `AGENT_PROGRESS_CODES` frozenset
  (imported by both `events.py`'s `agent.progress` schema and
  `multi_agent_artifacts.py` — one definition, not two).
* `app.run_engine.multi_agent_artifacts` — versioned `agent_artifact_v1`
  schema (strict closed field set, server-owned role/sequence, bounded
  content/claims/assumptions/uncertainties/citations, deterministic
  canonical-serialization hash), fail-closed marker detection reusing
  `chat_stream.MarkerScanner` + `ALL_WATCHED_MARKERS` (no second marker
  list), and sealed persistence reusing `payloads.seal_payload`/
  `open_payload` unchanged.
* `app.run_engine.multi_agent_readiness` — mirrors
  `deep_research_readiness` exactly, plus one deliberate addition: a
  code-owned `_MULTI_AGENT_IMPLEMENTATION_READY = False` constant that
  makes `multi_agent_executable()` unconditionally `False` regardless of
  config, since no worker exists yet to execute a claimed run. D5C flips
  this same constant; it must never grow a second, config-settable twin.

### 44.2 Defect found and corrected during this resume pass

`seal_agent_artifact` originally validated `parent_artifact_id` only for
*shape* (a non-empty string) — it never verified the id resolved to a
REAL, already-sealed `RunStep` of the expected parent role within the
same run/tenant/user. Corrected: `seal_agent_artifact` now resolves the
parent via `db.query(RunStep)` and fails closed
(`AgentArtifactBindingMismatch`) before any write unless the parent step
exists, has the expected role's `kind`, and belongs to the same
`run_id`/`tenant_id`/`user_id`. This is what makes self-parenting, a
skipped or wrong-role parent, and cross-run linkage structurally
impossible to seal, not merely discouraged by convention — proved by
`tests/test_multi_agent_artifacts.py`'s lineage tests against a real
hermetic DB, not source inspection alone.

Two smaller corrections: `AGENT_PROGRESS_CODES` was defined twice
(`multi_agent_artifacts.py` and, as a second literal, inside
`events.py`'s schema); consolidated into the one definition in
`multi_agent_orchestration.py`. The generic `RunWorker.claim_next_queued`/
`recover_expired` sweep did not yet exclude `Run.kind == "multi_agent"` —
since the handler registry is empty, a queued multi-agent run would have
been claimed and immediately failed closed as `unknown_handler` before
D5C's own dedicated sweep could ever see it. Fixed by adding the same
`Run.kind != _MULTI_AGENT_RUN_KIND` exclusion `deep_research`/`chat`/`mcp`
already have.

### 44.3 Verification

`tests/test_multi_agent_{orchestration,artifacts,readiness}.py` — 153
new tests (43 + 60 + 50), all passing. Focused re-run of directly
affected existing suites — `test_run_payloads.py`,
`test_run_engine_events.py`, `test_run_engine_models.py`,
`test_research_config.py`, `test_research_activation_boundary.py`,
`test_settings_mode_capability.py`, `test_run_engine_mp3c3d3a.py`,
`test_deep_research_readiness.py` (179 passed), and
`test_run_engine_worker.py`, `test_run_engine_recovery.py`,
`test_run_engine_mp3b.py`, `test_run_engine_mp3c1.py`,
`test_deep_research_worker.py` (80 passed) — confirm the worker
reservation change does not regress chat/deep-research/MCP claim
isolation. `py_compile` clean on every changed/new backend file;
`git diff --check` clean; `import app.main` OK. No migration (`Run.kind`/
`RunStep.kind`/`RunPayload.payload_type` remain unconstrained strings);
no new dependency; no provider/network/tool call anywhere in the new
code (confirmed by source inspection — no worker exists to make one);
`run_multi_agent_enabled`/`run_engine_enabled`/
`run_messages_stream_durable_enabled` all stay `False`.
`docs/superpowers/reports/phase-1b2r-mp3c3d4c-completion.md`.


## MP-3C3D5C implementation closeout (2026-09-17)

D5C is implemented. The dedicated three-role worker, route/capability readiness,
transaction-serialized per-user active-run admission, marker-tail rejection, and
production frontend progress callback are wired. Deployment flags remain off by
default. This supersedes earlier D5C-not-started statements and the hook-only UI
deferral; D5B Solution Specialist naming and its six progress codes remain the
approved contract. See [D5C completion report](../reports/phase-1b2r-mp3c3d5c-completion.md)
for validation, recovery behavior, and remaining deployment boundaries.


## MP-3C3D5C closeout verification (2026-09-17)

Both high-risk boundaries named for D5C closeout were re-checked against the submitted code.

- **Serialized admission — PASS, no change.** One database write lock on the owning user row
  (`submission.create_chat_run`) covers idempotency lookup, the active-run count
  (`enforce_multi_agent_active_run_cap`), the `submit_run(commit=False)` staging and the commit
  that lands Message + payload + step + `run.queued` + the `created→queued` transition. Both
  public routes reach it: `routes/runs.py` → `create_chat_run`, and `routes/messages.py` →
  `messages_stream.resolve_and_submit` → `create_chat_run`. Proved by
  `test_multi_agent_submission.py::test_concurrent_admission_serializes_through_commit` (two
  independent connections, file-backed SQLite, cap of one). **Single-process-verified only** —
  PostgreSQL row-lock behavior is untested; no hosted/multi-process safety is claimed.
- **Final Reviewer text recovery — DEFECT, FIXED.** The boolean "any text event exists → publish
  nothing" guard was insufficient because `TextEventBuffer._flush()` commits each capped chunk in
  its own session: a crash mid-burst left a committed partial prefix and the suffix was lost
  forever while the run still finalized `succeeded`. Replaced with **exact-prefix continuation** —
  new `text_events.read_persisted_text` reads the committed prefix and
  `RunWorker._finalize_multi_agent_success` publishes only the exact missing suffix, failing
  closed on a malformed payload (`text_recovery_unreadable`) or a non-prefix
  (`text_recovery_mismatch`). No provider is invoked on this path; the lease/version CAS remains
  the sole finalization authority and the `output_ref.message_id` check remains the sole
  duplicate-Message guard. Tests:
  `test_partial_final_text_publish_recovers_exact_suffix_without_gap_or_duplicate`
  (observed failing before the fix) and `test_unexpected_persisted_text_fails_closed_without_a_message`.
- **Stale test reconciled.** `test_run_engine_mp3c3b.py::test_unsupported_modes_fail_before_persistence`
  still asserted the pre-D5C generic `mode_unsupported_multi_agent`; updated to the superseding
  `multi_agent_not_executable`. MP-3C3B was not part of the "334 passed" set quoted by the D5C
  report, which is how the drift survived.

Deployment flags remain `False` by default. No migration, no dependency, no new abstraction.
See the [D5C completion report](../reports/phase-1b2r-mp3c3d5c-completion.md) §"D5C closeout
verification".
