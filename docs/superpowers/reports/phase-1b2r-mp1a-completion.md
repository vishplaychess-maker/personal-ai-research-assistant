# Phase 1B.2R — MP-1A completion report

**Auth session binding + RunEngine core transactional services**

**Date:** 2026-09-08 (correction round 2) · 2026-09-09 (decisions resolved)
**Branch:** `phase-1b1-approval-domain` · HEAD `b2e67183` · all Phase 1B work **uncommitted**
**Status:** **APPROVED FOR PROGRESSION** (uncommitted, not production-ready). The confirmed
auth-validation, event-schema, auth-lifecycle, atomicity and verification-evidence gaps are
corrected (see §12). The three pending decisions were resolved by the user on 2026-09-09 — see
the box below; only the doc / config-status updates they require were applied and the 613-test
MP-1A regression was **not** re-run.
**Not** "MP-1 done" — MP-1B (RunWorker + claim / lease + recovery) was implemented separately;
see `docs/superpowers/reports/phase-1b2r-mp1b-completion.md`.

> **Decisions 5A / 5B / 5C — RESOLVED (2026-09-09, user).**
> **5A** semantic-identity HMAC key → a dedicated `RUN_IDEMPOTENCY_HMAC_KEY` (never `JWT_SECRET`
> / `ENCRYPTION_KEY`; server-side; no hardcoded default; fails closed when the semantic / public
> Run path is enabled without it). Not implemented in MP-1B — no canonical public submission
> contract exists yet.
> **5B** canonical semantic-submission identity + its server-computed HMAC → **deferred to MP-3
> public route wiring**. Until then the structural tag is internal-only and documented
> incomplete; no public Run submission endpoint ships; no client fingerprint is authoritative;
> MP-3 must cover every behavior-changing request field with no raw prompt content and no
> unkeyed prompt hash stored.
> **5C** same-user cross-session run visibility → **STRICT origin-session binding confirmed**
> (`_SESSION_BOUND_RUN_ACCESS = True` stays). Cross-device transfer / adoption is deferred to a
> separately approved explicit re-authentication flow; the policy is not silently weakened.
**Plan:** `docs/superpowers/plans/2026-09-08-phase-1b2r-mp1a-auth-and-core-services.md`
**Architecture:** `docs/superpowers/plans/2026-09-08-phase-1b2r-durable-run-resume-architecture.md` §28

> **§12 (correction round 2) is the current, authoritative section.** §1–§11 below describe the
> first pass and are kept for history; where they conflict with §12, §12 wins.

---

## 1. Scope delivered (9 of 9)

1. **Server-authenticated refresh-family binding** — access token carries `sid` = the login
   session's `refresh_sessions.family_id`. Login reordered (refresh session first); refresh
   re-binds the same (stable) family.
2. **Fail-closed active-session validation** — `_require_active_binding(ctx)` on every mutation
   (`submit_run`, `transition_run`, `append_control_event`, `request_cancellation`), backed by
   the unchanged MP-0 `session_ref_is_active`.
3. **Core RunEngine services** in a new layer `backend/app/run_engine/{context,engine,events,
   _dberr}.py` — not in a route, `streaming_service`, or a model method.
4. **Scoped idempotent run submission** — scope `(tenant_id, user_id, session_id,
   idempotency_key)`; server-generated `run_id`; `uq_runs_idem_scope` is the final authority;
   bounded retry + read-after-conflict; exact-constraint matching (no broad `IntegrityError`
   catch); no duplicate `run.created` event / step on replay.
5. **Optimistic concurrency / transitions** — pure MP-0 state machine + `runs.version` CAS in
   one conditional `UPDATE`; version bumps once; stale writer → `RunStateConflict`; terminal
   protected; cancellation wins races.
6. **Durable monotonic control-event primitives** — `run_seq` strictly increasing per run;
   `(run_id, run_seq)` uniqueness is the authority; seq alloc + `runs.last_event_seq` bump +
   insert in one transaction, so a failed insert never permanently advances the mark; ascending
   read with a config-bounded page; allowlisted types + sanitized non-secret scalar data; no
   SSE/polling route; no text chunks.
7. **Cancellation-request signal at the service layer only** — owner + session-family check;
   idempotent (live-signal uniqueness = `uq_run_signals_live`); terminal run → deterministic
   idempotent result, no live signal, no mutation; does **not** mark the run cancelled; does
   **not** consume the signal; records `run.cancel.requested` once.
