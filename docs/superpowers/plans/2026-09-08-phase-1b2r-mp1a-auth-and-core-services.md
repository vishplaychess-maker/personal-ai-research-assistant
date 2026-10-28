# Phase 1B.2R — MP-1A implementation plan

**Auth session binding + RunEngine core transactional services**

**Date:** 2026-09-08
**Branch:** `phase-1b1-approval-domain` (HEAD `b2e6718`) — all Phase 1B work uncommitted.
**Predecessors:** 1B.1 approval domain, 1B.2 gateway, 1B.2R architecture (D1–D10), 1B.2R MP-0 schema foundation — all implemented, uncommitted, awaiting review.
**Scope gate:** MP-1A implements ONLY items 1–9 below. Everything in "Explicitly excluded" is out.

---

## 0. Stage-0 evidence (verified against the repo, not the PRD)

| Question | Finding |
|---|---|
| Branch / HEAD / merge-base | `phase-1b1-approval-domain`, HEAD `b2e67183`, `git diff --check` clean. `master` is a stale divergent line (merge-base `a92e3eb`); not touched. Dirty tree = the uncommitted 1B.1/1B.2/1B.2R-MP-0 work. |
| Do access tokens carry a family/session claim? | **No.** `create_access_token` mints `{sub, exp, iat}` only (`auth_service.py:59-64`). `decode_access_token` returns the full payload and does **no** claim-specific validation. `get_current_user` reads only `sub`. |
| Does adding a signed claim break contracts? | **No.** `create_access_token(data: dict)` merges a caller-controlled dict, so an extra key is additive. No test asserts the JWT claim set (`tests/test_auth.py` checks only that `access_token` is present and `/me` works). The frontend treats `access_token` as an opaque bearer string. `tests/test_auth.py:1681` unpacks `rotate_refresh_token` as a 2-tuple → its return arity must not change. |
| When does `family_id` become available in login / register / refresh? | **login:** `create_refresh_session` (`refresh_token_service.py:54`) generates `family_id` internally but returns only `raw_token`. Login currently mints the access token at step 7 **before** the refresh session at step 8 (`routes/auth.py:279` vs `:285`) — ordering must be swapped. **refresh:** `rotate_refresh_token` (`:97`) preserves `session.family_id` across rotation but returns `(new_raw_token, user_id)` only. **register:** creates no token and no refresh session (`routes/auth.py:110-171`) — nothing to bind; the user logs in separately. |
| Is `family_id` stable across rotation? | **Yes.** `rotate_refresh_token` builds `new_session` with `family_id=session.family_id` (`:164`). |
| Family revocation / reuse-detection mechanics | `revoke_refresh_session` / `revoke_all_user_sessions` set `revoked_at`. Reuse of a revoked token → `_revoke_family(db, family_id)` sets `revoked_at` on **every** row of the family (`:138`), then 401. `RefreshSession.revoked_at IS NULL` == active; `expires_at` bounds lifetime. |
| Can one user have multiple active families? | **Yes.** Every `create_refresh_session` mints a fresh `family_id`; `max_active_sessions` default 10. A run binds to exactly one family (its login session). |
| Do auth deps expose only `User`? | **Yes.** `get_current_user -> User`; `get_optional_user` falls back to default user id=1 (Phase 6B backcompat). No richer context object exists. |
| `session_ref_is_active` validator | Already built in MP-0 (`run_engine/session_binding.py`): fail-closed, `None`/unknown/revoked/expired → `False`. Reused unchanged. |
| Idempotency scope / DB authority | `runs.idempotency_key VARCHAR(80)`, `runs.idempotency_identity_hash VARCHAR(64)`, partial-UNIQUE `uq_runs_idem_scope` on `(COALESCE(tenant_id,''), user_id, COALESCE(session_id,-1), idempotency_key) WHERE idempotency_key IS NOT NULL` — already created by `_migrate_run_engine` (`main.py:844`, `_RUN_ENGINE_PARTIAL_UNIQUE`). |
| Event seq / signal-live authority | `UNIQUE(run_id, run_seq)` (`uq_run_events_run_seq`), `runs.last_event_seq INT DEFAULT 0`, partial-UNIQUE `uq_run_signals_live` on `run_signals(run_id, kind) WHERE consumed_at IS NULL` — already created in MP-0. |
| State machine | `run_engine/state_machine.py` (pure) — `RUN_TRANSITIONS`, `RUN_TERMINAL`, `assert_run_transition`, `race_winner` (cancel wins), `idempotent_decision_state`. Reused unchanged. |
| Migrations needed? | **None.** All 5 tables, all columns, all partial-unique indexes exist. `sid` claim needs no schema. |
| Architecture conflict? | **None.** D5 explicitly names this the "MP-1 auth seam"; D3 idempotency scope matches; state machine matches. Proceed. |

