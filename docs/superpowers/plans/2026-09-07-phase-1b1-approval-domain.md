# Phase 1B.1 — Approval Domain Model (implementation plan)

**Scope:** the approval **domain** only — canonical action envelope, `args_hash` +
`action_hash`, stable server/tool fingerprints, `PendingApproval` persistence, an atomic
approval state machine, session-scoped grants, a tamper-evident `ActionLog` hash chain, additive
migrations, hermetic tests, and real in-process eval targets for T41–T43.

**Explicitly OUT of scope (do not wire):** MCP, Python sandbox, terminal, browser,
`streaming_service.py`, `langgraph_workflow.py`, frontend, approval API routes, global auto mode,
carve-out enforcement, OpenRouter, paid calls, durable task manager, Phase 1B.2+.

**Not committed / not merged.** Branch `phase-1b1-approval-domain` off `main` (`b2e6718`).

---

## 1. Repository evidence (inspected & cited)

| Concern | Evidence | Decision for 1B.1 |
|---|---|---|
| Project instructions | **No `AGENTS.md`.** `THUNDER_AI_PRD.md` (root) + `~/.claude/CLAUDE.md`. PRD rules: no hallucinated APIs, verify before writing, ask on doubt, smallest thing that works (`ponytail`). | Followed. |
| SQLAlchemy models | `backend/app/models/models.py` — **all models in one file**. `Base` from `app.database` (`DeclarativeBase`). Integer autoinc PKs w/ `index=True`; **string PKs for opaque external ids** (`MediaVideoTask.task_id`, `AppSetting.key`). Newest models skip `relationship()` back-populates, just FK columns. Enums stored as **plain `String` + `.value` default** (`Document.status`, `Memory.category`) — `SAEnum` only in older `Message.role`. `ForeignKey("users.id", ondelete="CASCADE")` in newest model. `__table_args__ = (UniqueConstraint(...),)` pattern (`UserSkill`, `GraphEntity`). | 3 new models **in `models.py`**: `PendingApproval`, `SessionGrant`, `ActionLogEntry`. String UUID PKs. State/type columns = `String`. `__table_args__` for uniqueness + lookup indexes. |
| SQLite/PG compatibility | `backend/app/database.py` — one engine, dialect-branched `connect_args`; `_is_postgres()`. `_migrate_database()` runs **dialect-agnostic `CREATE TABLE IF NOT EXISTS`** helpers first, then `if engine.dialect.name != "sqlite": return` before the historical SQLite `ALTER` block. | New migration helper is **dialect-agnostic `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`**, mirroring `_migrate_user_skills()` / `_migrate_media_video_tasks()`. No `ALTER`. |
| Startup / table creation | `backend/app/main.py :: lifespan` → `_migrate_knowledge_graph()` → `init_db()` (`Base.metadata.create_all`) → `_migrate_database()` → `_create_default_user()`. `_migrate_database()` calls `_migrate_provider_encryption()`, `_migrate_user_skills()`, `_migrate_media_video_tasks()` **before** the sqlite guard. | Add `_migrate_approval_domain()` and call it in that same pre-guard block. Fresh installs also covered by `create_all()` once the models are imported (routes import `models`). |
| Config conventions | `backend/app/config.py` — pydantic-settings `Settings` singleton `settings`; feature flags `enable_*: bool = False`, timeouts `*_timeout_s: int`. `PRODUCTION_MODE`, `DATABASE_URL`, `JWT_SECRET`, `ENCRYPTION_KEY`. | Add `approval_default_ttl_s: int = 300`, `approval_purpose_max_chars: int = 280`, `approval_action_envelope_version: str = "1"`, `approval_policy_version: str = "1b1.1"`. No behaviour flag (domain is inert until 1B.2 wires it). |
| Auth / tenant identifiers | `app/services/auth_service.py :: get_current_user`. **No `tenant_id` / `org_id` / `workspace_id` anywhere** — the entire app is `user_id`-scoped (FK → `users.id`); KG models are `UniqueConstraint("user_id","name")`. | **`tenant_id` = `String NULL`, no FK, no tenants table** (there is none). Every table also carries `user_id INTEGER` (no FK constraint, to keep the domain unit-testable without the full `users` table; matches how test fixtures build isolated schemas). ActionLog scope for a NULL tenant = `user:<user_id>` or `system` (spec §8 fallback). |
| Session / run models | `ResearchSession` (`id`, `user_id`, `title`, `model`, `system_prompt`). **No "run" model** — runs are transient LangGraph state (`langgraph_workflow.py :: State`: `pending_command`, `command_approved`, `browser_pending_approval`). | `session_id INTEGER NULL`, `run_id String NULL` (nullable — no run entity yet; spec §3 "run ID when available"). |
| Current terminal/browser approval | `app/tools/terminal_executor.py` — `[PROPOSED_COMMAND: …]` regex → LangGraph `interrupt()` → `run_command` only after approval. `browser_agent.py` — `classify_risky_action()` → `[BROWSER_APPROVAL_REQUIRED: …]`. **Both are transient in-graph state — no persistent record, no hash binding, no replay/cross-user protection.** | The new domain is the persistent, hash-bound layer these will call in 1B.2. **Not wired here.** |
| Hashing / encryption utils | `hashlib.sha256(...).hexdigest()` used across `cache_service`, `semantic_cache`, `refresh_token_service` (stores `token_hash`, never raw). `app/services/encryption.py` — Fernet AES-at-rest, `FERNET_PREFIX="gAAAAA"`, key from `ENCRYPTION_KEY`. Domain-separated hashing precedent: `sha256(session_id + NUL + question)`. | `hashlib.sha256` hex, **length-prefixed domain separation** (stronger than bare NUL). **No encryption** of approval rows — spec §6 "do not assume approval storage is a secure secret vault"; store hashes + bounded redacted summaries only. |
| Test DB config | `tests/` at repo root (no `backend/tests/`). Pattern: `create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)` + `Base.metadata.create_all(engine)` + `sessionmaker(bind=engine)()`. Run with `PYTHONPATH=backend`. `tests/conftest.py` only warms Ollama. | `tests/test_approval_domain.py` uses that exact fixture pattern. PG-specific tests `skipif` unless `EVAL_PG_TEST_URL` is set. |
| Canonicalization dep | `backend/requirements.txt` — **no JCS library**; `rfc8785` / `canonicaljson` / `jcs` not importable. Adding a dep is discouraged (spec §2). Python `json.dumps(s, ensure_ascii=False)` string escaping **verified RFC 8785-compliant** (minimal escapes, lowercase `\uXXXX` for C0 controls, raw non-ASCII). `sorted()` sorts by code point; **RFC 8785 needs UTF-16 code-unit order** (verified to diverge for astral chars). | **Pure-stdlib RFC 8785-subset canonicalizer** (`backend/app/approval/canonical.py`), input domain **narrowed to `dict|list|str|bool|int|None`** (floats rejected) so number formatting is unambiguous and matches JCS for integers. Object keys sorted by `key.encode("utf-16-be")`. Documented + cross-platform test vectors. |