8. **Migrations** — none required (verified: all tables/columns/indexes exist from MP-0).
9. **Tests + documentation** — 42 new tests; this report; architecture §28; PRD §Status.

Everything on the MP-1A exclusion list (worker loop, lifespan startup, claiming, lease,
expired-lease recovery, tool/MCP execution, approval resume, `/messages/stream`, public Run API
routes, SSE/reconnect, text buffering, browser/desktop/python actions, scheduler, frontend) is
**untouched**.

## 2. Stage-0 evidence (verified against the repo)

| Item | Finding |
|---|---|
| Access token claims | `{sub, exp, iat}` only (`auth_service.create_access_token:59-64`). |
| Adding a signed claim | Safe — `data` dict is caller-merged; no test asserts the claim set; frontend treats the token as opaque; `decode_access_token` does no claim-specific checks. |
| `family_id` availability | login mints the token *before* the refresh session (`routes/auth.py:279` vs `:285`) → reordered. `create_refresh_session` / `rotate_refresh_token` return only `(…)` / `(raw, uid)` → a read-only `family_id_for_raw_token` helper added, no arity change (a test unpacks `rotate_refresh_token` as a 2-tuple). register creates no token/session. |
| `family_id` stability | Stable across rotation (`rotate_refresh_token` reuses `session.family_id`). |
| Revocation / reuse | `_revoke_family` sets `revoked_at` on every family row; `revoked_at IS NULL` == active; `expires_at` bounds it. |
| Multiple active families | Yes — one per login (`max_active_sessions` = 10). A run binds to one. |
| Auth deps | `get_current_user -> User` only; no richer context existed. |
| Idempotency scope / authority | `uq_runs_idem_scope` partial index already created by `_migrate_run_engine`. |
| Architecture conflict | None — D5 names this the "MP-1 auth seam"; D3 scope matches; state machine matches. |

**Stop-condition sweep:** none triggered. `sid` claim is client-safe; `family_id` is stable;
the issuer can obtain a trusted family id via a login reorder + one indexed lookup;
`idempotency_identity_hash` composition is specified in §3 (identity tuple + `kind` +
client-supplied `thread_id` + optional non-secret `request_fingerprint` — never the raw prompt);
no active-run product cap is in MP-1A scope (§24 assigns per-user caps to MP-6).

## 3. Design decisions

* **Claim name `sid`.** OpenID-Connect uses `sid` for a session identifier; RFC 7519 permits it
  as a private claim. It holds a `refresh_sessions.family_id` — never a token, hash, cookie,
  header, API key, or user-controlled id.
* **`idempotency_identity_hash`** = `sha256(RFC-8785-canonicalize({v:1, tenant_id, user_id,
  session_id, idempotency_key, kind, thread_id, request_fingerprint}))`. `thread_id` is the
  **client-supplied** value only (a server-derived `run:<id>` would make replay impossible).
  `request_fingerprint` is an optional caller digest (e.g. `sha256(normalized_prompt)`) folded
  in only when present — "never the raw prompt, never the prompt hash alone".
* **DB-error narrowing** (`_dberr.constraint_violation`) matches the exact constraint/index name
  on SQLite (message text — partial indexes surface the index name; the plain `run_events`
  UNIQUE surfaces its columns) *and* PostgreSQL (`.diag.constraint_name`). No path does
  `except IntegrityError:` broadly.
* **`AuthContext` dependency `get_auth_context`** is provided but unused by any MP-1A route —
  it is the seam MP-2 wires into `/messages/stream` and the approvals route.
* **`tenant_id`** is always `None` today (no tenant source in the repo). The isolation
  predicates are written for when one exists.
* **`idempotency_key` is required** for `submit_run` in MP-1A. The absent-key compat window
  (D3) is deferred to the route-wiring mini-phase.

## 4. Files

**New (7 code + 1 helper + 5 tests + 2 docs):**
- `backend/app/run_engine/context.py` — `AuthContext`, `build_auth_context`, `get_auth_context`
- `backend/app/run_engine/engine.py` — `submit_run`, `get_run`, `transition_run`,
  `request_cancellation`, `CancellationResult`
- `backend/app/run_engine/events.py` — `CONTROL_EVENT_TYPES`, `append_control_event`,
  `read_control_events`, `_sanitize_event_data`, `_try_append_event`
