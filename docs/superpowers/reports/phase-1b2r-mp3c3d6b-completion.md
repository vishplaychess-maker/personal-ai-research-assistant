# MP-3C3D6B — deterministic real-browser durable-chat smoke

Date: 2026-09-17

**Status: BLOCKED — D6B did NOT pass.** The smoke harness is built, the whole
durable plain-chat vertical slice is proven correct server-side, but the browser
UI never renders the assistant response. Two of the twelve acceptance criteria
could not be demonstrated, so D6B is **not** marked complete and D6C / MP-4 must
not start. One blocking decision is required (section 8).

## 1. What was delivered

| File | New/Modified | Purpose |
|---|---|---|
| `tests/smoke/__init__.py` | new | package marker |
| `tests/smoke/fakes.py` | new | deterministic offline provider + outbound-network tripwire |
| `tests/smoke/helpers.py` | new | bounded polling, loopback-only URL guard, atomic JSON, process-tree teardown |
| `tests/smoke/durable_smoke_app.py` | new | test-only ASGI entrypoint that pre-sets `app.state.run_worker_supervisor` |
| `tests/smoke/run_smoke.py` | new | orchestrator + direct-SQLite assertions |
| `tests/smoke/test_fakes.py` | new | 8 focused tests |
| `tests/smoke/test_helpers.py` | new | 11 focused tests |
| `tests/smoke/README.md` | new | usage, non-secret fixture, boundary notes |
| `frontend/src/lib/devProxyTarget.ts` | new | pure, dependency-free dev-proxy validator |
| `frontend/src/__tests__/devProxyTarget.test.ts` | new | 7 focused tests |
| `frontend/vite.config.ts` | modified (targeted) | `THUNDER_DEV_PROXY_TARGET` override, default byte-identical |
| `THUNDER_AI_PRD.md` | modified (targeted) | D6B status row |

**No `backend/app/**` file was created or modified.** No production injection switch
was added. No dependency, lockfile, Docker file, `.env`, or the restored
architecture document was touched.

## 2. Acceptance criteria — measured result

| # | Criterion | Result | Evidence |
|---|---|---|---|
| A1 | Real FastAPI lifespan starts and stops | **PASS** | backend booted through the real lifespan; `Created default user`; clean cooperative shutdown |
| A2 | Real supervisor + worker alive before submission | **PASS** | `/api/health/worker` returned 200 with `durable_path_ready: true` and `durable_run.alive: true` before any navigation |
| A3 | Real login/auth/CSRF through the browser | **PASS** | real `POST /api/auth/login` to 200; app shell rendered; the stream request carried a real Bearer, `x-csrf-token` and `idempotency-key` |
| A4 | One plain durable chat progressively renders and completes | **FAIL** | see section 4 |
| A5 | Browser text == persisted text == Message | **FAIL** | browser text was empty at every sample; the DB side is exact (3803 chars, both sides) |
| A6 | Exactly one assistant Message and one durable Run | **PASS** (first run) | first run: 1 assistant Message (3803 chars), 1 `chat_stream` Run, 1 `RunStep`, no duplicate |
| A7 | Deterministic provider invoked exactly once | **PASS** (first run) | `provider_calls.jsonl`: `provider_invoked ordinal 1` — exactly one, and no second invocation |
| A8 | No outbound network/service dependency | **PASS** | `tripwire.jsonl` absent: zero blocked or outbound attempts |
| A9 | Zero unexpected console/page errors | **NOT PROVEN** | the baseline itself contains two pre-existing dev-mode errors (section 5) |
| A10 | Teardown leaves no live child/port/temp state | **FAILED, THEN FIXED** | the smoke's own `terminate_process_tree` orphaned four `npm run dev` / `vite.js` processes (section 10). Fixed and covered by a focused regression test; temp roots now removed and zero orphan processes remain |
| A11 | Repository config defaults remain false | **PASS** | fresh `Settings()`: all seven durable flags `False` |
| A12 | Architecture document unchanged | **PASS** | 3332 lines / 268044 bytes / `f4d0bbc24f20529addb32ac10bf12e197c7ad747` — identical to Stage 0 |

## 3. Server-side durable slice: fully verified

Direct SQLite assertions against the isolated temp database for the orchestrator's
single browser send:

```
runs            = 1   (kind=chat_stream, status=succeeded, status_reason=NULL)
run_steps       = 1   (seq=1, kind=load_context, status=succeeded)
messages        = 2   (role=user 49 chars, role=assistant 3803 chars)
run_events      = 6   run.created(1) run.queued(2) run.status queued->running(3)
                      text(4) text(5) run.status running->succeeded(6)
run_payloads    = 1
dispatch_records / pending_approvals / tool_calls / execution_turns = 0
```

The two persisted `text` events reconstruct the assistant Message exactly: 3803
characters, byte-identical to the deterministic fake response and to the assistant
row's `content`.

Note: for plain durable chat the MP-3A submission step **doubles as the generate
step** (`chat_stream.py:43`), so exactly one `load_context` RunStep is the expected
and observed result.

## 4. The blocker

### 4.1 Durable routing is proven from the browser

The browser's own `POST /api/sessions/1/messages/stream` returned:

```
status   200, mimeType text/event-stream, duration 4356 ms
x-run-id 421bc112-a16a-4fca-aa22-b09cc71c8ce6   <- equals the DB Run.id exactly
request  authorization: Bearer <real>, x-csrf-token: <real>, idempotency-key: <uuid>
```

