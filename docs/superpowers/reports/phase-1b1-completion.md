# Phase 1B.1 — Approval Domain: Completion Report

**Date:** 2026-09-08
**Branch:** `phase-1b1-approval-domain` (HEAD `b2e6718`, off `main`)
**Verdict:** **READY FOR USER REVIEW** (self-host-first scope). PostgreSQL verification is an
explicit blocker for *hosted multi-user* deployment only — not for this review.
**Not committed. Not merged. Not pushed. Phase 1B.2 not implemented.**

> Source-control: per the user's decision, all Phase 1B work (1B.1 + 1B.2 + 1B.3) stays uncommitted
> on this feature branch until the complete Phase 1B is implemented and approved. No checkpoint
> commits.

---

## 1. Scope completed

The Phase 1B.1 **approval domain** — canonicalization, one-time `action_hash` + reusable
`grant_scope_hash`, stable server/tool fingerprint, `PendingApproval` + `SessionGrant` +
`ActionLogEntry` + `ActionLogHead` persistence, atomic approval state machine, session-scoped
grants with an explicit user decision, a tamper-evident `ActionLog` hash chain, additive
migrations, ~166 hermetic tests, and real in-process eval targets for T41–T43 — plus the two
prior correction passes and this final one. **No** wiring into MCP / Python / terminal / browser
/ streaming / LangGraph / frontend / routes. **No** risk-policy engine, auto mode, carve-out
enforcement, OpenRouter, paid calls, or durable task manager.

## 2. Files changed and reasons

### New — `backend/app/approval/` (10 modules)

| File | Responsibility |
|---|---|
| `__init__.py` | Public surface (43 re-exports incl. `GRANT_AUTHORIZATION_USED_EVENT`, `dispatch_with_session_grant`). |
| `errors.py` | `ApprovalError` family (8 typed errors: `CanonicalizationError`, `EnvelopeError`, `EnvelopeMismatch`, `InvalidTransition`, `ApprovalExpired`, `ApprovalAlreadyConsumed`, `GrantScopeError`, `ChainIntegrityError`). |
| `clock.py` | `Clock` protocol, `SystemClock`, `FixedClock` (injectable), `ensure_utc`. tz-aware UTC. |
| `canonical.py` | RFC 8785 canonicalization via `rfc8785==0.1.4`; typed error wrapping; depth pre-check. |
| `hashing.py` | `args_hash`, `tool_schema_hash`, `server_fingerprint`, `action_hash` (20 fields), **`grant_scope_hash`** (18 fields), `row_hash`, `GENESIS`. Length-prefixed domain tags. |
| `envelope.py` | Frozen `ActionEnvelope` + `hash_fields()`/`action_hash()` + `grant_scope_fields()`/`grant_scope_hash()` + validation + `redact_purpose` + `display_summary`. |
| `redaction.py` | Recursive redactor for `display_summary` / `event_metadata` — exact / anchored-suffix key policy + value-pattern scrub. |
| `state_machine.py` | State constants, `ALLOWED` transitions, `expire_if_due`, `approve`, `deny`, `consume` — all atomic CAS. |
| `action_log.py` | `chain_scope_id`, `_ChainLock` (PG `FOR UPDATE` / SQLite `BEGIN IMMEDIATE`), `append_event` (bounded retry, fail-closed), `verify_chain`, `ChainReport`, `_norm_ts`. |
| `service.py` | `_classify` (trusted policy), `request_approval`, `record_decision`, `consume_approval`, `create_session_grant`, `resolve_session_grant`, **`dispatch_with_session_grant`**, `revoke_session_grant`. |

### Modified

| File | Change | Reason |
|---|---|---|
| `backend/requirements.txt` | +`rfc8785==0.1.4` with a 9-line rationale comment | Locked decision 1 — RFC 8785 canonicalization engine |
| `backend/app/models/models.py` | +`_utcnow_aware()`; +4 models (`PendingApproval`, `SessionGrant`, `ActionLogEntry`, `ActionLogHead`). `SessionGrant`: `grant_scope_hash`, CSPRNG `grant_nonce` (`UNIQUE`), `grant_scope_version`. Precise `ActionLogEntry` docstring. **No existing model touched.** | Domain persistence; locked decision 2 |
| `backend/app/main.py` | +`_migrate_approval_domain()` (dialect-agnostic `CREATE TABLE/INDEX IF NOT EXISTS`, additive, idempotent) + one call in `_migrate_database()` pre-guard block | Repo migration convention (`_migrate_user_skills` / `_migrate_media_video_tasks`) |
| `evals/adapters/inprocess.py` | +`_approval_env()` helper, +`_t41`/`_t42`/`_t43` targets running real `app.approval` code; `_t43` does a real tenant-A→tenant-B rejection | T41–T43 become verified product behaviors |
| `evals/dataset.jsonl` | T41–T43: removed `blocked_by`, added `asserts`, tidied `product_target`. T44/T45 unchanged (`blocked_by: "1B"`). | T41–T43 unblocked by Phase 1B.1 |
| `evals/fixtures/T41.json`, `T42.json`, `T43.json` (new) | Synthetic-regression offline fixtures | Gate A stays exit 0 while `--inprocess` overrides with real evidence |
| `tests/test_evals_runner.py` | Baseline counts: synthetic 15→18, blocked 28→25 | T41–T43 no longer blocked |
| `tests/test_evals_inprocess.py` | `_MOCK_BOUNDARY_TASKS` += T41–T43; verified floor 12→16 | T41–T43 now boundary-mock verified |
| `tests/test_approval_domain.py` (new) | ~166 hermetic tests | Domain verification |
| `.github/workflows/evals.yml` | Gate B job now also runs `tests/test_approval_domain.py` | The `--inprocess` runner imports `app.approval`; Gate B installs full requirements incl. `rfc8785` |
| `docs/superpowers/plans/2026-09-07-phase-1b1-approval-domain.md` | Plan + 3 correction-pass sections + corrected Phase 1B.2 constraints | Design record |