- `backend/app/run_engine/_dberr.py` — `constraint_violation`
- `tests/run_engine_helpers.py`, `tests/test_run_engine_auth_context.py`,
  `tests/test_run_engine_submit.py`, `tests/test_run_engine_transitions.py`,
  `tests/test_run_engine_events.py`, `tests/test_run_engine_cancellation.py`
- `docs/superpowers/plans/2026-09-08-phase-1b2r-mp1a-auth-and-core-services.md`, this report

**Modified:**
- `backend/app/run_engine/__init__.py` — export the MP-1A surface
- `backend/app/run_engine/errors.py` — +12 typed errors
- `backend/app/services/auth_service.py` — extract `resolve_user_from_payload`
- `backend/app/services/refresh_token_service.py` — +`family_id_for_raw_token`
- `backend/app/routes/auth.py` — login reorder + `sid` in login & refresh
- `backend/app/config.py` — +3 limits + validator
- `docs/superpowers/plans/2026-09-08-phase-1b2r-durable-run-resume-architecture.md` — §28
- `THUNDER_AI_PRD.md` — §Status

**Migrations:** none.

## 5. Tests

New — `42 passed` (`.venv`, SQLite, hermetic + TestClient):

| File | n | Covers |
|---|---|---|
| `test_run_engine_auth_context.py` | 8 | `sid` in login token & stable across refresh; `build_auth_context` valid / missing-sid / revoked / expired / unknown / blank. |
| `test_run_engine_submit.py` | 12 | server `run_id`; one `run.created`; replay (same run, no 2nd event); key-vs-identity conflict; fingerprint conflict; per-user/per-session scope; key too long; missing key; bad kind; no binding; unrelated `IntegrityError` not swallowed; insert-race fallback to winner; owner-scoped `get_run`. |
| `test_run_engine_transitions.py` | 6 | version bumps once; stale conflict; illegal transition; terminal protected + idempotent re-assert; cancellation wins; no binding. |
| `test_run_engine_events.py` | 10 | allowlisted append + seq; disallowed type; monotonic no-gap; failed insert leaves `last_event_seq` unchanged; ascending + `after_seq`; page clamp; oversize; nested data; owner-scoped; no binding. |
| `test_run_engine_cancellation.py` | 6 | one live signal + one event; idempotent duplicate (signal not consumed, run not mutated); terminal → no signal/event/mutation; cross-user → `RunNotFound`; family mismatch → `SessionBindingMismatch`; no binding. |

## 6. Verification battery

| Suite | Result | Notes |
|---|---|---|
| `import app.main` (`.venv`) | OK | |
| MP-1A (5 files) | **42 passed** | |
| MP-0 run engine (`state_machine`, `run_payloads`, `run_signals`, `run_engine_models`, `run_engine_migrations`) + `test_auth.py` | **212 passed, 1 skipped** | skip = PG (`EVAL_PG_TEST_URL` unset). `test_auth` 100% — login reorder + `sid` did not regress. |
| approval domain + gateway + api + migrations + `mcp_tool` + `mcp_dispatch` + `model_validation` | **242 passed, 3 skipped, 24 errors** | every error is `httpx.ConnectError` from service-backed `test_models.py` (needs live Ollama) — pre-existing, unrelated. |
| `test_session_ownership.py` (TestClient portion) | **passed** | |
| Service-backed `test_sessions.py` / `test_health.py` / `test_models.py` (live httpx client) | not run — `ConnectError` | pre-existing environment limitation (no live backend). |
| PostgreSQL | **not verified** — hosted-deployment blocker stands. |

## 7. Adversarial review

* **Cross-owner / cross-tenant** — every read and write filters `user_id` and tenant;
  cross-owner lookups raise `RunNotFound` (indistinguishable from absent). Tested.
* **Session-family control** — `request_cancellation` rejects a caller whose `sid` differs from
  the run's `auth_session_ref` (`SessionBindingMismatch`). A legacy token (no `sid`) fails the
  binding check before any path is reached.
* **Idempotency-key abuse** — key length capped server-side (`run_idempotency_key_max_bytes`,
  client cannot raise); reuse for a different identity is a deterministic `IdempotencyKeyConflict`,
  never a silent overwrite; the DB partial index is the authority, not the pre-`SELECT`.
* **Wrong `IntegrityError` swallowed** — `test_unrelated_integrity_error_is_not_swallowed`
  proves a `NOT NULL` failure propagates rather than being read as a replay.