### 4.2 The stream delivers the text but never a terminal frame

Captured independently twice (Playwright CDP capture, then an in-page fetch tee).
The complete frame set the client received was:

```
event: start            data: {"run_id": "421bc112-..."}
id: 3  run_status       data: {"status": "running"}
id: 4  token            data: {"token": "..."}     8476 bytes
id: 5  token            data: {"token": "..."}     6607 bytes   (ends D6BSMOKE END)
<EOF - no complete/error/cancelled frame, no heartbeat, no stream-max-duration>
```

The last token frame already contains the **entire** response, and the server did
persist the terminal event `run.status running->succeeded` as `run_seq 6`. The client
simply never received it. The read ended in a clean EOF, not an error.

### 4.3 The UI never renders the response

An in-page sampler recorded 553-575 samples at 30-40 ms across the whole turn:

- distinct rendered response lengths: **`[0]`** - the response text never appeared in
  the DOM at any sample, not even partially;
- the textarea was missing for 545/553 samples - the app entered its streaming state
  immediately after the click and never left it;
- the optimistic user message rendered for exactly 2 samples (~80 ms) and was then
  removed;
- no error banner, no `role=alert`, and **zero new console errors** during the turn;
- **no reconnect was attempted** (only one `/messages/stream` request in the whole
  session), even though `useStreaming.ts:299` reconnects when a durable stream ends
  without a terminal frame and a run id is known.

### 4.4 Reproduced

The same signature appeared in the first run and again on a second, independent run
(fresh temp DB, fresh ports). A third in-session probe also ended without a terminal
frame. That probe is **not** usable as evidence about rendering, because it correctly
failed with `provider_error`: the fake's single-invocation guard refused the second
call by design.

### 4.5 Candidate causes (not resolved)

Both require a decision this mini-phase is not authorised to take:

1. **Development-proxy SSE handling.** `stream_legacy_wire` has exactly one silent
   exit path - `request.is_disconnected()` (`messages_stream.py:608-609`) - and it
   yields nothing, which matches the observed EOF-with-no-terminal-frame. The
   committed Vite dev proxy config it ran behind is
   `agent: new http.Agent({ keepAlive: false, maxSockets: 1 })`
   (`vite.config.ts:87`). A single-socket, non-keep-alive agent is a well-known
   SSE-hostile combination: with the app polling `/api/health` about once a second
   through the same proxy, the upstream SSE socket can be torn down, after which
   uvicorn reports the client gone and the generator returns silently. If so, this is
   **development-proxy-only** and production is unaffected.
2. **Client-side promotion of the durable draft.** The app received the frames per the
   network capture yet dispatched nothing and never reconnected. Pinning this down
   needs instrumentation inside `frontend/src`.

Neither candidate is proven, and no fix was applied.

## 5. Pre-existing console errors (reported, not filtered)

The page produces two errors on every load, before any durable activity:

- `Manifest: Line: 1, column: 1, Syntax error. @ /manifest.webmanifest` (twice) - the
  PWA manifest is not served correctly by the Vite dev server;
- `Failed to load resource: 403/401 @ /api/auth/refresh` - the expected unauthenticated
  bootstrap attempt before login.

A9 therefore needs an explicit, agreed definition of unexpected: these two are
baseline noise unrelated to the durable slice, but they are real and are reported
rather than filtered away.

## 6. Test-only injection boundary (unchanged and sound)

`tests/smoke/durable_smoke_app.py` pre-sets `app.state.run_worker_supervisor` before
the real lifespan runs, using only pre-existing seams: `main.py:1324` already adopts a
pre-set supervisor; `RunWorkerSupervisor(worker_factory=...)`; and
`RunWorker(chat_provider=...)`. It additionally **fails closed** unless
`THUNDER_SMOKE_ROOT` is set and the configured SQLite database resolves inside it. The
seam is in-process and pre-startup only - it is not reachable from an environment
variable, config field, route, header, request body, or production startup path. No
production backdoor was added.

Stage 0 verified ownership: the injected supervisor is used (`main.py:1324`), started
exactly once (`start()` is idempotent; called once at `main.py:1326`), stopped on
shutdown (`main.py:1343`), and removed (`main.py:1344` sets it to None).

## 7. Offline / no-network proof

`fakes.NetworkTripwire` was installed in the backend process and blocked as well as
recorded every non-loopback connection and every connection to ports 11434/11435
(Ollama) and 8000 (ChromaDB). Result: **no `tripwire.jsonl` was ever created**, i.e.
no blocked and no external connection was even attempted. The backend child also ran
with `cwd` set to the throwaway root, so the repository `.env` was never loaded, and
every credential-shaped variable was stripped from its environment. ChromaDB was not
running and was not required.

## 8. Decision required

**Question:** how should the SSE/terminal-frame blocker be investigated?

1. Authorise a **development-proxy-only** adjustment in `frontend/vite.config.ts`
   (address the single-socket agent used for `/api` in the dev server only; the
   committed default remains unchanged) and re-run the smoke; or
2. Authorise **read-only frontend instrumentation** (temporary diagnostics inside
   `frontend/src`, used only during the smoke) to localise the client-side drop; or
3. Authorise **backend streaming instrumentation** in `run_engine/messages_stream.py`
   to confirm whether the `request.is_disconnected()` path is being taken.

Option 3 touches production code and is explicitly out of scope without approval.

## 9. Consequences