## 3. Accepted decisions

1. **`rfc8785==0.1.4`** exact-pinned runtime dependency — RFC 8785/JCS canonicalization engine.
   Not to be replaced unless a verified compatibility or security defect is found.
2. **`grant_scope_hash`** — model + DB column name for the reusable session-grant identity,
   separate from the one-time `action_hash`. With `grant_scope_version`, `UNIQUE grant_nonce`,
   session-bounded expiry, tenant/user/session isolation, revocation, server-derived scope
   fields, per-use authorization audit evidence.
3. Authorization audit event **`grant.authorization_used`**, written at the authorization-to-
   dispatch handoff, meaning *authorization exercised*, **not** execution success.

## 4. Exact RFC 8785 canonicalization contract

`backend/app/approval/canonical.py :: canonicalize(value) -> bytes`, engine `rfc8785==0.1.4`
(Trail of Bits, Apache-2.0, pure-Python, **zero runtime dependencies**, `Requires-Python >=3.8`).

- **Numbers:** every finite JSON number representable as an IEEE-754 double, serialized in
  RFC 8785-compatible ECMAScript `Number.prototype.toString` form.
- **Rejected:** `NaN`, `+Infinity`, `-Infinity`.
- **Integers:** only the I-JSON safe range `[-(2**53 - 1), 2**53 - 1]`. A larger integer is
  rejected; callers must represent it as a string.
- **`decimal.Decimal` and other non-JSON-native numeric objects:** rejected. Callers must
  explicitly convert (e.g. to `float`) before canonicalization.
- **Strings:** preserved exactly. **No NFC, NFD, or any Unicode normalization** (RFC 8785
  forbids altering parsed string data). Minimal escaping.
- **Lone UTF-16 surrogates:** rejected.
- **Arrays:** order preserved.
- **Object keys:** recursive sort per RFC 8785/JCS (UTF-16 code-unit order).
- **Non-string object keys and unsupported non-JSON objects** (`bytes`, `bytearray`, `set`,
  arbitrary objects): rejected.
- **Nesting-depth protection:** retained (`_MAX_DEPTH = 200`; over-deep → typed
  `CanonicalizationError`, not `RecursionError`).
- **Output:** deterministic UTF-8 bytes, identical across Windows/Linux and supported Python
  versions.

## 5. Number-vector evidence

`test_rfc8785_number_vectors` (24 pinned vectors — the ECMAScript/RFC 8785 canonical forms;
`rfc8785` is validated against the RFC's own vectors and pinned here to catch a dependency
regression):

| input | canonical | | input | canonical |
|---|---|---|---|---|
| `4.5` | `4.5` | | `1e-6` | `0.000001` |
| `2e-3` | `0.002` | | `1e-7` | `1e-7` |
| `1e30` | `1e+30` | | `5e-324` (min subnormal) | `5e-324` |
| `1e-27` | `1e-27` | | `-5e-324` | `-5e-324` |
| `333333333.33333329` | `333333333.3333333` | | `1.7976931348623157e308` (max finite) | `1.7976931348623157e+308` |
| `0.0` / `-0.0` / `0` / `-0` | `0` | | `-1.7976931348623157e308` | `-1.7976931348623157e+308` |
| `1` / `-1` | `1` / `-1` | | `2**53 - 1` (max safe int) | `9007199254740991` |
| `4.0` / `100.0` | `4` / `100` | | `-(2**53 - 1)` (min safe int) | `-9007199254740991` |
| `1e21` | `1e+21` | | `2.2250738585072014e-308` (min normal) | `2.2250738585072014e-308` |
| `1e20` | `100000000000000000000` | | | |

Rejections proven (`test_canonical_rejects_unsupported`): `NaN`, `+Inf`, `-Inf`, `2**53`,
`-(2**53)`, `2**64`, `10**40`, `Decimal("1.5")`, `b"…"`, `bytearray`, `set`, `{1: …}` (int key),
`{(1,2): …}` (tuple key), `"…\ud800"` / `"…\udc00"` / nested lone surrogate.
Equivalence: `canonicalize({"n":4}) == canonicalize({"n":4.0})`; `args_hash({"x":10}) ==
args_hash({"x":10.0})`; `args_hash({"x":1}) != args_hash({"x":1.5})`.
No normalization: `canonicalize({"s": NFD}) != canonicalize({"s": NFC})`.
Determinism: `canonicalize(v)` byte-identical across 50 repeats
(`test_canonical_deterministic_cross_run`).

