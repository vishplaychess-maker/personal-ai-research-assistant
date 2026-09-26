# Phase 1B.2 — Trusted Registry, Stable Execution Identity, Approval Gateway, Reference MCP Wiring

**Status: PLAN — implementation NOT started. Two Stage-0 stop conditions are CONFIRMED; user
decision required before coding (see §4).**

Branch: `phase-1b1-approval-domain` (all Phase 1B work stays here, uncommitted, until the whole
of Phase 1B is approved). Nothing committed / merged / pushed.

---

## 1. Stage 0 — repository & branch verification (evidence)

### 1.1 git state (read-only)

```
HEAD                     = b2e67183d4791cbbaed9d3d98e8833983510f3ff
main                     = b2e67183d4791cbbaed9d3d98e8833983510f3ff   (tracks origin/main, [ahead 2])
master                   = a92e3ebce4f1ea7956b3fde3398c25d69194d4ec   (tracks origin/master, [ahead 41])
origin/HEAD              -> refs/remotes/origin/master   (stale clone-time default)
merge-base(HEAD, main)   = b2e6718  == main tip          → HEAD..main = 0, main..HEAD = 0
merge-base(HEAD, master) = a92e3eb  == master tip        → HEAD..master = 0, master..HEAD = 50
CI push branches         = [main, master]
```

Current working tree: 8 modified tracked files + `backend/app/approval/` + `tests/test_approval_domain.py`
+ `evals/fixtures/T41-43.json` + the two `docs/superpowers/{plans,reports}` Phase 1B.1 files
(the uncommitted Phase 1B.1 diff — unchanged).

### 1.2 Canonical branch = **`main`** (documented resolution)

| Evidence | Verdict |
|---|---|
| The current feature branch's tip **is exactly `main`'s tip** (`b2e6718`); Phase 1B.1 is entirely uncommitted working-tree changes on top of `main`. `merge-base(HEAD,main)==main` and both ahead-counts are 0. | current branch is safely based on `main` |
| `main` tracks `origin/main`; its history is the live product line (Phase 1A close-out → media-generation merge → …). | `main` canonical |
| `master` = `a92e3eb`, `[origin/master: ahead 41]`; `master..HEAD` = **50** commits of product history ahead of it, `HEAD..master` = 0. `master` tip = "Hermes self-healing loop, MCP UI" — predates ~50 commits. | `master` is a divergent historical line |
| Phase 1A was fast-forward-merged into **`main`** at `b2e6718` (Phase 1A completion report + project memory). | `main` canonical |
| Project memory `project-eval-harness`: "`main` (tracks origin/main) is canonical. `master` is a stale divergent line — never merge/rebase/reset it." | `main` canonical |
| `origin/HEAD -> origin/master` | **stale local symbolic ref** — contradicted by every substantive signal; `origin/master` is itself `[ahead 41]` / divergent. Not authoritative. |

**Resolution:** `main` is canonical. `master` is historical/divergent. The earlier Phase 1B.1
report's stray "`master` untouched" line was imprecise (it correctly noted the divergent `master`
*branch ref* was not moved; the canonical branch is `main`). The feature branch **is safely based
on `main`** — no rebase / merge / switch needed or performed. `origin/HEAD`'s stale target is
noted, not "fixed" (that would be a ref write outside scope).

### 1.3 Phase 1B.1 unchanged & green (this session)

```
PYTHONPATH=backend .venv/…python -m pytest tests/test_approval_domain.py -q  → 166 passed, 2 skipped  EXIT 0
.venv/…python -m pytest tests/test_evals_runner.py -q                        → 68 passed              EXIT 0
PYTHONPATH=backend .venv/…python -m pytest tests/test_evals_inprocess.py -q  → 6 passed               EXIT 0
git diff --check                                                             → clean                  EXIT 0
```

Repo instruction files: **no root `AGENTS.md` or `CLAUDE.md`** (project instructions live in the
user-global `~/.claude/CLAUDE.md` + `RTK.md` and `THUNDER_AI_PRD.md`). Existing plan
`docs/superpowers/plans/2026-08-29-mcp-integration.md` covers the MCP layer.

---

## 2. Repository architecture evidence relevant to Phase 1B.2