* **`last_event_seq` advance on failed insert** — `test_failed_insert_does_not_advance_last_event_seq`
  plants a phantom `(run_id, run_seq)` row; the append retries, exhausts, raises
  `ControlEventContention`, and the high-water mark is unchanged.
* **Control-event data exfiltration** — `_sanitize_event_data` rejects nested structures and
  non-scalars, and enforces a byte cap; only `run.*` types are accepted.
* **Cancellation side-effect claims** — `request_cancellation` never sets `status = cancelled`,
  never sets `consumed_at`; a terminal run gets a result with `signal_created=False` and no
  control event. Tested.
* **Version double-bump** — the CAS `UPDATE` sets `version = version + 1` exactly once;
  `test_legal_transition_bumps_version_exactly_once` checks `0 → 1 → 2`.
* **Concurrency** — the multi-process race is resolved by `uq_runs_idem_scope` /
  `uq_run_events_run_seq` / `uq_run_signals_live` at the DB, exercised deterministically by
  `test_insert_race_falls_back_to_the_winner`. True N-process contention on PostgreSQL is a
  MP-6 gate.

## 8. Known limitations (carried into MP-1B / MP-2 / MP-6)

1. **Revoke-in-the-gap:** a mutation validates the binding then commits even if the family is
   revoked between validation and commit. The next mutation fails closed. A worker-side
   re-check at dispatch/resume is MP-1B/MP-2.
2. **No worker:** `request_cancellation` records intent; nothing consumes the signal or drives
   the state machine yet.
3. **PostgreSQL unverified** here — the CAS + unique-index design is PG-safe by construction but
   `EVAL_PG_TEST_URL` is unset.
4. **Absent-`idempotency_key` compat path** deferred to the route-wiring mini-phase.
5. Full service-backed regression (live Ollama + ChromaDB) not run in this environment — the
   standing Phase 1B deployment-blocker.

## 9. Source control

Nothing committed, merged, pushed, reset, stashed, or branch-switched. All Phase 1B.1 / 1B.2 /
1B.2R (MP-0 + MP-1A) work remains uncommitted on `phase-1b1-approval-domain` (HEAD `b2e67183`).

---

# 12. Correction round 2 (2026-09-08) — authoritative

## 12.1 Root causes and current-code evidence (before this round)

| # | Gap | Evidence (pre-fix) |
|---|---|---|
| 1/2 | **Cached boolean was the authorization authority.** `engine._require_active_binding(ctx)` only read `ctx.durable_session_binding_valid` — a snapshot from token-parse time. A logout / family revoke / expiry / reuse-detection after context build did not stop a later op. Reads (`get_run`, `read_control_events`) and the idempotent-replay return did no family check at all — owner+tenant only. | `engine.py` `_require_active_binding`; `events.py` `_require_active_binding`; `_owned_run_or_404` filtered `user_id`/`tenant` only. |
| 3 | **Event data was a "non-secret scalar" sanitizer.** `_sanitize_event_data` accepted *any* string key with a scalar value — a caller could store `{"authorization": "<token>"}`. | `events.py` `_sanitize_event_data` — `for key, value in data.items(): ... isinstance(value, _SCALAR)`. |
| 4 | **`idempotency_identity_hash` was an unkeyed SHA-256** over identity tuple + `kind` + client `thread_id` + a client-trusted `request_fingerprint`. Not semantic; the fingerprint was a client identity knob; a low-entropy input is guessable from the digest. | `engine._identity_hash` (`hashlib.sha256(canonicalize(material))`), `submit_run(request_fingerprint=...)`. |
| 5 | **Replay / race-recovery returned an existing run with no fresh family or run-family check.** | `submit_run` `if existing is not None: ... return existing`. |
| 7 | **Login persists the refresh session before signing the token.** A signing failure left an undisclosed active refresh family with no cleanup. | `routes/auth.py` steps 7-8. |
| 9 | Prior report folded a 24-error (`httpx.ConnectError`) command into a "242 passed" line with no exit code. | report section 6. |

## 12.2 Files changed (round 2)