## 6. Decimal-object rejection clarification

- **Ordinary finite fractional JSON numbers represented as Python `float` ARE supported**
  (`test_finite_fractional_float_arguments_are_supported` — coordinates, thresholds, ratios,
  timeouts).
- **Python `decimal.Decimal` is REJECTED** unless the caller explicitly converts it (e.g. to
  `float`) before canonicalization (`test_python_decimal_is_rejected_not_supported`).
- Unsafe integers, `NaN`, `±Infinity`, lone UTF-16 surrogates, non-string object keys, and
  unsupported non-JSON objects remain rejected.
- Test names and comments were corrected — the earlier `test_decimal_number_arguments_are_supported`
  (which actually tested `float`) is renamed; the earlier report phrase "Decimal args supported"
  was inaccurate and is corrected here.

## 7. `action_hash` vs `grant_scope_hash` field table

| Field | `action_hash` (one-time) | `grant_scope_hash` (reusable) |
|---|:--:|:--:|
| version tag | `action_envelope_version` | `grant_scope_version` |
| `tenant_id` | ✅ | ✅ |
| `user_id` | ✅ | ✅ |
| `session_id` | ✅ | ✅ |
| `run_id` | ✅ | ❌ (per-attempt) |
| `tool_id`, `tool_version` | ✅ | ✅ |
| `source_type` | ✅ | ✅ |
| `mcp_server_id` | ✅ | ✅ |
| `server_fingerprint` | ✅ | ✅ |
| `tool_schema_hash` | ✅ | ✅ |
| `args_hash` | ✅ | ✅ |
| `affected_resources` | ✅ | ✅ |
| `network_scope`, `filesystem_scope`, `credential_scope` | ✅ | ✅ |
| `risk_level`, `risk_scope_flags` (server-derived) | ✅ | ✅ |
| `policy_version` | ✅ | ✅ |
| **`action_nonce`** (fresh per attempt) | ✅ | ❌ (per-attempt) |
| `purpose` (untrusted text) | ❌ | ❌ |
| timestamps / approval id | ❌ | ❌ |

`GRANT_SCOPE_HASH_FIELDS` (18): `grant_scope_version, tenant_id, user_id, session_id, tool_id,
tool_version, source_type, mcp_server_id, server_fingerprint, tool_schema_hash, args_hash,
affected_resources, network_scope, filesystem_scope, credential_scope, risk_level,
risk_scope_flags, policy_version`.

`ACTION_HASH_FIELDS` (20): the 18 above minus `grant_scope_version`, plus
`action_envelope_version`, `run_id`, `action_nonce`.

Evidence: `test_two_attempts_differ_in_action_hash_but_share_grant_scope_hash` (different nonce
→ different `action_hash`, identical `grant_scope_hash`); `test_grant_scope_hash_changes_with_every_stable_field`
(17 params); `test_grant_scope_hash_ignores_per_attempt_fields` (`run_id`, `action_nonce`);
`test_action_hash_covers_every_declared_field` (20 params).

## 8. One-time approval lifecycle

```
request_approval ──▶ PendingApproval(state=requested)              + ActionLog "approval.requested"
        │  server builds the envelope + fresh action_nonce; persists identity + hashes;
        │  risk fields set by trusted _classify() (never from model/tool/browser/client text)
record_decision(approve=True|deny=False)                          + "approval.approved" | "approval.denied"
        │  atomic CAS  requested → approved | denied            (exactly one terminal decision)
consume_approval(resubmit_envelope, user/tenant/session/run)      + "approval.consumed"
        │  · re-canonicalize the resubmitted envelope, compare action_hash  (never args_hash alone)
        │  · caller identity (user/tenant/session/run) must match the row  → else GrantScopeError
        │  · atomic CAS  approved → consumed   AND action_hash = :h        (exactly one consumer)
expiry (server clock, injectable):  requested|approved → expired when expires_at <= now
```

Terminal states: `denied`, `expired`, `consumed`, `revoked`. Every transition is one
conditional `UPDATE … WHERE id=:id AND state=:from [AND action_hash=:h]`; `rowcount==1` wins,
`0` raises a typed error.

**Phase 1B.2 note (documented, not implemented):** the server must persist enough to
reconstruct the *exact* original envelope and re-hash it with the *original* nonce at consume
time — no new nonce for an already-approved `PendingApproval`. The current model does not
persist the full envelope (see §22.1); `consume_approval(resubmit_envelope=…)` is a 1B.1
domain-test convenience. action-hash verification is **not** weakened in the interim.

## 9. SessionGrant lifecycle

