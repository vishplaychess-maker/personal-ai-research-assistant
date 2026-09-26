# Phase 1B.2R — MP-1B completion report

**RunWorker + claim / lease + cancellation / recovery core**

**Date:** 2026-09-09 · **Branch:** `phase-1b1-approval-domain` · HEAD `b2e6718` ·
**all work uncommitted, not production-ready**
**Decisions:** 5A / 5B / 5C locked by the user 2026-09-09 (recorded in architecture §28.8, the
MP-1A report header, and the PRD §Status). MP-1A is **approved for progression** — only the
doc/config-status updates the decisions require were applied; its 613-test regression was **not**
re-run.

MP-1B is the **mechanism only** — the worker is not started anywhere (see "Not started" below).

---

## Files changed

**New**
- `backend/app/run_engine/worker.py` — `RunWorker`, the handler registry, value objects.
- `tests/test_run_engine_worker.py` (14 tests), `tests/test_run_engine_recovery.py` (6 tests).
- `docs/superpowers/reports/phase-1b2r-mp1b-completion.md` (this file).

**Modified**
- `backend/app/run_engine/errors.py` — `+RunLeaseLost`.
- `backend/app/run_engine/__init__.py` — export `RunWorker` / registry / `RunLeaseLost`.
- `backend/app/config.py` — `+run_worker_enabled` (bool, off), `+run_worker_id` (str), and
  `run_worker_{lease_ttl_s=60, poll_interval_s=5, max_attempts=3, handler_timeout_s=300,
  recover_batch=20}` (ints, validated `>= 1` in `_validate_approval_limits`).
- `tests/run_engine_helpers.py` — `+Clock`, `+mk_factory` / `run_snapshot` / `signal_rows` /
  `event_blob` fresh-session read helpers.
- `docs/superpowers/plans/2026-09-08-phase-1b2r-durable-run-resume-architecture.md` — §28.8
  decisions resolved; `+§29` MP-1B as-built.
- `docs/superpowers/reports/phase-1b2r-mp1a-completion.md` — decisions-resolved header.
- `THUNDER_AI_PRD.md` — §Status: MP-1A → approved-for-progression, `+MP-1B` row.

**No migration.** Every column used (`runs.lease_owner`, `lease_expires_at`, `run_deadline_at`,
`version`, `last_event_seq`, `run_signals.consumed_at`) exists from MP-0.
**`events.py` unchanged** — worker error reasons live in `runs.status_reason` (a bounded module
constant set), leaving the closed control-event schema registry untouched.

## Worker / lease / recovery behaviour

- **Service boundary** — `RunWorker(session_factory)` owns its own DB sessions + lease identity.
  Public: `claim_next_queued`, `execute`, `recover_expired`, `renew_lease`, `poll_once`,
  `async run_forever`, `stop`.
- **Atomic claim** — one conditional `UPDATE … WHERE status='queued' AND version=:v AND
  (lease_expires_at IS NULL OR < :now)`; `rowcount == 1` is the sole winner, so two racing
  claimers produce exactly one lease owner; loser re-polls. `queued→running` + one `run.status`
  event, same transaction.
- **Lease owner / token** — `lease_owner = "<worker_id>/<uuid4-hex>"` (identity + unforgeable
  per-claim token; `worker_id` clamped to 24 chars). Every worker mutation carries
  `WHERE lease_owner=:mine AND version=:v`; a stale token / lost race → `RunLeaseLost` → the run
  is abandoned for this sweep. `renew_lease` extends only the current holder's non-terminal run.
- **CAS / version** — `runs.version` bumps exactly once per worker state change; the pure MP-0
  `assert_run_transition` guards every move.
- **Stale-worker rejection + fresh session validation** — one gate (`_precheck`) run at the
  start of handling **and** again immediately before invocation: re-read the run, `RunLeaseLost`
  unless still `running` under this exact `lease_owner` + `version`; then
  `validate_session_binding` against the LIVE DB with an `AuthContext` built **from the run
  row** (never a request), cached validity flag passed `False`. Revoked / expired / mismatched
  family → `running→failed(session_revoked)`; the handler never runs.
