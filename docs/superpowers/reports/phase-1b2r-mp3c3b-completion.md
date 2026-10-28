# Phase 1B.2R — MP-3C3B completion report

**Durable `/messages/stream` backend cutover + DB-authoritative notifier**

**Date:** 2026-09-11 · **Branch:** `phase-1b1-approval-domain` · HEAD `b2e6718` ·
**all work uncommitted, not production-ready**

Adds a **feature-flagged** (`run_messages_stream_durable_enabled`, OFF by default) durable backend
for `POST /api/sessions/{id}/messages/stream`: it submits (or idempotently reuses) an MP-3A
`chat_stream` run and streams the run's **persisted `run_events`** adapted into the *exact legacy
SSE wire format*. When the flag is off the route is the **byte-for-byte legacy path**. Also adds a
smallest in-process **run-event notifier** that shortens SSE latency while the DB stays the sole
source of truth.

**Backend-only.** No frontend change. Flag not enabled in any default/deployment config. Legacy
streaming implementation not removed. No production cutover claimed.

---

## Stage-0 compatibility findings (proven from code + tests)

Legacy `POST /api/sessions/{id}/messages/stream` (`routes/messages.py::stream_chat` +
`services/streaming_service.py`), and the frontend parser (`frontend/src/useStreaming.ts`):

| Aspect | Legacy (proven) | Durable adapter (flag ON) | Compatible? |
|---|---|---|---|
| Route / method | `POST /api/sessions/{id}/messages/stream` | same | ✓ |
| Auth | `get_current_user` + `require_csrf` | same **+** builds `AuthContext` from the bearer token's `sid` claim (durable branch only; 401 if no bearer) | ✓ legacy unchanged |
| Request body | `ChatRequest {message: str(1..10000), image_url: Optional[str]}` — **only 2 fields** | same schema; `image_url != None` → **422 before persistence** | ✓ (subset) |
| Idempotency-Key | not accepted / not used | **required header** (durable branch only); MP-1A charset/length rules | additive |
| Content type | `text/event-stream` + `Cache-Control: no-cache`, `X-Accel-Buffering: no`, `Connection: keep-alive` | identical + `X-Run-Id` response header (frontend ignores unknown headers) | ✓ |
| Wire framing | `event: <name>\ndata: <json>\n\n` (`format_sse`); **no `id:` line**; not standard SSE lib | same framing + an SSE-standard `id: <run_seq>` line (the frontend parser reads only `event: `/`data: ` lines — `id:`/`:` lines are skipped) | ✓ additive |
| Event vocabulary | `start`, `plan`, `agent_status`, `token`, `complete`, `error`, `cancelled`; frontend also knows a **dormant** `tool_approval_required` (never emitted today) | `start` (once), `token`, `run_status` (**new**, `switch` has no default → ignored), `tool_approval_required` (**redacted**: `{approval_id, tool_id}`), `complete`, `error`, `cancelled` | ✓ (`plan`/`agent_status` never emitted — those modes are rejected pre-persistence) |
| `token` data | `{"token": "..."}` — frontend accumulates `contentRef` | `{"token": <text-event chunk>}` | ✓ |
| `complete` data | route re-emits `{"message_id", "citations", "sources_used", "memories_used"}` (service also adds optional `confidence`/`confidence_reason`) | `{"message_id": <int from RunStep.output_ref>, "citations": [], "sources_used": false, "memories_used": false}` — honest empties, **never fabricated**; `confidence` omitted (optional in legacy) | ✓ |
| `error` data | `{"code": str, "detail": str}` bounded | `{"code": <bounded slug ← runs.status_reason>, "detail": <canned user-safe>}` | ✓ |
| `cancelled` data | `{"detail": "Client disconnected"}` (emitted on `is_disconnected()`) | `{"detail": "The run was cancelled."}` on run cancel/deny-terminal; **disconnect does NOT emit `cancelled`** (durable disconnect ≠ cancel) | shape ✓; disconnect semantics deliberately differ (documented) |
| Citations / confidence / deep-research / multi-agent progress | real values from RAG / deep-research / multi-agent / self-eval | **not emitted** — durable engine does not persist them; those request modes are **rejected before persistence** (allowlist) | ✓ (no fabrication) |
| Message persistence | route saves via `save_assistant_message` after intercepting `complete` | worker's `_finalize_chat_success` persists **exactly one** assistant `Message` atomically with `run→succeeded`; adapter reads `message_id` from `RunStep.output_ref` | ✓ one Message |
| Retry / idempotency | none — fresh generation per POST | same key + same semantic → reuse run; changed → **409** | additive |
| Approval-required | not on the legacy live path (shows text, executes nothing) | run parks `waiting_for_approval` → **redacted** `tool_approval_required` event; detailed object stays on `GET /api/approvals/{id}` | new capability |
| Replay / `Last-Event-ID` | **not supported** (frontend does one `fetch`, no reconnect) | supported backend-side (`Last-Event-ID` header / `?after_seq`); never re-sends `≤ cursor`; the current frontend won't use it yet | additive, forward-compatible |

