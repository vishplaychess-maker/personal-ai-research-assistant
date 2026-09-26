# Phase 1B.2 — Trusted Registry, Execution Identity, Approval Gateway, Reference MCP Wiring: Completion Report

**Date:** 2026-09-08 (+ Final Security Correction Pass, same day)
**Branch:** `phase-1b1-approval-domain` (HEAD `b2e6718`, off canonical `main`)
**Verdict:** **READY FOR USER REVIEW** (self-host-first scope; Option A) — after the correction pass in §0.
**Not committed. Not merged. Not pushed. Phase 1B.3 not started. Option B not started.**

> All Phase 1B work (1B.1 + 1B.2 + 1B.3) stays uncommitted on this feature branch until the whole
> of Phase 1B is implemented and approved. No checkpoint commits. `origin/HEAD` and all branch
> refs untouched.

---

## 0. Final Security Correction Pass — 2026-09-08

Five decisions applied before approval. Scope unchanged (Option A, self-host-first). Nothing
committed / merged / pushed; Phase 1B.3 not started.

### 0.1 What this pass changed

| # | Decision | Change |
|---|---|---|
| 1 | `session_grants` migration | `_migrate_approval_domain_1b2()` now: **empty** legacy (`allowed_action_hash` present, 0 rows) → guarded `DROP`+recreate with the current DDL **+ `_verify_session_grants_schema()`**; **non-empty** legacy → **`RuntimeError` halts startup** with an actionable message (rows are never transformed or dropped; `allowed_action_hash` ≠ `grant_scope_hash`). New `tests/test_approval_migrations.py` (7 scenarios). |
| 2 | Public grant creation deferred | `POST /api/approvals/{id}/grant` **removed entirely** (new this phase, no compat need). No client TTL anywhere. `list` / `revoke` of owned grants stay. `create_session_grant(...)` (explicit server-controlled `session_expires_at`) + its trusted tests stay — the only mint path. A one-time approval never becomes a grant. |
| 3 | `tool.execution.blocked` now | Emitted for **every** pre-invocation refusal on the authenticated server-scoped gateway path — sanitized, `execution_attempted=false`, trusted correlation only, stable closed reason vocabulary (`gateway.BLOCK_REASONS`, 19 codes). `action_log_failure` path does **not** recurse through the failed ActionLog. Callback never invoked on a block. |
| 4 | `call_index` wording + enforcement | "never ordinal" → **"not derived from, or authorized by, an ordinal position alone."** `tool_call_id` (row UUID) is the identity; `call_index` is a server-allocated slot; `open_tool_call` refuses to rebind a slot to a different `(tool_id, args_hash)`. Tests for slot-reuse / reorder / cross-turn / attacker-index added. Live-path transport-retry stability **not** claimed. |
| 5 | Bounded active proposals | New validated caps: per turn **3**, per session **10**, per user **20**; `max_mcp_calls_per_response` **3**. Gateway `_enforce_active_proposal_limits()` counts active (`requested`/`approved`) proposals per turn/session/user (excluding a retry of the same `tool_call_id`) under the per-scope ActionLog chain lock; over-limit → `approval.limit_exceeded` audit signal + `tool.execution.blocked` (`reason_code=active_proposal_limit`); callback not invoked. Client cannot pass/raise a limit. Approval- and grant-list page sizes already bounded (`limit` 1–100). |

### 0.2 Required statements

* **Migration behavior — empty vs non-empty legacy `session_grants`:** empty → guarded
  drop-and-recreate + post-recreate schema verification (columns, `grant_nonce` uniqueness, key
  indexes, obsolete column absent — raises otherwise); non-empty → startup halts with a clear
  actionable `RuntimeError`; the obsolete rows and column are left intact (no silent data loss),
  and the failure is deterministic on every retry. `allowed_action_hash` is **not** mapped to
  `grant_scope_hash` (no proven semantic equivalence).
* **Why public grant creation is deferred:** the repository has no trustworthy authenticated-
  session expiry / revocation binding, so there is no honest value for a reusable grant's
  lifetime; an arbitrary default TTL (the earlier 30-minute constant) is not acceptable. The
  route is omitted (not stubbed) because it is new this phase with no contract-compatibility
  requirement. Grant creation will be enabled only after a real session-expiry + revocation
  binding is implemented and tested.
