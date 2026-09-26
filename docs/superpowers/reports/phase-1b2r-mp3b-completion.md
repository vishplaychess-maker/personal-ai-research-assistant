# Phase 1B.2R — MP-3B completion report

**Worker-backed durable chat generation + durable text streaming**

**Date:** 2026-09-09 · **Branch:** `phase-1b1-approval-domain` · HEAD `b2e6718` ·
**all work uncommitted, not production-ready**

MP-3B adds a **RunWorker chat driver** that generates a durable `chat_stream` run: it opens the
MP-3A encrypted submission, invokes the existing `LLMProvider.generate_stream_async` seam **once**,
persists tokens through `TextEventBuffer`, heartbeats its lease in the background, honours
cancellation and session revocation, and creates the final assistant `Message` **exactly once**
inside a single transaction. Behind `run_chat_stream_enabled` (off) **and** `run_engine_enabled`.
**No `/messages/stream` cutover, no frontend, no lifespan wiring, no fallback chain, no tool
execution, no reusable grants, no scheduler.** MP-2 / MP-3A guarantees untouched.

---

## Files changed

**New**
- `backend/app/run_engine/chat_stream.py` — the smallest provider seam: `build_chat_input`
  (opens the sealed submission + rebuilds session history; `system_prompt=None` — MP-3A sealed
  only an HMAC), `default_chat_provider` (reuses `get_provider`, pinned to the sealed model
  snapshot), `run_stream` (async→sync bridge over `generate_stream_async`, `aclose()`s the
  iterator), `first_marker_index`, `CHAT_STATUS_REASONS`, typed `ProviderStream{Error,Timeout}` /
  `StreamAborted` (never carry provider text).
- `tests/test_run_engine_mp3b.py` — 21 tests, fake streaming providers only.
- this report.

**Modified**
- `backend/app/run_engine/worker.py` — `RunWorker(+chat_provider=)`; `_ChatHeartbeat`
  (bounded background lease renewal, owner/token-only, stops on completion/cancel/failure/lease
  loss); `advance_chat_runs` / `_advance_chat_run` / `_run_chat_generation` /
  `_finalize_chat_success`; `poll_once` also advances chat runs; `claim_next_queued` /
  `recover_expired` exclude `kind == "chat_stream"` **only when the flag is on** (so the
  existing generic-worker tests, which use `chat_stream` as a throwaway kind, are unaffected).
- `backend/app/run_engine/__init__.py` — export `CHAT_RUN_KIND`, `CHAT_STATUS_REASONS`,
  `build_chat_input`.
- `backend/app/config.py` — `+run_chat_stream_enabled` (bool, off).

**No migration.** `runs.kind == "chat_stream"` is in `_RUN_KINDS` already; the MP-3A submission
`RunStep` (seq 1) doubles as the generate step; text output uses the MP-3A `text` category.
`events.py` unchanged — the driver writes bounded `runs.status_reason` / `run_steps.error_code`
codes (`CHAT_STATUS_REASONS`), never a new event schema, never raw provider text.

## Provider / worker flow

`advance_chat_runs` (no-op unless both flags on) picks claimable `chat_stream` runs
(`queued`, or `running` with an expired lease). `_advance_chat_run`: atomic lease claim
(`version` CAS + lease-expiry guard) → `queued→running` → `_precheck` (lease + fresh DB
session-family) → **cancel-before-invocation** (consume once → `cancelled`, provider **never
called**) → `build_chat_input` from the sealed payload (unreadable ⇒ `chat_input_error`) →
**durable attempt-1 record**: step `pending→running`, `commit` → `_run_chat_generation`.
Streaming: per token → marker scan (publish only the clean prefix, never the marker) →
`buf.publish` + `buf.tick`; every 8 tokens a `_guard()` re-checks lease ownership/version,
a live cancel signal, and the session family. On `done`: `buf.close()` (final partial flush),
assemble the exact streamed text, `_finalize_chat_success`.

## Heartbeat / cancel / crash behaviour

- **Heartbeat** — `_ChatHeartbeat` renews every `lease_ttl // 3`s via `renew_lease` (CAS'd on
  `lease_owner`, refuses terminal runs). A failed renewal sets `lease_lost`; the producer's next
  `_guard()` aborts. `_guard()` also fails closed if the DB row's `lease_owner`/`version` no
  longer match — a stolen lease stops output persistence and finalisation immediately. A live,
  heartbeated `running` chat run is skipped by `advance_chat_runs` (`in_progress`) — no second
  worker reclaims a healthy generation.
- **Cancel** — checked before invocation, every 8 tokens, and again in `_finalize_chat_success`.
  During streaming: `aclose()` the provider iterator, keep already-persisted text as partial
  history, **no** assistant Message, `running→cancelling→cancelled` (one transaction). No claim
  that the provider op was externally undone. Disconnect is unrelated (MP-3A property).
