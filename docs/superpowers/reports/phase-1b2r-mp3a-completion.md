# Phase 1B.2R — MP-3A completion report

**Durable Run API contract + persisted text events + SSE reconnect**

**Date:** 2026-09-09 · **Branch:** `phase-1b1-approval-domain` · HEAD `b2e6718` ·
**all work uncommitted, not production-ready**

MP-3A adds the versioned canonical chat-run submission contract, its dedicated semantic-idempotency
HMAC, an additive authenticated `/api/runs` API (feature-flagged **off**), durable `text`-category
`run_events` with byte/time batching, and an authenticated SSE replay/reconnect stream backed by
the ledger. **No `/messages/stream` change, no LLM/model generation, no frontend, no
browser/Python/terminal execution, no reusable grants, no scheduler.** MP-2 guarantees are
untouched.

---

## Files changed

**New**
- `backend/app/run_engine/submission.py` — HMAC key access (`_hmac_key`, env-only, no default,
  min-16, never logged), `require_run_public_api` / `assert_hmac_key_configured`,
  `ChatRunSubmission` + `build_chat_submission` + `semantic_identity`, `create_chat_run`
  (atomic submission persistence).
- `backend/app/run_engine/text_events.py` — `append_text_event`, `TextEventBuffer`
  (byte/time batching, UTF-8-safe `_chunk_by_bytes`).
- `backend/app/run_engine/sse.py` — `stream_run_events` (DB-backed, per-seq replay, heartbeat
  comments, per-loop family revalidation, bounded pages, redacted errors), `parse_last_event_id`.
- `backend/app/routes/runs.py` — `POST /api/runs`, `GET /api/runs/{id}`,
  `POST /api/runs/{id}/cancel`, `GET /api/runs/{id}/events` (SSE).
- `tests/test_run_engine_mp3a.py` (24 tests, incl. a real-app HTTP slice).
- this report.

**Modified**
- `backend/app/run_engine/engine.py` — `submit_run(+identity_hash=, +commit=)`: an explicit
  64-hex `identity_hash` becomes the authoritative run identity (replaces the interim structural
  tag); `commit=False` lets the caller persist Message/RunStep/payload in the same transaction.
- `backend/app/run_engine/events.py` — `_try_append_event(+category=)`; `+read_run_events`
  (all categories, strict `run_seq` order, page-bounded).
- `backend/app/run_engine/__init__.py` — export the MP-3A surface.
- `backend/app/config.py` — `+run_public_api_enabled` (off), `+run_sse_poll_interval_s`,
  `+run_sse_heartbeat_s`, `+run_sse_max_seconds`, `+run_text_event_max_bytes` (ints validated ≥1;
  `heartbeat_s` may be 0). `RUN_IDEMPOTENCY_HMAC_KEY` is **not** a Settings field.
- `backend/app/main.py` — register `runs_router`; lifespan fails closed if
  `run_public_api_enabled` without the HMAC key.
- `tests/test_run_engine_mcp_resume.py` — one MP-2 boundary assertion relaxed (MP-3A legitimately
  adds `/api/runs`; it stays feature-gated off).

**No migration** — `run_events.category` and every other column already exist.

## API + HMAC contract

- **`RUN_IDEMPOTENCY_HMAC_KEY`** — env var only, no hardcoded/default value, required only when
  `run_public_api_enabled`, **fails closed** (503 at request time, `RuntimeError` at boot) when
  enabled-but-missing, never returned or logged. A present key < 16 chars counts as unconfigured.
- **Semantic identity** = `HMAC-SHA256( key, "thunder-ai/run-semantic-identity/v1" || 0x00 ||
  RFC-8785(canonical submission) )` → 64-hex. The canonical submission (`chat-run/v1`) covers
  every behaviour-changing field: `session_id, message, image_url, model (server-resolved),
  mode (server-resolved + allowlisted), system_prompt` (as a **keyed** `HMAC` under a separate
  domain — never the raw prompt), and a sorted scalar `options` map. The raw prompt is an HMAC
  *input* (keyed ⇒ not an offline-dictionary target); it is never placed in identity metadata,
  `run_steps.input_ref`, events, or logs. The request body (`extra="forbid"`) accepts **no**
  client identity/fingerprint/hash field.
- **`identity` semantics** — same scoped `Idempotency-Key` + same semantic identity ⇒ the same
  run (idempotent replay, no new rows). Same key + any changed prompt/model/mode/option ⇒
  `IdempotencyKeyConflict` → **409**. Scope is `(tenant_id, user_id, session_id, key)` — a
  different user with the same key gets a **separate** run.
- **`/api/runs` routes** — every one calls `require_run_public_api()` first: **404** when the flag
  is off (non-disclosing), **503** when on without the key. `Idempotency-Key` header **required**
  on submit (422 if absent). Server generates `run_id`. Auth = `get_auth_context` (bearer +
  strict `sid`/refresh-family binding); CSRF double-submit on the two mutations. Ownership is
  re-checked on every op via `get_run` / `request_cancellation`; `RunNotFound` **and**
  `SessionBindingMismatch` both map to **404** (no cross-user/session/family existence
  disclosure); `SessionBindingRequired` → 401. **No execution in any route** — submit only
  persists+queues; cancel only records intent (existing `request_cancellation`); the SSE route
  only reads the ledger.
