# Phase 1B.2R — MP-3C1 completion report

**Durable chat parity · stateful stream safety · feature-flag hardening**

**Date:** 2026-09-09 · **Branch:** `phase-1b1-approval-domain` · HEAD `b2e6718` ·
**all work uncommitted, not production-ready**

MP-3C1 makes the MP-3B worker-backed chat path *faithful* to the request it was submitted for,
*safe* under a running event loop and a stalled provider, and *fail-closed* on every invalid
feature-flag combination — **without** a `/messages/stream` cutover, lifespan wiring, frontend
change, chat→MCP resume, or tool execution.

---

## Files changed

**New**
- `tests/test_run_engine_mp3c1.py` — 21 tests, fake providers only.
- this report.

**Modified**
- `backend/app/run_engine/chat_stream.py` — **rewritten**:
  - `build_exec_snapshot` / `EXEC_SNAPSHOT_VERSION` — the versioned execution snapshot
    (`provider`, `model`, `mode`, resolved `system_prompt`, sorted `options`); **never a key**.
  - `build_chat_input` now reads the snapshot and **fails closed** on a missing / unknown-version
    snapshot — no deployment-provider / `system_prompt=None` substitution.
  - `default_chat_provider(chat_input, *, db, user_id)` — provider + model from the **snapshot**;
    the live credential is looked up by `user_id` and applied **only** when the live provider
    still equals the snapshot provider. Single provider, **no `build_fallback_chain`**.
  - `MarkerScanner` — bounded stateful scanner: holds back only a `< max-marker-length` suffix,
    detects a reserved opener across arbitrary chunk splits, and **never returns full or partial
    marker text**; `finish()` releases the tail only on a clean end. Replaces the per-chunk
    `first_marker_index` re-scan.
  - `run_stream` — safe under an already-running loop (drives the coroutine on a dedicated
    thread with its own loop when `get_running_loop()` succeeds); `_adrive` iterates with
    `asyncio.wait(timeout=poll_interval)` and calls `on_idle()` on every idle tick, so
    lease / cancel / session-family / deadline checks fire **even when the provider is stalled**;
    the pending `__anext__` and the async iterator are cancelled/`aclose()`d on exit.
- `backend/app/run_engine/submission.py` — `CONTRACT_VERSION = "chat-run/v2"`;
  `ChatRunSubmission +resolved_provider +resolved_system_prompt`; `build_chat_submission(+provider=)`;
  `_canonical_bytes` (hence `semantic_identity`) now covers `provider`; `create_chat_run` seals
  the merged `{message, image_url, exec:<snapshot>}` payload; `+RunFeatureNotExecutable` +
  `assert_chat_stream_executable()` (fail closed unless `run_engine_enabled` **and**
  `run_chat_stream_enabled`).
- `backend/app/routes/runs.py` — submit handler: `assert_chat_stream_executable()` **before any
  persistence** → 503, so an unusable `chat_stream` run is never queued; resolves the provider
  server-side (`get_user_llm_config(...).provider` or `settings.llm_provider`) and freezes it into
  the submission + snapshot; the api key is not part of the submission.
- `backend/app/run_engine/worker.py` — `claim_next_queued` / `recover_expired` **always** exclude
  `kind == "chat_stream"` (no longer flag-gated); `_run_chat_generation` uses `MarkerScanner`, the
  new `run_stream(on_idle=, poll_interval=)` signature, `default_chat_provider(chat_input, db=, user_id=)`,
  and flushes the scanner tail on a clean end before `buf.close()`.
- `backend/app/run_engine/__init__.py` — unchanged export surface (chat symbols already exported).
- `tests/test_run_engine_{worker,recovery}.py` — the throwaway generic-worker kind moved
  `chat_stream → chat_sync` (chat_stream is now always worker-reserved).
- `tests/test_run_engine_mp3b.py` — `chat_provider` seam signature `(chat_input, **kw)`;
  `test_flag_off…` → `test_chat_stream_always_reserved_from_generic_worker`.
- `tests/test_run_engine_mp3a.py` — the HTTP submit test sets `run_engine_enabled` +
  `run_chat_stream_enabled` (new fail-closed guard).

**No migration** — the snapshot rides inside the existing MP-3A sealed `RunPayload`; `events.py`,
`config.py` (poll interval reuses `run_worker_lease_ttl_s // 3`), and all schema unchanged.

## How each critical requirement is met