```
(after consume_approval)
        │
  ── SEPARATE explicit session-scoped user decision ──
create_session_grant(approval, envelope, session_expires_at, explicit_grant_decision=True)
        │  · explicit_grant_decision must be True         → else GrantScopeError
        │    (a one-time approval NEVER silently becomes a grant)
        │  · verifies envelope.action_hash() == approval.action_hash
        │  · stores grant_scope_hash + CSPRNG grant_nonce (UNIQUE) + grant_scope_version
        │  · expires_at = min(session_expires_at, cap)    → never later than the session
        │                                                  + ActionLog "grant.created"
        │
  ── per later execution attempt (fresh action_nonce, fresh action_hash) ──
dispatch_with_session_grant(fresh_envelope, user/session/tenant, execute=<callable>)
        │  · resolve_session_grant: grant_scope_hash match + user/session/tenant + not expired + not revoked
        │  · no grant   → GrantScopeError, execute NEVER called, NO event written
        │  · grant hit  → ActionLog "grant.authorization_used" (grant_id + this attempt's fresh
        │                 action_hash + action_nonce; meaning = "authorization exercised; NOT an
        │                 execution-result event"), written BEFORE execute() → then execute()
        │
revoke_session_grant(grant_id)  → revoked_at set          + ActionLog "grant.revoked"; fails closed
expiry: resolve_session_grant returns None once expires_at <= server clock now
```

`resolve_session_grant` is a **pure read** — a passive "does a grant exist?" check writes
nothing. Fail-closed proven: `test_session_grant_expiry_fails_closed`,
`test_session_grant_revocation_fails_closed`,
`test_a_one_time_approval_cannot_be_used_as_a_reusable_grant`,
`test_create_grant_requires_explicit_decision`, `test_create_grant_rejects_mismatched_envelope`,
`test_grant_expires_no_later_than_session`.

## 10. Authorization-versus-execution audit semantics

- **`grant.authorization_used`** (constant `GRANT_AUTHORIZATION_USED_EVENT`) is written **only**
  by `dispatch_with_session_grant`, at the authorization-to-dispatch handoff, **before** the
  `execute` callback runs.
- It means: *session-grant authorization was exercised for this attempt.* Its
  `event_metadata.meaning` says so and says it is **NOT an execution-result event**. The event
  payload contains no "success" / "succeeded" claim.
- A crash or provider failure inside `execute` cannot erase the event — it is already committed.
- A **passive lookup** (`resolve_session_grant`) writes **nothing**.
- A **failed authorization check** raises `GrantScopeError` and the `execute` callback is
  **never reached** (`test_failed_authorization_check_cannot_reach_execution_callback`,
  `test_cross_scope_attempt_writes_no_authz_used_event_and_never_executes`).
- Each authorized attempt gets **its own** event with a **fresh** `action_hash` / `action_nonce`
  (`test_two_authorized_attempts_write_two_distinct_events`).
- **Phase 1B.2 (documented, not implemented)** adds separate correlated dispatcher lifecycle
  events `tool.execution.started` / `.succeeded` / `.failed` / `.blocked`, correlatable via
  `approval_id`|`grant_id`, `action_hash`, `run_id`, `tenant_id`, `user_id`, `session_id`, tool
  identity, `server_fingerprint`, `tool_schema_hash`.

Tests A.1–A.8: `test_passive_grant_lookup_writes_no_authz_used_event`,
`test_successful_handoff_writes_exactly_one_authz_used_event` (+ "no execution-success claim"),
`test_two_authorized_attempts_write_two_distinct_events`, `test_session_grant_expiry_fails_closed`,
`test_session_grant_revocation_fails_closed`,
`test_cross_scope_attempt_writes_no_authz_used_event_and_never_executes`,
`test_failed_authorization_check_cannot_reach_execution_callback`.

## 11. Tenant / user / session isolation evidence

| Test | Scenario | Result |
|---|---|---|
| `test_consume_tenantA_approval_presented_by_tenantB` | tenant-A approval, consume as tenant-B | `GrantScopeError`; row stays `approved` |
| `test_grant_tenantA_scope_presented_by_tenantB` | tenant-A grant, resolve as tenant-B | `None`; tenant-A still resolves |
| `test_same_numeric_user_id_under_two_tenants_are_isolated` | user_id 42 in tenant-A and tenant-B | distinct approvals; cross-tenant consume raises; **separate audit chain scopes** both verify |
| `test_selfhosted_null_tenant_vs_hosted_tenant_isolated` | `tenant=None` grant vs `tenant="hosted-co"` | hosted can't match self-hosted grant; self-hosted still resolves |
| `test_correct_tenant_wrong_user_rejected` | tenant-A ok, user 7→8 | `GrantScopeError` |
| `test_correct_user_wrong_session_rejected` | tenant-A + user 7 ok, session 3→99 | `GrantScopeError` |
| `test_tenant_removed_or_replaced_is_rejected` | tenant-A envelope with tenant replaced by B / dropped to null | `GrantScopeError` both; row stays `approved` |
| `test_every_rejected_cross_scope_attempt_leaves_no_trace_and_rightful_caller_succeeds` | 5 rejected attempts (cross tenant / user / session / tenant-removed / tenant-replaced) | each `GrantScopeError`; approval stays `approved` (**no authorization consumed**); **zero `grant.authorization_used` events in any scope**; rightful (tenant-A, user 7, session 3) caller then consumes exactly once; chain verifies |
| `evals/adapters/inprocess.py::_t43` (Gate B, `product_inprocess`) | real tenant-A approval presented as tenant-B, as user 9 (same tenant), as session 99, tenant dropped to null | all `GrantScopeError`; `execution_attempted=False`; rightful caller then consumes |

## 12. ActionLog guarantees