**Stop-condition sweep:** (1) `fam`/`sid` claim does not break clients — safe. (2) `family_id` stable across rotation — yes. (3) issuer can obtain a trusted family id — yes, via a login reorder + one indexed lookup helper (no arity change). (4) submission identity fields — the **uniqueness scope** `(tenant_id, user_id, session_id, idempotency_key)` is fully specified by the prompt and already indexed; `idempotency_identity_hash` composition is a design choice recorded in §4 below (identity tuple + `kind` + `thread_id` + optional caller-supplied non-secret request fingerprint — never the raw prompt alone). Not materially unspecified. (5) active-run product cap — **not in MP-1A scope**; §24 of the architecture doc assigns per-user run caps to MP-6. The three MP-1A safety limits (idempotency-key length, event-read page size, control-event serialized size) have conservative repo-evidenced defaults (§7). No blocker.

---

## 1. Server-authenticated refresh-family binding

- **Claim:** add `sid` (OpenID-Connect-style session identifier; RFC 7519 private claim) = the login session's `refresh_sessions.family_id`. Never a token, token hash, cookie, header, API key, or user-controlled id.
- **`refresh_token_service.py`** — add `family_id_for_raw_token(db, raw_token) -> str | None`: one indexed `SELECT family_id FROM refresh_sessions WHERE token_hash = sha256(raw_token)`. No change to `create_refresh_session` / `rotate_refresh_token` return arity.
- **`routes/auth.py` login** — reorder: create the refresh session first, resolve its `family_id`, then mint the access token with `data={"sub": ..., "sid": family_id}`. One family per login (unchanged — `create_refresh_session` already mints exactly one).
- **`routes/auth.py` refresh** — after `rotate_refresh_token`, resolve `family_id` of the new token (family is stable) and mint the new access token with the same `sid`.
- A legacy access token with no `sid` claim still authenticates (`get_current_user` unchanged) but yields `durable_session_binding_valid = False` → every RunEngine mutation path rejects it. No content-derived or user-id-derived fallback.
- Untouched: JWT verify, refresh rotation, reuse detection, logout, `_revoke_family`, CSRF.

## 2. Fail-closed active-session validation

- `AuthContext.durable_session_binding_valid = bool(sid) and session_ref_is_active(db, sid)`.
- `_require_active_binding(db, ctx)` raises `SessionBindingRequired` ("fresh authentication / session binding required") — called at the top of every mutation path: `submit_run`, `transition_run`, `append_control_event`, `request_cancellation`.
- Revoke-between-validation-and-commit: conservative — the transaction still commits (the run row is written), but the binding is re-checked on the **next** mutation, which then fails closed. A worker-side re-check at dispatch/resume time is MP-1B/MP-2. Recorded as a known limitation.

## 3. AuthContext seam — `backend/app/run_engine/context.py` (new)

```
@dataclass(frozen=True)
class AuthContext:
    user: User
    user_id: int
    tenant_id: str | None                 # normalized (strip/lower); always None today — no tenant source in the repo
    auth_session_ref: str | None          # the 'sid' claim (a refresh_sessions.family_id)
    durable_session_binding_valid: bool

build_auth_context(db, *, user, token_payload) -> AuthContext
get_auth_context(credentials=Depends(bearer_scheme), db=Depends(get_db)) -> AuthContext   # dep; no route consumes it in MP-1A
```
- `auth_service.py` — extract `resolve_user_from_payload(payload, db) -> User` (the `sub → int → User` block currently duplicated in `get_current_user` and `_resolve_user_id`); both reuse it; `get_auth_context` reuses it.

## 4. Scoped idempotent run submission — `engine.submit_run`