### Documented decisions that differ slightly from repo convention (not material conflicts)

1. **Timezone-aware timestamps.** Existing models use naive `datetime.datetime.utcnow`. The new
   tables use `DateTime(timezone=True)` + an **injectable clock** (`approval/clock.py`), per spec
   §5/§6. Isolated to the 3 new tables; nothing existing changes.
2. **`tenant_id` with no `tenants` table / no FK.** There is no tenancy in the repo yet; the column
   is nullable and unconstrained so the domain is ready for 1B.x multi-tenant without inventing a
   table now (spec §6 "nullable tenant ownership from the beginning").
3. **`user_id` carried as a plain `Integer` (no FK).** Keeps the domain independently unit-testable
   on a schema that doesn't include `users`; the real FK can be added by a later additive migration
   when the call sites are wired. Ownership is still enforced in code on every read/consume.

If the reviewer considers (1)–(3) material, stop here — otherwise they are implemented as above.

---

## 2. Files

| File | Responsibility |
|---|---|
| `backend/app/approval/__init__.py` | Public surface re-exports. |
| `backend/app/approval/errors.py` | `ApprovalError` + subclasses (`CanonicalizationError`, `EnvelopeMismatch`, `InvalidTransition`, `ApprovalExpired`, `ApprovalAlreadyConsumed`, `GrantScopeError`, `ChainIntegrityError`). |
| `backend/app/approval/clock.py` | `Clock` protocol, `SystemClock`, `FixedClock`; module default `now()` (tz-aware UTC). |
| `backend/app/approval/canonical.py` | `canonicalize(value) -> bytes` (RFC 8785 subset), `CanonicalizationError`. Rejects float/NaN/Inf/bytes/set/objects. |
| `backend/app/approval/hashing.py` | `GENESIS`, `sha256_hex`, `args_hash`, `tool_schema_hash`, `server_fingerprint`, `action_hash`, `row_hash`. Length-prefixed domain separation + a version tag in every pre-image. |
| `backend/app/approval/envelope.py` | `ActionEnvelope` (frozen dataclass), `build_envelope(...)`, field validation, `redact_purpose()` (length-limit + strip control chars; never authorization evidence). |
| `backend/app/models/models.py` (edit) | `PendingApproval`, `SessionGrant`, `ActionLogEntry`. |
| `backend/app/approval/state_machine.py` | `STATES`, `TERMINAL`, `ALLOWED` transition set; `transition(db, approval_id, frm, to, *, clock, expected_action_hash=None)` — atomic conditional `UPDATE … WHERE id=? AND state=?` returning rowcount; helpers `approve/deny/expire/consume/revoke`. |
| `backend/app/approval/action_log.py` | `append_event(db, *, scope_id, event_type, …)` behind `_ChainLock` (SQLite `BEGIN IMMEDIATE`; PG `SELECT … FOR UPDATE` on a chain-head row) **plus** `UNIQUE(chain_scope_id, seq)` as the engine-independent backstop; `verify_chain(db, scope_id) -> ChainReport`. |
| `backend/app/approval/service.py` | `request_approval`, `record_decision`, `consume_approval`, `create_session_grant`, `resolve_session_grant`, `revoke_session_grant`. Every path re-canonicalizes the resubmitted envelope and compares `action_hash`; writes an `ActionLogEntry`. |
| `backend/app/main.py` (edit) | `_migrate_approval_domain()` + call in `_migrate_database()` pre-guard block. |
| `tests/test_approval_domain.py` | ~45 hermetic cases (spec §10). |
| `evals/adapters/inprocess.py` (edit) | `_t41`, `_t42`, `_t43` targets → `_TARGETS`. |
| `evals/dataset.jsonl` (edit) | T41–T43: drop `blocked_by`, add `asserts`. T44/T45 unchanged. |
| `evals/fixtures/T41.json`,`T42.json`,`T43.json` (new) | `synthetic_regression` offline fixtures so Gate A stays exit 0. |
| `evals/README.md`, closure table (edit) | baseline 25 blocked / 16 verified. |
| `tests/test_evals_runner.py`, `tests/test_evals_inprocess.py` (edit) | count assertions 28→25 blocked, synthetic 15→18, verified floor 12→16. |