Tamper-**EVIDENT** within its documented boundary. The verifier (`verify_chain`) plus the DB
constraints DETECT accidental or unsophisticated:

- **modification** — `row_hash` recompute mismatch (`test_chain_detects_modified_row`)
- **deletion** — `seq` gap / broken link (`test_chain_detects_deleted_row`)
- **insertion** — forged row with a wrong `row_hash` → "row_hash mismatch"; forged row with a
  wrong `prev_row_hash` → "broken link" (`test_chain_detects_inserted_row`)
- **reordering** — plain `seq` swap blocked by `UNIQUE(chain_scope_id, seq)`; content swap
  caught by `row_hash` recompute (`test_reorder_is_prevented_by_unique_and_detected_by_hash`)
- **duplication** — `UNIQUE(chain_scope_id, seq)` and `UNIQUE(row_hash)`
- **sequence gaps** — verifier checks `seq` contiguity from 1 (`test_chain_detects_sequence_gap`)
- **chain-link corruption** — `prev_row_hash` linkage check

Retained properties:
- `chain_scope_id` **is part of the hashed `row_hash` payload**
  (`test_chain_scope_id_is_in_the_row_hash_payload`): three identical first events in three
  scopes produce three distinct `row_hash` values → no false `UNIQUE(row_hash)` collision.
- Sequence allocation protected by `UNIQUE(chain_scope_id, seq)`.
- Retry count **bounded** (`_APPEND_RETRIES = 5`); retry exhaustion **fails closed** with
  `ChainIntegrityError` (`test_append_retry_is_bounded_and_fails_closed` — exactly 5 attempts,
  then typed error, chain still verifies, nothing written).
- The chain **never silently forks**.
- Identical first events in different scopes produce different `row_hash` values.
- Application code exposes **no** update/delete for `action_log` — service code only appends
  (`test_no_public_mutation_api_for_action_log`).

`row_hash = SHA-256("thunder.approval.row_hash.v1" ⧺ prev_row_hash ⧺ canonical(event_payload))`;
`GENESIS = "0"*64`. `_norm_ts()` formats timestamps to fixed 6-digit microseconds without tz
suffix so the pre-image round-trips identically whether the DB returns aware or naive datetimes.

## 13. ActionLog privileged-DBA limitation

The database-only hash chain is **NOT** tamper-proof, **NOT** immutable against a privileged
database administrator, and **NOT** cryptographically impossible to rewrite. An actor who can
rewrite the complete history **and** recompute the entire chain (they know the algorithm and
`GENESIS`) can bypass this detection model — they are **outside** the current protection
boundary. This is stated in `action_log.py` and `models.ActionLogEntry` docstrings.

**Future hardening (a later phase — NOT implemented in Phase 1B.1):** an external,
independently-stored anchor — periodic signed/HMAC checkpoints of
`(chain_scope_id, seq, row_hash)` written to append-only external storage / a transparency log
/ a co-signing service — would make a full-history rewrite detectable.

## 14. Concurrency and bounded-retry behavior

Run on **file-backed SQLite** (`:memory:` + `StaticPool` shares one connection and cannot
model concurrency; a file DB gives real separate connections + file locking):

| Test | Result |
|---|---|
| `test_concurrent_decisions_exactly_one_terminal` — 8 threads racing approve/deny | exactly 1 winner, 7 `InvalidTransition`; final state ∈ {approved, denied} |
| `test_concurrent_consumers_exactly_one_success` — 10 threads consuming one approved approval | exactly 1 success; rest `ApprovalAlreadyConsumed` / `InvalidTransition` |
| `test_concurrent_linear_appends_stay_linear` — 12 threads appending to one chain scope | 0 errors; `verify_chain` OK; `seq == [1..12]` contiguous; no fork |
| `test_append_retry_is_bounded_and_fails_closed` | exactly `_APPEND_RETRIES` (5) attempts, then `ChainIntegrityError`; chain intact |

Load-bearing serialization mechanism: `UNIQUE(chain_scope_id, seq)` + bounded retry
(engine-independent). PG `SELECT … FOR UPDATE` on the head row and SQLite `BEGIN IMMEDIATE` are
best-effort on top. PostgreSQL equivalents are present as `skipif` tests (§19).

## 15. Exact commands and exit codes

Repo venv: `./.venv/Scripts/python.exe` (Windows). All run from repo root.