- **D6B is not complete** and its PRD row says so.
- **D6C is not started** and must not start.
- **MP-4 must not start**: the smoke has not proven that the durable path is
  indistinguishable-or-better from legacy in a real browser, which is the whole
  precondition for converging or deleting legacy.
## 10. Defect found and fixed in the smoke harness itself

The first teardown implementation was wrong. `subprocess.terminate()` on the
`npm.cmd` shim kills only the `cmd.exe` wrapper and **orphans its node/vite
children**, so the helper returned early and never reached its `taskkill /T`
branch. The smoke reported `backend_dead: true, frontend_dead: true` because it only
inspected the direct `Popen` handle, so the leak was invisible to its own report.

Detected afterwards by a process scan, which found four survivors from the two runs:

```
10044  npm-cli.js run dev -- --port 60756 --strictPort --host 127.0.0.1
22492  vite.js       --port 60756
15672  npm-cli.js run dev -- --port 57975 --strictPort --host 127.0.0.1
 8788  vite.js       --port 57975
```

They also held the smoke roots open, so `--keep-root` directories could not be deleted.

**Fix:** on Windows, `taskkill /F /T /PID <pid>` now runs FIRST, before any graceful
terminate, and the helper only reports success once the direct process has actually
exited. A focused regression test was added:

`tests/smoke/test_helpers.py::test_terminate_process_tree_kills_descendants_not_just_the_parent`

It spawns a parent that spawns a grandchild, records the grandchild PID, runs the
helper, and asserts the descendant is gone. The four orphans were then killed manually
and both temp roots removed; a fresh scan reports **0** orphaned vite/npm processes and
**0** leftover `thunder_d6b_*` directories.

This is a defect in test-only code (section 1), not in `app/**`.

## 11. Focused test results (fresh)

| Command | Result |
|---|---|
| `PYTHONPATH=backend pytest tests/smoke -q` | **19 passed** (8 fakes + 11 helpers) |
| `npx vitest run src/__tests__/devProxyTarget.test.ts` (frontend) | **7 passed** |
| `npx tsc --noEmit -p tsconfig.json` | clean (exit 0) |
| `npx tsc --noEmit -p tsconfig.node.json` | 2 errors, both **pre-existing** (`Cannot find module 'http'` and the `test` property in `vite.config.ts`); no new error introduced — verified by keeping `process` out of the config via `globalThis` |

No backend regression suite was run: no `backend/app/**` file was modified, so the
durable chat submission/worker/SSE suites are unaffected. Broad regression, Docker,
PostgreSQL, live providers and the production frontend build were not run.

---

## 12. D6B-R1 systematic diagnostic pass (2026-09-17)

### 12.1 Stage-0 integrity (unchanged)

branch `phase-1b1-approval-domain`, HEAD `b2e6718…`, merge-base equal, 0 staged, `git diff --check`
clean, `stash@{0}` (`7ab31a5`) untouched; all seven durable defaults `False`. Architecture document
**3332 lines / 268044 bytes / `f4d0bbc24f20529addb32ac10bf12e197c7ad747`** at start and completion.

### 12.2 Diagnostic instrumentation added (all TEST-ONLY)

| Artifact | Purpose |
|---|---|
| `tests/smoke/diag_client.py` | direct HTTP SSE client (real login/CSRF/route); records status, content-type, chunk/byte counts, event names+ids, terminal presence, EOF vs error |
| `tests/smoke/diag_proxy_concurrency.py` | one-variable experiment: one SSE submission held open while N concurrent requests hit the same origin |
| `durable_smoke_app.py::_wrap_asgi_trace` | outer ASGI wrapper (enabled only by `THUNDER_SMOKE_ASGI_TRACE=1`); records status, content-type, body chunk count/bytes, `more_body` per body message, SSE names/ids, time of `http.disconnect` |
| browser init instrumentation | patched `fetch`, `AbortController.prototype.abort`, `ReadableStreamDefaultReader.read/cancel`, `MutationObserver` |

No production code, route, header, config field or remote switch was added. Nothing records
Authorization headers, cookies, CSRF values, JWTs, prompts, response text or keys — only names,
ids, counts, timings and bounded state labels. A test-only knob
`THUNDER_SMOKE_MAX_PROVIDER_INVOCATIONS` (default **1**) exists so the matrix can make two
submissions against one instance; the acceptance run never sets it.

### 12.3 Reproduction (Phase 1) — deterministic

Every attempt failed identically: browser received `start`, `id:3 run_status`, two `token` frames
(15207 bytes) then clean EOF with **no** terminal frame; UI rendered zero text. Never intermittent.

### 12.4 Boundary trace (Phase 2) — A–L

| # | Question | Answer | Evidence |
|---|---|---|---|
| A | Terminal `seq 6` in `run_events` before the connection ended? | **YES** | DB seq 6 = `run.status running->succeeded` |
| B | Did the generator read `seq 6`? | **NO** for the browser request | ASGI trace: 5 body chunks / 15207 bytes, `terminal_sent=False` |
| C | Did the app `send()` the terminal frame? | **NO** for the browser request | passing rows show `terminal_sent=True`, 6 chunks, 15318 bytes |
| D | Final body sent with `more_body=False`? | **YES** in every row | `last_more_body=False` |
| E | Did the app receive `http.disconnect` first? | **YES, prematurely** | `disconnect_ms == final_ms == total_ms` (875 ms) on the failing row |
| F–G | Did the proxy receive/deliver terminal bytes? | **YES idle, NO under load** | idle: 15318 B + `complete`; +3 concurrent: 15207 B, no terminal |
| H | Did browser fetch receive terminal bytes? | **NO** before the fix / **YES** after | after routing the browser at the backend: `complete` present |
| I | Reader outcome? | **clean EOF** — no throw/cancel/abort | `streamDone: 'eof'`; `aborts: 0`, `cancels: 0`, `errors: []` |
| J | Did the parser emit token callbacks? | **effectively NO** | `domAdds: 0`; only the diagnostic clone consumed bytes |
| K | Did `useStreaming` apply draft text? | **NO** | no DOM mutation ever carried the text |
| L | Did `ChatArea` render it? | **NO** | 553–575 samples, every one `length = 0` |