---

## 3. `action_hash` pre-image (canonical envelope)

`action_hash = sha256_hex( "thunder.approval.action_hash\x1f" + version_tag + canonicalize(envelope_dict) )`

`envelope_dict` keys (all present; unknown/absent → explicit `null`), server-trusted only:

```
action_envelope_version   tool_id            tool_version
source_type               mcp_server_id      server_fingerprint
tool_schema_hash          args_hash          affected_resources (sorted list)
network_scope             filesystem_scope   credential_scope
risk_level                risk_scope_flags (sorted list)
tenant_id                 user_id            session_id
run_id                    action_nonce       policy_version
```

- `args_hash = sha256_hex("thunder.approval.args_hash\x1f" + canonicalize(args))`.
- `risk_level` / `risk_scope_flags` / `*_scope` are **set by trusted server policy** (`service.py`),
  never read from model output / tool output / browser content / client text. In 1B.1 policy is a
  minimal deterministic classifier (`_classify(tool_id, affected_resources)`); 1B.3 replaces it.
- `purpose` is **not** in the pre-image. Stored redacted + length-limited, display only.
- Consumption (`consume_approval`) recomputes `action_hash` from the **resubmitted** envelope and
  compares; `args_hash` alone is never sufficient.

## 4. Server / tool fingerprint

`server_fingerprint = sha256_hex("thunder.approval.server_fingerprint\x1f" + canonicalize({...}))`