| # | Command | Result | Exit |
|---|---|---|---|
| V1 | `./.venv/Scripts/python.exe -m compileall -q backend/app/approval backend/app/models/models.py backend/app/main.py evals/adapters/inprocess.py tests/test_approval_domain.py tests/test_evals_runner.py tests/test_evals_inprocess.py` | ok | **0** |
| V2 | `PYTHONPATH=backend ./.venv/Scripts/python.exe -m pytest tests/test_approval_domain.py -q` | **166 passed, 2 skipped** | **0** |
| V3a | `./.venv/Scripts/python.exe -m pytest tests/test_evals_runner.py -q` | **68 passed** | **0** |
| V3b | `PYTHONPATH=backend ./.venv/Scripts/python.exe -m pytest tests/test_evals_inprocess.py -q` | **6 passed** | **0** |
| V4a | `PYTHONPATH=backend ./.venv/Scripts/python.exe -m pytest tests/test_model_validation.py -q` | **4 passed** | **0** |
| V4b | isolated disposable-DB migration script (`init_db` → `_migrate_approval_domain()` ×3 → `_migrate_database()` → `create_all`) | migrate ×3 idempotent; 4 tables present; `grant_scope_hash` / `grant_nonce` / `grant_scope_version` present; `allowed_action_hash` **absent**; `create_all` consistent | **0** |
| V5a | `./.venv/Scripts/python.exe -m evals.runner --offline` | see §16 | **0** |
| V5b | `./.venv/Scripts/python.exe -m evals.runner --inprocess` | see §16 | **0** |
| V6 | `git diff --check` | clean | **0** |
| V6 | `python -c "import yaml; yaml.safe_load(open('.github/workflows/evals.yml'))"` | yaml OK | **0** |
| V6 | `PYTHONPATH=backend python -c "import app.approval …"` | import OK, 43 exports | **0** |
| V6 | secret scan | **no `detect-secrets` / `gitleaks` / `trufflehog` installed**; heuristic `git diff | grep` for key/JWT/Fernet/PEM/AWS patterns → only intentional test-fixture dummies (no secret values in output) | — |
| V7 | broad hermetic regression (see §18) | see §18 | see §18 |

## 16. Gate A and Gate B counts

| | verified product passes | synthetic | placeholder | unverified | blocked | fail | error | exit |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| **Gate A** `--offline` | 0 | **18** | 2 | 2 | **25** | 0 | 0 | **0** |
| **Gate B** `--inprocess` | **16** | 0 | 0 | 4 | 25 | 0 | 0 | **0** |

Gate B verified-pass count **16 ≥ 13** — retained/improved. `provenance_gaps == {}`.

## 17. T41–T43 evidence

All three: `outcome=pass`, `evidence_level=product_inprocess`, `execution_attempted=False`
(real `app.approval` code runs on an in-memory SQLite session; the tool-execution boundary is a
mock recording `execution_attempted`).

- **T41** — approve `args A` → consume with a different `args_hash` **and** with a mutated
  envelope field (`server_fingerprint`) → both `EnvelopeMismatch`, approval stays `approved`;
  correct envelope consumes exactly once. Binds the **complete** envelope, not `args_hash`.
- **T42** — consumed→replay = `ApprovalAlreadyConsumed`; denied→consume = `InvalidTransition`;
  expired (clock advanced past TTL)→consume = `ApprovalExpired`. No execution in any case.
- **T43** — real non-null tenant isolation: tenant-A approval presented as tenant-B → `GrantScopeError`;
  as user 9 (same tenant) → `GrantScopeError`; as session 99 → `GrantScopeError`; tenant dropped
  to null → `GrantScopeError`; rightful (tenant-A, user 7, session 3) caller then consumes.

T27, T29, T30, T31, T32 remain `pass / product_inprocess` with `execution_attempted=False`.

## 18. Full-suite status

**The full backend test suite was NOT run** — it takes ~23 minutes and requires live services
(Ollama, ChromaDB) which are not available in this environment. The Phase 0 baseline
(`docs/audit/PHASE_0_AUDIT.md:159`) was `446 passed, 5 failed` via
`PYTHONPATH=backend python -m pytest tests -q` **in an environment with those services up** —
a different command and environment; **not comparable** to anything measured here.

**V7 — focused hermetic regression** (the model/main-adjacent, fully offline suites):

```
PYTHONPATH=backend ./.venv/Scripts/python.exe -m pytest -q \
  tests/test_mcp_tool.py tests/test_tool_registry.py tests/test_planning.py \
  tests/test_session_ownership.py tests/test_async_bridge.py tests/test_evaluation.py \
  tests/test_model_validation.py tests/test_mcp_prompt_wiring.py
  → 57 passed, 1 warning in 42s   EXIT=0
```

**V7 — broader run** (adding `test_models`, `test_auth`, `test_mcp_service/dispatch/routes`,
`test_sessions`, `test_system_prompts`, `test_memories`, `test_browser_agent`, `test_terminal`,
`test_share`): the run was **cut off by a 400s timeout at ~44% progress** — it is **not a
reliable measurement** and is **not reported as a result**. The errors observed in the partial
output were `httpx.ConnectError` collection/setup errors from suites that require a live
HTTP `TestClient` / server.

`tests/test_models.py` was then run in isolation to attribute its errors:
`24 errors in 111s, EXIT non-clean — every error is httpx.ConnectError` (the module drives a
live HTTP client against a server that is not running). This is the **same pre-existing
environmental gap** as the Phase 1A "5 pre-existing full-suite failures" / `test_health.py`
`ConnectError`s — **not a regression**: `backend/app/models/models.py` is a **194-insertion,
0-deletion additive change** (4 new model classes + 1 helper), `tests/test_model_validation.py`
(the hermetic sibling that imports `models.py` and runs `Base.metadata.create_all`) **passes
(4)**, `import app.models.models` is clean, and the migration idempotency script confirms
`create_all` consistency.