| # | Requirement | Where |
|---|---|---|
| 1 | Versioned encrypted execution snapshot; no credentials | `build_exec_snapshot` (v1) sealed by `create_chat_run` inside the context-bound Fernet envelope; api key never placed in the submission or snapshot |
| 2 | Semantic HMAC covers all behaviour-changing fields | `_canonical_bytes` +`provider`; `chat-run/v2`; changed provider + same key ⇒ `IdempotencyKeyConflict` |
| 3 | No deployment-provider / `system_prompt=None` substitution | `build_chat_input` reads the snapshot and raises `chat_input_error` if it is missing/unknown; `default_chat_provider` takes provider+model from the snapshot only |
| 4 | Bounded stateful marker scanner across chunk boundaries; no full/partial marker text | `MarkerScanner` — held-back suffix `< max marker length`, `feed()` returns only the safe prefix, `""` forever after a trip |
| 5 | Provider bridge safe under a running event loop | `run_stream` detects `get_running_loop()` and runs `asyncio.run` on a dedicated thread |
| 6 | Wall-clock lease / cancel / session / deadline checks even when stalled | `_adrive` `asyncio.wait(timeout=poll_interval)` → `on_idle()` every idle tick (deadline → `provider_timeout`, else `_guard()`) |
| 7 | `chat_stream` always worker-reserved; invalid flag combos fail closed, no unusable queued runs | unconditional `kind != chat_stream` in `claim_next_queued`/`recover_expired`; `assert_chat_stream_executable()` in the submit route before persistence |
| 8 | Paid models never an implicit fallback | single provider from the snapshot; `build_fallback_chain` not imported or used by `chat_stream` (asserted) |
| 9 | Preserve MP-3A/3B atomicity, privacy, idempotency, cancellation, SSE, one-final-Message | `_finalize_chat_success` (one commit, lease+version CAS), `_consume_cancel`, `_ChatHeartbeat`, sealed-payload confidentiality — all unchanged; regression suites green |

## Focused verification

| Check | Result |
|---|---|
| `compileall` (changed files) | clean |
| `tests/test_run_engine_mp3c1.py` | **21 pass** |
| `tests/test_run_engine_mp3b.py` (contracts changed) | **21 pass** |
| `tests/test_run_engine_mp3a.py` (submission/HMAC/route changed) | **24 pass** |
| directly-affected: `test_run_engine_{worker,recovery,session_binding,cancellation,transitions,events,mcp_resume,submit}.py` | **119 pass** |
| `git diff --check` · `import app.main` | clean · OK |

Cases: snapshot sealed + round-tripped, no `api_key` anywhere (incl. a live config carrying one);
unknown snapshot version → fail closed; `provider` in the semantic HMAC; same key + changed
provider → 409; worker uses the snapshot provider + system prompt (not deployment / None);
`default_chat_provider` builds from the snapshot, applies a live key only on a provider match;
scanner across `"["`/`"MC"`/`"P_CALL…"` splits, at stream start, and a benign near-miss released;
worker never shows a split marker; `run_stream` inside a running loop completes; a stalled
provider is cancelled mid-stall (< tick) and hits the deadline mid-stall; `chat_stream` never
generic-claimed with the flag on or off; invalid flag combo → 503, no run; no fallback chain,
exactly one provider call on error; one final Message + text/Message parity (bridge/scanner
regression guard).

## Deferred / not touched

`/messages/stream` cutover, lifespan wiring, frontend, chat→MP-2 tool suspend/resume, tool
execution, a budget-guarded fallback chain, PostgreSQL/Docker validation, and broad/full
regression — all remain **MP-3C+ / final phase gate**. A chat run stranded `running` by a
mid-flight flag flip is a documented rollback edge (swept only when the flag is re-enabled, or by
the MP-6 `outcome_unknown` reconcile) — not solved here.

## Confirmation

Nothing was committed, merged, pushed, reset, stashed, or cleaned. The entire dirty working tree
is preserved on `phase-1b1-approval-domain` at HEAD `b2e6718`.

---

## MP-3C1 close-out (2026-09-09)

Targeted resolution of four acceptance gaps. **Files changed:** `backend/app/run_engine/chat_stream.py`
(marker list now repo-derived; `MarkerScanner.truncated` + fail-closed `finish()`;
`+truncated_tool_marker` in `CHAT_STATUS_REASONS`), `backend/app/run_engine/worker.py`
(`_run_chat_generation` maps an EOF-truncated marker to `truncated_tool_marker`),
`tests/test_run_engine_mp3c1_closeout.py` (new, 21 tests). No migration; `mp3c1` contracts
unchanged (`test_run_engine_mp3c1.py` re-run: 21 pass).