| Concern | Evidence (path : line) | Implication |
|---|---|---|
| MCP dispatch | `app/tools/mcp_tool.py :: run_mcp_calls(calls, db, user_id)` — for each `(name, args)`: `tool_registry.resolve_mcp` → `mcp_service.call_tool(cfg, bare, args)` → formatted text block. **No approval, no identity, no idempotency.** Callers: `streaming_service.py:814`, `langgraph_workflow.py:1108,1366`. | The reference wire point. |
| Marker parse | `mcp_tool.extract_mcp_calls(text) -> list[tuple[str, dict]]` — line-scan regex, **max 3**, returns bare `(name, args)`. No call id, no index persisted. | No stable per-call identity exists. |
| Streaming path | `services/streaming_service.py :: stream_chat_response(context)` — a **hand-rolled async generator** (NOT a LangGraph graph). On `done`, it parses `[MCP_CALL]` from `full_response_text` and **immediately runs `run_mcp_calls` inline** with a fresh `SessionLocal()`, then a "Hermes" retry loop (≤3), all before the `complete` SSE event. | Single-pass, synchronous, fire-and-forget. |
| Assistant message persistence | `streaming_service.py :: _save_assistant_message` (line ~949) creates the `Message` row **AFTER** the whole generation + MCP calls. During streaming `message_id` is `None` ("filled in by the route after DB save"). `routes/messages.py:305` `/messages/stream` takes `ChatRequest{message, image_url}` — **no idempotency key / turn id / retry token**. "On cancellation, partial responses are NOT saved." | **No persisted turn/message id exists at MCP-call time.** A transport retry = a fresh POST that re-runs the LLM. |
| `Message` model | `models.py:132` — `id, session_id, role, content, image_url, citations, confidence, confidence_reason, created_at`. **No `turn_id`, no `tool_call_id`, no idempotency column, no generation id.** | Locked-decision-1 identities have nowhere to attach. |
| LangGraph | `langgraph_workflow.py:1741` `workflow.compile()` — **no `checkpointer=`**. `interrupt()` is used for terminal HITL but only within one in-process `run_research_workflow` call (non-streaming `/messages` path). `langgraph-checkpoint` is a dep but **no durable checkpointer is configured anywhere**. | **No durable, cross-request resume of an interrupted generation exists.** |
| Registry | `services/tool_registry.py` — `RegisteredTool(name, description, input_schema, source, server_id)`; `list_tools`, `resolve_mcp`. Reads `MCPServer` rows (`tools_json` discovered cache, `tool_allowlist_json`). **No version, no schema hash, no fingerprint, no locked/unlocked state, no discovery timestamp, no policy version.** | Needs the trusted-registry extension. |
| `MCPServer` model | `models.py:298` — `id, user_id, name, command, args_json, env_json, enabled, tool_allowlist_json, tools_json, last_discovered_at, last_error, created_at, updated_at`. `env_json` **can hold raw env incl. secrets**. | Fingerprint inputs must exclude `env_json` secret values; a `config_ref_id` seam is needed. |
| Config | `config.py` — `enable_mcp_tool: bool = False`, `enable_terminal_tool: bool = False`. No approval-gateway flag. | Add `approval_gateway_required` (default **true**). |
| Auth | `services/auth_service.py :: get_current_user`; CSRF via `require_csrf` dep (`routes/messages.py`). **No `tenant_id`** anywhere (whole app is `user_id`-scoped). | API ownership derivable from `get_current_user`; `tenant_id` stays nullable. |

---

## 3. CONFIRMED Stage-0 stop conditions

The task's **Stop conditions** section says to stop and ask before continuing if *"safe stable
turn/tool-call identity requires a materially larger redesign"* or *"the repository cannot safely
re-enter an approved logical call"*. Both are met:

### SC-1 — stable server-owned turn / tool-call identity requires a new persistence seam and a
### streaming-contract change (materially larger than a "smallest seam")

- There is **no persisted assistant-turn / generation record** at MCP-call time (the `Message`
  row is written only after generation completes; `message_id` is `None` throughout streaming).
- `/messages/stream` has **no idempotency key / turn token**. The architecture **cannot
  distinguish a transport retry from a regenerated response** — both are fresh POSTs that re-run
  the LLM and can produce reordered `[MCP_CALL]` markers.