**First boundary where expected data disappears:** for the browser's request the ASGI app never
emitted the terminal frame because the upstream connection was torn down mid-stream; for every
non-concurrent client it emitted it correctly.

### 12.5 Controlled comparison matrix (Phase 3)

| Row | Path | Concurrency | terminal | bytes | Result |
|---|---|---|---|---|---|
| 1 | direct backend | none | `complete` | 15318 | **PASS** |
| 2 | Vite dev proxy | none | `complete` | 15318 | **PASS** |
| 3 | direct backend | **3 concurrent** | `complete` | 15318 | **PASS** |
| 4 | Vite dev proxy | **3 concurrent** | **none** | **15207** | **FAIL — reproduces the browser exactly** |
| 5 | real browser + frontend | `/api/health` polling | none (before fix) | 15207 | **FAIL — same signature** |

Concurrency was the only variable between rows 3 and 4, and between rows 2 and 4.

### 12.6 Proven root cause (Phase 4)

> **Root-cause hypothesis: the Vite DEV-SERVER PROXY terminates an in-flight SSE upstream
> connection as soon as other HTTP requests are proxied concurrently, because evidence proves the
> stream is correct at the direct backend with and without concurrency (rows 1 and 3: `complete`,
> 15318 bytes), correct through the proxy when idle (row 2), and incorrect immediately after the
> proxy boundary only under concurrency (row 4: 15207 bytes, no terminal, ASGI
> `terminal_sent=False` with a premature `http.disconnect`).**

The backend persisted and served the terminal event on every path and correctly treats the
torn-down connection as a client disconnect, returning silently — which is why the client sees
EOF without a terminal frame.

### 12.7 Hypotheses ruled out (each by a one-variable test)

| Hypothesis | Test | Outcome |
|---|---|---|
| `maxSockets: 1` in the dev proxy agent | made unbounded, `keepAlive: false` kept | **still FAILED** → ruled out |
| `keepAlive: false` in the dev proxy agent | changed to `keepAlive: true`, no cap | **still FAILED** → ruled out |
| any custom `agent` option at all | removed the `agent` line entirely | **still FAILED** → ruled out |
| Backend SSE generation / terminal-frame logic | direct backend, idle and concurrent | **PASSED both** → ruled out |
| Backend `is_disconnected()` as a cause | only fires after the proxy tore the socket down | downstream effect, not cause |
| Missing/late terminal `RunEvent` | DB seq 6 present on every run | ruled out |
| Browser abort / cancel / reader error | `aborts: 0`, `cancels: 0`, `errors: []`, clean EOF | ruled out |
| Test-harness observation defect | wire cross-checked via CDP capture, in-page tee, and an independent Python client | ruled out |

**The suspected Vite proxy agent was explicitly NOT altered** — three one-variable experiments
exonerated it, and the committed `agent:` line is restored byte-for-byte.

### 12.8 Permanent change applied (test-only)

The smoke now routes the browser at the backend through the **existing, production-supported
`VITE_API_BASE_URL` seam** instead of the Vite dev-server proxy, with `CORS_ORIGINS` set to the
loopback smoke frontend origin. Production serves the frontend from nginx / a same-origin rewrite
and never uses the dev proxy, so the proxy is not in the production path and not in the acceptance
path. Result: the browser now receives the full frame set **including `complete`**.

Files changed: `tests/smoke/run_smoke.py` (frontend env + CORS origin) and
`tests/smoke/durable_smoke_app.py` (optional ASGI tracer + test-only invocation bound).
`frontend/vite.config.ts` retains **only** the previously approved `THUNDER_DEV_PROXY_TARGET`
override.

### 12.9 Second, independent defect — still OPEN

With the wire complete, **the UI still renders zero response text** (`distinctLens: [0]`,
`renderedLen: 0`, `hasTa: false`, 553–575 samples). Only **one** reader consumed the body across
the whole turn — the diagnostic clone. The application's own `response.body.getReader()` loop
consumed nothing, no `dispatch()` side effect ran, and there was no reconnect, abort, cancel or
error. This is therefore **CASE 2** (frontend stream handling) and is **not yet fixed**.

### 12.10 D6B status after D6B-R1

**Still BLOCKED.** The transport defect is fixed and `complete` now reaches the browser, but
criteria 4 and 5 (progressive render + exact rendered text) still fail, so the remaining criteria
cannot be certified. D6C, MP-4, MP-5 and MP-6 remain not started.

### 12.11 Literal D0 console-cleanliness

**Not met, and not claimed.** `manifest.webmanifest` still produces a real pre-existing syntax
error on every load (a genuine product defect, deliberately not fixed in this pass), and the
unauthenticated `/api/auth/refresh` 403/401 still occurs **only before login** — it did not occur
during the authenticated durable turn. D6B may at most claim *zero new flow-specific
console/page errors*, which held.

