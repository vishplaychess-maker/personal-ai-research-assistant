# Phase 1B.2R — MP-0 (Durable Run Ledger Schema Foundation): Completion Report

**Date:** 2026-09-08 — schema foundation · round 2 (payload confidentiality) · round 3 (obsolete-column migration)
**Branch:** `phase-1b1-approval-domain` (HEAD `b2e6718`)
**Status:** **READY FOR USER REVIEW.** MP-0 (schema foundation) implemented, including the
payload-confidentiality correction (round 2) and the obsolete-column migration correction
(round 3). MP-1/MP-2 and later NOT started. Phase 1B.3 NOT started. Nothing committed, merged, or
pushed.
**This is NOT "Phase 1B.2R implemented"** — MP-0 is only the additive schema + state machine +
encrypted payload store + allowlisted signal schema + migration + tests.

---

## 0. Correction round 2 — payload confidentiality (this pass)

The prior MP-0 report retained `run_payloads.plaintext_sha256` (an unkeyed SHA-256 of the
plaintext stored next to the ciphertext) and treated it as an integrity control. That is a
confidentiality regression: an attacker with DB read access can **offline-guess a low-entropy
payload** (a decision enum, a small int) by hashing candidates. Fernet already authenticates the
ciphertext, so the digest was also redundant. It provided **zero** cryptographic binding of a
ciphertext to its owning run/step/user/tenant/action.

### 0.1 What changed

| # | Before (round 1) | Now (round 2) |
|---|---|---|
| 1 | `run_payloads.plaintext_sha256 VARCHAR(64) NOT NULL` in ORM + DDL + verify + writes + reads + docs | **Removed** from the clean ORM model, clean DDL, `_RUN_ENGINE_REQUIRED_COLUMNS`, `seal_payload`, `open_payload`, and the schema docs. Not replaced with any other unkeyed hash / checksum / truncated hash / deterministic-encryption / searchable metadata. |
| 2 | ciphertext was `Fernet(plaintext_bytes)`; context lived only in row columns | ciphertext is `Fernet(canonical_json(envelope))` where the **envelope carries `version, tenant_id, user_id, run_id, step_id, payload_type, action_hash, payload_encoding, payload`** — every context field is inside the authenticated ciphertext. Encrypt exactly once; canonical bytes from `app.approval.canonical.canonicalize` (RFC 8785). |
| 3 | `open_payload` compared row columns to caller-expected values | `open_payload` decrypts once, then **fails closed unless every decrypted envelope field matches trusted server-supplied expected values** (`expected_tenant_id / _user_id / _run_id / _step_id / _payload_type / _action_hash`, all now required). Row columns are a non-authoritative fast pre-check only. |
| 4 | swap-resistance untested | 6 adversarial swap tests + envelope-adversarial tests (see §5). |
| 5 | `RunPayloadIntegrityError` covered "hash mismatch" | new typed errors: `RunPayloadEnvelopeError`, `RunPayloadEnvelopeVersionError`, `RunPayloadEncodingError`, `RunPayloadContextMismatch`. |
| 6 | — | **round 3 (migration correction):** an obsolete `plaintext_sha256 NOT NULL` column is **not** inert — it rejects authoritative inserts. `_reconcile_run_payloads_obsolete_column()` runs first every startup: **empty** stale table ⇒ atomic rebuild to the authoritative shape (SQLite `BEGIN IMMEDIATE`; PG transactional); **non-empty** stale table ⇒ **fail fast**, no data touched. `_verify_run_engine_schema()` now **hard-fails** if any obsolete column survives. |

### 0.2 Files changed in this correction

`backend/app/run_engine/payloads.py` (rewritten: envelope seal/open, all `expected_*` required,
context checks, typed errors, no digest), `backend/app/run_engine/errors.py`
(+`RunPayloadEnvelopeError`, `RunPayloadEnvelopeVersionError`, `RunPayloadEncodingError`,
`RunPayloadContextMismatch`; `RunPayloadIntegrityError` / `RunPayloadActionHashMismatch` docstrings
updated), `backend/app/run_engine/__init__.py` (re-exports), `backend/app/models/models.py`
(`RunPayload`: drop `plaintext_sha256`, docstring rewrite), `backend/app/main.py`
(`_migrate_run_engine` DDL drops the column, `_RUN_ENGINE_REQUIRED_COLUMNS` drops it,
`_RUN_ENGINE_OBSOLETE_COLUMNS` + a tolerate-and-log branch in `_verify_run_engine_schema`),
`tests/test_run_payloads.py` (full rewrite — 39 tests), `tests/test_run_engine_models.py`
(drop the `plaintext_sha256` kwarg), `tests/test_run_engine_migrations.py` (round-2: clean DDL
omits the digest; round-3: full obsolete-column policy suite — see §7), this report, the
architecture doc §0 + §27.1, `THUNDER_AI_PRD.md` (accuracy tweak).

### 0.3 Round 3 — obsolete-column migration (this pass)