**Included (stable, non-secret):** `source_type`, normalized `server_manifest_id` (e.g. MCP server
`name`), `executable_identity` (command basename + declared version), normalized non-secret launch
config (args with any value that matches a secret pattern replaced by `"<redacted>"`), discovered
`tool_name`, `tool_schema_hash`, `config_ref_id` (e.g. `mcp_servers.id` or a credential-reference
id — **never the credential value**).

**Excluded:** raw credentials, secret env-var *values*, tokens, absolute paths outside the
manifest, PIDs, ports, timestamps, any per-launch volatile data.

**Invalidation:** a changed `tool_schema_hash` or changed `server_fingerprint` ⇒ `action_hash`
changes ⇒ prior approval/grant no longer matches. Documented in `envelope.py` docstring.

## 5. State machine

States: `requested → approved | denied | expired`; `approved → consumed | expired`;
`approved → revoked` (for grants’ underlying approval, optional). Terminal: `denied`, `expired`,
`consumed`, `revoked`.

Every transition = `UPDATE pending_approvals SET state=:to, <ts col>=:now WHERE id=:id AND
state=:frm` (+ `AND action_hash=:h` on consume). `rowcount == 1` ⇒ won; `0` ⇒ lost the race /
wrong precondition → raise typed error. Expiry is **server-decided**: `resolve()` / `consume()`
first run `expire_if_due(now)` (same CAS, `requested|approved → expired` when `expires_at <= now`).
Injectable `clock`. Concurrency: `_run_concurrent()` test helper (threads + `StaticPool`) proves
exactly one approve wins and exactly one consume wins.

## 6. `PendingApproval` columns

`id` (uuid str PK) · `action_hash` · `args_hash` · `state` · `action_envelope_version` ·
`tool_id` · `server_fingerprint` · `tool_schema_hash` · `tenant_id?` · `user_id` · `session_id?` ·
`run_id?` · `action_nonce` (server CSPRNG, `UNIQUE`) · `policy_version` · `risk_level` ·
`display_summary` (bounded, recursively redacted JSON text) · `purpose_redacted` (≤ N chars) ·
`created_at` · `decided_at?` · `consumed_at?` · `expires_at`.
Indexes: `UNIQUE(action_nonce)`, `ix(user_id, state)`, `ix(tenant_id, state)`, `ix(session_id)`.
**Never stored:** raw args, API keys, passwords, auth headers, cookies, secret env values.

## 7. `SessionGrant` columns (narrow, revocable, ≤ session lifetime)

`id` PK · `tenant_id?` · `user_id` · `session_id` · `tool_id` · `server_fingerprint` ·
`tool_schema_hash` · `allowed_action_hash` (exact) · `policy_version` · `creating_approval_id` ·
`created_at` · `expires_at` · `revoked_at?`.
`resolve_session_grant(...)` matches **all** of tenant+user+session+tool_id+server_fingerprint+
tool_schema_hash+action_hash+policy_version and `revoked_at IS NULL` and `expires_at > now`.
Any mismatch ⇒ no grant. No wildcard tool/server/resource, no permanent grant.

## 8. Tamper-evident `ActionLogEntry`

`chain_scope_id` (`tenant:<t>` | `user:<u>` | `system`) · `seq` (int, per scope, from 1) ·
`event_type` · `approval_id?` · `grant_id?` · `action_hash?` · `event_metadata` (sanitized JSON) ·
`prev_row_hash` · `row_hash` · `created_at`. `UNIQUE(chain_scope_id, seq)`.

`GENESIS = "0" * 64`.
`row_hash = sha256_hex("thunder.approval.row_hash\x1f" + prev_row_hash + "\x1f" + canonicalize(
  {seq, chain_scope_id, event_type, approval_id, grant_id, action_hash, event_metadata, created_at}))`.

**Append:** `BEGIN IMMEDIATE` (SQLite) / `SELECT … FOR UPDATE` on a `chain_heads` row (PG) to
serialize; `UNIQUE(scope, seq)` guarantees no fork even if locking is imperfect (loser retries or
raises). Locking is behind `_ChainLock` so the storage-specific bit is swappable.
`verify_chain(scope)` recomputes every `row_hash`, checks linkage, `seq` contiguity from 1, and
`UNIQUE`; returns `ChainReport(ok, break_at, detail)`. **No public update/delete** method exists
for `ActionLogEntry` — service code only ever appends.

## 9. Migrations & rollback