* **Exact blocked-event contract:** event type `tool.execution.blocked`; emitted once per
  refused pre-invocation attempt on `gateway.dispatch`; `event_metadata` = trusted
  server-established correlation only (`reason_code`, `execution_attempted:false`,
  `tool_call_id`, `execution_turn_id`, `run_id`, `tenant_id`, `user_id`, `session_id`,
  `policy_version`, and when a trusted row is resolved `tool_id` / `mcp_server_id` /
  `server_fingerprint` / `tool_schema_hash`); **never** raw args, credentials, tokens, headers,
  launch config, or unrestricted error text; `reason_code` is always a `BLOCK_REASONS` member
  (attacker-influenced strings can't become a reason code); it never implies execution started
  and no `tool.execution.started` / `.succeeded` / `.failed` is written for that call; the
  dispatcher callback is not invoked; if the mandatory blocked-audit write itself fails,
  execution stays blocked and we do **not** recurse through the same failed ActionLog path
  (`reason_code = action_log_failure` returned with no further event). Unauthenticated HTTP
  never reaches the gateway, so it stays in normal access logs, not the tenant ActionLog.
* **Active-proposal & per-response limits:** per execution-turn 3, per session 10, per user 20
  (validated `>= 1` at startup; not client-settable). "Active" = `state ∈ {requested, approved}`.
  A retry of an already-pending logical call (same `tool_call_id`) is never blocked by the caps.
  MCP markers parsed per generated response ≤ `max_mcp_calls_per_response` (3). Approval-list and
  session-grant-list `limit` ∈ [1, 100]. Shared multi-worker / PostgreSQL enforcement of the
  caps remains subject to the hosted-deployment PostgreSQL verification gate (§19).
* **Precise `call_index` / `tool_call_id` semantics:** `tool_call_id` is the persistent
  server-owned identity (the `tool_calls` row UUID). `call_index` may be a server-assigned
  allocation slot within a persisted turn; it is trusted only because the trusted dispatch loop
  assigned it, **not** because it matches an LLM marker's ordinal. A slot is reusable only when
  the stored trusted `(tool_id, args_hash)` match; a different tool identity or `args_hash` on an
  existing slot fails closed (`ValueError`). Reordered / regenerated output cannot inherit a
  different call's authorization. `UNIQUE(execution_turn, call_index)` prevents cross-turn
  collision. Raw client / LLM `call_index` is never trusted directly — the trusted caller maps
  it to a server slot.
* **Live-streaming resume limitation:** unchanged — `run_mcp_calls` never auto-executes and
  creates no proposals; there is no durable turn identity, checkpointer, or task ledger on the
  live `/messages/stream` path, so an approved one-time call cannot be resumed there. Full
  end-to-end resume needs the dedicated durable-run architecture phase.
* **Full-suite & PostgreSQL status:** see §12 / §18 / §19 (re-run below). The full service-backed
  suite (live Ollama/ChromaDB, ~23 min) was **not** run here and is not claimed passing.
  PostgreSQL remains **unavailable** in this environment; PG verification of the partial-unique
  dedupe index, `SELECT … FOR UPDATE` chain serialization, dispatch idempotency, concurrent
  consumers, **and the new active-proposal caps** stays a **mandatory blocker before hosted
  multi-user deployment**.
* **All tests & exit codes:** §12 (updated).
* **Unresolved risks:** live one-time resume (durable-run phase); PostgreSQL verification;
  cross-process/worker cap enforcement (needs PG advisory lock / `FOR UPDATE` anchor); real
  session-expiry + revocation binding for grants; the minimal `_classify()` risk classifier
  (1B.3 engine); no FK from `user_id` / `session_id` to real tables.
* **Phase 1B.3 not started.** **Nothing committed, merged, or pushed** (no reset / stash / clean
  / discard; `origin/HEAD` and all branch refs untouched).

### 0.3 Files touched by the correction pass

`backend/app/config.py` (+4 caps, model-validator); `backend/app/approval/errors.py`
(+`ActiveProposalLimitError`); `backend/app/approval/__init__.py` (export);
`backend/app/approval/registry.py` (`resolve_reason` + `RESOLVE_REASONS`; split lock reason);
`backend/app/approval/identity.py` + `backend/app/models/models.py` (`ToolCall` docstring —
call_index wording); `backend/app/approval/gateway.py` (`BLOCK_REASONS`, `_block` w/ event,
`_enforce_active_proposal_limits`, reason-code remap); `backend/app/tools/mcp_tool.py`
(config-driven parse cap; `dispatch_mcp_call_via_gateway` docstring);
`backend/app/routes/approvals.py` (`/grant` route removed); `backend/app/main.py`
(`_migrate_approval_domain_1b2` fail-fast + `_verify_session_grants_schema` +
`_create_session_grants_table_current`); `evals/adapters/inprocess.py` (`_t45` reason codes);
`tests/test_approval_gateway.py` (+11 tests, reason-code asserts), `tests/test_approvals_api.py`
(grant-route removal tests), `tests/test_mcp_tool.py` (+config-cap test),
`tests/test_approval_migrations.py` (**new**, 7 tests).

---

## 1. Branch / source-control evidence

| ref | sha | relationship |
|---|---|---|
| HEAD / `main` | `b2e6718` (`origin/main`, ahead 2) | **canonical**; feature branch's tip *is* `main`'s tip — all Phase 1B work is uncommitted working-tree changes on top of `main` |
| `master` | `a92e3eb` (`origin/master`, ahead 41) | stale / divergent — `master..HEAD` = 50, `HEAD..master` = 0 |
| `origin/HEAD` | → `origin/master` | stale clone-time default; not authoritative; **not modified** |

Stage-0 resolution (accepted by the user): `main` canonical, `master` historical/divergent, the
feature branch is safely based on local `main`. No rebase / merge / switch / ref write performed.

`git status` after this phase: 11 modified tracked files + 24 new untracked paths (see §16). No
commit / merge / push / reset / stash / clean / branch-ref modification.

---

## 2. Requirements & locked decisions honored

| Locked decision | How |
|---|---|
| 1 — server-owned `execution_turn_id` + `tool_call_id`, never client/LLM, not marker text, not bare ordinal | `app/approval/identity.py` — `open_turn` / `open_tool_call`; `tool_calls.UNIQUE(execution_turn_id, call_index)`; `open_tool_call` **reuses** the row on a same-slot retry and **refuses to rebind** a different `(tool_id, args_hash)` to an existing slot; distinct calls → distinct ids; a new turn → new ids. `call_index` is a per-turn position, NOT the identity (the row's UUID is). Live `/messages/stream` **not modified** — real cross-request turn preservation documented as deferred (§10). |
| 2 — separate `proposal_dedupe_key` (≠ `args_hash` / `action_hash` / `grant_scope_hash` / `tool_call_id` alone) | `app/approval/dedupe.py` — RFC 8785/JCS over 21 fields incl. `run_id` + `execution_turn_id` + `tool_call_id` + full trusted scope. Enforced by a partial-UNIQUE index (`uq_pending_approvals_active_dedupe` on `COALESCE(tenant_id,'')`, `proposal_dedupe_key` WHERE `state IN ('requested','approved')`) + `request_approval` reuse (reuses the original row + original `action_nonce` + reconstructed envelope) + bounded CAS retry (`_DEDUPE_RETRIES = 5`, fail-closed `ChainIntegrityError`). Never accepted from the client. |
| 3 — `grant.authorization_used` write-before-execute; separate `tool.execution.*` events | `app/approval/gateway.py` — `grant.authorization_used` written by `_do_dispatch` for the grant path BEFORE the tool runs; `tool.execution.started` then `…succeeded`/`…failed`/`…blocked`, sanitized, with the full correlation field set (§7); raw output never in the payload (hash + length only); resolve = pure read (no event). |

Not implemented (as instructed): Option B durable streaming/resume; LangGraph checkpointer;
durable task ledger; global auto mode; risk-policy engine / carve-outs (1B.3); terminal / Python
/ browser / desktop wiring; frontend UI; OpenRouter; paid budgets; external ActionLog anchor;
Phase 1B.3.

---

## 3. Architecture — the reference MCP flow

```
[MCP_CALL] parsed by mcp_tool.extract_mcp_calls (unchanged, max 3/turn)
                    │
       ┌────────────┴─────────────┐
       │   LIVE chat path         │   TRUSTED INTERNAL reference path
       │ mcp_tool.run_mcp_calls   │   mcp_tool.dispatch_mcp_call_via_gateway
       │                          │   (tests / T45 / future durable dispatcher)
       ▼                          ▼
 NEVER auto-executes.        open_turn + open_tool_call  (server-owned ids)
 Returns a structured             │
 blocked / approval-required      ▼
 block:                     gateway.dispatch(ctx, mcp_name, args, execute_mcp)
  · tools disabled → blocked   1  gateway available?            (else blocked)
  · gateway not required →     2  server identity from ctx
    "risky MCP execution       3  registry.resolve_trusted       (unknown/locked/disabled → blocked)
    disabled" (never restores  -  prior DENIED for this tool_call_id → blocked (authz_denied)
    auto-exec)                 -  prior DispatchRecord for this tool_call_id → blocked
  · gateway unavailable →         (duplicate_dispatch / dispatch_incomplete)
    blocked (fail closed)      5  server_fingerprint + tool_schema_hash (from the trusted row)
  · gateway on → "approval     6  args_hash from the ACTUAL args
    required via /api/approvals 7  affected resources + deterministic risk metadata
    workflow; live resume not  8  build ActionEnvelope server-side (+ persist trusted_envelope_json)
    yet available; no tool     9  request_approval  (proposal_dedupe_key reuse; one ACTIVE row)
    executed and no proposal   10 exact one-time approval (state=approved)?
    created here"                   → reconstruct + re-hash from action_nonce, atomic consume
                                 else matching live SessionGrant?  → use it
                                 else → return approval_required   (NO execution)
                              7  at-most-once local: DispatchRecord.UNIQUE(tool_call_id) + CAS
                              -  grant path: write grant.authorization_used  (before execute)
                              13 write tool.execution.started
                              14 call execute_mcp()  ← the ONLY tool invocation
                              15 write tool.execution.succeeded | failed  (sanitized)
                                 DispatchRecord.outcome = succeeded | failed | incomplete
                              16 return GatewayResult(status, reason_code, tool_output, …)
```

`run_mcp_calls` accepts `**_ignored` so the unchanged `streaming_service` / `langgraph_workflow`
call sites keep working; the legacy auto-execution body is **gone**.

---

## 4. Identity lifecycle & dedupe contract

| Property | Value |
|---|---|
| `execution_turn_id` — creation | `identity.open_turn` on the trusted server dispatch path; never a public API, never the streaming handler |
| `execution_turn_id` — persistence | `execution_turns` (`id`, `tenant_id?`, `user_id`, `session_id?`, `run_id?`, `assistant_message_id?`, `kind`, `created_at`) |
| `tool_call_id` — creation | `identity.open_tool_call(turn, call_index, tool_id, args_hash)` |
| `tool_call_id` — persistence | `tool_calls` (`UNIQUE(execution_turn_id, call_index)`) |
| retry (same persisted logical call) | `open_tool_call` returns the stored row (and its id); `request_approval` returns the existing ACTIVE `PendingApproval` + its original `action_nonce` |
| a genuinely new call / new turn | new UUIDs; a different `(tool_id, args_hash)` on an existing `call_index` **raises** (no accidental identity reuse across reordered/regenerated markers) |
| multiple calls in one turn | distinct `call_index` → distinct `tool_call_id` |
| expiry / retention | turns/calls are audit records; pruned with the ActionLog retention policy (operational, later); no TTL enforced here |
| tenant/user/session ownership | every row carries `tenant_id?` + `user_id` (+ `session_id?`); code-enforced |
| **regenerated vs transport retry (live path)** | the live `/messages/stream` path is **NOT modified** and **cannot** distinguish them — both are fresh POSTs that re-run the LLM. Real cross-request turn preservation is **deferred** to the durable-run phase. The dedupe / retry guarantees apply **only** where the same trusted persisted `execution_turn_id` / `tool_call_id` are supplied through an internal path. |

`proposal_dedupe_key` field set (`PROPOSAL_DEDUPE_FIELDS`, 21): `proposal_dedupe_version`,
`tenant_id`, `user_id`, `session_id`, `run_id`, `execution_turn_id`, `tool_call_id`, `tool_id`,
`tool_version`, `source_type`, `mcp_server_id`, `server_fingerprint`, `tool_schema_hash`,
`args_hash`, `affected_resources` (sorted), `network_scope`, `filesystem_scope`,
`credential_scope`, `risk_level`, `risk_scope_flags` (sorted), `policy_version`.
`test_proposal_dedupe_key_is_distinct_from_every_1b1_hash` proves it ≠ `args_hash`, ≠
`tool_schema_hash`, ≠ `server_fingerprint`, changes on `tool_schema_hash` or `run_id`.
`test_concurrent_identical_proposals_create_one_active_pending` (8 threads, file SQLite) → exactly
one active `PendingApproval`.

---

## 5. Trusted registry contract (`app/approval/registry.py`, `trusted_tools` table)

| Field | Source |
|---|---|
| `tool_id`, `tool_version`, `source_type`, `mcp_server_id`, `mcp_server_name` | canonical, server-owned |
| `input_schema_json` | canonicalized (RFC 8785) non-secret schema |
| `tool_schema_hash` | `hashing.tool_schema_hash(canonical schema)` |
| `server_fingerprint` | `hashing.server_fingerprint` over source type + manifest id + executable identity + **non-secret launch config (command + args ONLY — `env` excluded)** + tool name + schema hash + `config_ref_id` |
| `config_ref_id` | `mcp_server:<id>` — a server-owned reference, **never** a credential / env value |
| `enabled`, `locked`, `lock_reason`, `risk_metadata_json` (1B.3 seam), `policy_version`, `discovered_at`, `updated_at` | server-owned |

Rules (all tested): unknown / unregistered tool → `resolve_trusted` returns None (fail closed);
**newly discovered tool → `locked = True` ("newly discovered")**; a second
`register_or_refresh(unlock_if_unchanged=True)` (operator re-verification, identity unchanged) →
unlocked; **any change to `tool_schema_hash` or `server_fingerprint` → `locked = True`
("schema or server-fingerprint changed")** and existing approvals / SessionGrants (which bind
those hashes) no longer match; disabled tool → not resolvable; a registry exception in the
gateway → `blocked / registry_error`. Raw credentials never enter `trusted_tools`, the
fingerprint pre-image, API output, or ActionLog payloads.

"Materially changed" = deterministic: `tool_schema_hash != stored` OR `server_fingerprint != stored`.

---

## 6. Approval API contract (`app/routes/approvals.py`)

| Route | Auth | CSRF | Notes |
|---|---|---|---|
| `GET /api/approvals?state&limit&offset` | `get_current_user`, owner-filtered | — | paginated (`limit` 1–100), redacted `display_summary`, `action_nonce` never returned |
| `GET /api/approvals/{id}` | owner | — | 404 for cross-user / cross-tenant (non-disclosing) |
| `POST /api/approvals/{id}/approve` | owner | ✅ | `requested → approved`; **idempotent** (`{idempotent: true}` on repeat); **409** if already `denied` / `expired` / `consumed`; body `extra="forbid"` (no trusted fields); server-decided expiry re-checked first; **never executes**; **never creates a grant** |
| `POST /api/approvals/{id}/deny` | owner | ✅ | mirror of approve |
| ~~`POST /api/approvals/{id}/grant`~~ | — | — | **REMOVED (correction — Decision 2).** No public session-grant creation in Phase 1B.2 — no route, no client TTL. A grant is minted only by the trusted domain fn `create_session_grant(...)` with a server-controlled `session_expires_at`. |
| `GET /api/session-grants?active_only&limit&offset` | owner | — | paginated (`limit` 1–100) |
| `POST /api/session-grants/{id}/revoke` | owner | ✅ | idempotent; 404 cross-user |

**No `/consume` route** (`test_no_public_consume_route_exists`). Approve/deny write ActionLog
events and never trigger `tool.execution.*`. Unauthenticated → 401. Duplicate identical decision
→ 200 idempotent; conflicting decision → 409. Response bodies redact secrets and are bounded.

---

## 7. Authorization-vs-execution & lifecycle events

- `grant.authorization_used` — written by `gateway._do_dispatch` (grant path) **before** the tool
  runs; means *session-grant authorization exercised for this attempt*, `event_metadata.meaning`
  says explicitly "NOT an execution-result event"; a crash right after cannot erase it.
- One-time path: `consume_approval` writes `approval.consumed` (from Phase 1B.1) at the atomic
  handoff — no separate `authorization_used` needed.
- `tool.execution.started` → `tool.execution.succeeded` | `tool.execution.failed`.
  **`tool.execution.blocked` is now emitted (correction — Decision 3)** for every pre-invocation
  refusal on `gateway.dispatch`: sanitized, `execution_attempted=false`, `reason_code` ∈
  `gateway.BLOCK_REASONS` (closed vocabulary), trusted correlation only, no
  `tool.execution.started` for that call, callback not invoked; the `action_log_failure` case
  does not recurse through the failed ActionLog. See §0.2. Each execution event carries
  correlation fields: `approval_id`|`grant_id`, `action_hash`,
  `run_id`, `execution_turn_id`, `tool_call_id`, `tenant_id`, `user_id`, `session_id`, `tool_id`,
  `mcp_server_id`, `server_fingerprint`, `tool_schema_hash`, `policy_version`.
- **Sanitization:** raw tool output is **never** in the payload — only `output_sha256` +
  `output_len`; a tool *error* string is `redact()`-ed and length-capped (500).
- `resolve_session_grant` writes nothing (pure read) — `test_passive_grant_lookup_writes_no_authz_used_event`.
- A failed authorization check never reaches `execute_mcp` —
  `test_first_call_returns_approval_required_and_does_not_execute`,
  `test_denied_expired_mismatch_block_execution`.

---

## 8. Replay / idempotency guarantee (stated precisely)

**LOCAL at-most-once per persisted logical `tool_call_id`** (`dispatch_records.UNIQUE(tool_call_id)`
+ CAS insert + an early gateway check):

- a consumed one-time approval cannot execute twice — `consume_approval` CAS + the `DispatchRecord`
  early check (`test_approved_one_time_executes_once_then_replay_blocked` → 1 execution, then
  `blocked / duplicate_dispatch`);
- a SessionGrant-authorized retry of the **same** `tool_call_id` does not execute twice (same
  early check);
- a fresh proposal for an already-dispatched `tool_call_id` is blocked (the early check runs
  before `request_approval`);
- a DENIED decision for a `tool_call_id` **sticks** — re-dispatching that logical call returns
  `blocked / authz_denied` (no nag re-prompt);
- **crash after dispatch begins:** `DispatchRecord.outcome` = `incomplete` (catchable
  `KeyboardInterrupt`/`SystemExit` propagate; a real `SIGKILL` leaves `started`). Either way, a
  later attempt on that `tool_call_id` is **blocked (`dispatch_incomplete`), not auto-retried**;
  the state is honestly "unknown" pending reconciliation
  (`test_crash_after_dispatch_leaves_started_and_is_not_auto_retried`).
- **External exactly-once is NOT claimed** — `dispatch_records` is a local guarantee; whatever the
  MCP tool does on its own server is outside it. Stated in `gateway.py`'s docstring and
  `test_external_exactly_once_is_not_claimed`.

---

## 9. Gateway failure behavior (all tested)

| Condition | Result |
|---|---|
| `approval_gateway_available = False` | `blocked / gateway_unavailable` |
| `approval_gateway_required = False` (live path) | `blocked` — "risky MCP execution disabled; never restores auto-execution" |
| registry exception | `blocked / registry_error` |
| DB error during `request_approval` | `blocked` |
| ActionLog write failure at `grant.authorization_used` or `tool.execution.started` | `blocked / actionlog_error`, `execute_mcp` never called |
| unknown / unregistered tool | `blocked / tool_not_authorized` |
| new / schema-changed / fingerprint-changed tool | `locked` → `blocked / tool_not_authorized` |
| denied / expired / consumed / mismatched / cross-user / cross-session / cross-tenant authorization | `blocked` (`authz_denied` / `authz_expired` / `authz_mismatch`) |
| valid exact one-time approval | executes once |
| valid SessionGrant within its exact stable scope | executes; writes `grant.authorization_used` |
| revoked / expired grant / scope mismatch | no match → `approval_required` (never executes) |

No error path falls back to legacy marker auto-execution.

---

## 10. Live streaming limitations (honest)

- `run_mcp_calls` (the live chat entry) **never executes an MCP tool** and **creates no
  proposals** — it returns a structured block: gateway-disabled / unavailable / "approval
  required via `/api/approvals`; the live chat path cannot resume an approved call yet
  (durable-run phase); no tool executed and no proposal created here."
- There is **no** way for the live streaming generator to resume an approved one-time call — the
  generator that parsed the marker is gone, there is no durable turn identity, no LangGraph
  checkpointer, no task ledger.
- Deliberately **not** done: execute from the approve API; a public `/consume`; an in-memory
  "durable" callback; re-running the LLM and calling it the same call; silent auto-execution.
- **Temporary user-visible MCP limitation (accepted for 1B.2):** with the gateway on, an MCP
  `[MCP_CALL]` in chat shows an "approval required — not yet resumable" block instead of running.
  Fail-closed security takes priority over unsafe auto-execution. Full end-to-end resume requires
  a dedicated durable-run architecture phase before Thunder AI is a durable autonomous work
  manager.
- The full gateway lifecycle (proposal → approve → trusted re-entry → execute-once) **is** proven
  in-process (`tests/test_approval_gateway.py`) and via `dispatch_mcp_call_via_gateway` (T45).

---

## 11. Migrations

`backend/app/main.py :: _migrate_approval_domain_1b2()` — dialect-agnostic, **additive,
idempotent** (`CREATE TABLE/INDEX IF NOT EXISTS`, guarded `ALTER … ADD COLUMN`). Runs in
`_migrate_database()`'s pre-guard block after `_migrate_approval_domain()`.

| New table / column | Ownership | Uniqueness / index | Nullable (self-host vs hosted) |
|---|---|---|---|
| `trusted_tools` | `owner_user_id` + `tenant_id?` | `UNIQUE(owner_user_id, tool_id)`; `ix(owner_user_id, tool_id, enabled, locked)`; `ix(tenant_id)` | `tenant_id` NULL for self-host |
| `execution_turns` | `user_id` (+ `session_id?`, `tenant_id?`) | `ix(user_id, session_id)` | `tenant_id`/`session_id` nullable |
| `tool_calls` | `user_id` (+ `session_id?`, `tenant_id?`) | `UNIQUE(execution_turn_id, call_index)`; `ix(user_id, session_id)`; `ix(execution_turn_id)` | nullable as above |
| `dispatch_records` | `user_id` (+ `tenant_id?`) | `UNIQUE(tool_call_id)`; `ix(user_id)` | `tenant_id` nullable |
| `pending_approvals.proposal_dedupe_key` / `.execution_turn_id` / `.tool_call_id` / `.trusted_envelope_json` | inherited | `ix(tenant_id, proposal_dedupe_key)`, `ix(execution_turn_id)`, `ix(tool_call_id)`; **partial-UNIQUE** `uq_pending_approvals_active_dedupe` on `COALESCE(tenant_id,''), proposal_dedupe_key` WHERE `state IN ('requested','approved')` | all nullable (pre-1B.2 rows) |

**`session_grants` reconciliation (correction — Decision 1):** a straddling DB still carrying the
obsolete `allowed_action_hash` column is handled by column state:

| Legacy `session_grants` state | Behavior |
|---|---|
| `allowed_action_hash` absent | no-op (already the current shape; idempotent) |
| `allowed_action_hash` present, **0 rows** | guarded `DROP TABLE` + recreate with the current DDL + all indexes, then **`_verify_session_grants_schema()`** — asserts every required column, `grant_nonce` uniqueness, `ix_session_grants_lookup` / `ix_session_grants_scope`, and that `allowed_action_hash` is gone; raises `RuntimeError` on any gap |
| `allowed_action_hash` present, **≥ 1 row** | **startup halts** with an actionable `RuntimeError` (names the table, the obsolete column, the row count, and the manual remediation). Rows and the obsolete column are left intact — **no silent data loss** — and the failure is deterministic on every retry. `allowed_action_hash` (one-time action hash) is **not** mapped to `grant_scope_hash` (reusable scope) — no proven equivalence. |

Validated (`tests/test_approval_migrations.py`, 7 tests): clean-DB creation; upgrade from the
1B.1 schema; `_migrate_approval_domain_1b2()` run **×3** (idempotent); empty-legacy rebuild +
schema verification + re-run stability; non-empty-legacy fail-fast (message names table + column
+ count) + no data loss + deterministic re-raise; post-migration ORM compatibility (all 6
approval-domain models insert against the migrated DDL; `create_all` agrees). Partial-unique
`uq_pending_approvals_active_dedupe` present with the expected `COALESCE`/`WHERE` clause.
`_migrate_database()` was **not** re-ordered; the existing startup path is unchanged.

---

## 12. Exact commands & exit codes

Repo venv: `./.venv/Scripts/python.exe`. All from repo root. **Re-run after the correction pass,
2026-09-08.**

| Command | Result | Exit |
|---|---|---|
| `compileall -q backend/app/approval backend/app/models/models.py backend/app/main.py backend/app/config.py backend/app/tools/mcp_tool.py backend/app/routes/approvals.py evals/adapters/inprocess.py tests/test_approval_domain.py tests/test_approval_gateway.py tests/test_approvals_api.py tests/test_approval_migrations.py tests/test_mcp_tool.py` | ok | **0** |
| `PYTHONPATH=backend … pytest tests/test_approval_domain.py -q`  (Phase 1B.1 regression) | **166 passed, 2 skipped** | **0** |
| `PYTHONPATH=backend … pytest tests/test_approval_gateway.py -q` | **34 passed, 1 skipped** (PG) — was 23; +11 (Decision 3/4/5) | **0** |
| `PYTHONPATH=backend … pytest tests/test_approvals_api.py -q` | **18 passed** — was 13; grant-route-removal + bounds | **0** |
| `PYTHONPATH=backend … pytest tests/test_approval_migrations.py -q`  (**new** — Decision 1) | **6 passed** (7 scenarios) | **0** |
| `PYTHONPATH=backend … pytest tests/test_mcp_tool.py tests/test_mcp_dispatch.py tests/test_mcp_service.py tests/test_mcp_routes.py tests/test_mcp_prompt_wiring.py tests/test_tool_registry.py -q` | **33 passed** — was 32; +1 config-cap | **0** |
| `PYTHONPATH=backend … pytest tests/test_model_validation.py -q`  (model/`create_all`) | **4 passed** | **0** |
| `PYTHONPATH=backend … pytest tests/test_evals_runner.py tests/test_evals_inprocess.py -q`  (Gate A unit + Gate B integrity) | **74 passed** (68 + 6) | **0** |
| `PYTHONPATH=backend … -m evals.runner --offline` | 19 synthetic / 2 placeholder / 2 unverified / 24 blocked / 0 fail / 0 error | **0** |
| `PYTHONPATH=backend … -m evals.runner --inprocess` | **17 verified** / 4 unverified / 24 blocked / 0 fail / 0 error | **0** |
| `git diff --check` | clean | **0** |
| `python -c "import yaml; yaml.safe_load(open('.github/workflows/evals.yml'))"` | yaml OK | **0** |
| `PYTHONPATH=backend python -c "import app.main; import app.approval"` | import OK | **0** |
| secret scan | no `detect-secrets` / `gitleaks` / `trufflehog` installed; heuristic `git diff | grep` (sk-…, password=, PRIVATE KEY, filtered for non-test) → **no matches** | — |
| broad hermetic regression | `<SEE §18>` | `<§18>` |

T27, T29–T32, T41–T43, and **T45** remain verified (§13, §17). T44 remains `blocked_by: "1B"`
(Phase 1B.3). T45 stays a real `product_inprocess` evaluation — its `_t45` adapter runs the real
registry + gateway and now asserts the precise reason codes (`unknown_tool`, `tool_locked`,
`schema_mismatch`).

---

## 13. Gate A / Gate B counts

| | verified | synthetic | placeholder | unverified | blocked | fail | error | exit |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| Gate A `--offline` | 0 | **19** | 2 | 2 | **24** | 0 | 0 | **0** |
| Gate B `--inprocess` | **17** | 0 | 0 | 4 | 24 | 0 | 0 | **0** |

Verified-pass **17 ≥ 16** (retained/improved: **T45** joined T27/T29–T32/T41–T43). `provenance_gaps == {}`.

---

## 14. Adversarial findings

| # | Finding | Severity | Evidence / repro | Fix / mitigation | Residual risk | Blocks 1B.2? |
|---|---|---|---|---|---|---|
| A | Prompt-injection causing `[MCP_CALL]` emission | — | model emits the marker | `run_mcp_calls` never auto-executes; gateway builds identity + envelope server-side; marker text is not an authority boundary | injected markers create no side effect on the live path | No |
| B | Client field tampering on the decision API | — | `POST /approve` with `{action_hash, risk_level, state}` | body `extra="forbid"` → 422 (`test_client_cannot_send_trusted_fields`) | none | No |
| C | Replay of a completed side effect | — | re-dispatch same `tool_call_id` | `DispatchRecord.UNIQUE` + early gateway check → `blocked` | external server's own dedup is its concern (documented) | No |
| D | Duplicate / conflicting API decisions | — | approve twice / approve then deny | idempotent 200 / deterministic 409 | none | No |
| E | Cross-tenant / cross-user access to approvals & grants | — | `GET/POST` another user's id | owner filter → 404 (non-disclosing); grant reconstruction re-checks identity | none | No |
| F | **Straddling-DB `session_grants` schema defect (Phase 1B.1)** — `_migrate_approval_domain()` uses `CREATE TABLE IF NOT EXISTS`, so a DB created **before** the 1B.1 close-out (which renamed `allowed_action_hash → grant_scope_hash` + added `grant_nonce`) keeps the old, broken shape and `create_session_grant` fails with `NOT NULL constraint failed`. | **medium** (correctness) | run 1B.1 `create_session_grant` against a DB created pre-close-out | **corrected (Decision 1):** `_migrate_approval_domain_1b2()` — legacy column present + **0 rows** → guarded `DROP`+recreate with the current DDL **+ `_verify_session_grants_schema()`**; legacy column present + **≥1 row** → **startup halts** with an actionable `RuntimeError` (rows + column left intact, no silent loss, deterministic). No mapping of `allowed_action_hash` → `grant_scope_hash`. `tests/test_approval_migrations.py` (7 scenarios). | a straddling DB with real grant rows now **fails fast** with a manual-remediation message instead of silently continuing with a broken schema. | No — corrected |
| G | Registry cache poisoning | — | tamper `tools_json` on `MCPServer` | `trusted_tools` (not the `MCPServer.tools_json` cache) is the authority; a schema/fingerprint mismatch re-locks; `resolve_trusted` returns None for locked/unknown | operator must re-verify a genuinely changed tool | No |
| H | Stale grant after schema/fingerprint change | — | change tool schema, present old grant | `grant_scope_hash` binds `tool_schema_hash` + `server_fingerprint` → no match; also the tool re-locks | none | No |
| I | Approval reuse across runs / turns / reordered calls | — | present a different `run_id` / `call_index` / reordered marker | `proposal_dedupe_key` binds `run_id` + `execution_turn_id` + `tool_call_id`; `open_tool_call` refuses to rebind a slot | none | No |
| J | Partial multi-call execution | low | one call fails, another proceeds | per-`tool_call_id` `DispatchRecord`; `test_one_call_failure_does_not_corrupt_another` → independent outcomes | if the process dies between calls, each is independently reconcilable | No |
| K | Crash between authorization and execution | low | `KeyboardInterrupt` in `execute_mcp` | `DispatchRecord.outcome = incomplete`; later attempt `blocked / dispatch_incomplete`, not auto-retried | a side-effecting tool that *did* run once but crashed before logging is recorded `incomplete` for human reconciliation | No |
| L | Crash after external execution, before success logging | low | tool returns then process dies before `succeeded` event | `DispatchRecord.outcome` stays `started`/`incomplete`; the outcome is honestly unknown, not claimed succeeded | same as K | No |
| M | ActionLog write failure at a security transition | — | monkeypatch `append_event` to raise on `tool.execution.started` | `blocked / action_log_failure`, `execute_mcp` not called, and the failed ActionLog path is **not** retried by writing a blocked event (`test_action_log_failure_reason_does_not_recurse`) | if the `succeeded`/`failed` event write fails *after* execution, `_safe_event` swallows it — but the `DispatchRecord.outcome` still records the true result | No |
| N | DB transaction rollback during authorization | — | IntegrityError on the active-dedupe index | bounded CAS retry (5) then fail-closed `ChainIntegrityError`; concurrent test → one active row | none | No |
| O | Error messages leaking arguments / credentials | — | tool raises with a secret in the message; output contains `sk-…` | `_sanitize_error` = `redact()` + 500-char cap; `succeeded`/`failed` payloads carry `output_sha256`/`output_len`, not the text (`test_started_then_succeeded_events_sanitized`) | none | No |
| P | DoS via unlimited pending proposals | low | a caller of the trusted path spams genuinely-new `tool_call_id`s | **corrected (Decision 5):** bounded active-proposal caps per execution-turn (3) / session (10) / user (20) enforced in the gateway under the per-scope chain lock, excluding a retry of the same `tool_call_id`; over-limit → `approval.limit_exceeded` audit + `blocked / active_proposal_limit`, callback not invoked; client cannot pass/raise a limit. Live `run_mcp_calls` still creates **no** proposals; markers parsed per response ≤ `max_mcp_calls_per_response` (3). Tests: per-turn/session/user boundary, retry-not-blocked, 8-thread concurrency. | strict cross-worker/PG enforcement needs a PG advisory lock or `FOR UPDATE` anchor — same gate as §19 | No — corrected |
| Q | Unbounded approval / grant listing | — | `GET /api/approvals?limit=99999`, `GET /api/session-grants?limit=99999` | `limit` `ge=1, le=100` → 422 on **both** list endpoints (`test_list_is_paginated_and_bounded`, `test_session_grant_list_page_size_is_bounded`) | none | No |
| R | Unauthorized / arbitrary session-grant creation | — | any `POST` to create a grant with a client-chosen TTL | **corrected (Decision 2):** there is **no public grant-creation route** — omitted entirely; no client TTL is accepted anywhere; a one-time approval never becomes a grant. Grants are minted only by the trusted domain fn with a server-controlled `session_expires_at`. Tests: no `/grant` route; unauth/auth' cannot; client TTL impossible; approve creates no grant; no disabled path writes a row. | grant creation stays disabled until a real session-expiry + revocation binding exists (§15) | No — corrected |
| S | Gateway config rollback restoring unsafe execution | — | set `approval_gateway_required = False` | `run_mcp_calls` returns `blocked` ("risky MCP execution disabled; never restores auto-execution"); no code path re-enables the legacy body | none | No |

**No blocker or unmitigated high finding.**

---

## 15. Known limitations & residual risks

- **Live one-time approval resume is unavailable** (§10) — deferred to the durable-run phase.
- **PostgreSQL not verified in this environment** — `EVAL_PG_TEST_URL` unset, no `psql`, Docker
  daemon unreachable. **PG verification (partial-unique dedupe index, `SELECT … FOR UPDATE` chain
  serialization, dispatch idempotency, concurrent consumers, and strict cross-connection
  enforcement of the active-proposal caps) remains a mandatory blocker before hosted multi-user
  deployment** — not a blocker for this self-host-first review.
- **Public session-grant creation is deferred (Decision 2).** No route, no client TTL. The repo
  has no trustworthy authenticated-session expiry / revocation binding, so there is no honest
  reusable-grant lifetime; the earlier arbitrary 30-minute default was removed. Grants are minted
  only by `create_session_grant(...)` with a server-controlled `session_expires_at`. Re-enable
  only after a real session-expiry + revocation binding is implemented and tested.
- **Active-proposal caps: strict multi-worker enforcement is best-effort outside SQLite.** The
  gateway serializes count+insert via the per-scope ActionLog chain lock (deterministic on SQLite
  `BEGIN IMMEDIATE`); a PG advisory lock / `FOR UPDATE` anchor for cross-connection strictness is
  part of the §19 PG gate.
- `_classify()` (risk policy) is still the minimal 1B.1 deterministic classifier; the real engine
  is 1B.3.
- No FK from the new `user_id` / `session_id` columns to real tables (testability; add later).
- A **non-empty** straddling `session_grants` (obsolete `allowed_action_hash` + real rows) now
  **halts startup** with a manual-remediation message (Decision 1) — an operator must back up and
  drop/clear the table; a proper data-preserving backfill is a follow-up only if such a DB with
  genuinely resolvable grant rows ever needs to be preserved (evidence says it cannot exist).
- `dispatch_mcp_call_via_gateway` is the only path that actually executes; it is not reachable
  from the live UI (by design, this phase).

---

## 16. Exact files changed

**Modified (11):**

| File | Change |
|---|---|
| `backend/requirements.txt` | (1B.1) `rfc8785==0.1.4` — unchanged this phase |
| `backend/app/config.py` | +`approval_gateway_required: bool = True`, +`approval_gateway_available: bool = True` |
| `backend/app/models/models.py` | +4 models (`TrustedTool`, `ExecutionTurn`, `ToolCall`, `DispatchRecord`); +4 `PendingApproval` columns; dropped redundant per-column `index=True` (kept explicit composite indexes) |
| `backend/app/main.py` | +`_migrate_approval_domain_1b2()` (+ its call); +`approvals_router` import & `include_router` |
| `backend/app/tools/mcp_tool.py` | `run_mcp_calls` → gated, never auto-executes; +`dispatch_mcp_call_via_gateway` (trusted internal path); +`_blocks` helper; docstring rewrite |
| `evals/adapters/inprocess.py` | +`_t45` target (registry + gateway); registered in `_TARGETS` |
| `evals/dataset.jsonl` | T45: `blocked_by` removed, `asserts` + `product_target` set (T44 unchanged) |
| `tests/test_evals_runner.py` | baseline counts: synthetic 18→19, blocked 25→24 |
| `tests/test_evals_inprocess.py` | verified-pass floor 16→17 |
| `tests/test_mcp_tool.py` | 4 old auto-execution `run_mcp_calls` tests → 4 gated-behavior tests |
| `.github/workflows/evals.yml` | Gate B runs `test_approval_gateway.py` + `test_approvals_api.py` |

**New (untracked):**

| File | Lines | Purpose |
|---|---|---|
| `backend/app/approval/registry.py` | ~240 | Trusted registry |
| `backend/app/approval/identity.py` | ~120 | `execution_turn_id` / `tool_call_id` seam |
| `backend/app/approval/dedupe.py` | ~75 | `proposal_dedupe_key` |
| `backend/app/approval/gateway.py` | ~290 | Centralized gateway / dispatcher boundary |
| `backend/app/approval/service.py` | (modified) | `request_approval` dedupe reuse; `reconstruct_envelope` |
| `backend/app/approval/__init__.py` | (modified) | +12 exports |
| `backend/app/routes/approvals.py` | ~250 | Approval + session-grant API |
| `tests/test_approval_gateway.py` | ~490 | 24 tests |
| `tests/test_approvals_api.py` | ~300 | 13 tests |
| `evals/fixtures/T45.json` | — | synthetic-regression fixture |
| `docs/superpowers/plans/2026-09-07-phase-1b2-trusted-registry-gateway.md` | — | plan + Stage 0 + user decision |
| `docs/superpowers/reports/phase-1b2-completion.md` | — | this report |

`git diff --stat`: 11 files changed, **1227 insertions(+), 56 deletions(-)** (modified-tracked
only; the ~1900 new lines are in untracked files).

---

## 17. T45 evidence

`evals/adapters/inprocess.py::_t45` runs **real** `app.approval.registry` + `app.approval.gateway`
on an in-memory SQLite DB; the MCP `execute_mcp` callback is a mock counting invocations. Proves,
in one target:

1. **unregistered** MCP tool → `blocked / unknown_tool`, dispatcher callback not invoked;
2. **newly discovered** tool → `locked = True` → `blocked / tool_locked`;
3. after operator re-verification (unchanged identity) + user approval → the **exact registered
   version** executes **exactly once**;
4. a **schema change re-locks** the tool → a fresh call is `blocked / schema_mismatch`, and
   the earlier approval (bound to the old `tool_schema_hash`) does not authorize it.

Result: `product_inprocess` **pass**, `authorized_executions=1`, `unauthorized_executions=0`.
`blocked_by` removed only because this genuinely passes. T44 remains `blocked_by: "1B"` (Phase
1B.3 auto-mode / risk policy). T27, T29, T30, T31, T32, T41, T42, T43 remain verified.

---

## 18. Full-suite status (re-run after the correction pass)

**The full service-backed backend suite was NOT run** — it needs live Ollama / ChromaDB, absent
here, and takes ~23 min. Not mislabeled as passing.

**Hermetic regression re-run, 2026-09-08 (no live services):**

| Suite | Result |
|---|---|
| `tests/test_approval_domain.py` (1B.1 regression) | **166 passed, 2 skipped**, EXIT 0 |
| `tests/test_approval_gateway.py` | **34 passed, 1 skipped**, EXIT 0 |
| `tests/test_approvals_api.py` | **18 passed**, EXIT 0 |
| `tests/test_approval_migrations.py` (new) | **6 passed**, EXIT 0 |
| `tests/test_mcp_tool.py … test_tool_registry.py` (6 files) | **33 passed**, EXIT 0 |
| `tests/test_model_validation.py` | **4 passed**, EXIT 0 |
| `tests/test_evals_runner.py tests/test_evals_inprocess.py` | **74 passed**, EXIT 0 |
| broad subset `test_planning test_session_ownership test_async_bridge test_evaluation test_system_prompts test_browser_agent test_sessions` | **44 passed, 15 errors** — all 15 are `httpx.ConnectError` in `tests/test_sessions.py` (needs a live server — pre-existing environment gap), EXIT 1 |

**Non-regressions (pre-existing, unrelated to this work):**
- `tests/test_sessions.py` / `test_memories.py` / `test_health.py` → `httpx.ConnectError` (no
  live backend in this environment).
- `tests/test_terminal.py::test_output_byte_cap` → Windows `FIND.exe` vs Unix `find`
  (`FIND: Invalid switch`) — pre-existing OS assumption; nothing here touches `terminal_panel.py`.

**Deliberate test behavior changes in this pass** (not regressions): 5 reason-code asserts in
`test_approval_gateway.py` updated to the `BLOCK_REASONS` vocabulary; `test_approvals_api.py`
grant tests rewritten for the removed route; `_t45` reason codes updated. The 4 `run_mcp_calls`
gated-behavior tests from the original phase are unchanged.

- **Genuine regressions from the correction pass:** none observed.
- **Phase 1B.1 regression:** none — `tests/test_approval_domain.py` **166 passed, 2 skipped**,
  unchanged.
- **Skipped:** 2 PostgreSQL-only. Recorded as a hosted-deployment blocker (§19).
- **Not run:** the full 23-min service-backed suite — a **mandatory gate before the complete
  Phase 1B commit/push approval**.
- **Not claiming "all tests pass"** — see skips, the environment `ConnectError`s, and the un-run
  full suite.

---

## 19. PostgreSQL status

Unavailable (`EVAL_PG_TEST_URL` unset, `psql` not installed, Docker daemon unreachable). **Not
faked.** PG-specific tests retained as `skipif`-skips. **PostgreSQL verification of the
partial-unique dedupe index, ActionLog `SELECT … FOR UPDATE` serialization, dispatch idempotency,
concurrent consumers, AND the new active-proposal caps (which on PG need an advisory lock or a
`FOR UPDATE` anchor for strict cross-connection enforcement — the correction pass serializes via
the per-scope ActionLog chain lock, deterministic on SQLite `BEGIN IMMEDIATE`, best-effort
elsewhere) is a MANDATORY BLOCKER before hosted multi-user deployment.** Not a blocker for this
self-host-first review (SQLite fully verified; the `UNIQUE` + bounded-retry mechanisms are
engine-independent; the 8-thread cap concurrency test passes on file SQLite). Self-host SQLite
evidence is kept separate; database-engine equivalence is **not** claimed from shared SQLAlchemy
code alone.

---

## 20. Deferred to Phase 1B.3 (or a dedicated durable-run phase)

Durable LangGraph execution + task ledger; live `/messages/stream` resume-after-approval + a
turn/idempotency-token request contract; the regenerated-vs-transport-retry distinction on the
live path; frontend approval UI; terminal / sandboxed-Python / browser / desktop wiring; global
auto mode; mandatory-risk carve-out engine; full risk-policy engine; OpenRouter; paid-model
budgets; external ActionLog anchoring; a real authenticated-session expiry + revocation binding
(re-enables public session-grant creation); strict cross-worker/PG enforcement of the
active-proposal caps; T44.

**Done in the correction pass (no longer deferred):** `tool.execution.blocked` dispatcher event
(Decision 3); bounded active-proposal caps per turn / session / user + per-response MCP-call
bound (Decision 5).

---

## 21. Confirmations

- **Phase 1B.3 NOT started.** **Option B NOT started.**
- **Nothing committed, merged, or pushed.** No reset / stash / clean / discard / branch-ref
  modification. `origin/HEAD` untouched. All Phase 1B work is uncommitted on
  `phase-1b1-approval-domain`.
- **Phase 1B.1 domain behavior unchanged** except the reported straddling-DB `session_grants`
  migration defect (§14-F), now corrected in the 1B.2 migration (empty → verified rebuild;
  non-empty → fail-fast).
- **Correction pass (§0) applied in full:** all 5 decisions + new/updated tests + doc updates +
  regression re-run. No source-control operations performed.
- Not claiming Phase 1B.2 is approved — that is the user's gate.