- **Attempts / crash** — provider attempts = **1**. The step's non-`pending` status is the
  durable proof invocation began. Expired lease + step still `pending` ⇒ reclaimable. Expired
  lease + step `running` ⇒ `outcome_unknown`, **never** re-invoked. Provider timeout / error
  chunk / raised exception ⇒ `failed` with a bounded code (`provider_timeout` / `provider_error`);
  raw exception text and response bodies are never persisted or logged.
- **Marker** — `[MCP_CALL` / `[PYTHON_CODE` / `[PROPOSED_COMMAND` / `[BROWSER_ACTION` / … →
  step `incomplete` `tool_resume_required`, run `failed`, marker never shown. Durable chat→MP-2
  tool suspend/resume and the live-route cutover are **deferred to MP-3C**.

## Final-message atomicity

`_finalize_chat_success`: `_precheck` → late-cancel check → (idempotency guard: a step whose
`output_ref` is already set ⇒ a prior finalize won, just re-assert `succeeded`) → insert
`Message`, `flush` → set step `output_ref` (`{message_id, output_len}` — non-secret) + `succeeded`
→ `_apply_transition(running→succeeded)` (**lease + `version` CAS is the sole "who finalises"
authority** — a stale worker cannot match, so cannot create a second Message or a second success)
→ **one `commit`** carrying Message + step + run + `run.status` event. `_apply_transition` raise
⇒ `rollback` ⇒ no Message, run stays `running`. `commit` failure ⇒ `rollback` ⇒
`outcome_unknown` (`output_persist_failed`) — success is never reported before the Message and
final state persist.

## Focused test counts (per the mini-phase instruction — no broad regression)

| Check | Result |
|---|---|
| `python -m compileall` (all changed files) | clean |
| `tests/test_run_engine_mp3b.py` | **21 pass** |
| directly-affected: `test_run_engine_{mp3a,worker,recovery,session_binding,cancellation,mcp_resume,transitions,events}.py` | **109 pass** |
| `git diff --check` | clean |

Required cases covered: multi-chunk UTF-8 stream (incl. emoji) → one assistant Message == the
streamed text; low-threshold multi-event flush + final partial flush; resweep of a terminal run
creates no second Message; `_apply_transition` failure → rollback, no Message, run non-successful;
commit failure → `outcome_unknown`, no Message; cancel before invocation (0 provider calls);
cancel mid-stream (partial kept, no Message, `cancelled`); revoked family before invocation
(0 provider calls) and mid-stream (`session_revoked`); heartbeat renews only the current owner and
stops at terminal; a ~2.4 s slow provider (> lease TTL) still completes; a stolen lease mid-stream
→ `abandoned`, no Message; crash-before-invocation reclaimed → `succeeded`; crash-after-invocation
→ `outcome_unknown`, 0 provider calls; provider error chunk / timeout / raised exception → bounded
code, secret canary absent from events / refs / `status_reason` / logs; `[MCP_CALL` marker not
executed or shown, only the clean prefix persisted; flag-off leaves `chat_stream` to the generic
claim path; `streaming_service.{stream_chat_response,save_assistant_message}` unchanged.

## Deferred tool suspension & live-route cutover

An inline action marker in durable chat generation is a **fail-closed stop** in MP-3B
(`tool_resume_required`). Wiring durable chat generation into the MP-2 one-time approval /
`tool_dispatch` suspend-and-resume machinery, and cutting `/messages/stream` over to
`submit()` + tail, are **MP-3C**. The legacy `/messages/stream` path keeps its full inline
tool capability, unchanged.

## Remaining blockers

- **Not wired into the app lifespan / `/messages/stream`** — deliberate (MP-3C). `advance_chat_runs`
  is inert unless `run_engine_enabled` **and** `run_chat_stream_enabled`.
- **No durable system prompt / provider snapshot** — MP-3A sealed only a keyed HMAC of the
  system prompt and no provider id; MP-3B runs with `system_prompt=None` and the deployment's
  `settings.llm_provider` (never mutable per-user settings) pinned to the sealed model. A
  full context/provider snapshot in the `chat-run/v1` contract is MP-3C/MP-4.
- **Single provider attempt, no fallback chain** — `attempts = 1` by design; the free-tier
  fallback chain in the durable path is MP-3C/MP-4.
- **PostgreSQL unverified** — the lease/CAS/heartbeat approach is PG-safe by construction; the
  concurrency matrix + `SELECT … FOR UPDATE SKIP LOCKED` tightening are MP-6 (`EVAL_PG_TEST_URL`
  unset). Hosted-deployment blocker stands.
- **Broad regression deferred** — full Phase 1B / 613-test hermetic, evals, Ollama/ChromaDB,
  Docker, frontend — all deferred to the final phase gate.

## Confirmations

`/messages/stream`, the LLM generation route, and the frontend were **not changed**.
MP-3C / Phase 1B.3 were **not started**. Nothing was committed, merged, pushed, reset,
stashed, or cleaned — the entire dirty working tree is preserved on `phase-1b1-approval-domain`
(HEAD `b2e6718`).