`_migrate_approval_domain()` — 3 × `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` +
a `chain_heads` helper table. Additive, idempotent, SQLite + PG. Existing startup path unchanged.
Code rollback ⇒ tables remain, inert, audit data retained. **No destructive schema rollback in
this phase**; "rollback" is never "drop the approval/action-log tables".

## 10. Tests — `tests/test_approval_domain.py`

canonical vectors (+ cross-platform digest constants) · equal JSON ⇒ equal hash · array-order
changes hash · unsupported values rejected (float/NaN/Inf/bytes/set/object) · `args` mismatch ·
full-envelope mismatch · wrong tenant/user/session/run/tool_id/server_fingerprint/schema_hash/
policy_version · wrong or reused nonce · expired · denied · already-consumed · concurrent
decisions (exactly one) · concurrent consumers (exactly one) · grant expiry · grant revocation ·
grant cross-user / cross-tenant rejection · chain verify OK · chain detects modify / delete /
reorder / bad link · independent scopes don't interfere · concurrent linear appends stay linear ·
sensitive-field redaction in `display_summary` / `purpose_redacted` · bounded display length ·
SQLite path · PG path (`skipif` no `EVAL_PG_TEST_URL`).

## 11. Evaluations

- `evals/adapters/inprocess.py`: `_t41` (approve args A → consume with args B and with a mutated
  envelope field → both rejected, `execution_attempted=False`), `_t42` (consume after
  consumed / denied / expired / replayed nonce → rejected), `_t43` (approval for (tenantX,userX)
  presented as (tenantX,userY) and (tenantY,userX) → rejected). Each runs **real
  `backend/app/approval` code** on an in-memory SQLite session; the "execute" boundary is a mock
  that records `execution_attempted`.
- `evals/dataset.jsonl`: T41–T43 lose `blocked_by`, gain `asserts` satisfied by both the synthetic
  fixture and the real target. T44/T45 stay blocked (`1B` → keep; their `product_target` already
  says 1B.2 / 1B.3).
- New `evals/fixtures/T41-43.json` (`synthetic_regression`) so `--offline` stays exit 0.
- Expected new baseline: **Gate A** 18 synthetic / 2 placeholder / 2 unverified / **25 blocked**;
  **Gate B** **16** verified product passes / 4 unverified / 25 blocked. T27, T29–T32 stay verified.

## 12. Completion

Report per spec §13, propose the Phase 1B.2 prompt, **do not commit or merge**, stop.

---

## Security Correction Pass — 2026-09-07 (pre-commit)

Phase 1B.1 was conditionally accepted with 5 blocking corrections. Applied:

### 1. Full RFC 8785 number domain
`canonical.py` no longer rejects floats. It now delegates to **`rfc8785==0.1.4`**
(Trail of Bits, Apache-2.0, pure-Python, **zero runtime deps**, `Requires-Python>=3.8`)
— added to `backend/requirements.txt`. This is the security-community reference
implementation; chosen over hand-rolled ECMAScript double-to-string. Contract:
finite IEEE-754 doubles in ES6 `Number.toString` form; `NaN`/`±Inf` rejected;
integers only in the I-JSON safe range `±(2^53−1)` (larger → reject, pass as
string); `Decimal`/bytes/set/objects/non-str keys/lone surrogates rejected;
**no Unicode normalization**; UTF-16 key order; deterministic cross-platform
bytes. Tests in `tests/test_approval_domain.py`: 24 pinned RFC 8785 number
vectors; **ordinary finite fractional JSON numbers as Python `float` ARE
supported** (`test_finite_fractional_float_arguments_are_supported`); **Python
`decimal.Decimal` is REJECTED** unless the caller converts to `float` first
(`test_python_decimal_is_rejected_not_supported`); NFD≠NFC; integer/float
equivalence; unsafe-int / lone-surrogate / non-str-key / unsupported-object
rejection.

### 2. `grant_scope_hash` — reusable session-grant identity, separate from one-time `action_hash`
- `action_hash` (unchanged): the **one-time** instance — includes `action_nonce`
  and `run_id`, so two equivalent attempts differ. Consumed once against a
  `PendingApproval`.