**No stop condition triggered:** run identity is exposed via an ignored response header (+ optional
`id:` lines) — no wire break; `{message, image_url}` maps into `chat-run/v2` with **no schema
change** (images rejected pre-persistence); no citation/progress/confidence is fabricated;
unsupported modes fail **closed** with a bounded status **before** any Run/Message is created.

## Files changed

**New**
- `backend/app/run_engine/notifier.py` — `RunNotifier` singleton (`bind_loop` / `subscribe` /
  `unsubscribe` / `notify` — thread-safe via `loop.call_soon_threadsafe`), bounded subscriber
  map, and a global `before_flush` + `after_commit` + `after_rollback` ORM `Session` hook that
  fires **one `notify(run_id)` per committed transaction** that touched a `RunEvent` / `Run`.
- `backend/app/run_engine/messages_stream.py` — `assert_durable_stream_ready`,
  `unsupported_mode_code`, `valid_idempotency_key`, `resolve_and_submit` (server-resolved
  provider/model/mode/system-prompt via `build_chat_submission` + `create_chat_run` — **no
  executor**), `acquire/release_stream_slot` (per-user concurrency cap),
  `submit_rate_limited` (reuses `services/rate_limiter`), `parse_cursor`, `stream_legacy_wire`
  (durable `run_events` → legacy wire adapter).
- `tests/test_run_engine_mp3c3b.py` — 39 tests.
- `docs/superpowers/reports/phase-1b2r-mp3c3b-completion.md` (this file).

**Modified**
- `backend/app/config.py` — `+run_messages_stream_durable_enabled` (False),
  `+run_messages_stream_max_concurrent_per_user` (4), `+run_messages_stream_submits_per_min`
  (30); the two ints validated `>= 1`.
- `backend/app/routes/messages.py` — `stream_chat` gains an **optional** `_bearer =
  Depends(bearer_scheme)` (HTTPBearer `auto_error=False` → no legacy effect) and, as its **first
  line**, `if settings.run_messages_stream_durable_enabled: return await _stream_chat_durable(...)`
  — otherwise the legacy body is unchanged. New `_stream_chat_durable` helper.
- `backend/app/main.py` — lifespan: `run_notifier.bind_loop()` after the supervisor starts,
  `run_notifier.unbind_loop()` on shutdown.
- `backend/app/run_engine/__init__.py` — export `RunNotifier`, `run_notifier`,
  `install_notifier_hooks`.

**No migration.** No new run state / control event / `run_events` column. No frontend file
touched.

## Feature-flag / dependency behavior

`run_messages_stream_durable_enabled = False` (default) ⇒ `stream_chat` is the exact legacy path;
**nothing durable is created**. When on, `_stream_chat_durable` runs `assert_durable_stream_ready`
**before any persistence** — a bounded **503** ("Durable streaming is (temporarily) unavailable.",
no operational detail) unless **all** of:

* `run_engine_enabled` ∧ `run_worker_enabled` ∧ `run_chat_stream_enabled`
* `run_public_api_enabled`
* `RUN_IDEMPOTENCY_HMAC_KEY` valid (`assert_hmac_key_configured`)
* `assert_chat_stream_executable()` (MP-3C1)
* `app.state.run_worker_supervisor.is_durable_path_ready()` — i.e. the MP-3C3A worker is **alive,
  not fatal, not stopping**

A dead / fatal / stopping worker ⇒ 503, **no Run created**. The flag is set in **no** default or
deployment file.