- `backend/app/run_engine/session_binding.py` — **+`validate_session_binding(db, ctx, *, run=None)`** (the authority) + `_SESSION_BOUND_RUN_ACCESS` (5C toggle, STRICT).
- `backend/app/run_engine/events.py` — **rewritten**: closed `CONTROL_EVENT_SCHEMAS` registry + `validate_event_data`; `CONTROL_EVENT_TYPES` narrowed to the 5 MP-1A types; `_sanitize_event_data` **deleted**; `append_control_event` / `read_control_events` call `validate_session_binding` (entry + pre-commit).
- `backend/app/run_engine/engine.py` — `_require_active_binding` **removed**; `validate_session_binding` at entry, on the replay return, on the IntegrityError race-recovery return, and again immediately before every `commit()`. `_identity_hash` relabelled **structural**, domain-separated `"structural-1"`, `request_fingerprint` **parameter removed**. Event writes go through `validate_event_data`. `reason_code` validated against a closed enum.
- `backend/app/run_engine/errors.py` — **+`ControlEventFieldError`** (field name + reason slug only, never the value).
- `backend/app/run_engine/context.py` — docstring: `durable_session_binding_valid` is **diagnostic only, not authorization**.
- `backend/app/run_engine/__init__.py` — export `validate_session_binding`, `validate_event_data`, `CONTROL_EVENT_SCHEMAS`, `ControlEventFieldError`.
- `backend/app/routes/auth.py` — login wraps steps 8-9 in `try/except`: on failure `revoke_refresh_session(db, user, refresh_token)` then re-raise (no orphan active family; raw token never delivered; failure not swallowed).
- Tests — helper `run_engine_helpers.py` seeds / `revoke` / `expire` a real `refresh_sessions` row; `test_run_engine_events.py` rewritten to the schemas; **new** `test_run_engine_session_binding.py`, `test_run_engine_atomicity.py`; login-failure + one-family tests added to `test_run_engine_auth_context.py`; the client-fingerprint test asserts the kwarg is a hard `TypeError`.

## 12.3 Fresh session-validation contract and call sites

`validate_session_binding(db, ctx, *, run=None)` — MUST run inside the operation's own
transaction/session. Proves against the **live DB**, every call:

1. `ctx.auth_session_ref` present (legacy token without `sid` -> `SessionBindingRequired`);
2. a `refresh_sessions` row exists for that `family_id` **AND `user_id == ctx.user_id`** (query is
   always owner-constrained — never by family id alone), `revoked_at IS NULL`, `expires_at > now`
   (else `SessionBindingRequired`);
3. when `run` is given: `run.user_id == ctx.user_id` and tenant match (else `RunNotFound`,
   non-disclosing); and — STRICT default (5C) — `run.auth_session_ref == ctx.auth_session_ref`
   (else `SessionBindingMismatch`).

A family id is **only** ever taken from `ctx` (built from the signed token) — never from a
request body or a service argument. `ctx.durable_session_binding_valid` is **not** consulted.

| Call site | entry check | run-bound check | pre-commit re-check |
|---|---|---|---|
| `submit_run` | yes `validate_session_binding(db, ctx)` | yes on replay return AND on IntegrityError race-recovery return (`run=existing`) | yes `validate_session_binding(db, ctx, run=run)` immediately before `commit()` |
| `get_run` | — | yes `run=run` | n/a (read) |
| `read_control_events` | — | yes `run=run` | n/a (read) |
| `append_control_event` | — | yes `run=run` after load | yes before `commit()` |
| `transition_run` | — | yes `run=run` after load | yes before `commit()`, and re-validated on the freshly-loaded row after commit |
| `request_cancellation` | — | yes `run=run` each retry iteration | yes before `commit()`; also on the idempotent race-recovery return |

Tests (`test_run_engine_session_binding.py`): revoke-after-build -> every op raises; expire ->
every op raises; family swapped to another user -> fail closed; legacy token (no `sid`) -> every
durable op raises; another active family of the same user -> `SessionBindingMismatch` on
read/replay/mutate; no run existence leak across users (`RunNotFound` == missing); revoke between
the entry and pre-commit checks -> caught before commit, state not persisted.

## 12.4 Session-bound read / replay evidence

`get_run`, `read_control_events`, and `submit_run`'s replay + race-recovery returns all call
`validate_session_binding(db, ctx, run=run)` — so a replay clears **the same** authorization as a
fresh submit (fresh family, owner, tenant, run-family, then the structural identity-tag compare).
A run owned by another user is `RunNotFound`; a run owned by the same user under a different active
family is `SessionBindingMismatch` (STRICT default). Covered by
`test_run_engine_session_binding.py` and `test_run_engine_submit.py::test_get_run_is_owner_scoped`.