- **`grant_scope_hash`** (new, `hashing.grant_scope_hash` + `GRANT_SCOPE_HASH_FIELDS`,
  `GRANT_SCOPE_VERSION="1"`): the **stable** authorization template — `grant_scope_version`,
  tenant, user, session, tool id+version, source type, MCP server id, server
  fingerprint, tool-schema hash, `args_hash`, affected resources, network/fs/
  credential scope, server-derived risk, policy version. **Excludes** `action_nonce`,
  `run_id`, approval id, timestamps, `purpose`.
- `SessionGrant`: `allowed_action_hash` → **`grant_scope_hash`**; added
  `grant_scope_version` and a CSPRNG **`grant_nonce`** (`UNIQUE`). Migration DDL
  updated (still additive `CREATE TABLE IF NOT EXISTS`; 1B.1 uncommitted).
- `create_session_grant(..., *, envelope, explicit_grant_decision: bool)` — **requires
  `explicit_grant_decision=True`** (a one-time approval never silently becomes a
  grant) and verifies `envelope.action_hash() == approval.action_hash` before
  deriving the scope. Grant `expires_at` capped at the session end.
- `resolve_session_grant` matches on `grant_scope_hash` (fresh nonce / run_id
  still match; any stable-field change does not). **Pure read — writes nothing.**
- **`dispatch_with_session_grant(..., execute)`** — the server-side
  authorization-to-dispatch handoff for one attempt. Resolves the grant; if
  none → raises `GrantScopeError` and `execute` is never called and no event is
  written; if a grant matches → writes exactly one **`grant.authorization_used`**
  ActionLog event (constant `GRANT_AUTHORIZATION_USED_EVENT`) BEFORE calling
  `execute`, referencing the grant id + this attempt's fresh `action_hash` /
  `action_nonce`, with `event_metadata.meaning` stating it is *authorization
  exercised, NOT an execution-result event*. Then calls `execute()`.

### 3. Non-null tenant isolation
Added `test_consume_tenantA_approval_presented_by_tenantB`,
`test_grant_tenantA_scope_presented_by_tenantB`,
`test_same_numeric_user_id_under_two_tenants_are_isolated`,
`test_selfhosted_null_tenant_vs_hosted_tenant_isolated`,
`test_correct_tenant_wrong_user_rejected`, `test_correct_user_wrong_session_rejected`.
`evals/adapters/inprocess.py::_t43` now performs a real `tenant-A` approval
presented as `tenant-B` → `GrantScopeError`, plus cross-user/session/drop-tenant.

### 4. ActionLog guarantee stated precisely
`action_log.py` + `models.ActionLogEntry` docstrings: **tamper-EVIDENT**, detects
accidental/unsophisticated modification/deletion/reorder/duplication; **not
tamper-proof, not immutable against a privileged DBA** who can rewrite every row
AND recompute the chain (outside the boundary). Future-hardening note: external
signed/HMAC checkpoint anchor (a later phase, **not implemented here**). Retry
loop is bounded (`_APPEND_RETRIES = 5`) and fails closed with `ChainIntegrityError`.
`chain_scope_id` is in the row-hash payload — verified by
`test_chain_scope_id_is_in_the_row_hash_payload` (identical first events in 3
scopes → 3 distinct `row_hash`, no `UNIQUE(row_hash)` collision).
`test_append_retry_is_bounded_and_fails_closed` proves the bound.

### 5. Server-fingerprint redaction aligned to the precise policy
`hashing._scrub_launch_config` no longer uses broad substring matching. It now
uses `redaction._is_sensitive_key` (exact / anchored-suffix) **plus** a small set
of bare credential words (`token`, `key`, `secret`, `auth`, `cred`, `cookie`,
`passphrase`, `apikey`, `bearer`). Benign identity fields survive: `token_budget`,
`max_tokens`, `tokenizer`, `timeout_s`, `model`, `LOG_LEVEL`. Tests: credential
rotation → fingerprint unchanged; `config_ref_id` change → fingerprint changes;
`token_budget`/`timeout_s`/`model`/`tokenizer`/`max_tokens` change → fingerprint
changes.

### Baseline after the correction pass
- `tests/test_approval_domain.py`: **154 passed, 2 skipped** (PG).
- Gate A `--offline`: exit 0 — 18 synthetic / 2 placeholder / 2 unverified / 25 blocked.
- Gate B `--inprocess`: exit 0 — **16 verified product passes** / 4 unverified / 25 blocked.

---

## Final Correction and Approval-Readiness Pass — 2026-09-08 (pre-commit)