- Scope tuple = `(normalized tenant_id, user_id, session_id, normalized idempotency_key)`. Server generates `run_id = uuid4()`. `thread_id = thread_id or f"run:{run_id}"`.
- `idempotency_identity_hash = sha256(canonicalize({v:1, tenant_id, user_id, session_id, idempotency_key, kind, thread_id, request_fingerprint}))` via `app.approval.canonical.canonicalize`. `request_fingerprint` is an optional caller-supplied non-secret digest (e.g. `sha256(normalized_prompt)`), folded in only when present — never the raw prompt, never the prompt hash alone.
- Algorithm (bounded retry, 3 attempts):
  1. `_require_active_binding`.
  2. Normalize + length-check the key → `IdempotencyKeyTooLong` if `> run_idempotency_key_max_bytes`.
  3. `SELECT` by idem scope. Found + identity hash equal → **return it** (replay: no new run, no new `run.created` event). Found + identity hash differs → `IdempotencyKeyConflict(run_id=existing.id)`.
  4. Else `INSERT` the run (`status="created"`, `auth_session_ref=ctx.auth_session_ref`, `version=0`, `last_event_seq=0`), `flush`. On `IntegrityError` whose constraint is exactly `uq_runs_idem_scope` → `rollback`, loop (a racing request won); any other `IntegrityError` re-raises (no broad catch).
  5. Append the initial `run.created` control event **in the same transaction** (`run_seq=1`, `last_event_seq=1` set directly — the row is txn-local, no CAS needed), `commit`.
  6. Retries exhausted → `RunSubmitContention`.
- DB uniqueness is the final authority; the pre-`SELECT` is an optimization, not the guard.

## 5. Optimistic concurrency / state transitions — `engine.transition_run`

- `transition_run(db, ctx, run_id, *, to, expected_version, status_reason=None, allow_reconcile=False) -> Run`.
- `_require_active_binding`; load owner-scoped run (`RunNotFound` if absent / not owned).
- Terminal source: `to == status` → return unchanged (idempotent); else `RunAlreadyTerminal(state)`.
- `assert_run_transition(frm, to, allow_reconcile=...)` (pure MP-0 machine) — `InvalidRunTransition` propagates.
- Conditional `UPDATE runs SET status=:to, status_reason=:r, version=version+1, updated_at=now, <started_at/finished_at as appropriate> WHERE id=:id AND version=:expected_version AND status=:frm`. Version increments **once**, inside this single write.
- `rowcount != 1` → `rollback`, re-read: if the run is now `cancelling`/`cancelled` and `to` is not a cancel state → `CancellationInProgress` (cancellation wins races); else `RunStateConflict(expected_version, actual_version)`.
- No in-memory locks. No worker transitions (there is no worker in MP-1A).

## 6. Durable monotonic control-event primitives — `backend/app/run_engine/events.py` (new)

- `CONTROL_EVENT_TYPES` — closed allowlist (lifecycle + cancel + status/error): `run.created`, `run.queued`, `run.running`, `run.waiting_for_approval`, `run.approved`, `run.denied`, `run.resuming`, `run.cancelling`, `run.cancelled`, `run.succeeded`, `run.failed`, `run.timed_out`, `run.outcome_unknown`, `run.cancel.requested`, `run.status`, `run.error`.
- `_sanitize_event_data(data)` — keys must be `str`; values `str | int | float | bool | None` only (no nested dict/list); serialized `json.dumps(sort_keys, separators)`; reject if encoded length `> run_control_event_max_bytes` → `ControlEventTooLarge`. No secrets, no raw tool output, no args/headers.
- `append_control_event(db, ctx, run_id, *, type, data=None) -> RunEvent` — `_require_active_binding`; disallowed type → `DisallowedControlEventType`; bounded retry (5):
  - read owner-scoped run; `new_seq = last_event_seq + 1`;
  - `UPDATE runs SET last_event_seq=:new WHERE id=:id AND last_event_seq=:cur` (CAS); rowcount 0 → `rollback`, retry;
  - `INSERT run_events (run_id, run_seq=:new, category='control', type, data)`; `commit`;
  - `commit` raises `IntegrityError` on `uq_run_events_run_seq` → `rollback` (which also reverts the `last_event_seq` bump — the two writes are one transaction, so a failed insert never permanently advances the high-water mark), retry;
  - retries exhausted → `ControlEventContention`.