- To give Locked Decision 1 a real home (server-generated `execution_turn_id` +
  server-generated `tool_call_id`, *stable across a legitimate transport retry*), Phase 1B.2
  would need: (a) a new `execution_turns` table + a new `tool_calls` table (or columns), (b) a
  turn record created **before/at the start of** generation, (c) a client-supplied
  idempotency/turn token on `/messages/stream` so a transport retry re-attaches to the same
  turn, (d) changes to `streaming_service.stream_chat_response` and `routes/messages.py`. This
  is a change to the **chat request contract and the streaming generator**, not an isolated
  additive seam.

### SC-2 — the repository cannot safely re-enter an approved logical MCP call

- The MCP call executes **inline inside the streaming async generator**. There is **no durable
  execution state, no LangGraph checkpointer, no task ledger**. On interruption the generation
  context is discarded.
- After an approval decision there is **no code path** that can resume "the same logical MCP
  call" — the generator that parsed it no longer exists. The client can only re-POST
  `/messages/stream`, which re-runs the LLM.
- The task forbids the only shortcuts that would paper over this: no public `/consume`, no
  execute-inside-approve-API, no "in-memory-only callback presented as durable".

The task's **"Architecture requirement — safe resume after approval"** anticipates exactly this
and directs: *"implement and test the reference domain/dispatcher boundary only; clearly record
that production UI resume remains blocked by the later durable-run phase; ask me if a larger
architecture change is required."*

---

## 4. Decision required from the user