### A. Grant authorization event renamed + coupled to dispatch
`grant.used` -> **`grant.authorization_used`** (constant `GRANT_AUTHORIZATION_USED_EVENT`).
`authorize_via_session_grant` replaced by **`dispatch_with_session_grant(db, *, envelope,
user_id, session_id, execute, tenant_id=None, clock=None)`**: resolves a live grant for the
fresh-nonce envelope; **no grant -> `GrantScopeError`, `execute` never called, no event
written**; grant matched -> **exactly one** `grant.authorization_used` event, written
**before** `execute` (a later crash cannot erase the evidence that authorization was
exercised), referencing `grant_id` + this attempt's fresh `action_hash` / `action_nonce`,
with `event_metadata.meaning` = "session-grant authorization exercised for this attempt; NOT
an execution-result event"; then calls `execute()`. `resolve_session_grant` stays a **pure
read**. Tests A.1-A.8 added.

### B. Decimal terminology corrected
`test_decimal_number_arguments_are_supported` -> **`test_finite_fractional_float_arguments_are_supported`**;
added **`test_python_decimal_is_rejected_not_supported`**. Wording everywhere: ordinary finite
fractional JSON numbers as Python `float` are supported; Python `decimal.Decimal` is rejected
unless converted first; unsafe integers, NaN, +/-Inf, lone surrogates, non-string object
keys, unsupported objects remain rejected.

### C. Server-fingerprint input contract documented
`hashing.server_fingerprint` docstring states the `launch_config` contract: raw credentials
MUST NOT be placed in `launch_config`; credentials are carried via `config_ref_id`
(server-owned reference), never the value; an unrecognised raw-credential field is an
operator/config violation; raw secrets are never logged or persisted as fingerprint inputs.
The scrub stays the precise exact/anchored policy + a small bare-word set - **no catch-all
heuristic**. `test_fingerprint_scrub_keeps_benign_lookalike_keys` proves `sort_key`,
`idempotency_key`, `keyboard_layout`, `tokenization_mode`, `max_tokens`, `cache_key_prefix`
survive.

### D. ActionLog detection list completed
Docstrings enumerate: modification, deletion, **insertion**, reordering, duplication,
**sequence gaps**, chain-link corruption; explicitly not tamper-proof, not immutable against
a privileged DBA, not cryptographically impossible to rewrite. Added
`test_chain_detects_sequence_gap` and `test_chain_detects_inserted_row` (forged row with
wrong `row_hash` -> detected; forged row with wrong `prev_row_hash` -> "broken link").

### E. Tenant isolation - no-trace assertions
Added `test_tenant_removed_or_replaced_is_rejected` and
`test_every_rejected_cross_scope_attempt_leaves_no_trace_and_rightful_caller_succeeds`: every
rejected cross-tenant/user/session attempt (including tenant dropped to null and tenant
replaced) -> `GrantScopeError`, approval stays `approved` (no authorization consumed), **zero
`grant.authorization_used` events in any scope**, rightful caller then consumes exactly once.

### Baseline after this pass
- `tests/test_approval_domain.py`: **166 passed, 2 skipped** (PG).
- Gate A `--offline`: exit 0 - 18 synthetic / 2 placeholder / 2 unverified / 25 blocked.
- Gate B `--inprocess`: exit 0 - **16 verified product passes** / 4 unverified / 25 blocked.

---

## Corrected Phase 1B.2 design constraints (documentation only - NOT implemented)

### 1. Action nonce & approval consumption
- The **trusted server** builds the action envelope and fresh `action_nonce` when the
  approval proposal is first created.
- The complete trusted data needed to **reconstruct and re-hash the exact proposed
  envelope** must be persisted server-side. `PendingApproval` binds the original
  `action_hash`.
- At consumption the **server reconstructs and re-hashes the originally approved envelope
  using its original `action_nonce`** - it does **not** generate a new nonce for an
  already-approved `PendingApproval`.
- A genuinely new execution attempt gets a new `action_nonce` + `action_hash` and requires
  a **new `PendingApproval`** unless a matching `SessionGrant` authorises it.
- The client never supplies/overrides nonce, hashes, trusted scope/identity/risk/policy
  fields, or execution state.