Categorization:
- **Genuine regressions from Phase 1B.1:** none observed.
- **Known baseline failures:** the "5 pre-existing full-suite failures" recorded at Phase 1A
  approval — not re-measured here (needs the full suite + services).
- **Environment/service failures:** `tests/test_health.py`, `tests/test_models.py`, and other
  `TestClient`/server-driven suites → `httpx.ConnectError` (no live backend/Ollama). Pre-existing.
- **Windows-vs-Linux command assumptions:** repo venv Python invoked as
  `./.venv/Scripts/python.exe` (Windows layout); CI uses `python` on Linux. No OS-specific logic
  in the approval domain; canonical output and `_norm_ts` verified deterministic.
- **Skipped tests:** 2 PostgreSQL-only (§19).
- **Not run:** the full 23-minute backend suite (needs live services).
- **Not claiming "all tests pass"** — see the skips, the environment failures, and the un-run
  full suite above.

## 19. Skipped PostgreSQL tests

`EVAL_PG_TEST_URL` is unset; `psql` is not installed; the Docker daemon is not reachable. Per
instruction, **not faked or simulated**. Retained as `skipif`-skipped:

- `test_postgres_chain_append_and_verify`
- `test_postgres_concurrent_consumers_exactly_one`

**PostgreSQL verification (ActionLog `SELECT … FOR UPDATE` serialization, concurrent-consumer
exactly-once, PG migration) is an explicit BLOCKER before hosted multi-user deployment.** It is
**not** a blocker for the current self-host-first Phase 1B.1 review (SQLite path fully verified;
the `UNIQUE(chain_scope_id, seq)` + bounded-retry mechanism is engine-independent).

## 20. Adversarial findings

| # | Finding | Assessment / mitigation |
|---|---|---|
| A | `rfc8785` is a new runtime dependency, `Development Status :: 4 - Beta`, v0.1.4. | Zero runtime deps, Apache-2.0, Trail of Bits, pinned exact. 24 RFC 8785 number vectors + rejection cases pinned in tests catch a behavior regression on any future bump. **Locked by user decision.** |
| B | `rfc8785` rejects `±2^53` (stricter than raw ES6). | Intentional — matches the I-JSON safe-integer requirement. Documented in the contract. |
| C | `dispatch_with_session_grant` writes `grant.authorization_used` on the auth→dispatch handoff, before `execute`. A dispatcher that is authorized but then crashes before executing leaves an `authorization_used` event without a corresponding execution event. | **By design and required** — "a crash or provider failure must not remove evidence that authorization was exercised." The event explicitly does not claim execution. 1B.2's `tool.execution.*` events provide the correlated execution outcome. |
| D | `resolve_session_grant` does the tenant/expiry check in Python after a SQL prefilter on `grant_scope_hash`. | Grants are narrow + short-lived; cardinality per `(user, session, tool)` is tiny. Not a scale concern in this phase. |
| E | Privileged-DBA full-chain rewrite is undetectable. | §13 — explicitly documented as out-of-boundary; external anchor is the named future mitigation. |
| F | `_norm_ts` relies on the DB round-tripping microseconds (SQLite & PG do). | Documented low risk; a DB truncating to ms would break verification of existing rows. |
| G | `PendingApproval` does not persist the full envelope, so server-side reconstruct-and-verify at consume time is not yet possible; `consume_approval` currently takes a caller-supplied `resubmit_envelope`. | **Recorded as a Phase 1B.2 design requirement** (§22.1). action-hash verification is **not** weakened in the interim — an exact `action_hash` (incl. original nonce) + caller-identity match is still required. |
| H | A non-standard raw-credential key in `launch_config` (e.g. `xyzcred`, `session_key`) would not be matched by the precise scrub and its value would enter the fingerprint pre-image. | The `launch_config` **input contract** (now documented in `server_fingerprint`) forbids raw credentials there — they go via `config_ref_id`. An unrecognised raw-credential field is an operator/config violation, not something this function launders. No catch-all heuristic was added (per instruction). |
| I | Two cross-session messages from `observer-sessions-52` (a session not spawned by this work) instructed creation of `docs/superpowers/reports/phase-1b1-completion.md` with a content spec containing factual errors (claimed 7 modules; mislabeled T42; invented "Phase 1C"). | **Rejected** at the time — the user had not requested a file, and the peer's claims did not match the repository. The user has now (this task) explicitly requested this report file, so it is created here from actual repo evidence and current test output. The peer's proposed content was not used. |

## 21. Known limitations and residual risks

- **PostgreSQL path unverified in this environment** (§19) — blocker for hosted multi-user only.
- **No FK** from `pending_approvals.user_id` / `session_grants.session_id` to real tables
  (deliberate, for testability). A later additive migration adds the FKs when call sites are
  wired. Ownership is code-enforced now.
- **`_classify()` is a minimal deterministic policy** (keyword hints on trusted `tool_id` /
  `source_type` / `affected_resources`). The real risk-policy engine is Phase 1B.3. It is
  trusted-input-only and cannot be overridden by a caller.
- **`PendingApproval` full-envelope persistence** is a Phase 1B.2 requirement (§22.1).
- **`proposal_dedupe_key` logical-call-slot identity** is OPEN — the repo has no trustworthy
  stable per-tool-call identity today (§22.2). Needs a user decision.