## 12.5 Semantic idempotency / identity — BLOCKED (5A + 5B)

`idempotency_identity_hash` is now an explicitly **interim structural tag**:
`sha256(RFC-8785({v:"structural-1", tenant_id, user_id, session_id, idempotency_key, kind,
thread_id}))` — domain-separated so a future semantic HMAC cannot collide. The client
`request_fingerprint` parameter is **removed** (a client value must never be an identity input;
`test_client_cannot_inject_identity_via_fingerprint_kwarg` asserts the kwarg is a hard `TypeError`).
Same key + changed `kind` or client `thread_id` still deterministically conflicts.

It does **not** cover semantic request content (prompt / task / model options) — MP-1A `submit_run`
has **no such parameter**. Making it semantic requires two decisions:

**5A — HMAC key source.** Repo inspection: `settings.jwt_secret` has an unsafe hardcoded default
(`"change-me-in-production"`) and rotating it invalidates all JWTs — unsuitable as a MAC key.
`settings.encryption_key` (`ENCRYPTION_KEY`, a Fernet 32-byte key) is real but (a) legitimately
**unset in dev** (the Fernet layer raises `RuntimeError`), and (b) its documented lifecycle is
*per-row `MultiFernet` re-encryption*, which gives no "verify under the old key" path for a
standalone HMAC — rotation would silently turn in-flight replays into conflicts. There is **no
dedicated stable server MAC secret**. Stop-and-ask: choose
  1. a dedicated `RUN_IDEMPOTENCY_HMAC_KEY` (new required-in-prod env var, fail-closed when unset), OR
  2. a documented domain-separated derivation `HKDF/HMAC(ENCRYPTION_KEY, "thunder-ai/run-idempotency-identity/v1")` — accepting that `submit_run` then fails closed when `ENCRYPTION_KEY` is unset and that identity tags churn on encryption-key rotation.

**5B — canonical semantic-submission contract.** MP-1A has no prompt/task/model input, so there
is nothing to HMAC yet. Decision: either (i) add a minimal internal `submission: dict` parameter
to `submit_run` now with an explicit list of behaviorally-material fields (prompt, system prompt,
model id, tool set, options) to canonicalize + HMAC (no public route), or (ii) defer the semantic
tag to the MP-3 `/messages/stream` wiring and keep the structural tag until then. This report does
**not** guess — the structural tag is clearly labelled not-semantic in code and docs, and no test
claims it detects prompt changes.

## 12.6 HMAC key lifecycle / rotation limitation

Pending 5A. Whichever key is chosen: the tag is a fixed-size hex string in
`idempotency_identity_hash`; comparison is `hmac.compare_digest` (constant-time) at the app layer;
the HMAC input carries the `"...v1"` domain label so a future format cannot collide. Rotating the
key makes prior tags non-matching -> prior in-flight idempotent submissions become
`IdempotencyKeyConflict` (fail-safe, not fail-open). Raw canonical submission bytes never enter
`runs`, `run_events`, logs, reprs, or exception messages.

## 12.7 Exact per-event schema table (closed registry `CONTROL_EVENT_SCHEMAS`)

| type | required | optional | field rules |
|---|---|---|---|
| `run.created` | `kind` | — | `kind` in {`chat_stream`,`chat_sync`,`scheduled`,`sub_run`} |
| `run.queued` | — | — | no keys accepted |
| `run.status` | `from_status`, `to_status` | — | both in the 13 `RUN_STATES` enum |
| `run.cancel.requested` | — | `reason_code` | `reason_code` in {`user_abort`,`client_request`,`superseded`,`policy`,`timeout_guard`} |
| `run.error` | `error_code` | — | `error_code` in {`submission_failed`,`binding_lost`,`state_conflict`,`seq_contention`,`internal`} |

Rejected for **every** type: unknown event type (`DisallowedControlEventType`), unknown/extra key,
missing required key, nested dict/list/tuple/set, `bool` where an enum is expected, any value
outside the enum, `null` (unless a rule is `nullable`), serialized form over
`run_control_event_max_bytes` (`ControlEventTooLarge`). No free-form strings, numbers, notes,
prompts, model i/o, tool names/args, request/response bodies, credentials, tokens, cookies,
headers, ciphertext, exception text, URLs, or client-supplied timestamps / ids / seqs are storable
— every field is a closed enum of short server codes. `run.error` carries a bounded code, never a
raw exception message. `run.status` `from`/`to` are validated against the real run-state enum.
Tests: `test_run_engine_events.py` — parametrized rejection of `note`/`value`/`debug`/
`authorization`/`secret`/`api_key`/`prompt`/`args`/`output`/`exception` as plain strings; a
unique-secret **canary** under an innocent unknown key is proven absent from the DB, every log
record, and the raised exception's `str`/`repr`.