- **1B.2 DESIGN REQUIREMENT (repo evidence):** the current `PendingApproval` model persists
  identity columns + `action_hash` + `args_hash` + `action_nonce` + `policy_version` +
  `risk_level`, but **not** the full envelope (`tool_version`, `mcp_server_id`,
  `affected_resources`, `network/fs/credential scope`, `risk_scope_flags` are not columns).
  So server-side reconstruct-and-verify is **not possible today**. 1B.2 must persist the
  full trusted envelope (or its canonical bytes) on `PendingApproval`. Until then the 1B.1
  `consume_approval(resubmit_envelope=...)` API is a **domain-test convenience** (it
  re-canonicalises a caller-supplied envelope and compares `action_hash`); in 1B.2 the
  dispatcher reconstructs from persisted state and the client submits nothing.
  **action-hash verification is NOT weakened in the interim** - `consume_approval` still
  requires an exact `action_hash` match (which includes the original nonce) and the caller
  identity to match the row.

### 2. Pending-proposal idempotency - separate `proposal_dedupe_key`
- **Do NOT** dedupe one-time pending approvals on `grant_scope_hash` (it deliberately
  excludes `run_id` and `action_nonce`; it is for reusable `SessionGrant` matching only).
- Define a server-owned **`proposal_dedupe_key`** binding at least: `tenant_id`, `user_id`,
  `session_id`, `run_id`, **logical tool-call slot / trusted idempotency identity**,
  `tool_id` + `tool_version`, `source_type`, `mcp_server_id`, `server_fingerprint`,
  `tool_schema_hash`, `args_hash`, `affected_resources`, `network/fs/credential scope`,
  `risk_level`, `risk_scope_flags`, `policy_version`.
- Behaviour: retrying the *same logical proposal* returns the existing `PendingApproval` and
  reuses its original server-owned envelope + original `action_nonce`; never creates
  unbounded duplicate rows; a different run, a different logical slot, or any changed
  arg/schema/fingerprint/policy/identity/resource/risk field must **not** reuse the row; a
  denied/expired/consumed approval is **not** an active pending proposal.
- **OPEN - needs user decision.** Repo evidence: `app/tools/mcp_tool.py::extract_mcp_calls`
  returns bare `list[tuple[name, args]]` with **no call id / index persisted**;
  `MAX_MCP_CALLS_PER_TURN = 3`; `langgraph_workflow.py` state carries a single
  `pending_command` (not indexed slots); there is **no run/turn model** in the DB. So the
  repo does **not** currently provide a trustworthy stable "logical tool-call slot /
  idempotency identity". Candidate approaches for 1B.2 (do not pick without your input):
  (a) a server-issued `run_id` + per-turn monotonic `call_index` assigned by the dispatch
  loop as it iterates `extract_mcp_calls` output; (b) hash `(run_id, ordinal position in
  the parsed marker list, args_hash)` - fragile if the model re-emits markers reordered on
  retry; (c) require the streaming layer to pass a stable turn/message id (needs a
  `messages`-table id or a new column). **Do not trust an LLM/client-supplied call id
  without server validation.** Which approach do you want?

### 3. No public consume API (retained)
No public client-controlled `/consume` endpoint. The authenticated user-facing API may only
list / inspect / approve / deny **owned** approvals, and (separately, with an explicit
ownership-checked intent) create a session grant. Only the trusted server-side dispatcher
consumes authorization, at the final execution boundary, after reconstructing and verifying
the exact server-owned approved envelope. Client input never overrides trusted fields.

### 4. Gateway failure behaviour (retained)
The approval gateway is **required by default** for every enabled MCP and Python execution
path. **No legacy zero-approval fallback.** Gateway disabled/unavailable -> the affected
risky tool execution is **blocked/disabled**. `approval_gateway_enabled=false` must **not**
restore auto-execution; a rollback flag may disable risky tools but must **not** bypass
approval requirements. Unknown/unregistered tools **fail closed**. Newly added or materially
changed MCP tools **start locked**. A schema or server-fingerprint change **invalidates**
prior approval/grant matches.

### 5. Future dispatcher lifecycle events (1B.2, documentation only)
`tool.execution.started` / `.succeeded` / `.failed` / `.blocked`, each correlatable via
`approval_id`|`grant_id`, `action_hash`, `run_id`, `tenant_id`, `user_id`, `session_id`,
tool identity, `server_fingerprint`, `tool_schema_hash`. **Not implemented in 1B.1.**