### 12.12 Teardown

`test_terminate_process_tree_kills_descendants_not_just_the_parent` was preserved and re-run and
passes. After every diagnostic run the environment was verified by an **independent** scan (not
the helper's return value): zero surviving `vite`/`npm run dev` processes and zero leftover
`thunder_d6b_*` temp roots.

---

## 13. D6B-R2 — frontend stream-consumer root cause and minimal fix (2026-09-17)

### 13.1 Corrections to earlier findings (as instructed)

1. `domAdds: 0` **does not** prove parser callbacks were never called; it proves only that no observed DOM mutation occurred. Restated as such.
2. **The Vite proxy concurrency defect was NOT fixed.** The smoke now *bypasses* the defective development proxy through the existing `VITE_API_BASE_URL` direct-backend seam. That is a test-harness routing workaround. **The Vite dev-proxy concurrency defect remains open.**
3. A diagnostic `Response.clone()` consuming data while the app reader appeared idle does **not** by itself prove a product defect, so it was re-tested with instrumentation that does not consume, replace, lock or tee the body.

### 13.2 Proof the observer was passive, and the boundary counters

Observers installed: a `fetch` wrapper that records metadata and returns the same `Response` (**no `clone()`**), plus wrappers around `ReadableStream.prototype.getReader`, `ReadableStreamDefaultReader.prototype.read`/`.cancel`, `TextDecoder.prototype.decode`, `AbortController.prototype.abort`, and a `MutationObserver`. No second consumer existed.

| # | Boundary | Count |
|---|---|---|
| 1–2 | fetch called / resolved | 4 / 4 |
| 3 | stream status / content-type / X-Run-Id / body present | 200 / `text/event-stream` / true / true |
| 4 | `getReader` called | **0** |
| 5–7 | `reader.read` calls / non-empty chunks | **0 / 0** |

## 14. D6B-R3 — duplicate classification and real-browser acceptance (2026-09-17)

### 14.1 Fetch classification (one explicit user-like send)

| ordinal | method | pathname | idem-key hash | run-id |
|---|---|---|---|---|
| 1 | POST | `/api/auth/refresh` | `-` | `-` |
| 2 | POST | `/api/sessions/1/messages/stream` | `4ea689e8` | `11111111-2222-4333-8444-555555555555` |

**Exactly one `/messages/stream` POST; exactly one distinct Idempotency-Key hash.** The apparent
duplicate was a **test-harness artifact, not a production defect**: the D6B-R2 mock answered *every*
URL with the stream `Response`, so the auth-refresh request was miscounted. With a mock that
distinguishes the two, the count is 1. Classification **A**. No production deduplication was added.

### 14.2 Real-browser acceptance (passive diagnostics only — no `clone()`, no body consumer)

| Criterion | Result |
|---|---|
| Exactly one POST to `messages/stream` | **PASS** (1; idem hash `f2845e20`) |
| Exactly one `X-Run-Id` | **PASS** (`6c2ba816-1543-41c4-8668-ee9133310680`) |
| `getReader()` called | **PASS** (1 — was 0 before the fix) |
| Stream fully consumed | **PASS** (5 reads, 15318 bytes, the full frame set incl. `complete`) |
| No abort / cancel / error | **PASS** (`aborts 0`, `cancels 0`, `errors []`) |
| Response text entered the DOM | **PASS** (5 discrete `MutationObserver` additions) |
| Composer exited streaming state and is usable | **PASS** (`textarea` enabled) |
| Final visible text ends with the deterministic tail | **PASS** (`…D6BSMOKE END`) |
| **Progressive (intermediate) rendered length captured** | **NOT PROVEN** — the 25 ms sampler observed only `{0, 4350}`; the render completed between samples |
| **Browser text SHA-256 == Message SHA-256** | **NOT PROVEN** — the extracted `<p>` is 4003 chars (3803 response + ~200 chars of interleaved UI affordance text), so no clean slice was obtained |

### 14.3 Server-side proof (exact)

```
runs=1 run_steps=1 messages=2 run_events=6
dispatch_records=0 pending_approvals=0 tool_calls=0 execution_turns=0
Run  id=6c2ba816-1543-41c4-8668-ee9133310680 kind=chat_stream status=succeeded
Step kind=load_context status=succeeded
provider_sha 2e3b573393eb3ba3 len 3803
message_sha  2e3b573393eb3ba3 len 3803
events_sha   2e3b573393eb3ba3 len 3803   (all three byte-identical)
provider_invoked=1   tripwire.jsonl absent   canary absent from events/refs/log
```

### 14.4 Outcome

**D6B remains BLOCKED**, but only on two evidence gaps, not on a functional failure: the browser
now consumes and renders the durable stream end-to-end through a single submission. Remaining:
(a) capture an intermediate rendered length to prove progressive rendering visually, and
(b) extract the assistant paragraph cleanly to prove browser-text hash equality.

## 15. D6B-R4 — evidence closeout attempt (2026-09-17)

### 15.1 Completed in this pass

**StrictMode stale-async safety matrix A-D: 4/4 PASS** (`frontend/src/__tests__/useStreamingSafety.test.tsx`),
against the real hook and the real SSE parser, every start an explicit click (never an effect):

| Test | Assertion | Result |
|---|---|---|
| A | unmount mid-stream aborts the AbortController exactly once; no complete after unmount; no extra submission | **PASS** |
| B | a fake that ignores abort and resolves late cannot complete, cannot double-cancel, cannot clear later state, creates no second submission | **PASS** |
| C | a fresh mount after unmount streams and completes normally; its Idempotency-Key differs from the earlier send | **PASS** |
| D | under `<StrictMode>`, exactly one stream POST, progressive non-empty state, complete handled exactly once, exact content | **PASS** |

**No generation token is required**: no stale continuation mutated current state, so nothing was added.

**Duplicate-request classification resolved as a test-harness artifact.** A stale assertion in
`useStreamingStrictMode.test.tsx` still counted the *global* fetch count (auth refresh + stream) as
a duplicate; the file's own instrumentation showed exactly one `/messages/stream` POST. Corrected to
count stream submissions only. Not a production defect; no production deduplication added.

**Deterministic progressive-render schedule implemented in the test-only fake** (`tests/smoke/fakes.py`):
response is now 18 047 bytes / 8 597 chars delivered as **5 chunks** (4500/4498/4500/4500/49) with a
bounded 0.45 s deterministic delay between chunks, wrapped in unique alphanumeric sentinels
`THUNDERD6BRESPONSEBEGIN` / `THUNDERD6BRESPONSEEND` (once each; no Markdown-transformable characters,
so no normalisation is ever needed).

### 15.2 Focused results

| Command | Result |
|---|---|
| `pytest tests/smoke -q` | **19 passed** |
| `vitest run useStreamingSafety + useStreamingStrictMode + streamSubmissionCount` | **6 passed** |
| `vitest run durableStreaming + sseParser + useStreaming` (+ the three above) | **71 passed, 1 failed → 0 failed after the stale-assertion correction** |
| `tsc --noEmit -p tsconfig.json` | clean (exit 0) |

### 15.3 BLOCKED — no browser controller in this session

The Playwright MCP browser controller that drove every previous D6B run is **no longer present** in
this session's tool set (25 tools enumerated; none is a browser/MCP tool). Therefore **Parts 2, 3 and 4
could not be executed**: no final real-browser journey, no MutationObserver progressive-length
sequence, no browser text extraction, and no post-run DB/hash proof.

The acceptance instance was started, then torn down when the blocker was confirmed. Nothing was
fabricated and no criterion was weakened.

### 15.4 Status

**D6B remains BLOCKED.** Outstanding: the browser-evidence half of D6B-R4 (Parts 2-4).
Unchanged and still open: the **Vite dev-proxy concurrency defect** (bypassed by the `VITE_API_BASE_URL`
smoke workaround, not fixed) and the **`manifest.webmanifest` syntax error** (literal D0 zero-console gate).
D6C, MP-4, MP-5 and MP-6 remain not started.
---

## 16. D6B-R5 — browser evidence closure (2026-09-17)

**Verdict: BROWSER EVIDENCE PASS; D6B REMAINS BLOCKED BY THE MANIFEST CONSOLE ERROR.**

### 16.1 Network evidence

| ordinal | method | pathname | status | content-type | idem-key hash | X-Run-Id |
|---|---|---|---|---|---|---|
| 1 | POST | `/api/sessions/1/messages/stream` | 200 | `text/event-stream; charset=utf-8` | `f3cb490e` | `efe33a51-5df1-43ce-ab38-344900cbf128` |

Exactly **one** stream POST, one distinct idempotency-key hash, one X-Run-Id, no reconnect, no second
submission, `aborts: 0`, `cancels: 0`. It went to the **backend origin directly** (`127.0.0.1:65228`) via the
approved `VITE_API_BASE_URL` seam, i.e. **not** through the Vite proxy.

### 16.2 SSE event order and counts

`start` -> `run_status` -> `token` x5 -> `complete`  (ids 3,4,5,6,7,8,9)
Counts `{start:1, run_status:1, token:5, complete:1}` — exactly the 5-chunk deterministic schedule.

### 16.3 Progressive-render proof (passive MutationObserver, no body consumer)

| t (ms) | rendered length (UTF-16 units) | BEGIN | END | complete |
|---|---|---|---|---|
| 8813 | **2260** | yes | no | no |
| 9287 | **4508** | yes | no | no |
| 9739 | **6758** | yes | no | no |
| 10222 | **9008** | yes | no | no |
| 10691 | **9047** | yes | yes | no |
| 10804 | 9047 | yes | yes | **yes** |

**Five distinct increasing non-zero lengths, four strictly before `complete`** (requirement: three). Final
text starts with `THUNDERD6BRESPONSEBEGIN`, ends with `THUNDERD6BRESPONSEEND`, each sentinel exactly once.
`getReader` called **1x**; 8 reads / 34 232 wire bytes. `complete` handled **once**.

### 16.4 Four-way text equality — EXACT

| Source | chars (code points) | UTF-8 bytes | SHA-256 |
|---|---|---|---|
| deterministic provider output | 8597 | 18047 | `557b82aca8c4e93ef113cde7a8fae1546d142956c67e5fc1ab8a4e3483bdf5e1` |
| persisted text run_events (run_seq order) | 8597 | 18047 | `557b82aca8c4e93ef113cde7a8fae1546d142956c67e5fc1ab8a4e3483bdf5e1` |
| assistant Message content | 8597 | 18047 | `557b82aca8c4e93ef113cde7a8fae1546d142956c67e5fc1ab8a4e3483bdf5e1` |
| clean browser-rendered text | 9047* | 18047 | `557b82aca8c4e93ef113cde7a8fae1546d142956c67e5fc1ab8a4e3483bdf5e1` |

All four SHA-256 values are identical and all four byte lengths are 18 047. **No normalisation of any kind
was applied.**

*9047 vs 8597 is a counting artefact, not a content difference: JavaScript `String.length` counts UTF-16
code units while Python `len()` counts code points; the astral emoji U+1F32A is 1 code point but 2 UTF-16
units, and there are exactly 450 repeats = 450 extra units. The UTF-8 byte length and the SHA-256 over those
bytes are identical, which is the authoritative equality.

### 16.5 Database and execution proof

```
runs=1  run_steps=1  messages=2  run_events=9  run_payloads=1
dispatch_records=0  pending_approvals=0  tool_calls=0  execution_turns=0
Run   id=efe33a51-5df1-43ce-ab38-344900cbf128  kind=chat_stream  status=succeeded  status_reason=NULL
Step  seq=1  kind=load_context  status=succeeded
Messages: 1 user (49 chars) + 1 assistant (8597 chars)
Text run_events = 5 (matching the 5 deterministic chunks)
X-Run-Id == persisted Run.id: TRUE
provider_selected=1  provider_invoked=1
tripwire.jsonl: ABSENT (no outbound network attempt)
secret canary: absent from run_events, run_steps, messages, runs and the backend log
```

### 16.6 Console / page-error classification (unfiltered)

| Event | Classification |
|---|---|
| `manifest.webmanifest` Syntax error (x2) | **REAL OPEN D0 BLOCKER** — not suppressed, not fixed here |
| `/api/auth/refresh` 401 | **expected baseline**, pre-login unauthenticated bootstrap only; it did **not** repeat during the authenticated turn (zero refresh requests recorded after instrumentation was installed) |
| React DevTools info | informational, not an error |
| `apple-mobile-web-app-capable` deprecation | pre-existing warning, not an error |

Authenticated-turn application errors: **ZERO**. `unhandledrejection`: **none**. No failed authenticated-turn
network requests. **Literal D0 zero-console acceptance remains BLOCKED by the manifest error.**

### 16.7 Artefacts (all OUTSIDE the repository)

| Artefact | Path |
|---|---|
| intermediate partial-render screenshot | `C:\deepseek-harness\d6b-r5-intermediate.png` |
| final post-complete screenshot | `C:\deepseek-harness\d6b-r5-final.png` |
| DOM snapshots (accessibility YAML) | `C:\deepseek-harness\.playwright-mcp\page-*.yml` |

Verified **0** `d6b-r5-*.png` inside the repository; no binary artefact entered the working tree.
The model could not visually re-inspect the PNGs (this model does not declare image input), so the visual
claim rests on the DOM snapshot, extraction counters and four-way hash equality, not on my own eyes.

### 16.8 Focused verification

| Command | Result |
|---|---|
| `pytest tests/smoke -q` | **19 passed** |
| `vitest run` (safety, StrictMode, submission-count, durableStreaming, sseParser, useStreaming) | **6 files / 72 tests passed** |
| `tsc --noEmit -p tsconfig.json` | clean (exit 0) |
| `git diff --check` | clean |

### 16.9 Independent teardown

Verified by process/port/filesystem scan, not a helper boolean: **0** npm/Vite/node descendants, **0**
Uvicorn/backend processes, **0** waiting orchestrators, **0** bound smoke ports (65228/65229 free), **0**
`thunder_d6b_*` temporary roots.

### 16.10 Remaining blockers (unchanged, explicitly NOT closed)

1. **`manifest.webmanifest` syntax error — OPEN literal-D0 console blocker**; unfixed by design in this phase.
2. **Vite development-proxy concurrency defect — OPEN.** NOT fixed. The smoke only *bypasses* it via the
   approved `VITE_API_BASE_URL` direct-backend seam; that is a workaround, not a repair.

D6B is therefore **functionally proven but formally BLOCKED**: every functional criterion passes, while the
literal D0 zero-console gate cannot pass until the manifest defect is fixed in a separately authorized phase.
---

## 17. D6B-R6 — manifest.webmanifest defect fix and final D6B acceptance (2026-09-17)

**Verdict: D6B COMPLETE — READY FOR USER REVIEW.** The sole remaining formal blocker is fixed and verified.

### 17.1 Root cause (proven, not assumed)

`frontend/index.html` hand-declared `<link rel="manifest" href="/manifest.webmanifest" />` **while**
`vite-plugin-pwa` already injects the identical link at build time. Evidence:

- a production `dist/index.html` carried the link **TWICE** (hand-written + plugin-injected);
- `frontend/public/manifest.webmanifest` does **not** exist, and `vite.config.ts` sets **no** `devOptions`, so
  the dev server never serves a manifest at that path;
- the pre-fix browser request `GET /manifest.webmanifest` returned **200**, `content-type: text/html`,
  `content-length: 1507`, body `<!DOCTYPE html>…` — i.e. the **SPA fallback served `index.html`** where a
  web manifest was expected.

Chrome therefore parsed HTML as a manifest and reported `Manifest: Line: 1, column: 1, Syntax error.`
The defect was a **duplicate declaration whose URL only exists after a build**, not malformed manifest JSON.

### 17.2 RED regression (before the fix)

`frontend/src/__tests__/manifestDeclaration.test.ts` — asserts the HTML declares no hand-written manifest
link (the plugin owns it) **and** that the plugin manifest configuration is still present.

```
npx vitest run src/__tests__/manifestDeclaration.test.ts
-> FAIL  AssertionError: expected [ Array(1) ] to deeply equal []      (1 hand-written link found)
-> PASS  1 of 2 (plugin config intact -> proves this is a duplicate removal, not a manifest removal)
```

### 17.3 Minimal fix

`frontend/index.html`: removed the redundant hand-written `<link rel="manifest" …>` (6 lines changed incl.
an explanatory comment). **One file, one declaration.** The manifest itself is untouched: it is still
declared once in `vite.config.ts` and emitted/injected by `vite-plugin-pwa`. No console suppression, no
browser-side catch, no manifest disabling, no icon/branding/service-worker change.

### 17.4 Post-fix validation

| Check | Result |
|---|---|
| RED regression | **GREEN — 2/2 passed** |
| dev server manifest request | **none requested at all** (correct: nothing to serve in dev) |
| dev browser manifest syntax errors | **ZERO** (was 2) |
| production build `npm run build` | **exit 0** (`tsc && vite build`; PWA v1.3.0, precache 15 entries) |
| `dist/index.html` manifest links | **exactly 1** (plugin-injected) |
| `dist/manifest.webmanifest` | parses: `name=Thunder AI`, `icons=2` |
| `tsc --noEmit -p tsconfig.json` | clean (exit 0) |

### 17.5 Browser console closeout (unfiltered)

| Event | Before | After |
|---|---|---|
| `manifest.webmanifest` Syntax error | **2** | **0** |
| `/api/auth/refresh` 403 | 1 (pre-login) | 1 (**pre-login only**) |
| `apple-mobile-web-app-capable` deprecation warning | 1 | 1 (pre-existing, unrelated to this defect; not fixed) |
| React DevTools info | 1 | 1 (informational) |
| `pageerror` / `unhandledrejection` | 0 | **0** |

During the authenticated turn: `refreshDuringTurn: 0` — the baseline 403 did **not** repeat. **Zero new
authenticated-turn errors.**

### 17.6 Durable-stream spot-check (fresh journey, same deterministic fixture)

| Criterion | Result |
|---|---|
| stream POSTs | **exactly 1** (200, `text/event-stream; charset=utf-8`, idem-key hash `4e074ca0`) |
| X-Run-Id | `87a83c6a-2592-4532-a2bb-701f260d6447` == persisted `Run.id` |
| SSE order | `start -> run_status -> token x5 -> complete` (counts 1/1/5/1) |
| progressive render | 2260 -> 4508 -> 6758 -> 9008 -> 9047, **all marked before complete** |
| sentinels | exactly 1 each; starts with BEGIN, ends with END |
| `getReader` / reads | 1 / 8; `aborts 0`, `cancels 0`, no reconnect, no second submission |
| `complete` handled | **1** |
| browser text bytes / SHA-256 | **18047** / `557b82aca8c4e93ef113cde7a8fae1546d142956c67e5fc1ab8a4e3483bdf5e1` |
| assistant Message bytes / SHA-256 | **18047** / `557b82aca8c4e93ef113cde7a8fae1546d142956c67e5fc1ab8a4e3483bdf5e1` |
| persisted text events bytes / SHA-256 | **18047** / `557b82aca8c4e93ef113cde7a8fae1546d142956c67e5fc1ab8a4e3483bdf5e1` |
| provider output bytes / SHA-256 | **18047** / `557b82aca8c4e93ef113cde7a8fae1546d142956c67e5fc1ab8a4e3483bdf5e1` |
| DB | `runs=1 run_steps=1 messages=2 run_events=9 run_payloads=1`; approvals/tool/dispatch/execution = **0**; Run `chat_stream`/`succeeded`; step `succeeded` |
| provider invoked | **1** |
| tripwire | **silent** (`tripwire.jsonl` absent) |
| canary | absent from DB and logs |

Four-way equality holds exactly and the SHA-256 is **identical to D6B-R5**, so this spot-check is comparable
rather than merely self-consistent.

### 17.7 Focused test and build commands

| Command | Result |
|---|---|
| `npx vitest run src/__tests__/manifestDeclaration.test.ts` | **2 passed** (RED before, GREEN after) |
| `npx vitest run` (manifest + safety + StrictMode + submission-count + durableStreaming + sseParser + useStreaming) | **7 files / 74 tests passed** |
| `pytest tests/smoke -q` | **19 passed** |
| `npx tsc --noEmit -p tsconfig.json` | clean (exit 0) |
| `npm run build` (in `frontend/`) | **exit 0** |
| `git diff --check` | clean |

### 17.8 Residual risk (open, NOT fixed here)

**Vite development-proxy concurrency defect remains OPEN.** The smoke continues to use the approved
`VITE_API_BASE_URL` direct-backend path; that is a workaround, not a repair, and this phase did not touch it.
The `apple-mobile-web-app-capable` deprecation warning is a separate pre-existing warning and was deliberately
not fixed. Neither is a D6B acceptance blocker.

### 17.9 Teardown and source-control confirmation

Independently scanned (not a helper boolean): **0** smoke npm/Vite/node descendants, **0** backend/Uvicorn
processes, **0** waiting orchestrators, **0** bound smoke ports (62604/62605 free), **0** `thunder_d6b_*`
temporary roots. HEAD `b2e67183d4791cbbaed9d3d98e8833983510f3ff`, **0 staged**, `git diff --check` clean,
`stash@{0}` (`7ab31a5`) untouched, architecture document **3332 / 268044 / f4d0bbc2…** unchanged. No commit,
merge, push, stage, reset, rebase, stash, clean, discard or branch switch.