- `read_control_events(db, ctx, run_id, *, after_seq=0, limit=None) -> list[RunEvent]` — owner-scoped; `WHERE run_id AND run_seq > after_seq ORDER BY run_seq ASC`; `limit` clamped to `run_event_read_max_page` (also the default). No SSE, no polling route, no text-category rows.

## 7. Cancellation-request signal — `engine.request_cancellation`

- `request_cancellation(db, ctx, run_id, *, reason_code=None) -> CancellationResult`.
- `_require_active_binding`; load run; owner check (`run.user_id == ctx.user_id` and tenant match) else `RunNotFound` (non-disclosing); if `run.auth_session_ref` and `ctx.auth_session_ref` are both set and differ → `SessionBindingMismatch`.
- Terminal run → `CancellationResult(status=run.status, signal_created=False, already_terminal=True)` — **no live signal, run not mutated, no control event**.
- Else look for a live `cancel_requested` signal (`consumed_at IS NULL`). Present → `CancellationResult(signal_created=False, already_requested=True)` (idempotent).
- Else `INSERT run_signals(kind='cancel_requested', payload=build_signal_payload('cancel_requested', reason_code, server_ts), created_by_user_id=ctx.user_id, created_by_tenant_id=ctx.tenant_id)`, `flush`. `IntegrityError` on `uq_run_signals_live` → `rollback`, return the idempotent `already_requested` result (a racer created it).
- On the branch that actually created the signal: append `run.cancel.requested` **once** (CAS append, same transaction), `commit`.
- Never sets `consumed_at`; never sets `run.status = cancelled`. The worker (MP-1B+) consumes the signal and drives the state machine.

## 8. Config — `backend/app/config.py`

| Setting | Default | Repo evidence |
|---|---|---|
| `run_idempotency_key_max_bytes` | `80` | `runs.idempotency_key VARCHAR(80)` |
| `run_control_event_max_bytes` | `4096` | matches `run_event_text_flush_bytes`; `run_signals` payload cap is 512 chars |
| `run_event_read_max_page` | `200` | conservative bounded page; smaller than any batch byte budget |

All three added to `_validate_approval_limits` (`>= 1`, client cannot raise or disable).

## 9. Typed errors — `backend/app/run_engine/errors.py` (extend)

`SessionBindingRequired`, `SessionBindingMismatch`, `RunNotFound`, `IdempotencyKeyConflict(run_id)`, `IdempotencyKeyTooLong`, `RunSubmitContention`, `RunStateConflict(expected_version, actual_version)`, `RunAlreadyTerminal(state)`, `CancellationInProgress`, `DisallowedControlEventType(type)`, `ControlEventTooLarge`, `ControlEventContention` — all under `RunEngineError`, none carrying secret material.

## 10. DB-error helper — `backend/app/run_engine/_dberr.py` (new)

`constraint_violation(exc: IntegrityError, name: str) -> bool` — SQLite: `name` substring in `str(exc.orig)` (partial/expression indexes surface the index name); PostgreSQL: `getattr(exc.orig, "diag", None)` → `constraint_name == name`. Used so no path does a broad `except IntegrityError`.

---

## Files

**New:** `run_engine/context.py`, `run_engine/engine.py`, `run_engine/events.py`, `run_engine/_dberr.py`.
**Edit:** `run_engine/errors.py`, `run_engine/__init__.py`, `services/refresh_token_service.py`, `services/auth_service.py`, `routes/auth.py`, `config.py`.
**Docs:** architecture plan (add §28 MP-1A as-built), `THUNDER_AI_PRD.md` §Status, new `docs/superpowers/reports/phase-1b2r-mp1a-completion.md`.
**Migrations:** none.

## Tests (`tests/`)