- **Submission atomicity** — `create_chat_run` = `submit_run(commit=False)` (Run `created` +
  `run.created` event, no commit) → user `Message` (existing privacy model) → sealed input
  `RunPayload` (`payload_type="other"`, context-bound Fernet envelope) → initial `RunStep`
  (`kind="load_context"`, `input_ref` = ids only: `message_id`, `model`, `mode`,
  `contract_version`) → `run.queued` event → `transition_run(→queued)` issues the **single
  commit**. A replay adds nothing (Message/Run/Step/Payload/Event counts unchanged).

## Text batching + SSE behaviour

- **Text events** — `run_events.category="text"`, `type="text"`, `data={"text": <chunk>}`,
  sharing the per-run monotonic `run_seq` with control events (control stays immediate, never
  routed through the buffer). `TextEventBuffer` accumulates a Python `str`; flushes at
  `run_event_text_flush_bytes` **or** `run_event_text_flush_ms` (via `tick()`); `close()` flushes
  the final partial buffer. A flush larger than `run_text_event_max_bytes` (8192) is split on
  **codepoint** boundaries — a multi-byte char is never torn. `append_text_event` rejects a
  single body over the cap (`ControlEventTooLarge`). No token id / credential / header / cookie /
  raw arg / ciphertext is ever stored as a text event.
- **SSE** — `GET /api/runs/{id}/events`, `text/event-stream`, `Cache-Control: no-cache`,
  `X-Accel-Buffering: no`. Replays strictly by `run_seq` from the DB; emits `id: <run_seq>`;
  honours `Last-Event-ID` header and `?after_seq=` (query wins) and never re-sends `≤` the
  cursor. Reads bounded pages (`run_event_read_max_page`, 200) — a slow consumer only blocks the
  generator on its own socket write; at most one page is held. `: heartbeat` comment lines
  (`run_sse_heartbeat_s`, 0 ⇒ every idle poll) — never a persisted/fake event. Closes on: run
  terminal (after draining), client disconnect (connection only — **no cancel, no mutation**),
  refresh-family revocation/expiry/mismatch (revalidated **every** poll → `event: error` +
  close), the `run_sse_max_seconds` (1800) cap, or an internal error (redacted to
  `event: error {"detail":"internal stream error"}`). The DB is the source of truth; no
  in-memory notifier in MP-3A.

## Focused test counts (per the mini-phase instruction — no broad regression)

| Check | Result |
|---|---|
| `python -m compileall` (all changed files) | clean |
| `tests/test_run_engine_mp3a.py` | **24 pass** |
| directly-affected: `test_run_engine_{submit,events,session_binding,cancellation,transitions,atomicity,worker,recovery,mcp_resume,migrations,models}.py` | **123 pass / 1 skip** |
| `git diff --check` · `import app.main` | clean · OK |

Required cases covered: identical semantic retry → one run; changed prompt/model/mode + same key
→ 409 conflict; client cannot inject an identity/fingerprint field (schema `extra="forbid"`);
missing HMAC key → fail closed when enabled; duplicate submission → zero duplicate rows; strict
cross-user & cross-session/family isolation; text batches preserve exact UTF-8 (incl. codepoint
chunking); byte-threshold, time-threshold and final flush; reconnect replays by seq with no gap
or duplicate; disconnect does not cancel/mutate; revoked family closes the stream; bounded page
reads / slow-consumer bound; no LLM/MCP/DispatchRecord in the submission path; secret canary
absent from the identity hash, `input_ref`, events, and the sealed payload's plaintext.

## Remaining blockers

- **Not wired to `/messages/stream`** — the real streaming producer + a live in-memory notifier
  are MP-3B/MP-3C. MP-3A's text pipeline is exercised by an injected in-process publisher only.
- **No real generation** — `create_chat_run` queues a run; nothing advances it (no chat handler
  registered; the worker is still not lifespan-wired).
- **PostgreSQL unverified** — `run_seq` allocation, the partial-unique idem index and the SSE
  page reads are PG-safe by construction; the concurrency matrix is MP-6 (`EVAL_PG_TEST_URL`
  unset). Hosted-deployment blocker stands.
- **Broad regression deferred** — full Phase 1B / 613-test hermetic, evals, Ollama/ChromaDB,
  Docker, frontend — all deferred to the final phase gate.

## Confirmations

`/messages/stream`, the LLM/model generation path, and the frontend were **not changed**.
MP-3B / MP-3C / Phase 1B.3 were **not started**. Nothing was committed, merged, pushed, reset,
stashed, or cleaned — the entire dirty working tree is preserved on `phase-1b1-approval-domain`
(HEAD `b2e6718`).