**1 — Authoritative reserved-marker list.** Derived from the repository's single canonical
model-emitted opener list `app.skills.loader._MARKER_OPENS`, filtered to the execution subset and
verified against it at import (`RuntimeError` if the canonical list drifts). The list is exactly:
`[mcp_call`, `[python_code`, `[proposed_command`, `[browser_action` (MCP / Python / proposed-command
·shell / browser). Persistence·planning openers (`[save_memory]`, `[plan]`, `[lesson]`,
`[use_memory]`, `[save_directive]`) are deliberately not fail-closed (handled elsewhere,
harmless). The two MP-3B defensive extras with no repo evidence (`[BROWSER_APPROVAL_REQUIRED`,
`[COMPUTER_CALL`) were dropped.
**Split-boundary matrix:** for each of the 4 markers, a canonical instance with a canary
argument is tested at **every interior character split** (≈156 distinct 2-chunk boundaries),
plus: marker at stream start, marker after ordinary clean text, and the full instance fed one
character at a time. Every case: `tripped`, no `[`/opener-char/canary in emitted text, clean
pre-marker text preserved. End-to-end: the marker's arguments are absent from `run_events`,
`run_steps.input_ref`/`output_ref`, `runs.status_reason`, and captured logs; only the clean
prefix is persisted as a text event.

**2 — Incomplete marker prefix at EOF.** `MarkerScanner._pending` only ever holds a suffix that is
a live prefix of a reserved marker (a benign near-match is released by the next `feed`). So
`finish()` **never emits the held tail**: a non-empty tail sets `truncated`, and the worker fails
closed → run `failed`, step `incomplete`, `status_reason = error_code = truncated_tool_marker`
(bounded, non-secret). Tested: `"["`, `"[MCP_"`, `"[PYTHON_"`, `"[PROPOSED_COM"`, `"[BROWSER_"` at
EOF → blocked, prefix never emitted; `"[note: …]"` / `"[ref 12]"` / `"[x=1]"` → emitted normally
(succeeded). No conflict with the legacy `SkillStreamFilter` protocol — different marker class
(execution vs skill/persistence), different path (durable vs `/messages/stream`), and the durable
path already fails closed on a *complete* execution marker.

**3 — Thread / DB-session ownership (already correct; proven, not rewritten).**
Model: the durable operation's primary `db` Session is created and used **only on the
worker/session-owning thread** (the `poll_once` / `advance_chat_runs` caller). `run_stream` runs
the provider coroutine inline on that same thread when no event loop is running (the real
deployment path — `run_forever` calls `poll_once` via `asyncio.to_thread`), and only when a loop
is *already* running does it drive the coroutine on an isolated daemon thread with its own loop
(no nested `asyncio.run`). `_ChatHeartbeat` is the one always-separate thread and calls only
`renew_lease`, which opens its **own** short-lived Session. Every DB callback that can run off the
owner thread (`_guard`: lease / cancel / session-family checks) opens its **own** Session via the
factory on whatever thread it runs on. **No SQLAlchemy Session object ever crosses a thread
boundary.** Final persistence (`_finalize_chat_success`) always runs on the owner thread with the
owner's `db`. Tests record thread identities and prove: provider iteration is isolated from the
owner thread under a running loop; final persistence and (no-loop) every DB callback are on the
owner thread; lease renewal is on the dedicated heartbeat thread; a stall→cancel/deadline closes
the provider iterator (`aclose`); and no `chat-provider-bridge` / `chat-lease-hb` thread is alive
after completion or failure.

**4 — Provider-key isolation.** `default_chat_provider` resolves `get_user_llm_config(db, user_id)`
with the **run's** `user_id` only — another user's key is unreachable (tested). The live key is
attached to the provider config **only when the live provider still equals the snapshot
provider** (reused `test_default_chat_provider_builds_from_snapshot_not_deployment`). A full run
with a matching live key present proves the key reaches the in-memory provider object but is
**absent from** `RunPayload` plaintext and ciphertext, `run_events`, step refs,
`runs.status_reason`, and logs.

**Verification:** `compileall` (changed files) clean · `test_run_engine_mp3c1_closeout.py` **21
pass** · `test_run_engine_mp3c1.py` **21 pass** (re-run; contracts unchanged) · `import app.main`
OK · `git diff --check` clean. The 119-test directly-affected group, broad/613/PG/Docker/Ollama/
ChromaDB/frontend/live-provider were **not** run (deferred to the final phase gate). Nothing
committed / merged / pushed / reset / stashed / cleaned; HEAD `b2e6718`.