## Mode & feature parity boundary

`unsupported_mode_code(has_image)` — checked **before** `resolve_and_submit`:

| Condition | Result |
|---|---|
| `payload.image_url is not None` | 422 `mode_unsupported_image` |
| `settings.enable_deep_research` (default **True**) | 422 `mode_unsupported_deep_research` |
| `settings.enable_multi_agent` (default **True**) | 422 `mode_unsupported_multi_agent` |
| otherwise | proceed (plain chat + MCP approval/resume) |

User-facing 422 detail is generic ("Durable streaming currently supports plain chat and
tool-approval conversations only."); the specific slug is logged, not returned. **No partial
persistence, no fallback to the legacy pipeline after the durable branch is taken.** Because
`enable_deep_research` / `enable_multi_agent` default on, the durable flag is only *usable* when an
operator also turns those two off — a documented dependency, fail-closed otherwise.

## Idempotent submission flow

`Idempotency-Key` header **required** (MP-1A `_normalize_key` charset/length; else 422).
`resolve_and_submit`: owner-scoped `ResearchSession` lookup (404 non-disclosing) → server-resolve
provider (`get_user_llm_config(...)['provider']` else `settings.llm_provider`), `model =
sess.model`, `mode = None → "chat"`, `system_prompt = sess.system_prompt`, `options = {}` →
`build_chat_submission` (`chat-run/v2`) → `create_chat_run` (MP-3A: semantic HMAC over the
canonical submission). **No** client `run_id` / `execution_turn_id` / `tool_call_id` / action
hash / dedupe key / provider identity / session-family id is ever accepted. Same key + identical
semantic ⇒ the **same run** (no new `Run`/`Message`/`RunStep`). Same key + behaviour-changing
input ⇒ **409**. Cross-user reuse of a key ⇒ 404 (the session is not theirs); no cross-user Run is
created or disclosed. The route calls **no** LLM / MCP / shell / browser / any executor — only
`create_chat_run` (persist + queue).

## Stream wire mapping (`stream_legacy_wire`)

Per persisted `run_events` row, strictly after the cursor, in `run_seq` order (bounded
`run_event_read_max_page` pages):

| durable event | legacy-wire frame |
|---|---|
| `category="text"` `{"text": chunk}` | `id: <seq>\nevent: token\ndata: {"token": <chunk>}\n\n` |
| `run.status → waiting_for_approval` | `event: tool_approval_required\ndata: {"approval_id": <id>, "tool_id": <server id>}` — **redacted** (no args / server env / marker text / action hash / family id) |
| `run.status → running / resuming / approved / queued / cancelling` | `event: run_status\ndata: {"status": <s>}` (frontend-ignored) |
| `run.status → succeeded` | `event: complete\ndata: {"message_id": <RunStep.output_ref>, "citations": [], "sources_used": false, "memories_used": false}` → **stop** |
| `run.status → failed` | `event: error\ndata: {"code": <status_reason slug, ≤48c>, "detail": <canned>}` → stop |
| `run.status → denied` | `event: error\ndata: {"code": "approval_denied", "detail": "…denied."}` → stop |
| `run.status → cancelled / cancelling` | `event: cancelled\ndata: {"detail": "The run was cancelled."}` → stop |
| `run.status → timed_out / outcome_unknown` | `event: error\ndata: {"code": "<slug>", "detail": <canned>}` → stop |
| `run.created / run.queued / run.cancel.requested / run.error` | dropped (never surfaced) |

A single `event: start\ndata: {"run_id": <id>}` is emitted once when the cursor is 0. `token`
carries only clean assistant text (`MarkerScanner` never lets marker/arg text into a text event —
MP-3C1/3C2). Never emitted: marker syntax, tool args/results, provider error text, API keys,
prompts, `status_reason` raw beyond the bounded slug.

## Run identity / reconnect contract

* **Run identity:** `X-Run-Id: <run.id>` **response header** (the frontend ignores unknown
  headers). No wire-format break; no new mandatory event.
* **Reconnect:** `Last-Event-ID` header or `?after_seq=` → `parse_cursor` → the adapter never
  emits a frame with `run_seq ≤ cursor`; `id: <run_seq>` lines let a future client track its
  cursor. A reconnect whose cursor is at/after a terminal `run.status` still gets a synthesized
  terminal frame (`_terminal_frame`) so it stops cleanly. Bounded page reads; `run_sse_max_seconds`
  duration cap; per-poll `validate_session_binding` (STRICT origin family) → a revoked/expired
  family closes the stream with a generic `error` (`code: stream_closed`). `request.is_disconnected()`
  closes the connection **only** — it never cancels or mutates the run.

## Notifier design

`RunNotifier` (one per process, single-process delivery only — **not** claimed multi-process, no
broker):

* `subscribe(run_id) → asyncio.Event` (bounded: ≤ 64 waiters/run, ≤ 4096 runs tracked — over cap
  ⇒ an **unregistered** Event ⇒ the reader falls back to its poll timeout). `unsubscribe` drops
  the run key when its last waiter leaves.
* `notify(run_id)` — safe from **any** thread (the MP-3C3A worker's owned thread): hands off via
  `self._loop.call_soon_threadsafe(self._wake, run_id)`; `_wake` runs on the app loop and
  `.set()`s each waiter. No-op when no loop is bound or nothing is waiting.
* **Publication** = a global `Session` hook: `before_flush` accumulates the `run_id`s of pending
  `RunEvent` inserts (and any touched `Run`) onto `session.info`, across every flush of the
  transaction (MP-3A/3B/3C2 flush early, commit late); `after_commit` fires **one** `notify` per
  `run_id`; `after_rollback` clears the accumulator. ⇒ a wake is emitted **only after commit**.
* The SSE loop always **re-queries the DB** after a wake, then `ev.clear()`s — so a missed /
  early / duplicate notification cannot cause a gap or a duplicate event; a broken notifier
  degrades to bounded polling (`run_sse_poll_interval_s`). Payload carries **at most** a
  `run_id` — no prompt / text / args / result / provider output / user id / session id / secret.
* Subscribers are removed on disconnect, revocation, `run_sse_max_seconds`, stream completion,
  and generator GC (the `finally` in `stream_legacy_wire`). Completed runs leave no residue.

## Approval / resume journey (tested, fakes only)

`POST` (flag on) → one `chat_stream` run created (no execution) → worker generates clean text →
`MarkerScanner` captures a `[MCP_CALL]` marker (**never emitted**) → run parks
`waiting_for_approval` → stream emits a **redacted** `tool_approval_required` (`{approval_id,
tool_id}`) → `POST /api/approvals/{id}/approve` records the decision + one `approval_decided`
signal, **executes nothing** → a later worker sweep consumes the signal → MCP dispatches
**at-most-once** via the MP-2 gateway → result sealed → provider continuation runs → the same
stream *or* a reconnect (cursor-resumed) receives the continuation `token`s → **exactly one**
assistant `Message` → `event: complete` is emitted **only after** the `Message` + `run→succeeded`
commit. Denial → terminal `event: error` (`approval_denied`), no dispatch, no continuation.
Cancel-while-waiting → `event: cancelled`, a later approval cannot dispatch.

## Security / privacy evidence

* Auth: `get_current_user` + `require_csrf` (POST) unchanged; the durable branch additionally
  requires a bearer token and enforces STRICT origin session-family binding via
  `build_auth_context` + per-poll `validate_session_binding`. Cross-user / cross-session / other
  family run access → generic `error` (`stream_closed`), never run content.
* Resource bounds: per-user concurrent durable streams capped
  (`run_messages_stream_max_concurrent_per_user`, 429 over cap, slot released in `finally`);
  submit rate-limited (`run_messages_stream_submits_per_min`, reuses the in-memory limiter);
  bounded event pages; bounded stream duration; bounded notifier subscriber map. Clients cannot
  raise or disable any of them.
* Redaction: canaries seeded in the prompt, system prompt, MCP args, MCP output, provider error,
  and the `[MCP_CALL]` marker are asserted **absent** from every wire frame (`token` / `error` /
  `tool_approval_required` / `run_status` / `complete` data + `id:` lines), the transport error
  frames, and the DEBUG logs; only the intended clean assistant text appears in `token` events.
  The approval event exposes at most `{approval_id, tool_id}`.

## Failure / race behavior

| Situation | Behavior |
|---|---|
| submission commit fails | `create_chat_run` is one transaction → **no** partial `Run` / `Message` / `RunStep` / payload |
| disconnect right after submit | `is_disconnected()` → generator returns; **run continues**, `Message` still persisted by the worker |
| reconnect during generation | cursor-resumed; no `run_seq ≤ cursor` re-sent → **no duplicate text** |
| event committed between the DB query and the notifier wait | the wait times out after `run_sse_poll_interval_s` (or the `after_commit` wake arrives) → next query returns it — **not lost** |
| notifier wake before the subscriber begins waiting | the very next DB re-query finds the row (DB is authoritative) |
| broken / failing notifier | falls back to bounded polling (tested) |
| worker dies mid-stream | `is_durable_path_ready()` was checked at submit; a run stuck non-terminal streams heartbeats then hits `run_sse_max_seconds` and stops; the worker never falsely cancels the run |
| worker shutdown mid-generation (MP-3C3A) | run stays `running` → `outcome_unknown` next boot; the stream eventually surfaces `error`/`outcome_unknown` — never a false `complete` |
| terminal failure | a later sweep cannot flip it to `succeeded`; the stream's terminal `error` is final |
| late approval on a denied/cancelled run | cannot resurrect it (MP-2/MP-3C2 state machine); no dispatch |
| cross-user stream read | `validate_session_binding` fails → generic `stream_closed`; no run data leaked |

## Exact focused tests

| Check | Result |
|---|---|
| `compileall` — `config.py`, `main.py`, `routes/messages.py`, `run_engine/{notifier,messages_stream,__init__}.py`, the test | clean |
| `pytest tests/test_run_engine_mp3c3b.py -q` | **39 passed**, 1 warning (unrelated `python_multipart` PendingDeprecation) |
| `pytest tests/test_run_engine_mp3a.py -q` (shared SSE / `read_run_events` / submission contract; TestClient lifespan now binds the notifier) | **24 passed** |
| `pytest tests/test_run_engine_mp3c3a.py -q` (lifespan gained `run_notifier.bind_loop/unbind_loop`) | **41 passed** |
| `import app.main` · `git diff --check` | OK · clean |
| `tests/test_streaming.py` in-process legacy tests | Docker-gated (`httpx` → live backend); pre-existing `ConnectError` without Docker — **not** run per policy; `test_run_engine_mp3c3b.py::test_flag_off_invokes_exact_legacy_path` is the in-process legacy-compat regression and passes |

Not run (deferred to the final Phase 1B gate): MP-3C2's 111-test suite, the 119-group, the 613
regression, the full suite, PostgreSQL, Docker, Ollama, ChromaDB, the frontend build,
live-provider.

## Remaining blockers

- **Frontend integration** — `useStreaming.ts` sends no `Idempotency-Key`, has no reconnect /
  `Last-Event-ID` handling, and does not read `X-Run-Id`. Until it does, the cutover flag must
  stay off. (A future phase.)
- **Mode parity** — deep research, multi-agent, images, citations, confidence, and `plan` /
  `agent_status` progress are **not** in the durable allowlist; the flag is only usable with
  `enable_deep_research=False` and `enable_multi_agent=False`. Convergence is a later phase.
- **Disconnect semantics differ deliberately** — legacy emits `cancelled` + saves nothing on
  disconnect; the durable path keeps the run and Message. A frontend cutover must account for
  this.
- **Single-process notifier** — no multi-process / broker delivery; multi-process still awaits
  the **MP-6** PostgreSQL concurrency gate.
- **Broad regression / Docker / PG / service-backed / frontend build** — deferred to the final
  phase gate.

## Not started (confirmed)

Frontend changes, enabling the flag by default, deleting the legacy pipeline, deep-research /
multi-agent convergence, image/video/browser/terminal/Python/desktop execution, provider-fallback
expansion, paid models, a Redis / shared notifier, the PostgreSQL matrix, Docker/service-backed
tests, broad regression.

## Source control (confirmed)

Nothing was committed, merged, pushed, rebased, reset, stashed, cleaned, or discarded; no branch
switch. The pre-existing stash was not touched. The entire dirty working tree is preserved on
`phase-1b1-approval-domain` at HEAD `b2e6718`.
