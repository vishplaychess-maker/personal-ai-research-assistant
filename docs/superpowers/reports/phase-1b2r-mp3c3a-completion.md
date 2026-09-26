# Phase 1B.2R — MP-3C3A completion report

**FastAPI worker lifespan + operational safety**

**Date:** 2026-09-11 · **Branch:** `phase-1b1-approval-domain` · HEAD `b2e6718` ·
**all work uncommitted, not production-ready**

MP-3C3A makes the durable run backend **operational**: the existing MP-1B `RunWorker` is started
and stopped through the one canonical FastAPI app lifespan when the approved feature flags are on,
with cooperative shutdown, bounded runtime-failure containment, a non-secret readiness surface,
and a fail-closed deployment-topology guard. **No `/messages/stream` cutover, no frontend/SSE
change, no chat-UX change.**

> ⚠️ **SUPERSEDED HISTORICAL TEXT.** The body immediately below (Stage-0 findings →
> "Not started") describes the *initial* MP-3C3A design — an `asyncio.Task` (`_supervise`)
> running `RunWorker.poll_once` via `asyncio.to_thread`, with `stop()` calling `task.cancel()`.
> The **[MP-3C3A lifecycle close-out](#mp-3c3a-lifecycle-close-out-2026-09-11)** section at the
> end **replaces that model**: the sweep+backoff loop now runs on **one explicitly owned
> `threading.Thread(name="run-worker", daemon=True)`**; there is **no supervisor asyncio task**,
> **no `task.cancel()`**, and `alive` derives from `thread.is_alive()`. The change was forced by
> proving (CPython 3.11.15) that `asyncio.to_thread` threads are `daemon=False` and
> `loop.shutdown_default_executor()` *joins* them. Read the close-out section for the final,
> authoritative model; treat every `asyncio.Task` / `to_thread` / `task.cancel` / "daemon sweep
> thread ends with the process" phrase in the body below as obsolete.

---

## Stage-0 lifespan / topology findings

* **One canonical lifespan** — `@asynccontextmanager lifespan(app)` (`main.py`), passed to
  `FastAPI(lifespan=…)`. No `@app.on_event`, no second lifespan. Startup order:
  mkdirs → `_validate_jwt_secret` → `_warn_ephemeral_sqlite_in_production` →
  `_migrate_knowledge_graph` → `init_db()` (create_all) → `_migrate_database()` (all
  `_migrate_*` + `_verify_run_engine_schema`, **halts startup on any schema gap**) →
  `_create_default_user` → MP-3A `assert_hmac_key_configured()` (iff `run_public_api_enabled`) →
  `start_scheduler()` → `yield` → `shutdown_scheduler()`.
* **DB / session factory** — `SessionLocal = sessionmaker(bind=engine)` (module-level). `RunWorker`
  already takes a zero-arg factory and its `_session()` ctx-manager opens **and closes** a fresh
  session per sub-call. Passing `SessionLocal` is exactly right — **no shared long-lived
  Session**.
* **`run_forever`** — an `async` method; refuses unless `run_engine_enabled AND run_worker_enabled`;
  loop offloads each blocking `poll_once` via `asyncio.to_thread`. It owns **no** thread of its
  own; `poll_once` runs on a threadpool thread with **no running event loop**, so MP-3C1's
  `run_stream` drives the provider coroutine **inline on that same thread** (no nested
  `asyncio.run`, no `chat-provider-bridge` thread in the deployed path). `stop()` sets a bool
  checked between sweeps.
* **`run_forever` gaps for operational use** — it swallows every sweep error with a class-name
  log only (no bounded classification, no backoff, no fatal fail-closed), has a fixed
  non-interruptible inter-sweep sleep, and exposes no status. It also cannot close a *stalled*
  provider on forced shutdown (nothing signals the in-flight `_guard()` / idle tick).
* **Provider-iterator seam** — `_run_chat_generation._guard()` (every 8 tokens) and `_on_idle()`
  (every `run_worker_lease_ttl_s // 3` s while the provider is stalled) already abort on
  lease-loss / cancel / session-revocation. There was **no shutdown signal** they consult.
* **App tests** — many modules use `with TestClient(app) as c:` which runs the real lifespan on
  enter/exit. The supervisor must be a no-op with flags off and cleanly start+stop on every
  lifespan entry without duplicating loops.
* **No `app.state` usage, no web-process-count setting** today.

**Conclusion:** wire the existing worker into the existing lifespan through a thin
**`RunWorkerSupervisor`** that reuses `poll_once` unchanged and adds the four operational
concerns. No second lifespan, no worker-architecture replacement, no thread force-kill, no
Session crossing a thread. No stop condition triggered.

## Files changed

**New**
- `backend/app/run_engine/worker_supervisor.py` — `RunWorkerSupervisor` (one embedded worker
  lifecycle per FastAPI app), `WorkerStatus` (non-secret snapshot), `SupervisorFatalError`,
  `assert_embedded_topology_ok`, `_classify` (bounded error taxonomy).
- `tests/test_run_engine_mp3c3a.py` — 31 tests (fake worker for lifecycle; real in-memory engine
  + fake streaming provider for shutdown semantics; `TestClient` for health; direct canonical
  parser calls for the adversarial spot check).
- `docs/superpowers/reports/phase-1b2r-mp3c3a-completion.md` (this file).

**Modified**
- `backend/app/config.py` — `run_worker_web_concurrency` (1), `run_worker_shutdown_grace_s` (30),
  `run_worker_backoff_min_s` (2), `run_worker_backoff_max_s` (60); all validated `>= 1`.
- `backend/app/main.py` — lifespan: create `app.state.run_worker_supervisor` (only if absent)
  after all boot checks, `await …start()`; on shutdown `await …stop()` then clear it (before
  `shutdown_scheduler()`). New `GET /api/health/worker` (non-secret status; **503** when the
  public durable Run API is enabled but the worker is not alive/ready). `Response` imported.
  **`/api/health` and every other route unchanged.**
- `backend/app/run_engine/worker.py` — `RunWorker.attach_stop(fn)` + `_stopping()` predicate
  (default absent → always False → **zero behavior change**); `_stopping()` consulted at the top
  of `claim_next_queued` / `advance_mcp_runs` / `advance_chat_runs` (accept no new work while
  stopping) and in `_run_chat_generation._guard()` (a `"shutdown"` abort → **no finalise, no
  Message, no terminal transition**); `run_forever` loop also honours it. `run_forever` docstring
  notes the supervisor is the lifespan path.
- `backend/app/run_engine/__init__.py` — export `RunWorkerSupervisor`, `WorkerStatus`,
  `SupervisorFatalError`, `assert_embedded_topology_ok`.

**No migration.** No new run state, no new control event, no schema change.

## Startup ordering

```
lifespan enter
  ├─ dirs, JWT-secret validation, ephemeral-SQLite warning
  ├─ _migrate_knowledge_graph
  ├─ init_db()                         (Base.metadata.create_all)
  ├─ _migrate_database()               (all _migrate_* + _verify_run_engine_schema — HALTS on gap)
  ├─ _create_default_user()
  ├─ assert_hmac_key_configured()      (iff run_public_api_enabled — MP-3A fail-closed)
  ├─ start_scheduler()
  └─ RunWorkerSupervisor().start()     ← MP-3C3A. no-op unless run_engine_enabled AND
                                         run_worker_enabled. assert_embedded_topology_ok()
                                         raises (HALTS startup) on unsafe multi-process SQLite.
                                         Then: build RunWorker(SessionLocal), attach_stop,
                                         asyncio.create_task(_supervise).
yield
lifespan exit
  ├─ RunWorkerSupervisor().stop()      ← cooperative: set stop → bounded grace await → clear
  └─ shutdown_scheduler()
```

The worker task is created **only** inside `.start()`, which runs **only** after every boot
check above has succeeded. Nothing starts a thread or task at module import.

## Worker / task / session ownership

| Concern | Owner |
|---|---|
| supervision task (`_supervise`) | one `asyncio.Task` named `run-worker-supervisor`, created by `start()`, stored on `app.state.run_worker_supervisor` |
| the blocking sweep | `await asyncio.to_thread(worker.poll_once)` — a threadpool thread with **no running loop** |
| every DB `Session` | opened **and** closed inside `RunWorker._session()` / the MP-3A `TextEventBuffer` / the `_ChatHeartbeat`, each on the thread that opened it — **no Session crosses a thread** |
| lease heartbeat | `_ChatHeartbeat` thread (`chat-lease-hb`), its own short-lived Session; joins on stop |
| provider coroutine | driven **inline** on the sweep thread (`run_stream` sees no running loop) — no `chat-provider-bridge` thread in the deployed path |
| cooperative stop signal | `threading.Event` owned by the supervisor; `worker.attach_stop(event.is_set)` — read from the sweep thread, set from the loop thread |
| exactly one per app instance | `if not getattr(app.state, "run_worker_supervisor", None): app.state.run_worker_supervisor = RunWorkerSupervisor()` + `start()` idempotent on `self._started`; repeated lifespan entry / `TestClient(app)` never duplicates the task |

## Shutdown behavior

1. `stop()` sets the `threading.Event` → `claim_next_queued` / `advance_mcp_runs` /
   `advance_chat_runs` accept **no new work**; an in-flight `_run_chat_generation` aborts at its
   next `_guard()` / idle tick with `state["abort"] = "shutdown"`.
2. A `"shutdown"` abort returns `RunOutcome(abandoned, "worker_shutdown")` — **no Message, no
   terminal transition**. The run stays `running` with its lease; the next boot's recovery
   (`running` + expired + invocation began) declares it `outcome_unknown`. **Never a false
   success, never a false cancel.**
3. `_supervise`'s loop exits (`while not stop.is_set()`, ~200 ms granularity — no tight loop).
4. `stop()` awaits the task for `run_worker_shutdown_grace_s` (30 s default, kept above
   `lease_ttl // 3`). On timeout: record the bounded code `shutdown_deadline_exceeded`, log it,
   `task.cancel()` (stop *awaiting*), and return. The in-flight sweep thread is a **daemon** that
   exits with the process — **it is never force-killed**.
5. An already-started MCP dispatch is untouched: `dispatch_records.started` → next boot
   `outcome_unknown`, never re-executed, never claimed cancelled/undone.

| Run state at shutdown | Outcome |
|---|---|
| `queued` | stays `queued` — resumable next boot |
| `waiting_for_approval` | stays `waiting_for_approval` (lease already dropped) — resumable |
| not yet invoked (`load_context`/`llm_generate` step `pending`) | lease expires; reclaimed next boot |
| chat generation already started, interrupted by shutdown | run stays `running`; lease expires → `outcome_unknown` next boot. **Never `succeeded`.** |
| MCP dispatch `started`, no terminal | `outcome_unknown` next boot; **never re-executed** |
| final `Message` already committed + `succeeded` | terminal — untouched |

## Failure / backoff behavior

`_supervise` wraps each `poll_once` in `except BaseException`:

* rollback/close of the failed sweep's session is the worker's own responsibility
  (`_session()` `finally: db.close()`); the supervisor holds no session. A subsequent clean
  sweep proves nothing is wedged.
* `_classify(exc) → (bounded_code, is_fatal)`: `OperationalError/ProgrammingError` with
  `no such table` / `no such column` / `does not exist` → `("schema_error", True)`;
  `OperationalError/InterfaceError/DBAPIError/TimeoutError` → `("db_unavailable", False)`;
  `SupervisorFatalError` → `(its code, True)`; anything else → `("sweep_error", False)`.
* **recoverable** → `consecutive_failure_count += 1`, `last_bounded_error_code = code`, bounded
  exponential backoff `run_worker_backoff_min_s → run_worker_backoff_max_s` (×2 each failure),
  **reset to the minimum after any clean sweep** (`consecutive_failure_count = 0`,
  `last_bounded_error_code = None`, `last_successful_poll_at` refreshed). The backoff sleep is
  `_sleep_or_stop` — interrupted **promptly** by the stop signal.
* **fatal** (schema/config/invariant, or `consecutive_failure_count >= 12` →
  `max_consecutive_failures`) → log `critical`, **stop the supervision task** — it does not retry
  forever and does not spin.
* No raw exception text or secret is ever stored (`last_bounded_error_code` is a fixed slug) or
  logged (class name + bounded code only).

## Health / readiness contract

`GET /api/health/worker` → `{"durable_run": <status>, "durable_path_ready": <bool>}`.

* `status` keys (exactly): `enabled`, `started`, `alive`, `stopping`,
  `last_successful_poll_at` (ISO-8601 UTC or null), `last_bounded_error_code` (bounded slug or
  null), `consecutive_failure_count` (int). **Never** a prompt, tool name/args/result, provider
  error, API key, user id, session id, run id, or database URL.
* `durable_path_ready` = `True` when `run_public_api_enabled` is off; otherwise `True` **only**
  when the worker is enabled, `alive`, and not in a fatal state (`schema_error` / `config_error`
  / `max_consecutive_failures`). When `False` the endpoint returns **HTTP 503** so a readiness
  probe cannot conclude the durable execution path is healthy.
* `/api/health` (legacy chat health) is **byte-for-byte unchanged** — same five keys, same
  behavior, whether or not the durable flags are set.

## Feature-flag matrix

| `run_engine_enabled` | `run_worker_enabled` | `run_chat_stream_enabled` | `run_public_api_enabled` | Behavior |
|:-:|:-:|:-:|:-:|---|
| off | off | off | off | supervisor idle — no task, no thread; `/api/health` unchanged; `/api/health/worker` 200, `enabled=false`, `durable_path_ready=true` |
| **on** | off | – | – | supervisor idle (worker flag off) — no task; durable runs can be *submitted* internally but nothing executes them |
| **on** | **on** | off | off | supervisor runs; worker sweeps `recover_expired` + `advance_mcp_runs` (+ `advance_chat_runs` is a no-op without its flag) |
| on | on | **on** but `run_engine_enabled` off | – | **submit fails closed** (MP-3C1 `assert_chat_stream_executable` → 503 *before* any persistence); no unusable queued run; supervisor idle |
| on | off | – | **on** | boot: `assert_hmac_key_configured()` must pass; `/api/health/worker` → **503** (`durable_path_ready=false`) — the public durable path is advertised but has no live worker |
| **on** | **on** | **on** | **on** (+ HMAC key) | complete valid durable-chat config — supervisor alive, `/api/health/worker` 200 ready; submit → durable chat → MCP approval → same-run resume (MP-3C2) executes |
| unsafe: `run_worker_web_concurrency > 1` **and** SQLite **and** worker enabled | | | | **startup HALTED** — `SupervisorFatalError` (repository's strongest safe rejection) |

Legacy `/messages/stream` behavior is unchanged in every row (cutover is MP-3C3B+).

## Deployment topology

**Supported initial topology:** self-host, **exactly one web process**, SQLite *or* PostgreSQL.
The embedded worker runs inside that single web process's event loop.

* Only **one** web process may enable the embedded worker before the MP-6 PostgreSQL
  concurrency verification. `run_worker_web_concurrency` (default 1) is the operator's declared
  web-process count; `assert_embedded_topology_ok()` also best-effort reads `WEB_CONCURRENCY` /
  `GUNICORN_WORKERS` / `UVICORN_WORKERS` / `GUNICORN_CMD_ARGS -w N`. If the effective count is
  `> 1` **and** `database_url` is SQLite **and** the worker is enabled → **startup is halted**.
* PostgreSQL topology may run `> 1` web process (the lease/`version`-CAS/`dispatch_records`
  approach is PG-safe by construction) but the concurrency matrix + `SELECT … FOR UPDATE SKIP
  LOCKED` tightening remain the **MP-6** gate — this phase does **not** claim hosted/multi-process
  safety.
* A **future dedicated worker process** is a supported evolution: `RunWorker(session_factory)` +
  `RunWorkerSupervisor(worker_factory=…)` are unchanged interfaces; a standalone entrypoint would
  construct the same supervisor without a FastAPI app. No Redis / Celery / scheduler framework /
  queue was introduced.

## Marker adversarial result

The canonical capture/parser was spot-checked against JSON arrays with internal `]`, quoted
strings containing `]` and `}` and escaped quotes/backslashes, nested objects/arrays,
char-by-char split around internal brackets, a marker followed by discarded trailing text
(including a stray `}`/`]`), a multi-line JSON body, and an unsupported `[PYTHON_CODE]` marker.

**No weakness found — no parser change made.** `MarkerScanner` captures exactly one line;
`app.tools.mcp_tool.extract_mcp_calls`'s regex `\{.*\}` is greedy **with backtracking**, so it
locks onto the **first** `}]`-terminated body that `json.loads` accepts and ignores any trailing
text; `]` / `}` inside JSON strings, arrays and objects are handled by `json.loads`. A body that
never validates on the line (multi-line JSON, genuinely malformed) yields **zero** calls →
`chat_tools.parse_one_mcp_call` raises `ChatToolMarkerError` (`tool_marker_malformed`) → the run
fails closed. The clean prefix is always preserved; marker syntax, arguments, and trailing text
are never emitted to the text stream.

## Preserved guarantees

MP-2 approval binding + at-most-once dispatch, MP-3A semantic idempotency + SSE ordering, MP-3B
one-final-Message + cancellation, MP-3C1 snapshot/marker/EOF/thread/provider-key protections,
MP-3C2 same-run approval/tool/continuation, strict origin-session binding, encrypted `RunPayload`
handling, free/local-only provider policy, no implicit paid fallback — **all untouched**. The
worker changes are additive and inert unless a supervisor calls `attach_stop`.

## Focused test counts

| Check | Result |
|---|---|
| `compileall` — `config.py`, `main.py`, `worker.py`, `worker_supervisor.py`, `run_engine/__init__.py`, the new test | clean |
| `tests/test_run_engine_mp3c3a.py` (new) | **31 passed** |
| `tests/test_run_engine_worker.py` + `test_run_engine_recovery.py` (`RunWorker` additive stop hook) | **passed** |
| `tests/test_run_engine_mp3b.py` (`_run_chat_generation._guard()` touched) | **passed** |
| `tests/test_run_engine_mp3a.py` (lifespan via `TestClient`) | **passed** |
| `tests/test_run_engine_mp3c2.py` — happy-path + plain-chat spot checks | **passed** (2/2; MarkerScanner/parser unchanged so the full 111 were not rerun) |
| combined focused run | **67 passed** + 31 = **98 passed** |
| `import app.main` · `git diff --check` | OK · clean |

Not run (deferred to the final Phase 1B gate): the 119-test directly-affected group, the full
111-test MP-3C2 suite, the 613-test hermetic regression, the full suite, PostgreSQL, Docker,
Ollama, ChromaDB, frontend build, live-provider.

## Remaining risks / blockers

- **Multi-process hosted safety** — not claimed. SQLite + `> 1` web process is fail-closed;
  PostgreSQL multi-process awaits the **MP-6** concurrency matrix + `FOR UPDATE SKIP LOCKED`.
- **`/messages/stream` cutover + frontend** — still out of scope (MP-3C3B+). The live chat path
  still shows "approval required" and executes nothing.
- **Forced-shutdown grace vs. a very slow provider** — *(see the close-out section for the
  final behavior)* if a single sweep's provider ignores the stop signal past
  `run_worker_shutdown_grace_s`, `stop()` records `shutdown_deadline_exceeded`, keeps the status
  `alive: true` / `stopping: true` (never claims "stopped"), and returns; the owned
  `daemon=True` thread does **not** block process exit and is abandoned at interpreter exit. No
  run is falsified. Operators on slow local models should raise the grace above their model's
  worst-case idle-tick latency.
- **Dedicated worker process** — interface-ready, not built (future MP-6 topology).
- **Broad regression / Docker / PG / service-backed** — deferred to the final phase gate.

## Not started (confirmed)

`/messages/stream` replacement, frontend/SSE UI, chat-UX changes, reusable session grants,
Python/shell/browser/desktop execution, paid providers/fallback, Redis/Celery/new queue
framework, the PostgreSQL concurrency matrix, Docker/service-backed verification.

## Source control (confirmed)

Nothing was committed, merged, pushed, rebased, reset, stashed, cleaned, or discarded; no branch
switch. The pre-existing stash was not touched. The entire dirty working tree is preserved on
`phase-1b1-approval-domain` at HEAD `b2e6718`.

---

## MP-3C3A lifecycle close-out (2026-09-11)

Five lifecycle concerns resolved with the smallest safe change. **Files changed:**
`backend/app/run_engine/worker_supervisor.py` (execution model + exception boundary + readiness),
`tests/test_run_engine_mp3c3a.py` (`_task`→`_thread`; +10 close-out tests). No `main.py`,
`config.py`, or other change; `run_engine/__init__.py` exports unchanged.

### 1 — Shell / process cleanup

The "2 shells still running" were **completed-but-hung background jobs from the *previous*
session's verification**, not leaked application processes:
- `bjmq9c90a` — a chained `compileall && diff && pytest && git` whose **pytest sub-process
  deadlocked ~50 min** on a SQLite lock on `C:\data\app.db`. Root cause: an early draft of three
  `/api/health/worker` tests enabled both worker flags under `TestClient`, so the *real* lifespan
  supervisor started a real sweep against the real DB and contended on file locks. That draft was
  already fixed (those tests now inject a fake supervisor); the actual MP-3C3A verification was
  re-run cleanly (`bt8yju2nd` → 31 passed).
- Leaked OS processes: WINPID **15580** (`.venv/Scripts/python` launcher) + **5212**
  (base interpreter, 192 MB — the stuck pysqlite). Terminated by **exact WINPID** with
  `taskkill /F /T` (my earlier attempt used the MSYS cygwin PID, not the WINPID — that is why it
  survived; `wmic` is removed on this Windows 11 build). Verified gone via `tasklist`.
- Unrelated `python.exe` (WINPID 2444, 1248, from 12:51 / 13:09 — different interpreters,
  different timestamps, `uv`-managed) were **not touched**.
- **Confirmed:** no pytest, `run-worker` / `run-worker-supervisor`, `chat-lease-hb`,
  `chat-provider-bridge` thread, or test server from MP-3C3A remains active.

### 2 — BaseException / CancelledError boundary corrected

The prior `_supervise` used `except BaseException` around the sweep — it would have classified
`asyncio.CancelledError` / `KeyboardInterrupt` / `SystemExit` / `GeneratorExit` as `sweep_error`
and backed off. **Fixed:** the sweep-failure boundary is now `except Exception` (which by
language definition never catches those four). A dedicated
`except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit)` does **minimal
cleanup only** — sets `last_bounded_error_code = "worker_interrupted"` (a bounded slug, *not*
`sweep_error` / `db_unavailable`), does **not** touch `consecutive_failure_count`, does **not**
back off — then **re-raises** so the loop and the thread end immediately. `_classify` is now
typed `(exc: Exception)`.

### 3 — Actual `asyncio.to_thread` behavior (proven, CPython 3.11.15)

A runtime probe established:

| Claim in the prior report | Reality |
|---|---|
| "the daemon sweep thread ends with the process" | **FALSE.** `asyncio.to_thread` worker threads are **`daemon=False`** (named `asyncio_N`). |
| cancelling the awaiting task stops `poll_once` | **FALSE.** After `future.cancel()` the underlying function kept running; it finished only when its own cooperative event was released. |
| loop teardown does not wait for it | **FALSE.** `loop.shutdown_default_executor()` (run by `asyncio.run` on close) **joins** a still-running `to_thread` function (measured a 0.61 s wait). |

⇒ with the old model an **uncooperative in-flight call could keep FastAPI shutdown alive past
`run_worker_shutdown_grace_s`**. The prior report's claim is corrected here and in the architecture
doc.

### 4 — Smallest safe correction: one owned `daemon=True` thread

The sweep+backoff loop now runs on **one explicitly owned `threading.Thread(target=self._run_loop,
name="run-worker", daemon=True)`**, not `asyncio.to_thread`. Rationale (repository-evidenced —
this mirrors `scheduler_service`'s module-owned singleton pattern, and `RunWorker.poll_once`'s
sub-calls already each open + close their own session on the calling thread):

- **`daemon=True`** ⇒ a stuck worker thread **cannot** block `asyncio.run()` / process exit
  (verified: the loop's `shutdown_default_executor` join no longer applies — there is no executor
  thread). We never *rely* on the abrupt daemon kill; cooperative stop is primary.
- **Owned handle** ⇒ `alive` = `self._thread.is_alive()` (the truth); `stop()` waits up to
  `run_worker_shutdown_grace_s` with a **non-blocking `await asyncio.sleep(0.05)` poll**, never a
  blocking `join`.
- **No force-kill.** No `SQLAlchemy` Session crosses a thread (each `poll_once` sub-call owns
  its session on the worker thread, same as before). The cooperative stop Event is consulted by
  `_run_chat_generation._guard()` / `_on_idle()` (MP-3C3A, unchanged) so a well-behaved provider
  iterator closes and the thread exits within one idle tick.
- **MP-2 at-most-once** and the MP-3B/3C2 "one final Message / no false success or cancel"
  guarantees are untouched — an interrupted generation still returns `abandoned/worker_shutdown`
  (no Message, no transition ⇒ `outcome_unknown` next boot); a `dispatch_records.started` MCP
  dispatch still → `outcome_unknown`, never re-executed. Queued and `waiting_for_approval` runs
  remain durable.

### 5 — Shutdown timeout & readiness (honest)

If the grace expires while the worker thread is still alive: `stop()` records
`last_bounded_error_code = "shutdown_deadline_exceeded"`, **keeps** `started = True` and
`stopping = True`, and `alive` continues to reflect `self._thread.is_alive()` — it **never claims
the worker stopped while its thread runs**. `is_durable_path_ready()` now returns **False**
whenever `stopping` is set or `self._stop.is_set()` (stuck-stopping ⇒ unready), in addition to the
fatal-code check (now also including `shutdown_deadline_exceeded`). A later `stop()` call (or the
thread finishing on its own) completes the cleanup.

### 6 — Web-concurrency guard accuracy

`assert_embedded_topology_ok()` already takes `max(run_worker_web_concurrency, detected env
count)` where the env count reads `WEB_CONCURRENCY` / `GUNICORN_WORKERS` / `UVICORN_WORKERS` /
`GUNICORN_CMD_ARGS -w N`. **Added:** when a detected value **disagrees** with
`run_worker_web_concurrency`, a `WARNING` is logged. **Documented explicitly:** SQLite +
`effective count > 1` is the **only** case auto-blocked (`SupervisorFatalError`, halts startup);
**PostgreSQL multi-process is NOT auto-protected** (the MP-6 concurrency-matrix gate) — no
automatic multi-process safety is claimed there, and `run_worker_web_concurrency` **must** be set
to the real server process count. No Redis / Celery / queue / process-supervisor introduced.

### Thread / task ownership model (final)

| Piece | Owner / lifetime |
|---|---|
| sweep + backoff loop | `RunWorkerSupervisor._run_loop` on **one owned `threading.Thread` `run-worker`, `daemon=True`**, created in `start()`, ends on cooperative stop / fatal error / a propagating BaseException |
| stop-interruptible sleep | `threading.Event.wait(timeout)` on that thread — no asyncio |
| every DB `Session` | opened + closed inside `RunWorker._session()` / `TextEventBuffer` / `_ChatHeartbeat` on the thread that opened it — **none crosses a thread** |
| `start()` / `stop()` | thin `async` methods on the app event loop; `stop()`'s wait is a non-blocking `asyncio.sleep(0.05)` poll of `is_alive()` |
| provider coroutine | driven **inline** on the worker thread (`run_stream` sees no running loop) — no bridge thread |
| cooperative stop signal | `threading.Event` owned by the supervisor; `worker.attach_stop(event.is_set)` |

### Health state while stuck stopping

`/api/health/worker` → `alive: true`, `stopping: true`, `started: true`,
`last_bounded_error_code: "shutdown_deadline_exceeded"`, `durable_path_ready: false`, **HTTP
503** (when `run_public_api_enabled`). No secret / identifier / raw text in any field.

### Residual limitation (documented, accepted)

A truly **uncooperative synchronous third-party call** inside `poll_once` (one that never yields
and never checks the stop Event) cannot be interrupted without force-killing a thread. In embedded
self-host mode the outcome is honest and safe: the `daemon=True` thread does **not** block process
exit, the status truthfully reports `alive: true` / `shutdown_deadline_exceeded`, no run is
falsified, and the thread is abandoned at interpreter exit (its `poll_once` sub-operations are
individually atomic + crash-recoverable). The **only fully-bounded** fix is a **separate worker
process** — a future MP-6 production topology, **not implemented here**. The `RunWorker` /
`RunWorkerSupervisor(worker_factory=…)` seams already support it.

### Focused verification (re-run at resume, 2026-09-11)

| Check | Result |
|---|---|
| `compileall` — `worker_supervisor.py`, `worker.py`, `main.py`, `config.py`, `run_engine/__init__.py`, the test | clean |
| `python -m pytest tests/test_run_engine_mp3c3a.py -q` | **41 passed**, 1 warning (unrelated `python_multipart` PendingDeprecation) — 31 original + 10 lifecycle close-out |
| `import app.main` · `git diff --check` | OK · clean |
| leftover process / thread scan | none (`.venv` python, `run-worker`, pytest all absent) |

The lifecycle close-out touched **only** `worker_supervisor.py` + the test module.
`worker.py`'s `attach_stop`/`_stopping()`/`_run_chat_generation` shutdown-abort were added by the
original MP-3C3A and are **unchanged** here, so `RunWorker` stop behavior is not altered ⇒
`test_run_engine_{worker,recovery}` not rerun. MP-3C2's 111-test suite, the 119-group, the 613
regression, PG / Docker / service-backed / frontend / live-provider — **not** rerun.

### Resume audit (2026-09-11, post-interruption)

The prior session hit its usage limit after the edits + one green test run but before this
consolidated re-verification. On resume: `worker_supervisor.py` was re-read in full and is
**structurally clean** — one docstring, one import block, one `RunWorkerSupervisor`, one
`_run_loop`, one `_stop` Event, `alive` from `thread.is_alive()`; **no** `_task` field, **no**
`_supervise`/`_sleep_or_stop` method, **no** `asyncio.to_thread`, **no** `task.cancel()`, **no**
duplicated constants / topology checks / loop. `worker.py`, `main.py`, `config.py`,
`run_engine/__init__.py` and the 41-test module were re-inspected and match the intended final
model. The completion-report body above and architecture §35.2–35.5 were annotated as
**superseded historical text** pointing at this close-out / §35.11. No code correction was
needed at resume. Source control untouched (HEAD `b2e6718`, pre-existing stash intact).