- **Cancellation, consumed once** — a live `run_signals(cancel_requested)` is consumed with
  `UPDATE … SET consumed_at=:now WHERE id=:id AND consumed_at IS NULL` (`rowcount == 1` = the
  once-guarantee); consume + two `run.status` events + `running→cancelling→cancelled` commit
  together, and any failure rolls the whole unit back (signal stays unconsumed, run stays
  `running`). Cancel is checked before invocation (blocks it) and again after a side-effect-free
  handler returns (cancel wins).
- **Handler registry** — `register_handler(kind, fn, *, retry_safe)` / `resolve_handler` /
  `clear_handlers`; **ships empty**. Unknown `run.kind` → `failed(unknown_handler)` fail closed.
  `HandlerContext` carries only `run_id` / `kind` / `user_id` / `tenant_id` / `attempt` — no db,
  token, or prompt. A raising handler → `status_reason="handler_error"` (never the text);
  `ok=False` → a bounded code (`handler_rejected`, else collapsed to `handler_error`).
- **Bounded attempts / timeouts** — per-run handler wall clock (`run_worker_handler_timeout_s`,
  exceeded → `timed_out`); recovery bounded by `run_deadline_at` set at first claim to
  `now + handler_timeout × max_attempts` (past it → `failed(deadline_exceeded)`);
  `run_worker_recover_batch` bounds runs per sweep.
- **Retry-safe recovery** — `recover_expired` sweeps `status='running' AND lease_expires_at <
  now`. A registered **`retry_safe`** handler within the deadline → atomic reclaim (new lease
  token, `version++`) then `execute`. Unknown handler / non-retry-safe handler →
  `running→outcome_unknown`, **never** auto-retried. A fresh `RunWorker` == a restart: only
  retry-safe kinds resume.
- **Disabled by default** — `run_worker_enabled=False`; `run_forever` returns immediately unless
  `run_engine_enabled` **and** `run_worker_enabled` are both true. A sweep exception is logged
  as its class name only (no traceback / message); the loop continues.

## Focused test counts (per the mini-phase instruction — no broad regression)

| Check | Result |
|---|---|
| `python -m compileall` (all changed files) | clean |
| `tests/test_run_engine_worker.py` | **14 pass** |
| `tests/test_run_engine_recovery.py` | **6 pass** |
| `test_run_engine_session_binding.py` + `_events` + `_cancellation` + `_transitions` (directly affected) | **44 pass** |
| `git diff --check` | clean |
| `import app.main` | OK |

Required cases covered: two claimers → one owner; stale token cannot mutate / renew; current
owner renews; expired retry-safe reclaimed; expired unknown & non-retry-safe → `outcome_unknown`;
revoked session prevents invocation; cancel-before-invocation blocks the handler; cancel signal
consumed once; signal/event/state atomic (injected mid-cancel event failure → full rollback);
handler exception → bounded code only (secret string absent from row + events); restart resumes
only retry-safe work; empty registry runs nothing.

## Remaining blockers

- **Not wired into the app lifespan** — deliberate; MP-3 starts the worker. An always-on loop
  with an empty registry would be a production no-op.
- **PostgreSQL unverified** — the claim / lease / recovery CAS is PG-safe by construction;
  `SELECT … FOR UPDATE SKIP LOCKED` tightening + the PG concurrency matrix are MP-6
  (`EVAL_PG_TEST_URL` unset). Hosted-deployment blocker stands.
- **No per-run attempt counter** — recovery is `run_deadline_at` wall-clock bounded; a strict
  integer cap would be a `runs` column (migration) if a later phase needs one.
- **`resuming`-state recovery** deferred to MP-2 (no approval-resume path exists yet).
- **Broad regression deferred** — full Phase 1B / 613-test hermetic, approval / MCP / eval,
  Ollama / ChromaDB service-backed, Docker / frontend — all deferred to the final phase gate.

## Not started (confirmed)

MP-2 (MCP one-time approval resume), MP-3 (streaming persistence / reconnect), MP-4, MP-5, MP-6,
and Phase 1B.3 are **not started**. No real MCP / LLM / browser / terminal / desktop / Python
execution; no approval resume; no public Run API; no `/messages/stream`, SSE, or frontend
change; no scheduler integration; no fake production feature.

## Source control (confirmed)

Nothing was committed, merged, pushed, reset, stashed, cleaned, or rebased. All Phase 1B /
1B.2R work remains uncommitted on `phase-1b1-approval-domain` (HEAD `b2e6718`).