**Root cause:** round 2's report claimed a stale `run_payloads.plaintext_sha256 NOT NULL` column
was "inert" and that `_verify_run_engine_schema` would "tolerate" it. That is wrong: the
authoritative ORM/write path no longer supplies that column, so a `RunPayload` insert on a stale
dev DB fails its NOT NULL constraint — startup succeeds, the first payload write fails.

**Fix (`backend/app/main.py`, migration code only):**

- `_RUN_PAYLOADS_DDL` + `_RUN_PAYLOADS_INDEXES` — one authoritative source of the 13-column
  `run_payloads` shape, used by both the normal create path and the rebuild.
- `_reconcile_run_payloads_obsolete_column()` — runs **first** in `_migrate_run_engine()`, every
  startup, idempotent. Classifies precisely: table absent ⇒ nothing (clean install); obsolete
  column absent ⇒ nothing; **obsolete column present + 0 rows** ⇒ atomic rebuild; **obsolete
  column present + ≥1 row** ⇒ `_stale_run_payloads_nonempty_error` (fail fast, non-secret,
  actionable — names only the column name(s) + row count); **unsupported dialect** ⇒ fail fast.
- `_repair_or_fail_run_payloads_sqlite()` — explicit `BEGIN IMMEDIATE` (RESERVED lock: no
  concurrent insert between the row-count check and the drop) → prove 0 rows → `DROP TABLE` →
  recreate authoritative shape → recreate the 4 indexes → `COMMIT`. Any exception ⇒ `rollback()`
  (verified: DDL inside an explicit `BEGIN IMMEDIATE` **is** fully rolled back — unlike
  SQLAlchemy's auto-commit-DDL path), leaving the stale table + rows untouched. A crash between
  DROP and CREATE self-heals: the follow-up `CREATE TABLE IF NOT EXISTS` in `_migrate_run_engine`
  rebuilds it.
- `_repair_or_fail_run_payloads_postgres()` — one `engine.begin()` (PostgreSQL DDL is fully
  transactional). **Unverified** (no PG env).
- `_verify_run_engine_schema()` — the tolerate-and-log branch is replaced by a **hard
  `RuntimeError`** if any obsolete column survives, so verification can never report success for a
  shape that rejects authoritative inserts.

**Round-3 files:** `backend/app/main.py`; `tests/test_run_engine_migrations.py` (the
"starts-safely" test replaced by a full policy suite — see §7); the architecture doc §0 table
row; this §0.3 + §7 + §10. **No `app/run_engine/` or model change in round 3.** No worker, route,
streaming, SSE, resume, or MCP execution. No stale non-empty `run_payloads` exists anywhere in
the repo (only `./test.db`, which has no such table).

**No worker, route, streaming, SSE, resume, or MCP execution was added in any round. MP-1/MP-2/
Phase 1B.3 were not started. Nothing was committed, merged, or pushed.**

---

## 1. Recovery from the interrupted session

The prior session hit its usage limit mid-MP-0. On resume the working tree was inspected before
any edit (`git status` / `diff --stat` / `diff --name-status` / `diff --check` — the last clean;
full diffs of every changed file read).

### 1.1 Partially applied / incomplete edits found

| Area | State on resume | Correction |
|---|---|---|
| `backend/app/run_engine/payloads.py` | The feared duplicate implementation had **not** persisted — the second `Write` (using `get_cipher()` once for encrypt and once for decrypt) had landed cleanly. But it lacked the hardening the MP-0 prompt requires. | Rewritten: constant-time hash compare (`hmac.compare_digest`); `expected_run_id` (required) + `expected_step_id` (optional) ownership checks; `expected_action_hash` required-if-`envelope_action_hash`-set (constant-time); `expires_at` → `RunPayloadExpired`; `enc_scheme` allowlist → `RunPayloadSchemeError`; non-UTF-8 → `RunPayloadIntegrityError`; docstring rewritten to the *verified* contract (no key-version claim). |
| `backend/app/main.py` | `_migrate_run_engine()` and `_verify_run_engine_schema()` were **defined** but **never called** from `_migrate_database()`. | Wired the call in right after `_migrate_approval_domain_1b2()` (runs on both dialects, before the SQLite-only raw section). |
| `_migrate_run_engine()` DDL | Had a stray `ix_run_steps_run` index not present in the ORM model → ORM/DDL disagreement. | Removed it (`run_id` is the prefix of `UNIQUE(run_id, seq)`). |
| ORM models (`Run` / `RunStep` / `RunSignal` / `RunPayload`) | Carried per-column `index=True` on `tenant_id` / `user_id` / `session_id` / `run_id` / `step_id` that the raw DDL did not create → ORM/DDL disagreement on a fresh install (raw migration wins the race, `create_all` skips the existing table). | Removed the per-column `index=True`; kept only the explicit composite `Index(...)` in `__table_args__`; added `ix_run_payloads_owner` to both the model and the DDL. ORM and DDL now agree exactly (test-enforced). |
| `errors.py` | Only 5 payload error types. | Added `RunPayloadExpired`, `RunPayloadSchemeError`, `RunPayloadActionHashMismatch`. |
| `state_machine.py` | No idempotent-decision helper. | Added `idempotent_decision_state(current, decision)`. |
| `run_signals` policy | `payload` was an undocumented free `Text` column. | Added `app/run_engine/signals.py` — an enforced allowlist builder/parser; model comment points at it as the only sanctioned writer. |
| `__init__.py` | Did not export the new symbols. | Re-exported all. |

No duplicated docstrings, malformed class definitions, or contradictory edits were found in
`models.py` / `config.py` / `state_machine.py` / `session_binding.py` — those had persisted
cleanly. No unrelated user changes overlapped (all other modified/untracked paths are the
pre-existing uncommitted Phase 1B.1 / 1B.2 work from earlier in this branch).

---

## 2. Locked decisions D1–D10

Recorded verbatim-in-effect in the architecture doc **§0**. Summary of how MP-0 honours each:

| D | MP-0 effect |
|---|---|
| **D1** custom RunEngine now, LangGraph deferred | 5 SQLAlchemy tables; **one** authoritative store; `run_steps.engine ∈ {native, langgraph}` seam; no `langgraph` bump, no `-checkpoint-sqlite/postgres`, no Temporal/DBOS/Prefect. "≈80%" claim withdrawn → replaced by the primitives-available / capabilities-missing list (architecture §27.4). |
| **D2** worker topology | No worker built. Config seam `run_engine_enabled` (default `False`); lease columns on `runs` + `run_steps`; "SQLite ⇒ single process" and "hosted ⇒ dedicated worker process + PG" recorded as architecture, not code. |
| **D3** request idempotency | `runs.idempotency_key` + partial-UNIQUE `uq_runs_idem_scope` scoped by `(COALESCE(tenant_id,''), user_id, COALESCE(session_id,-1), idempotency_key) WHERE idempotency_key IS NOT NULL`; `runs.idempotency_identity_hash` for the "same-key-different-identity ⇒ conflict" rule. `/messages/stream` untouched. |
| **D4** disconnect ≠ cancel | State machine encodes cancel transitions + `race_winner` (cancel wins) + `uq_run_signals_live` (idempotent duplicate cancel). No route/worker. |
| **D5** session revocation mandatory | `runs.auth_session_ref` = a `refresh_sessions.family_id` (never a token). `session_ref_is_active()` fail-closed predicate. **Auth seam recorded** (access token lacks a `family_id` claim → column stays NULL → future resume fails closed). |
| **D6** migration order | Documented; MP-2 (MCP one-time approval resume) is the first behavioural flow. |
| **D7** run-event granularity | `run_events.category ∈ {control, text}`; `run_event_text_flush_bytes` (4096) / `_ms` (100) validated ≥ 1. No batching logic. |
| **D8** Python sandbox via gateway | Not before MP-4. Nothing in MP-0. |
| **D9** scheduler via RunEngine | Not before MP-5. Nothing in MP-0. |
| **D10** roadmap doc | `THUNDER_AI_PRD.md` §Status updated with a state-classified Phase 1B table. MP-0 ≠ "Phase 1B.2R implemented". |

---

## 3. Secure payload decision and evidence

**Decision: an encrypted `run_payloads` table using the repository's existing verified Fernet
service.** No new encryption or key-management design is required — so no stop condition fired.

**Evidence inspected (not assumed):**

- `backend/app/services/encryption.py` — `get_cipher()` returns `cryptography.fernet.Fernet`
  (**AES-128-CBC + HMAC-SHA256, authenticated**: a tampered/truncated/wrong-key token raises
  `InvalidToken`). Key from `ENCRYPTION_KEY` env → `settings.encryption_key` fallback; **raises
  `RuntimeError` when unset** (no ephemeral fallback in the canonical module). `rotate_api_key()`
  uses `MultiFernet([Fernet(new), get_cipher()])` — re-encrypt under a new primary, decrypt with
  the old key as fallback. **There is no key-version column anywhere.**
- `backend/app/services/encryption_service.py` — `decrypt_key()` **swallows** `InvalidToken` /
  `RuntimeError` and returns `""` (backward-compat for legacy plaintext API-key rows). **This is
  unsafe for run payloads** and is deliberately NOT used — `app/run_engine/payloads.py` calls the
  raising cipher directly and fails closed.
- `backend/app/models/models.py` — provider secrets already live in
  `user_providers.encrypted_api_key` (Fernet); MCP env in `mcp_servers.env_json`. Run state
  references these by id; never copies them.

**Empty-payload behaviour:** `plaintext == ""` → the envelope's `payload` field is `""` →
`cipher.encrypt(canonical_json(envelope))` → round-trips to `""`. **No `"\x00"`, whitespace, or
sentinel substitution; no truncation; encrypt-once / decrypt-once** (a spy-cipher test asserts
exactly one `.encrypt` on seal and exactly one `.decrypt` on open). **No adjacent plaintext
digest** — see §0.

**The "secrets vs re-parsed args" contradiction is resolved** (architecture §0): RunEngine never
creates a new plaintext copy of tool args — the `[MCP_CALL: …]` marker already lives in
`messages.content` (existing Message privacy model, unchanged), re-parsed + re-hashed at dispatch
in MP-2+. `run_payloads` is the *only* sanctioned home for run-scoped sensitive bytes **not**
already in a `messages` row, always encrypted.

**Full `run_payloads` contract table:** architecture doc §0 ("secure payload persistence
contract").

---

## 4. Exact schema / models

### 4.1 New tables (5) — additive; **no `ALTER`, `DROP`, rename, or data move on any existing table**

`runs` (`VARCHAR(36)` PK, 24 cols) — `id, tenant_id?, user_id, session_id?, thread_id, kind,
parent_run_id?, status, status_reason?, idempotency_key?, idempotency_identity_hash?,
auth_session_ref?, version, lease_owner?, lease_expires_at?, run_deadline_at?,
pending_approval_id?, last_event_seq, reconciled_by?, reconciled_at?, created_at, updated_at,
started_at?, finished_at?`. Indexes: `ix_runs_status_lease(status,lease_expires_at)`,
`ix_runs_owner_created(user_id,session_id,created_at)`, `ix_runs_pending_approval`,
`ix_runs_parent`, `ix_runs_tenant_status(tenant_id,status)`,
`ix_runs_idem(tenant_id,user_id,session_id,idempotency_key)` + **partial-UNIQUE**
`uq_runs_idem_scope`.

`run_steps` (`VARCHAR(36)` PK, 24 cols) — `id, run_id, tenant_id?, user_id, session_id?, seq,
kind, engine, status, execution_turn_id?, tool_call_id?, approval_id?, attempt, input_ref?,
output_ref?, input_payload_id?, output_payload_id?, error_code?, version, lease_owner?,
lease_expires_at?, created_at, started_at?, finished_at?`. `UNIQUE(run_id, seq)` +
`ix_run_steps_status_lease`, `ix_run_steps_tool_call`, `ix_run_steps_owner(user_id,session_id)`.

`run_events` (`VARCHAR(36)` PK, 9 cols) — `id, run_id, run_seq, tenant_id?, user_id, category,
type, data, created_at`. `UNIQUE(run_id, run_seq)` + `ix_run_events_run(run_id,created_at)`,
`ix_run_events_owner(user_id)`.

`run_signals` (`VARCHAR(36)` PK, 8 cols) — `id, run_id, kind, payload?, created_by_user_id,
created_by_tenant_id?, consumed_at?, created_at`. `ix_run_signals_run_kind(run_id,kind)` +
**partial-UNIQUE** `uq_run_signals_live(run_id, kind) WHERE consumed_at IS NULL`.

`run_payloads` (`VARCHAR(36)` PK, **13 cols — no `plaintext_sha256`**) — `id, tenant_id?, user_id,
run_id, step_id?, payload_type, enc_scheme, ciphertext, envelope_action_hash?, byte_len,
expires_at?, deleted_at?, created_at`. `ix_run_payloads_run`, `ix_run_payloads_step`,
`ix_run_payloads_owner(user_id)`, `ix_run_payloads_expiry`. `ciphertext` = Fernet token over the
canonical **envelope** (§0); `envelope_action_hash` mirrors the envelope's `action_hash` for
operational filtering; `byte_len` is the payload UTF-8 length (size cap + ops only —
**intentionally reveals length, not confidential**).

Conventions: tz-aware `DateTime(timezone=True)` + `_utcnow_aware`; `VARCHAR(36)` UUID PKs;
nullable `tenant_id` no-FK; `user_id` plain `Integer` no-FK (code-enforced ownership); plain
`String` status columns with `app.run_engine.state_machine` constants.

### 4.2 `execution_turns` — unchanged

No column added. The existing `run_id VARCHAR(64)` is reused as the `runs.id` link (documented in
the model + migration). `runs.thread_id` / `runs.idempotency_key` are the homes for those,
reachable from a turn via `run_id`. **MP-0 touches zero existing tables.**

### 4.3 New domain modules (no worker / route / I/O beyond the DB)

`app/run_engine/` — `errors.py` (10 typed errors), `state_machine.py` (pure), `payloads.py`
(seal/open/purge), `signals.py` (allowlist), `session_binding.py` (fail-closed predicate),
`__init__.py`.

---

## 5. State transition table

Run states (13): `created, queued, running, waiting_for_approval, approved, denied, resuming,
cancelling, cancelled, succeeded, failed, timed_out, outcome_unknown`.
Terminal: `{succeeded, failed, timed_out, cancelled, denied, outcome_unknown}`.

| From | Legal targets |
|---|---|
| `created` | `queued`, `cancelling` |
| `queued` | `running`, `cancelling`, `cancelled` |
| `running` | `waiting_for_approval`, `succeeded`, `failed`, `timed_out`, `cancelling`, `outcome_unknown` |
| `waiting_for_approval` | `approved`, `denied`, `failed`, `cancelling` |
| `approved` | `resuming`, `cancelling`, `failed` |
| `resuming` | `running`, `failed`, `cancelling`, `outcome_unknown` |
| `cancelling` | `cancelled`, `outcome_unknown` |
| terminal states | *(none)* |
| `outcome_unknown` | `succeeded` / `failed` **only** with `allow_reconcile=True` (an explicit future op) |

Step states (8): `pending → running → {succeeded, failed, blocked, waiting, incomplete}`;
`pending → skipped`; `waiting → {running, blocked, skipped}`.

Rules proven by tests: every legal edge accepted; every non-declared edge raises
`InvalidRunTransition`; unknown states rejected; terminal states have no outgoing edge;
`outcome_unknown` leaves only via `allow_reconcile` and only to a decided terminal; `denied`
never reopens; `timed_out` reachable only from `running`; **cancel always wins** the
approval/cancel race; `idempotent_decision_state` returns a deterministic state for duplicate
approve/deny and raises on a conflicting/undecidable one; the module imports no
`sqlalchemy`/`app.models`/`asyncio`/socket/file I/O.

---

## 6. Migration evidence

`_migrate_run_engine()` — first calls `_reconcile_run_payloads_obsolete_column()` (round 3), then
`CREATE TABLE / INDEX IF NOT EXISTS` for the 5 tables + the 2 partial-UNIQUE indexes;
dialect-agnostic; **no `ALTER`, and `DROP` only of an EMPTY stale round-1 `run_payloads`**. Called
from `_migrate_database()` immediately after `_migrate_approval_domain_1b2()` (both dialects).
`_verify_run_engine_schema()` runs immediately after and **halts startup** (`RuntimeError`) on a
missing table / required column / `run_steps` or `run_events` uniqueness / (SQLite) missing
partial-UNIQUE index / **any surviving obsolete column**.

| Check | Result |
|---|---|
| Clean DB → 1B.1 + 1B.2 + run-engine migrations + verify | 5 tables, all required columns, both partial-UNIQUE indexes; verify passes; `run_payloads` has 13 cols, no `plaintext_sha256` |
| Upgrade from the current Phase 1B (1B.1 + 1B.2) schema | run tables appear; `execution_turns` unchanged (`run_id` present, no `thread_id`/`idempotency_key`) |
| Repeat `_migrate_run_engine()` + verify ×3 (clean and post-repair) | idempotent — no error, stable schema |
| ORM `Base.metadata.create_all` vs raw DDL (clean **and** post-repair `run_payloads`) | column sets, composite index names, unique constraints **match** for all 5 tables; the 2 partial-UNIQUE indexes are migration-only |
| `_verify` detects a renamed/missing column | raises `RuntimeError` naming the table + column |
| `_verify` detects a dropped partial-UNIQUE index | raises `RuntimeError` naming `uq_run_signals_live` |
| **`_verify` detects a surviving obsolete column** (round 3) | raises `RuntimeError` naming `run_payloads` + `plaintext_sha256` — verification can never pass a shape that rejects authoritative inserts |
| **Stale `run_payloads` (`plaintext_sha256 NOT NULL`), 0 rows** (round 3) | auto-rebuilt to the authoritative 13-col shape; `plaintext_sha256` gone; 4 indexes restored; `create_all` parity; idempotent ×3; `seal_payload`+`open_payload` (incl. empty string) round-trip afterwards |
| **Stale `run_payloads`, ≥1 row** (round 3) | `_migrate_run_engine()` raises `RuntimeError` naming `run_payloads` + the row count; message carries **no** ciphertext / digest value / row id; the table, row count, `ciphertext`, and every column are **byte-for-byte unchanged**; a 2nd startup raises identically with still no mutation |
| **Crash between DROP and CREATE** (round 3) | self-heals — the follow-up `CREATE TABLE IF NOT EXISTS run_payloads` rebuilds the authoritative shape; verify passes |
| **Unsupported dialect + obsolete column** (round 3) | `_reconcile…` raises `RuntimeError` ("SQLite and PostgreSQL" only); the stale table is untouched |
| SQLite atomicity of the empty-table rebuild (round 3) | explicit `BEGIN IMMEDIATE` on the raw DBAPI connection: DDL **is** rolled back on error (verified directly); RESERVED lock blocks a concurrent insert between the count and the drop; migration runs in `lifespan` before serving anyway |
| Existing `pending_approvals` / `dispatch_records` rows across the migration (×2) + verify | **byte-for-byte unchanged** (row snapshot equality) |
| SQLite NULL `tenant_id` / `session_id` in `uq_runs_idem_scope` | `COALESCE(...,'')` / `COALESCE(...,-1)` — two NULL-scope rows with the same key collide; rows with `idempotency_key IS NULL` never collide |
| PostgreSQL execution — incl. `_repair_or_fail_run_payloads_postgres` | **NOT run** — `EVAL_PG_TEST_URL` unset; `test_postgres_run_engine_migration` is `skipif`-skipped. PG stale-schema repair is **unverified**. Enumerated hosted-deployment blocker. |

---

## 7. Tests & exit codes

Repo venv `./.venv/Scripts/python.exe`, from repo root. `PYTHONPATH=backend`.

Re-run **after the round-3 obsolete-column migration correction**.

| Command | Result | Exit |
|---|---|---|
| `python -m compileall -q` (all changed + new `.py`) | ok | **0** |
| `pytest tests/test_run_engine_state_machine.py -q` | **14 passed** | **0** |
| `pytest tests/test_run_payloads.py -q` | **39 passed** (envelope + swap-resistance + envelope-adversarial + low-entropy-guess) | **0** |
| `pytest tests/test_run_signals.py -q` | **10 passed** | **0** |
| `pytest tests/test_run_engine_models.py -q` | **7 passed** | **0** |
| `pytest tests/test_run_engine_migrations.py -q` | **16 passed, 1 skipped** (PG) — round 3: +stale-empty-auto-repaired, +repaired-matches-authoritative, +stale-nonempty-fail-fast-no-mutation, +seal/open-after-repair, +verify-hard-fails-if-obsolete-survives, +crash-between-drop-and-create-self-heals, +unsupported-dialect-fail-fast | **0** |
| `pytest tests/test_approval_domain.py -q`  (Phase 1B.1) | **166 passed, 2 skipped** | **0** |
| `pytest` 1B.2 gateway/api/migrations + 6 MCP suites | **91 passed, 1 skipped** (PG) | **0** |
| `pytest tests/test_model_validation.py -q` | **4 passed** | **0** |
| `pytest tests/test_evals_runner.py tests/test_evals_inprocess.py -q` | **74 passed** | **0** |
| `python -m evals.runner --offline` | 19 synthetic / 2 placeholder / 2 unverified / 24 blocked / 0 fail / 0 error | **0** |
| `python -m evals.runner --inprocess` | **17 verified** / 4 unverified / 24 blocked / 0 fail / 0 error | **0** |
| `git diff --check` | clean | **0** |
| `python -c "import yaml; yaml.safe_load(open('.github/workflows/evals.yml'))"` | OK | **0** |
| `python -c "import app.main, app.approval, app.run_engine"` | OK | **0** |
| structural secret-field grep over run-engine models + migration | `plaintext_sha256` appears **only** in `_RUN_ENGINE_OBSOLETE_COLUMNS` + explanatory comments — **not** in any authoritative create/write path; no secret-named column | — |
| log-capture secret canary (`test_no_secret_canary_in_captured_logs`) | canary + ciphertext + `gAAAAA…` absent from `caplog.text` | **0** |
| broadest practical hermetic regression (24 suites incl. run-engine, approval, MCP, evals, planning, session-ownership, async-bridge, evaluation, system-prompts, browser-agent) | **465 passed, 4 skipped** (4 × PostgreSQL-only) | **0** |

**MP-0 test totals: 86 passed, 1 skipped** (round 1: 61/1 → round 2: 80/1 → round 3: 86/1). No
Phase 1B.1 / 1B.2 regression. Eval counts unchanged from the 1B.2 baseline.

### Swap-resistance evidence (`tests/test_run_payloads.py`)

The protection comes from the **authenticated encrypted envelope + trusted expected context**,
never from an adjacent digest. Proven to fail closed:

- `test_swap_only_ciphertext_between_rows_fails_closed` — put row B's ciphertext into row A;
  `open_payload(A, expected=<run-A context>)` → `RunPayloadContextMismatch` (envelope says runB).
- `test_swap_ciphertext_plus_every_mutable_column_fails_closed` — rewrite row A's `ciphertext`,
  `tenant_id`, `user_id`, `run_id`, `step_id`, `payload_type`, `enc_scheme`,
  `envelope_action_hash`, `byte_len` all to B's values; opening it as run A still raises a
  `RunPayloadError` (the check is envelope-vs-trusted-expected, not row-columns).
- `test_ciphertext_copied_between_runs_steps_users_tenants` — ciphertext copied, row A keeps its
  own columns so the fast pre-check passes → envelope run/step/user/tenant = B →
  `RunPayloadContextMismatch`.
- `test_swap_resistance_with_action_hash_null_in_both` / `..._with_equal_action_hash_in_both` —
  swap still fails on the other context fields; equal or null `action_hash` does not rescue it.
- `test_tenant_null_normalization_does_not_collide` — a payload sealed with a real tenant, opened
  expecting tenant-null → `RunPayloadContextMismatch(field="tenant_id")`; `""` normalizes to
  `null` and matches expected `None`.

### Envelope-adversarial (`tests/test_run_payloads.py`)

Authenticated-but-hostile decrypted bytes are rejected: non-object (`"string"` / `[]` / `42` /
`null`) and malformed JSON → `RunPayloadEnvelopeError`; missing / extra field →
`RunPayloadEnvelopeError`; `version: 2` → `RunPayloadEnvelopeVersionError`; `user_id: "7"` →
`RunPayloadEnvelopeError`; `payload_encoding: "latin-1"` and non-UTF-8 decrypted bytes →
`RunPayloadEncodingError`.

### Low-entropy offline-guessing

- `test_no_plaintext_digest_column_exists` — the `RunPayload` model has no `plaintext_sha256` /
  `*sha*` / `*hash*` column (`action_hash` — a hash of trusted non-secret fields — excepted).
- `test_low_entropy_payload_leaves_no_guessable_digest` — seal `"approved"`; assert
  `sha256("approved"|"denied"|"yes"|"no"|...)` (full and first-32-hex) match nothing in the row,
  and the plaintext itself is absent.

### Structural + canary secret checks (detail)

- `test_no_secret_canary_in_any_run_table` — seal a canary, then scan every column of
  `runs` / `run_steps` / `run_events` / `run_signals` / `run_payloads` / `action_log` → the
  canary is **absent everywhere**; `run_payloads.ciphertext` is a Fernet token (`gAAAAA…`).
- `test_no_secret_canary_in_captured_logs` — seal + open + a forced integrity failure under
  `caplog(DEBUG)` → neither the canary nor `gAAAAA…` appears in logs.
- `test_encryption_and_decryption_each_happen_exactly_once` — spy cipher: `encrypt` count 1 on
  seal, `decrypt` count 1 on open. `test_oversized_..._before_encryption` — `encrypt` count 0
  when the size cap rejects.
- `test_key_rotation_multifernet_still_opens` — a new primary + the old key as `MultiFernet`
  fallback still decrypts a previously sealed payload.

---

## 8. Full-suite status

The **full service-backed backend suite** (live Ollama + ChromaDB, ~23 min) was **NOT run** —
those services are not available in this environment. It is **not** claimed passing. It remains a
**mandatory gate before any Phase 1B commit/push**.

Hermetic coverage that *was* run: 440 passed / 4 skipped across 24 suites (above). The 4 skips are
all PostgreSQL-only.

---

## 9. PostgreSQL status

**Unavailable** (`EVAL_PG_TEST_URL` unset). `test_run_engine_migrations.py::
test_postgres_run_engine_migration` is `skipif`-skipped and **enumerated as a hosted-deployment
blocker**, alongside the pre-existing 1B.2 PG blockers (dedupe index, ActionLog `FOR UPDATE`,
dispatch idempotency, active-proposal caps) and the future 1B.2R run-lease / resume-signal /
migration concurrency verification. `TIMESTAMP` (raw DDL) vs `TIMESTAMP WITH TIME ZONE`
(`DateTime(timezone=True)` in the ORM) column-type parity on PostgreSQL is part of that
unverified gate — same established pattern as the Phase 1B tables.

---

## 10. Known limitations

- **MP-0 is schema only.** No worker, run loop, route, `/messages/stream` change, SSE, approval
  resume, MCP execution, frontend change, scheduler integration. The live-resume gap is
  unchanged until MP-2/MP-3.
- **`runs.auth_session_ref` is never populated in MP-0.** The access-token JWT carries no
  `family_id` claim (`create_access_token` mints only `{"sub","exp","iat"}`). Adding that claim
  (or resolving `family_id` from the refresh cookie at submit) is a **required MP-1 auth seam**.
  Until it lands, `session_ref_is_active()` returns `False` for every run → a future resume/
  dispatch fails closed. This is a documented seam, **not** a fake revocation state.
- **`run_payloads` interface is UTF-8 `str` / canonical JSON**, not raw bytes (tool args are
  JSON strings). A bytes interface is a documented future extension, not a silent behaviour
  (`test_interface_is_utf8_str_only`; `seal_payload` rejects a `bytes` `plaintext` with
  `ValueError`).
- **`byte_len` reveals payload length.** It is a column OUTSIDE the ciphertext, used only for the
  pre-encryption size cap and operational reporting. It is **not confidential** — documented in
  the model, module, architecture §0, and this report.
- **No per-row key-version.** The repo's only rotation mechanism is `MultiFernet`
  (primary re-encrypt + old-key fallback; `test_key_rotation_multifernet_still_opens`).
  `enc_scheme` is a cipher-scheme tag, not a key version — stated explicitly everywhere.
- **Obsolete round-1 `run_payloads.plaintext_sha256` (round 3).** It is **not** inert — a NOT NULL
  obsolete column rejects authoritative inserts. `_reconcile_run_payloads_obsolete_column()`
  (runs first, every startup, idempotent): **empty** stale table ⇒ atomic rebuild to the
  authoritative shape (SQLite `BEGIN IMMEDIATE` + rollback-safe DDL; PostgreSQL transactional);
  **non-empty** stale table ⇒ **fail fast** at startup, no data touched (old rows' ciphertext
  predates the envelope format and are never transformed/deleted/copied). `_verify_run_engine_schema`
  hard-fails if any obsolete column survives. Manual drop/recreate is only an operator recovery
  option *after* the fail-fast, never the sole protection. **PostgreSQL stale-schema repair is
  unverified** (no PG env). No stale non-empty `run_payloads` exists anywhere in the repo.
- **PostgreSQL unverified** (§9) — includes the stale-schema repair path (`_repair_or_fail_run_payloads_postgres`).
- `run_events.id` is a `VARCHAR(36)` uuid4 (not a global autoincrement) — chosen for
  SQLite+PostgreSQL DDL safety; per-run ordering is `UNIQUE(run_id, run_seq)`.
- The `SAWarning: Skipped unsupported reflection of expression-based index uq_runs_idem_scope` in
  the migration test is benign (SQLAlchemy cannot reflect a `COALESCE(...)` index — same as
  1B.2's `uq_pending_approvals_active_dedupe`); the test checks `sqlite_master` text directly.

---

## 11. Exact git status / diff

```
branch: phase-1b1-approval-domain    HEAD: b2e67183d4791cbbaed9d3d98e8833983510f3ff  (unchanged)

Modified (tracked):
  THUNDER_AI_PRD.md                    (§Status — D10 Phase 1B table; accuracy tweak)
  backend/app/config.py                (+run_engine_enabled, run_payload_max_bytes,
                                        run_event_text_flush_bytes/ms + validator entries)
  backend/app/main.py                  (+_migrate_run_engine, +_verify_run_engine_schema, +call;
                                        round 2: DDL drops plaintext_sha256;
                                        round 3: +_RUN_PAYLOADS_DDL/_INDEXES constants,
                                        +_reconcile_run_payloads_obsolete_column +
                                        _repair_or_fail_run_payloads_{sqlite,postgres} +
                                        _stale_run_payloads_nonempty_error; verify hard-fails
                                        on a surviving obsolete column)
  backend/app/models/models.py         (+Run, RunStep, RunEvent, RunSignal, RunPayload;
                                        round 2: RunPayload drops plaintext_sha256 — no round-3 change)
  .github/workflows/evals.yml  backend/app/tools/mcp_tool.py  backend/requirements.txt
  evals/adapters/inprocess.py  evals/dataset.jsonl  tests/test_evals_inprocess.py
  tests/test_evals_runner.py  tests/test_mcp_tool.py     (Phase 1B.1/1B.2 — unchanged this task)

Untracked (new this MP-0 task):
  backend/app/run_engine/              (__init__, errors, state_machine, payloads, signals,
                                       session_binding — round 2: payloads.py rewritten,
                                       errors.py +4 types; NO round-3 change)
  docs/superpowers/plans/2026-09-08-phase-1b2r-durable-run-resume-architecture.md  (§0, §27.1)
  docs/superpowers/reports/phase-1b2r-mp0-completion.md   (this report — rounds 1-3)
  tests/test_run_engine_state_machine.py (14)  test_run_payloads.py (39)  test_run_signals.py (10)
  tests/test_run_engine_models.py (7)  test_run_engine_migrations.py (16 + 1 PG skip)

Untracked (pre-existing Phase 1B.1/1B.2, not touched this task):
  backend/app/approval/  backend/app/routes/approvals.py
  docs/superpowers/plans/2026-09-07-*.md  docs/superpowers/reports/phase-1b{1,2}-completion.md
  evals/fixtures/T4{1,2,3,5}.json
  tests/test_approval_domain.py test_approval_gateway.py test_approval_migrations.py test_approvals_api.py

git diff --check: clean (exit 0)
```

**MP-0's own footprint:** `backend/app/run_engine/` (6 files, ~750 LoC), 5 ORM models
(~230 LoC), `_migrate_run_engine` + `_verify_run_engine_schema` + the obsolete-column
reconcile/repair (~340 LoC), 4 config settings + 3 validator entries, 5 test files
(**86 tests, 1 PG skip**), doc updates. Zero existing table altered; `run_payloads` is only ever
rebuilt from an EMPTY stale round-1 dev shape.

---

## 12. Confirmations

- **MP-1 was NOT started.** No `RunWorker`, no `asyncio` worker task, no run-claim/lease
  behaviour, no API routes, no `/messages/stream` change, no SSE reconnect, no approval resume,
  no MCP execution, no frontend change, no scheduler integration.
- **Phase 1B.3 was NOT started.**
- **MP-0 is NOT approved** — this report is for user review only.
- **Nothing was committed, merged, or pushed.** No reset / stash / clean / discard / branch-ref
  modification. `HEAD` is still `b2e6718` on `phase-1b1-approval-domain`.
- **No Phase 1B.1 / 1B.2 table, model, or migration was rewritten.** MP-0 adds 5 new tables and
  `ALTER`s nothing; the only `DROP` is of an **empty** stale round-1 `run_payloads` dev shape
  (proven empty in the same transaction). Phase 1B.1 (166 + 2 skip) and Phase 1B.2 + MCP
  (91 + 1 skip) regression suites are green.
- **Rounds 2 (payload confidentiality) and 3 (obsolete-column migration) added NO worker / route
  / streaming / SSE / resume / MCP execution / frontend / scheduler code.** Round 3 touched only
  `backend/app/main.py` migration code + `tests/test_run_engine_migrations.py` + docs.