- `test_run_engine_auth_context.py` — `sid` present after login + after refresh; stable across refresh; `build_auth_context` valid / missing-sid / revoked-family / expired-family; legacy (no `sid`) token → binding invalid.
- `test_run_engine_submit.py` — new run (server `run_id`, `status=created`, `auth_session_ref` set, one `run.created` event); replay (same key+identity) → same run, still exactly one event, no new step; same key + different identity → `IdempotencyKeyConflict`; N-thread concurrent submit on SQLite → exactly one run row; key `> 80` bytes → `IdempotencyKeyTooLong`; no active binding → `SessionBindingRequired`; unrelated `IntegrityError` not swallowed.
- `test_run_engine_transitions.py` — legal CAS bumps version by exactly 1; stale `expected_version` → `RunStateConflict`; terminal source protected; `to == terminal state` idempotent; concurrent cancel vs advance → `CancellationInProgress`.
- `test_run_engine_events.py` — allowlisted append; disallowed type → `DisallowedControlEventType`; seq strictly increasing, no gaps; forced `(run_id, run_seq)` collision → `last_event_seq` unchanged after failure; `read_control_events` ascending + `after_seq`; page clamp to `run_event_read_max_page`; oversize `data` → `ControlEventTooLarge`.
- `test_run_engine_cancellation.py` — first call creates one signal + one `run.cancel.requested` event; duplicate → idempotent, no 2nd signal/event, signal not consumed, `run.status` untouched; terminal run → deterministic result, no signal, run unchanged; cross-user → `RunNotFound`; `sid` mismatch → `SessionBindingMismatch`.
- Regression: `tests/test_auth.py`, `tests/test_run_engine_*.py` (MP-0), import `app.main`.

## Explicitly excluded (unchanged from the MP-1A prompt)

Background worker loop, lifespan worker startup, run/step claiming, lease acquire/renew, expired-lease recovery, tool/MCP execution, approval resume, `/messages/stream` changes, public Run API routes, SSE/reconnect, text-event buffering, browser/desktop/Python actions, scheduler integration, frontend changes.

---

## Correction round 2 (2026-09-08) — supersedes §2, §4, §6 above where they conflict

**Status after this round: BLOCKED — DECISION REQUIRED** (see report §12.5 / §12.7).

### Fresh DB-backed session validation (replaces the cached-boolean check)

`session_binding.validate_session_binding(db, ctx, *, run=None)` is the authority. Runs inside
each operation's own transaction; `AuthContext.durable_session_binding_valid` is diagnostic only.

Proves, live: `sid` present; a `refresh_sessions` row for that `family_id` **AND**
`user_id == ctx.user_id`, not revoked, not expired (owner-constrained — never a family-id-only
query); when a run is given, `run.user_id`/tenant match (else `RunNotFound`) and — STRICT default
`_SESSION_BOUND_RUN_ACCESS = True` — `run.auth_session_ref == ctx.auth_session_ref` (else
`SessionBindingMismatch`).

Call sites: `submit_run` (entry, replay return, race-recovery return, pre-commit);
`get_run` / `read_control_events` (run-bound); `append_control_event` / `transition_run` /
`request_cancellation` (run-bound + pre-commit; `transition_run` also re-validates the
freshly-loaded row after commit).

### Closed per-event schema registry (replaces the "non-secret scalar" sanitizer)

`events.CONTROL_EVENT_SCHEMAS` — 5 types, each with exact required/optional keys and a closed
enum per field: `run.created{kind}`, `run.queued{}`, `run.status{from_status,to_status}`,
`run.cancel.requested{reason_code?}`, `run.error{error_code}`. `validate_event_data` rejects
unknown type/key, missing required, nested values, `bool`-for-enum, out-of-enum, oversize.
No free-form strings/numbers/prompts/args/output/exception-text/URLs/credentials/client ids.

### Identity tag — structural only (semantic HMAC is a pending decision)

`_identity_hash` = `sha256(RFC-8785({v:"structural-1", tenant/user/session/key/kind/thread_id}))`.
`request_fingerprint` parameter removed (a client value is never an identity input). A
server-keyed semantic HMAC over a canonical validated submission needs decisions **5A** (key
source) and **5B** (submission field contract) — see report §12.5.

### Login orphan cleanup

`routes/auth.py` login wraps `sid` resolution + token signing in `try/except`: on failure
`revoke_refresh_session(db, user, refresh_token)` then re-raise.

### Pending decisions (5A / 5B / 5C)

Enumerated in architecture-plan §28.8 and report §12.5 / §12.7. Do not proceed to "ready"
without them.