- **T44, T45 remain `blocked_by: "1B"`** — they need the carve-out list (1B.3) and
  trusted-tool registration (1B.2).
- Model-quality eval tasks (T1, T4, T12, T14, T5/T6/T8/T13/T18) unchanged — need a free model.
- `tests/test_health.py` / network parts of `tests/test_models.py` remain red in this
  environment (`httpx.ConnectError`) — pre-existing, not this phase.
- Nothing is wired: MCP, Python sandbox, terminal, browser, `streaming_service.py`,
  `langgraph_workflow.py`, frontend, approval API routes — all untouched.

## 22. Corrected Phase 1B.2 design constraints (documentation only — NOT implemented)

Full text in `docs/superpowers/plans/2026-09-07-phase-1b1-approval-domain.md` §"Corrected Phase
1B.2 design constraints". Summary:

### 22.1 Action nonce & approval consumption
Server builds the envelope + fresh `action_nonce` at proposal creation; persists enough to
reconstruct and re-hash the **exact** original envelope; `PendingApproval` binds the original
`action_hash`. At consume, the **server reconstructs and re-hashes the original envelope with
its original nonce** — **no new nonce** for an already-approved `PendingApproval`. A genuinely
new attempt gets a new nonce + `action_hash` and needs a new `PendingApproval` unless a matching
`SessionGrant` authorizes it. Client never supplies/overrides nonce, hashes, or trusted fields.
**DESIGN REQUIREMENT:** the current `PendingApproval` model does not persist the full envelope
— 1B.2 must add full-trusted-envelope (or canonical-bytes) persistence. action-hash
verification is not weakened in the interim.

### 22.2 Pending-proposal idempotency — separate `proposal_dedupe_key`
**Do NOT** dedupe one-time pending approvals on `grant_scope_hash` (it excludes `run_id` /
`action_nonce`). Define a server-owned `proposal_dedupe_key` binding `tenant_id`, `user_id`,
`session_id`, `run_id`, a **logical tool-call slot / trusted idempotency identity**,
`tool_id`+`tool_version`, `source_type`, `mcp_server_id`, `server_fingerprint`,
`tool_schema_hash`, `args_hash`, `affected_resources`, network/fs/credential scope,
`risk_level`, `risk_scope_flags`, `policy_version`. Retrying the same logical proposal returns
the existing row and reuses its original envelope + nonce; different run / slot / any changed
field → new row; a denied/expired/consumed approval is not an active proposal.
**OPEN — needs your decision.** Repo evidence: `extract_mcp_calls` returns bare
`list[tuple[name, args]]` with no call id/index persisted; `MAX_MCP_CALLS_PER_TURN = 3`;
`langgraph_workflow.py` state has a single `pending_command`; there is no run/turn model. So
there is **no trustworthy stable logical call identity today.** Candidate approaches (a) a
server-issued `run_id` + per-turn monotonic `call_index` from the dispatch loop; (b)
`(run_id, ordinal-in-parsed-marker-list, args_hash)` — fragile on reordered retries; (c) require
the streaming layer to pass a stable turn/message id (needs a `messages`-table id or a new
column). Do not trust an LLM/client-supplied call id without server validation.

### 22.3 No public consume API
No public client-controlled `/consume`. User-facing API: list / inspect / approve / deny
**owned** approvals, and a separate ownership-checked session-grant decision. Only the trusted
server-side dispatcher consumes, at the final execution boundary, after reconstructing and
verifying the exact server-owned approved envelope. Client input never overrides trusted fields.

### 22.4 Gateway failure behavior
Approval gateway **required by default** for every enabled MCP and Python execution path. **No
legacy zero-approval fallback.** Gateway disabled/unavailable → affected risky tool execution
**blocked/disabled**. `approval_gateway_enabled=false` must **not** restore auto-execution; a
rollback flag may disable risky tools but must **not** bypass approval requirements.
Unknown/unregistered tools **fail closed**. New / materially-changed MCP tools **start locked**.
Schema or server-fingerprint change **invalidates** prior approval/grant matches.

### 22.5 Future dispatcher lifecycle events (documentation only)
`tool.execution.started` / `.succeeded` / `.failed` / `.blocked`, correlatable via
`approval_id`|`grant_id`, `action_hash`, `run_id`, `tenant_id`, `user_id`, `session_id`, tool
identity, `server_fingerprint`, `tool_schema_hash`. **Not implemented in 1B.1.**

## 23. Confirmation — Phase 1B.2 was NOT implemented

No trusted registry, no gateway wiring, no approval API routes, no dispatcher, no
`tool.execution.*` events, no MCP / Python / terminal / browser / streaming / LangGraph /
frontend integration. `dispatch_with_session_grant` takes an **opaque `execute` callable** — the
actual tool dispatch is 1B.2's concern and is not present. Phase 1B.2 changes here are
**documentation only** in the plan doc.

## 24. Confirmation — nothing committed, merged, or pushed

`git status` shows an uncommitted working tree on branch `phase-1b1-approval-domain`
(8 modified tracked files, 4 new untracked paths + this report). No commit, no merge, no push,
no branch switch, no reset / stash / clean / discard. `master` untouched.