## 12.8 Auth partial-failure handling

`routes/auth.py` login: `create_refresh_session` (persists + commits) -> **try**: resolve
`family_id`, `create_access_token(sid=...)` -> **except**: `revoke_refresh_session(db, user,
refresh_token)` (best-effort, logged if it fails) then `raise` (failure surfaced, never swallowed;
the raw token was never set as a cookie). Tests (`test_run_engine_auth_context.py`): login creates
exactly one family; a signing failure leaves **zero** active refresh sessions for that user; `sid`
present after login and unchanged across refresh; reuse-detection / logout family revocation
covered by the existing `test_auth.py` rotation suite (unchanged, green). Refresh-service return
arities unchanged (a test unpacks `rotate_refresh_token` as a 2-tuple).

## 12.9 Atomic state/event and signal/event evidence

`test_run_engine_atomicity.py`:
- `submit_run` — if the `run.created` event cannot be written, the whole run insert rolls back
  (0 `runs`, 0 `run_events`).
- `transition_run` — a lost `version` CAS raises `RunStateConflict` and writes **no** status
  event and does **not** bump `version` (stays at the one prior bump).
- `request_cancellation` — if the `run.cancel.requested` event cannot be written, **neither** the
  live `run_signals` row **nor** the event survives, and `run.status` is untouched.
- a successful cancellation commits **exactly one** signal and **exactly one** event.
- `last_event_seq` + event insert atomicity: `test_run_engine_events.py::
  test_failed_insert_does_not_advance_last_event_seq` (phantom `(run_id, run_seq)` row -> the
  high-water mark is unchanged after the failure).
- a schema-rejected payload raises before any DB write (`last_event_seq` unchanged).

MP-1A `transition_run` deliberately does **not** co-write a status event (the worker does, MP-1B+)
— so there is no "both or neither" claim to make for transitions beyond the CAS itself.

## 12.10 Multi-user / session / tenant adversarial evidence

Covered by `test_run_engine_session_binding.py` + `test_run_engine_submit.py` +
`test_run_engine_cancellation.py`: revoked family, expired family, missing `sid`, another active
family of the same user (STRICT -> `SessionBindingMismatch`), different user (`RunNotFound`),
tenant-null vs a real tenant (mismatch -> `RunNotFound`), nonexistent run (`RunNotFound`), no
existence leak across owners, cross-user cancel produces no signal.

## 12.11 Exact test commands, exit codes, counts

All via `PYTHONPATH=backend .venv/Scripts/python.exe -m pytest ... -q` (the `.venv` has
`rfc8785` / `chromadb`; the ambient interpreter does not).

| Command | exit | result |
|---|---|---|
| `-m compileall -q backend/app` | 0 | ok |
| 24-file hermetic command (MP-1A ×7 + MP-0 run-engine ×5 + approval/migrations ×4 + mcp ×5 + `test_model_validation` + `test_evals_inprocess` + `test_evals_runner`) | 0 | **487 passed, 4 skipped** |
| `tests/test_auth.py` (login / refresh / logout / rotation / reuse detection) | 0 | **126 passed** |
| `git diff --check` | 0 | clean |
| secret scan (`grep -rnE "(api[_-]?key|secret|password|token)\s*=\s*[\"'][A-Za-z0-9/+_-]{16,}"` over `backend/app/run_engine/` + touched auth/config files) | — | no hardcoded secrets (only the pre-existing `jwt_secret = "change-me-in-production"` default, unchanged) |
| captured-log secret-canary (`test_run_engine_events.py::test_secret_canary_never_reaches_db_logs_or_errors`) | 0 | pass — canary absent from DB, logs, error `str`/`repr` |

Hermetic aggregate (24-file command **+** `test_auth.py`): **613 passed, 4 skipped, exit 0**.
Skips = 1 PG migration skip + 3 optional provider-path skips in the approval/mcp set. The MP-1A
subset alone (the 7 `test_run_engine_*` files added/reworked this round + `test_run_engine_auth_context`)
is **70 passed**.