**Option A — scoped domain/dispatcher boundary only (matches the task's own fallback).** Build,
in-process/DB/API-tested, WITHOUT wiring live streaming execution resume:

1. **Trusted registry** (`app/approval/registry.py` + a `trusted_tools` table): canonical
   `tool_id`, `tool_version`, `source_type`, `mcp_server_id`, `tenant_id?`, canonical
   `input_schema`, `tool_schema_hash`, `server_fingerprint`, `enabled`, `locked`,
   `discovered_at`, `policy_version`, a capability/risk-metadata JSON seam. Deterministic
   "materially changed" = any change to `(canonical schema, server_fingerprint inputs)`.
   Unknown/unregistered/new/changed → **locked, fail closed**. Cache is not an authority; a
   cache miss / registry outage / disabled or unavailable gateway → **execution blocked**.
2. **Execution identity** (`execution_turns`, `tool_calls` tables + minimal seam): server-owned
   `execution_turn_id` and `tool_call_id`, tenant/user/session-owned, created by the trusted
   server path, with documented lifecycle / retry / retention. Since live streaming cannot
   supply a stable transport-retry token yet, the seam is exercised by the **reference
   dispatcher + tests**, and "live UI resume" is explicitly recorded as blocked on the
   durable-run phase.
3. **`proposal_dedupe_key`** (`app/approval/dedupe.py` + `pending_approvals.proposal_dedupe_key`
   column, `UNIQUE(tenant_id, proposal_dedupe_key)` partial on active states): server-derived,
   RFC 8785-canonicalized over the ~20 bound fields incl. `execution_turn_id` + `tool_call_id`.
   `request_approval` reuses an existing active `PendingApproval` on a dedupe hit (reusing the
   original envelope + `action_nonce`); CAS + bounded retry; denied/expired/consumed rows are
   not active. Never accepted from the client.
4. **Gateway service** (`app/approval/gateway.py`): the 16-step pure-ish function
   `gateway.dispatch(ctx, *, mcp_name, args, execute_mcp)` that authenticates the *server-side*
   caller context, resolves via the registry, builds the `ActionEnvelope` server-side, does
   `request_approval` (dedupe) → returns `approval_required` on first pass (**no dispatch**);
   on trusted re-entry reconstructs + re-hashes the persisted envelope with the *original*
   nonce, atomically `consume_approval` **or** `dispatch_with_session_grant`, writes
   `tool.execution.started` → invokes `execute_mcp` → writes `succeeded`/`failed`/`blocked` +
   a per-call dispatch-idempotency record (at-most-once local; unknown-outcome-after-crash is
   recorded as `incomplete`, not auto-retried).
5. **Reference wire** (`app/tools/mcp_tool.py`): a gated variant so that when
   `approval_gateway_required` is on and `enable_mcp_tool` is on, `run_mcp_calls` **returns a
   structured `approval_required` block and does NOT auto-execute** (the safe default). The
   live streaming path shows "approval required"; the tool actually runs only through the
   trusted re-entry dispatcher — **not reachable from the live UI until the durable-run phase**
   (documented).
6. **Approval API** (`app/routes/approvals.py`): list / inspect / approve / deny **owned**
   pending approvals; explicit separate session-grant creation; list/revoke own grants. **No
   `/consume`.** Ownership from `get_current_user`; CSRF; bounded/paginated; redacted
   responses; idempotent duplicate decisions; deterministic conflict on contradictory
   decisions; ActionLog events. Approve/deny **never executes**.
7. **Migrations** (`main.py :: _migrate_approval_domain_1b2()`): additive, idempotent, SQLite+PG,
   `CREATE TABLE/INDEX IF NOT EXISTS`, tenant/user/session ownership + indexes + uniqueness +
   retention notes for every new table/column.
8. **T45** → real `product_inprocess` target (registry + gateway boundary; no live streaming):
   unregistered tool locked, `execution_attempted=False`, no dispatcher callback, cross-schema/
   fingerprint approval does not authorize, only the exact registered version executes after
   proper approval. `blocked_by` removed only when this genuinely passes.
9. Full test matrix (identity/dedupe, registry, gateway, execution lifecycle, API, multi-call,
   concurrency), the adversarial pass, and `docs/superpowers/reports/phase-1b2-completion.md`.

**Explicitly NOT in Option A:** durable turn persistence wired into `/messages/stream`; a
streaming-resume mechanism; any change to the streaming request contract; live end-to-end "user
approves in UI → the original generation resumes and the tool runs". That is a **durable-run
phase** (1B.3 or a dedicated 1B.2.x).

**Option B — also do the durable turn + streaming-resume redesign now.** Materially larger:
new durable turn/tool-call ledger, `/messages/stream` idempotency-token contract, a
resume-after-approval mechanism in `streaming_service.py`, and the regenerated-vs-retry
distinction. Arguably its own phase.

**Recommendation:** Option A. It is exactly the task's stated fallback for this situation,
delivers the full trusted domain + gateway + API + reference-boundary + tests + adversarial
review, keeps the change reviewable, and does not smuggle in a redesign under a "seam" label.
Live UI resume is honestly recorded as blocked on the durable-run phase.

---

## 5. If Option A is chosen — file plan (for reference; not yet built)

| File | New/mod | Responsibility |
|---|---|---|
| `backend/app/approval/registry.py` | new | Trusted registry: derive/store canonical tool identity + schema hash + fingerprint + locked state; `resolve_trusted(db, ctx, mcp_name)`; fail-closed. |
| `backend/app/approval/dedupe.py` | new | `proposal_dedupe_key(...)` (RFC 8785 + existing hashing); field set incl. `execution_turn_id` + `tool_call_id`. |
| `backend/app/approval/identity.py` | new | `execution_turn_id` / `tool_call_id` value objects + `open_turn(...)` / `open_tool_call(...)` persistence seam; lifecycle documented. |
| `backend/app/approval/gateway.py` | new | The centralized 16-step `dispatch(...)`; `tool.execution.*` events; dispatch-idempotency record. |
| `backend/app/approval/service.py` | mod | `request_approval` gains `proposal_dedupe_key` reuse (CAS + bounded retry). `dispatch_with_session_grant` reused. **No 1B.1 behavior change** beyond the additive dedupe path. |
| `backend/app/models/models.py` | mod | +`TrustedTool`, `ExecutionTurn`, `ToolCall`, `DispatchRecord`; +`pending_approvals.proposal_dedupe_key`, +`execution_turn_id`, +`tool_call_id`. tz-aware; nullable `tenant_id`; indexes + uniqueness. |
| `backend/app/main.py` | mod | `_migrate_approval_domain_1b2()` — additive/idempotent; called in `_migrate_database()` pre-guard block. |
| `backend/app/config.py` | mod | `approval_gateway_required: bool = True`; `approval_gateway_available()` check hook. |
| `backend/app/tools/mcp_tool.py` | mod | gated `run_mcp_calls` — returns `approval_required`, never auto-executes when the gateway is on. |
| `backend/app/routes/approvals.py` | new | list/inspect/approve/deny/create-grant/list-revoke-grants. No `/consume`. |
| `backend/app/main.py` (router include) | mod | register `approvals` router. |
| `tests/test_approval_registry.py`, `tests/test_approval_gateway.py`, `tests/test_approval_identity_dedupe.py`, `tests/test_approvals_api.py` | new | The Phase 1B.2 test matrix. |
| `evals/adapters/inprocess.py`, `evals/dataset.jsonl`, `evals/fixtures/T45.json` | mod/new | T45 real target. |
| `.github/workflows/evals.yml` | mod | run the new suites in Gate B. |
| `docs/superpowers/reports/phase-1b2-completion.md` | new | completion report. |

---

## 6. Deferred to Phase 1B.3 (or a dedicated durable-run phase)

Durable LangGraph execution + task ledger; live `/messages/stream` resume-after-approval; the
regenerated-vs-transport-retry distinction on the live path; frontend approval UI; terminal /
sandboxed-Python / browser / desktop wiring; global auto mode; mandatory-risk carve-out engine;
full risk-policy engine; OpenRouter; paid budgets; external ActionLog anchoring; T44.

---

## 7. Stop point

**Implementation is not started.** Awaiting the user's choice of **Option A** (recommended) or
**Option B**, plus any correction to the §1.2 branch resolution. No further Phase 1B.2 code will
be written until that decision.

---

## User decision — 2026-09-08

- **Option A APPROVED** (scoped domain/dispatcher boundary only). Option B (durable
  streaming/resume redesign) **deferred** to a dedicated architecture phase.
- Stage-0 **canonical branch resolution accepted**: `main` canonical, `master` stale/divergent,
  feature branch `phase-1b1-approval-domain` safely based on local `main`. Do not modify
  `origin/HEAD` or any branch refs.
- Live streaming one-time approval **resume remains unavailable**; `run_mcp_calls` never
  auto-executes — it returns a structured blocked / approval-required result and creates no
  proposals on the live path. Durable execution/resume must be a dedicated immediately-following
  architecture phase before Thunder AI can be called a durable autonomous work manager.
- Not committed / merged / pushed. Phase 1B.3 not started.

## Implemented (Option A) — see `docs/superpowers/reports/phase-1b2-completion.md`

`backend/app/approval/` +5 modules (`registry.py`, `identity.py`, `dedupe.py`, `gateway.py`,
service additions), `backend/app/routes/approvals.py`, `backend/app/models/models.py` +4 tables
(`trusted_tools`, `execution_turns`, `tool_calls`, `dispatch_records`) +4 `pending_approvals`
columns (`proposal_dedupe_key`, `execution_turn_id`, `tool_call_id`, `trusted_envelope_json`),
`backend/app/main.py :: _migrate_approval_domain_1b2()` (additive/idempotent; also fixes a
straddling-DB `session_grants` schema defect — see report), `backend/app/config.py`
(`approval_gateway_required` default True, `approval_gateway_available`),
`backend/app/tools/mcp_tool.py` (gated `run_mcp_calls` + trusted `dispatch_mcp_call_via_gateway`),
`tests/test_approval_gateway.py` (24), `tests/test_approvals_api.py` (13), `T45` real
`product_inprocess` target + fixture, CI.

---

## Final Security Correction Pass — 2026-09-08

Applied before approval. Five decisions; scope unchanged (Option A, self-host-first). Nothing
committed / merged / pushed; Phase 1B.3 not started.

### Decision 1 — `session_grants` migration reconciliation
`_migrate_approval_domain_1b2()` now handles a straddling DB (obsolete `allowed_action_hash`
column) as: **EMPTY** → guarded `DROP` + recreate with the current DDL + `_verify_session_grants_schema()`
(required columns, `grant_nonce` uniqueness, key indexes, obsolete column absent — raises on any
gap); **NON-EMPTY** → `RuntimeError` that **halts startup** with an actionable message. Those
pre-close-out rows are never transformed or dropped: `allowed_action_hash` (a one-time action
hash) has no proven equivalence to `grant_scope_hash` (a reusable scope template). Fail-fast is
preferred over guessing (user-confirmed). Tests: `tests/test_approval_migrations.py` (clean DB;
1B.1 upgrade; ×3 idempotency; empty-legacy rebuild+verify; non-empty-legacy fail-fast + no data
loss + deterministic re-raise; post-migration ORM compatibility).

### Decision 2 — public session-grant creation deferred
`POST /api/approvals/{id}/grant` **removed** (new this phase, no compatibility requirement — the
route is omitted entirely, not stubbed). No client-selected TTL exists anywhere. `list` /
`revoke` of owned grants stay. The trusted domain function `create_session_grant(...)` (requires
an explicit server-controlled `session_expires_at`) and its tests stay — that is the only path
that mints a grant. A one-time approval never becomes a reusable grant. Re-enable only after a
real authenticated-session expiry + revocation binding exists and is tested. Tests updated in
`tests/test_approvals_api.py` (no `/grant` route; unauth can't; auth' can't; client TTL
impossible; approve creates no grant; no disabled path writes a row; list/revoke owner-scoped;
grant-list page size bounded).

### Decision 3 — `tool.execution.blocked` emitted now
`app/approval/gateway.py`: every pre-invocation refusal calls `_block(...)` → returns a blocked
`GatewayResult` **and** appends a sanitized `tool.execution.blocked` ActionLog event
(`execution_attempted=false`, trusted correlation fields only — never raw args / creds / headers
/ launch config / unrestricted error text). Stable closed reason vocabulary `BLOCK_REASONS`
(19 codes: `gateway_unavailable`, `registry_unavailable`, `unknown_tool`, `tool_disabled`,
`tool_locked`, `schema_mismatch`, `fingerprint_mismatch`, `tenant_mismatch`, `user_mismatch`,
`session_mismatch`, `approval_denied`, `approval_expired`, `approval_consumed`,
`envelope_mismatch`, `active_proposal_limit`, `duplicate_dispatch`, `dispatch_incomplete`,
`action_log_failure`, `database_failure`) — a value outside the set can never be emitted
(attacker strings can't become a reason code). Registry gains `resolve_reason()` for the precise
`unknown_tool` / `tool_locked` / `tool_disabled` / `schema_mismatch` / `fingerprint_mismatch` /
`tenant_mismatch` split; `register_or_refresh` splits the lock reason into "tool schema changed"
/ "server fingerprint changed" / both. The `action_log_failure` path does **not** recurse
through the failed ActionLog (`emit_event=False`). Callback is never invoked on a blocked path.
Unauthenticated HTTP never reaches the gateway (the live `run_mcp_calls` returns text and never
calls it), so it stays in normal access logs, not the tenant ActionLog.

### Decision 4 — call_index wording + enforcement
Wording corrected everywhere from "never ordinal" to **"not derived from, or authorized by, an
ordinal position alone."** `call_index` is a server-assigned allocation slot; `tool_call_id`
(the row UUID) is the identity. `open_tool_call` already refuses to rebind a slot to a different
`(tool_id, args_hash)` — tests added: same slot + different tool fails closed; reordered /
regenerated markers cannot cross-authorize; raw/attacker index never returns another call's id;
different turns never collide. Transport-retry stability is **not** claimed on the live streaming
path (no durable turn identity there — deferred).

### Decision 5 — bounded active proposals + per-response bounds
New validated config: `max_active_proposals_per_turn` (3), `_per_session` (10), `_per_user` (20),
`max_mcp_calls_per_response` (3); a `Settings` model-validator rejects any `< 1`. The gateway's
`_enforce_active_proposal_limits(...)` counts **active** (`requested`/`approved`) proposals for
the attempt's turn / session / user, **excluding a retry of the same `tool_call_id`**, under the
per-scope ActionLog chain lock (SQLite `BEGIN IMMEDIATE` after a `rollback()`; PG
`SELECT … FOR UPDATE` on the scope head — shared multi-worker enforcement still gated on the
PostgreSQL verification). Over-limit → `approval.limit_exceeded` audit signal (scope + counts,
no args) **and** a `tool.execution.blocked` with `reason_code=active_proposal_limit`; the
callback is never invoked. The client cannot pass or raise a limit (no param on
`request_approval`, no field on `GatewayContext`). `extract_mcp_calls` now takes its cap from
`settings.max_mcp_calls_per_response`. Approval-list and session-grant-list page sizes are
already bounded (`limit` 1–100). Tests: per-turn / per-session / per-user boundary, retry-of-
pending not blocked, 8-thread concurrency (cap holds), client-cannot-raise, config-driven parse
cap.