## 12.12 Failed external-service command and classification

Command: `PYTHONPATH=backend .venv/Scripts/python.exe -m pytest tests/test_models.py -q`
Exit code: **1**. Result: **0 passed, 24 errors** — every error is `httpx.ConnectError` (the file
drives a live backend URL that is not running here). **Classification: ENVIRONMENT-BLOCKED, not
passing.** This command is an **open, mandatory Phase 1B gate**; it is NOT part of any green
regression line. Other live-backend files in the same class (not run): `tests/test_sessions.py`,
`tests/test_health.py` (both `httpx.ConnectError` in prior rounds). The ~23-min service-backed
full regression (live Ollama + ChromaDB) is likewise not run here.

## 12.13 Clean hermetic regression command and exclusions

The 24-file command + `tests/test_auth.py` in 12.11 ARE the clean hermetic regression:
**613 passed, 4 skipped, exit 0**. Skips = 1 PG migration skip + 3 optional provider-path skips
in the approval/mcp set. **No hermetic test is excluded to hide a failure.**

**Excluded, and why:** `tests/test_models.py`, `tests/test_sessions.py`, `tests/test_health.py`
— each drives a live backend over `httpx` and fails with `httpx.ConnectError` with no server
running (proven, 12.12). Broad files outside MP-1A's blast radius and not run here:
`test_planning.py`, `test_search.py`, `test_documents.py`, `test_memories.py`,
`test_browser_agent.py`, `test_evaluation.py`, `test_async_bridge.py`, `test_export*.py`,
`test_media_generation.py` — covered by the standing service-backed gate, not by this round.

## 12.14 PostgreSQL / full-suite status

**PostgreSQL: NOT verified.** `EVAL_PG_TEST_URL` unset. The `validate_session_binding` query, the
`runs.version` / `last_event_seq` CAS, and the three unique indexes are PG-correct by
construction, but no PostgreSQL run has been executed — hosted-deployment blocker stands. The
~23-min live-service regression is also unexecuted here.

## 12.15 Exact git status / diff summary

`git status`: 15 tracked files `M` from Phase 1B.1/1B.2/1B.2R; untracked `backend/app/approval/`,
`backend/app/run_engine/`, `docs/superpowers/reports/`, plus plan/test files. Round-2 touched (all
already uncommitted): `backend/app/run_engine/{session_binding,events,engine,errors,context,__init__}.py`,
`backend/app/routes/auth.py`, `tests/{run_engine_helpers,test_run_engine_events,test_run_engine_submit,
test_run_engine_transitions,test_run_engine_cancellation,test_run_engine_auth_context}.py`, plus new
`tests/test_run_engine_session_binding.py`, `tests/test_run_engine_atomicity.py`. `git diff --check`
-> clean.

## 12.16 Remaining risks

1. **5A / 5B / 5C unresolved** (12.5, 12.7). The structural identity tag does not detect a changed
   prompt; STRICT cross-session access may be too strict for a "continue on another device" UX.
2. **Revoke-in-the-gap (residual):** on a single SQLite connection there is no true concurrent
   revoke between the pre-commit check and `commit()`; the pre-commit check is as close to the
   write as the txn model allows. On PostgreSQL a revoke committed by another connection in that
   window would still be missed by this op — the MP-1B/MP-2 worker MUST re-validate immediately
   before any external side effect. No row-level lock was added (unverified locking is out of
   scope; no PG test).
3. **PostgreSQL unverified** (12.14).
4. **No worker** — cancellation records intent only.
5. Full service-backed regression not run here.

## 12.17 Scope confirmation — MP-1B / MP-2 / MP-3 / Phase 1B.3 NOT started

No worker loop, no lifespan worker, no run/step claiming, no lease acquire/renew/expiry, no
tool/MCP execution, no approval resume, no `/messages/stream` change, no public Run API route, no
SSE/reconnect, no text-event buffering, no browser/desktop/python execution, no scheduler
integration, no frontend change. `get_auth_context` exists but is wired into no route. No
destructive migration; **no migration at all** this round.

## 12.18 Source-control confirmation

Nothing committed, merged, pushed, reset, stashed, cleaned, reverted, or branch-switched. No refs
modified. All Phase 1B work remains uncommitted on `phase-1b1-approval-domain` (HEAD `b2e67183`).
