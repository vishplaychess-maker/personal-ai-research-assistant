# Phase 1B.2R — MP-3C3D6A: deterministic real-browser durable-path activation smoke (planning audit)

**Date:** 2026-09-17  
**Status:** planning/audit complete. **No product code, test code, config default, dependency, or legacy file was changed by this phase.**  
**Scope:** evidence gathering and a build plan only. This document does not enable any flag, install any dependency, or start MP-4.

---

## 1. Stage-0 evidence

### 1.1 Source control (verified before any repository read)

| Check | Result |
|---|---|
| `git status --short --branch` | `## phase-1b1-approval-domain`, intentionally dirty |
| `git rev-parse HEAD` | `b2e67183d4791cbbaed9d3d98e8833983510f3ff` (expected prefix `b2e6718`) |
| `git merge-base HEAD main` | `b2e67183d4791cbbaed9d3d98e8833983510f3ff` — HEAD and `main` share the same base |
| staged index | **0** files |
| `git diff --check` | clean |
| `git stash list` | pre-existing `stash@{0}` on `7ab31a5` — untouched |

All expectations matched; no Git state was created, repaired, or destroyed.

### 1.2 Stage A closeout artifacts confirmed present (not re-run)

| Artifact | Evidence |
|---|---|
| `read_persisted_text` | `backend/app/run_engine/text_events.py:91` |
| prefix validation | `backend/app/run_engine/worker.py:2927` |
| exact missing-suffix publication | `worker.py:2930-2934` |
| fail-closed codes | `worker.py:2926` `text_recovery_unreadable`; `worker.py:2929` `text_recovery_mismatch` |
| focused recovery tests | `tests/test_multi_agent_worker.py:660` (shared crash driver), `:729`, `:756` |
| D5C report updated | `docs/superpowers/reports/phase-1b2r-mp3c3d5c-completion.md`, section D5C closeout verification |
| flags remain False | fresh `Settings()` import — see section 5.1 |

The Stage A focused suites were **not** re-run: no current code contradicts the D5C report.

### 1.3 Incident disclosure — architecture document truncated, then restored byte-exact

During the previous (Stage A) turn I used a full-replace file write whose content was built from a **partial** read, which truncated `docs/superpowers/plans/2026-09-08-phase-1b2r-durable-run-resume-architecture.md` from **3297 lines to 41 lines**. This was a real, self-inflicted data-loss event and it is recorded here rather than quietly repaired.

Recovery: the pre-truncation content still existed in the object database as a dangling blob (`843b33e10abeed86afbd1c19e2ececcd082096e4`, 265504 bytes — the file is untracked, so this blob came from an earlier staging in a prior session). It was restored and verified **byte-exact**: on-disk size 265504 and `git hash-object <path>` equal to the original blob SHA. The intended D5C closeout addendum was then re-applied with a targeted edit (final state: 3332 lines / 268044 bytes).

**Process rule adopted for the remainder of Phase 1B:** never use a full-replace write against an existing file; use targeted edits only, and verify with `git hash-object` after any recovery.

---

## 2. Exact D6 objective, from repository evidence (question 1)

D6 is defined in exactly two places. Both are quoted rather than paraphrased.

### 2.1 D0 plan section 10, MP-3C3D6 (lines 443-454)

- **Scope:** exactly the flows in section 8, executed live via `claude-in-chrome`, backend in dev-only mode with the durable flag on, DB checked directly for zero duplicate Messages.
- **Files:** none (verification only) unless a fake/deterministic provider seam needs to be added — check first, per section 8 open item.
- **Acceptance:** every flow in section 8 passes with zero console errors and zero duplicate Messages.
- **Security risk:** none (test-only).
- **Complexity:** **small**, but has a hidden dependency: confirm whether a deterministic provider seam exists.
- **Blocked by:** MP-3C3D2 at minimum; ideally after D4/D5 too if those modes ship before wider enablement.

### 2.2 D0 plan section 8, real-browser activation gate (lines 300-317)

- Backend: durable flag ON, plain-chat mode only. **Whether a deterministic/fake provider seam already exists was not confirmed — OPEN**; check before building one.
- Auth/session: real login flow, real bearer + CSRF.
- Flows to prove live, in order: plain token streaming renders correctly, forced reconnect, `tool_approval_required` opens the dialog, Approve resumes correctly, Deny reaches a terminal state correctly, Cancel via the durable cancel button waits for the real persisted terminal event, repeat at mobile and desktop viewports, zero unexpected browser console errors, and a **server-side DB check** that exactly one `Message` row exists for the turn.
- Tooling: use the already-connected `claude-in-chrome` MCP tools. **Do not install Playwright.**

### 2.3 Where D6 sits in the activation ladder

D0 section 9 places the real-browser smoke as a **required test at the Limited-user canary (hosted) row**, not at the Development-only row. D0 section 11 states it is required **before replacing legacy globally**. It is **not** required before a plain-chat self-host pilot.

### 2.4 Two divergences from D0 that the user must adjudicate (see section 12)

1. **Browser tool.** D0 mandates `claude-in-chrome` and forbids installing Playwright. `claude-in-chrome` is **not available in this session**; the harness instead exposes Playwright MCP browser tools. Using them adds **nothing** to the repository (no dependency in `frontend/package.json`, no `playwright.config.*`), so it satisfies D0's intent but not its letter.
2. **Provider.** D0 says to use whatever free/local provider is already configured, while the recommended D6A policy (and the locked free/local-only rule) favour an injected deterministic fake. Section 6 shows a fake needs no production backdoor, so the fake is strictly safer and also removes Ollama from the loop.

---
## 3. Existing test / browser infrastructure

| Layer | What exists today | Evidence |
|---|---|---|
| Backend tests | pytest, run as `PYTHONPATH=backend .venv/Scripts/python.exe -m pytest ...` | `tests/`, prior reports |
| Hermetic DB helpers | in-memory SQLite via `Base.metadata.create_all` plus the three partial indexes the real migration creates | `tests/run_engine_helpers.py` (`mk_engine`, `mk_session`, `make_ctx`, `ensure_user`, `_PARTIAL_INDEXES`) |
| ASGI-level tests | FastAPI `TestClient` used by route suites | `tests/test_multi_agent_submission.py`, `tests/test_run_engine_mp3a.py` |
| Frontend tests | **vitest + jsdom** + @testing-library; `npm run test`; jsdom is the only rendered surface | `frontend/package.json:10,33-46`, `frontend/vite.config.ts:74-79` |
| Browser automation | **none** — no Playwright, no Cypress, no `e2e/` directory, no `playwright.config.*` | repository file listing; `frontend/package.json` |
| Docker stacks | `docker-compose.yml` (live, backend :8080), `docker-compose.test.yml` (isolated project `ra-test`, backend :18080, own volumes, throwaway DB), `docker-compose.prod.yml` | `docker-compose.test.yml` header |
| Startup scripts | **none** — no `.bat`, `.sh`, or `.ps1` in the repository | repository file listing |
| Harness tooling | Playwright MCP browser tools available in this session; `claude-in-chrome` is not | session tool inventory |

### 3.1 A real constraint: the committed Vite dev proxy targets a Docker hostname

`frontend/vite.config.ts:80-93` proxies `/api` to `http://backend:8080` — the **Docker service name**. A smoke that runs the backend as a host process (the recommended policy, so the test-only worker factory can be injected) cannot use that target as-is. Options, smallest first:

1. **Env-overridable proxy target:** `target: process.env.VITE_PROXY_TARGET ?? "http://backend:8080"`. The default string is unchanged, so production behaviour does not change, but it edits a committed build-config file and therefore needs explicit approval.
2. Serve the **built** frontend from the smoke backend, or use `vite preview` behind a small proxy — avoids the dev-server proxy entirely.
3. Run the backend inside `docker-compose.test.yml` — rejected: injecting a test-only worker factory into a container entrypoint would need an image change or a bind-mounted override, i.e. more surface than option 1.

**Recommendation: option 1**, raised as an explicit user decision in section 12.

---

## 4. Real vs fake component matrix (questions 5 and 6)

| Component | Real / Fake | Mechanism / justification |
|---|---|---|
| Chromium browser | **REAL** | harness Playwright MCP tools |
| Frontend application | **REAL** | `npm run dev` (with proxy override) or the production build served locally |
| FastAPI app, routes, middleware, CORS | **REAL** | unchanged `app.main.app` |
| FastAPI **lifespan** | **REAL** | runs normally; migrations and boot checks execute |
| `RunWorkerSupervisor` and its worker thread | **REAL** | the genuine supervisor, handed a test worker factory |
| `/messages/stream` durable branch | **REAL** | `routes/messages.py:354` gates on `run_messages_stream_durable_enabled` |
| Submission, idempotency HMAC, admission lock | **REAL** | `run_engine/submission.py` unchanged |
| `/api/runs`, SSE replay, notifier | **REAL** | unchanged |
| Auth: register, login, cookies, CSRF | **REAL** | real HTTP against real routes |
| SQLite DB and `runs`/`run_steps`/`run_events`/`run_payloads`/`messages` | **REAL** | dedicated throwaway file DB |
| LLM transport | **FAKE** | injected `chat_provider` |
| DuckDuckGo search | **FAKE** | injected `research_search_adapter` (`SearchAdapter` protocol) |
| Outbound HTTP fetch | **FAKE** | injected `research_fetch_adapter` (`FetchAdapter` protocol) |
| MCP tool server / execution | **FAKE** | injected `mcp_transport`, still driven through `app.approval.gateway.dispatch` |
| Ollama | **NOT USED** | the fake provider never contacts it |
| ChromaDB | **NOT USED** | RAG and memory retrieval failures are non-fatal and degrade to honest empty context |
| OpenRouter or any paid service | **NOT USED** | not applicable |

### 4.1 ChromaDB — why the smoke can still be fully offline

`messages_stream._build_context_snapshot` wraps both retrieval calls in a broad `except Exception` that logs **only the exception class name** and continues:

- RAG: `backend/app/run_engine/messages_stream.py:247-249`
- memory: `backend/app/run_engine/messages_stream.py:262-264`
- the returned snapshot is built by `chat_stream.build_context_snapshot(...)` with honest `citations=[]`, `sources_used=False`, `memories_used=False` (`messages_stream.py:266-269`)

So with no ChromaDB running, durable chat still submits, executes and finalizes; the only effect is an honestly empty citation set. This is what makes an offline, deterministic, free smoke possible. D6B must **assert** that empty-citation honesty rather than accept any value.

### 4.2 What must be real for the label real-browser to be truthful

The smoke may call itself **real-browser / end-to-end** only because the following are genuine: the browser engine, the frontend application (dev server or built bundle), the HTTP layer, the FastAPI route functions, the lifespan-started supervisor and its worker thread, SSE framing, the notifier, and every database write. The fakes are confined to **outbound third-party transports** behind seams that already exist for unit tests. A test that bypasses the public browser or the route layer must not be described as end-to-end.

---

## 5. Required temporary flags and environment (questions 7 and 14)

### 5.1 Defaults confirmed False (fresh import during this phase)

| Setting | Default | Source |
|---|---|---|
| `run_engine_enabled` | `False` | `config.py:138` |
| `run_worker_enabled` | `False` | `config.py:166` |
| `run_messages_stream_durable_enabled` | `False` | `config.py:205` |
| `run_deep_research_enabled` | `False` | `config.py:215` |
| `run_multi_agent_enabled` | `False` | `config.py:226` |
| `run_public_api_enabled` | `False` | `config.py:272` |
| `run_chat_stream_enabled` | `False` | `config.py:284` |

The legacy global capability flags stay at their defaults, `enable_deep_research=True` (`config.py:343`) and `enable_multi_agent=True` (`config.py:88`), and are **not touched**: since MP-3C3D1 the durable path no longer gates on them, and `routes/messages.py:375,378` consults them only on the **legacy** branch.

### 5.2 Environment for the smoke process only

Set in the spawned process environment — never in `.env`, never in a config default:

```
RUN_ENGINE_ENABLED=true
RUN_WORKER_ENABLED=true
RUN_PUBLIC_API_ENABLED=true
RUN_CHAT_STREAM_ENABLED=true
RUN_MESSAGES_STREAM_DURABLE_ENABLED=true
# only for the Deep Research / Multi-Agent journeys:
RUN_DEEP_RESEARCH_ENABLED=true
RUN_MULTI_AGENT_ENABLED=true
RUN_IDEMPOTENCY_HMAC_KEY=<ephemeral random, >=16 chars, generated per run>
ENCRYPTION_KEY=<ephemeral Fernet key, generated per run>
DATABASE_URL=sqlite:///<temp-dir>/smoke.db
RUN_WORKER_WEB_CONCURRENCY=1
UPLOAD_DIR=<temp-dir>/uploads
DATA_DIR=<temp-dir>/data
```

Why each matters:

- `run_public_api_enabled` is a genuine prerequisite of both readiness predicates (`deep_research_readiness.py:32`, `multi_agent_readiness.py:26`), and boot **fails closed** without the HMAC key (`main.py:1310-1312`).
- `ENCRYPTION_KEY` is required for `RunPayload` sealing.
- `RUN_WORKER_WEB_CONCURRENCY=1` keeps the embedded-worker topology guard satisfied on SQLite (`worker_supervisor.assert_embedded_topology_ok`).
- Ephemeral keys are generated per run and never written to a durable file, so no secret enters the repository or its `.env`.

**Defaults are unchanged.** No repository file is edited to enable a flag, and `.env` is neither read for flags nor written.

---

## 6. Test-only dependency-injection design (questions 2 and 3)

**Answer: yes.** A deterministic fake provider can drive the real lifespan worker with **zero production backdoor**, by composing three injection seams that already exist in the shipped code. No new seam is required for the recommended design.

### 6.1 The three existing seams

**Seam 1 — the app already honours a pre-set supervisor** (`backend/app/main.py:1323-1326`):

```python
from app.run_engine.worker_supervisor import RunWorkerSupervisor
if not getattr(app.state, "run_worker_supervisor", None):
    app.state.run_worker_supervisor = RunWorkerSupervisor()
await app.state.run_worker_supervisor.start()
```

The `if not getattr(...)` guard is pre-existing, not added by D6. If a test-only module assigns `app.state.run_worker_supervisor` before the lifespan runs, the app uses **that** supervisor. Production never sets it, so production behaviour is unchanged.

**Seam 2 — the supervisor accepts a worker factory** (`backend/app/run_engine/worker_supervisor.py:163-178`):

```python
def __init__(self, *, session_factory=None, worker_factory=None, now_fn=None):
    if worker_factory is not None:
        self._worker_factory = worker_factory
    else:
        self._worker_factory = lambda: RunWorker(sf, now_fn=now_fn)
```

**Seam 3 — `RunWorker` accepts every outbound transport** (`backend/app/run_engine/worker.py:343-370`):

```python
RunWorker(session_factory, *, worker_id=None, now_fn=None,
          mcp_transport=None,            # default: real MCP service
          chat_provider=None,            # default: real get_provider seam
          research_search_adapter=None,  # default: DuckDuckGoSearchAdapter()
          research_fetch_adapter=None)   # default: GuardedHttpFetchAdapter()
```

The real fallbacks are visible at `worker.py:2046-2047`.

### 6.2 The composition (test-only module, never imported by production)

A new module such as `tests/smoke/durable_smoke_app.py` would:

1. import the real `app.main.app`;
2. import the real `RunWorker` and `RunWorkerSupervisor`;
3. build fakes implementing the **existing protocols** — `chat_provider` as an async generator yielding token chunks then a done chunk; `SearchAdapter.search(query, *, max_results)`; `FetchAdapter.fetch(...)`; `mcp_transport(db, user_id, tool_id, args)` returning `(text, is_error)`;
4. assign `app.state.run_worker_supervisor = RunWorkerSupervisor(worker_factory=lambda: RunWorker(SessionLocal, chat_provider=fake_llm, research_search_adapter=fake_search, research_fetch_adapter=fake_fetch, mcp_transport=fake_mcp))`;
5. expose `app` for uvicorn.

### 6.3 Security boundary of the seam, and what must NOT be built

- The seam is reachable **only by importing a module and mutating an in-process attribute before startup**. It is not reachable from any environment variable, config field, CLI flag, route, header, or request body. There is **no production-accessible fake-provider switch**, and D6 must not create one.
- The test module must live outside the application package (under `tests/`), must never be imported by `app.main` or any `app.*` module, and should fail closed if it is somehow imported in a production process.
- Fakes must obey the same bounds as the real transports they replace: bounded output size, no secrets, and no raw provider text in events, status fields, or refs.
- **No fake may bypass the approval gateway.** The fake `mcp_transport` is still invoked only through `app.approval.gateway.dispatch`, so one-time approval consumption and the at-most-once dispatch record remain genuinely exercised. A fake wired to bypass the gateway would make the approval journey worthless, and the smoke must not then claim it.

### 6.4 Explicitly NOT confirmed this phase

The exact signature the `chat_provider` seam receives is `(chat_input, *, db, user_id)` per the existing worker test harness (`tests/test_multi_agent_worker.py:137-138`) and must be re-verified against `chat_stream.default_chat_provider` when D6B is implemented. Any drift there is a D6B task, not an assumption of this plan.

---

## 7. Database, auth and session setup (questions 8 and 9)

### 7.1 Database isolation and removal

- A **dedicated temporary directory per smoke run** holding `smoke.db`, `uploads/`, and `data/`.
- `DATABASE_URL=sqlite:///<temp-dir>/smoke.db`, so the boot path `init_db()` then `_migrate_database()` then `_verify_run_engine_schema()` creates the full current schema **without migrating any user data**. D6 authors no migration.
- **Never** point at the repository `test.db`, at `/data/app.db`, or at any Docker volume. The smoke must assert its `DATABASE_URL` is inside the temp directory before starting.
- Removal: delete the temp directory in a `finally` block, after verifying the path is outside the repository. Nothing is written inside the working tree.
- `docker-compose.test.yml` already establishes the project convention of an isolated throwaway stack; D6 does **not** need Docker, but reuses its isolation principle.

### 7.2 Authentication and session-family creation

Important finding: `_create_default_user()` (`backend/app/main.py:1348-1364`) creates `username="default"` with **no password hash**, so the seeded default account **cannot** be used to log in. The smoke must create its own account through the real public flow:

1. `POST /api/auth/register` — open, no auth dependency, IP rate-limited (`routes/auth.py:111-134`), bcrypt-hashes the password.
2. Drive the **real browser login form** (or `POST /api/auth/login` through the page context) so the server sets the HttpOnly refresh cookie `research_assistant_refresh_token` and the non-HttpOnly CSRF cookie `research_assistant_csrf_token`, and issues the access token (`routes/auth.py:279-309`).
3. The refresh session created at step 2 supplies the `family_id` the access token `sid` claim carries; `build_auth_context` binds it as `AuthContext.auth_session_ref`, which becomes `Run.auth_session_ref`. Every durable read and mutation re-validates it against the database.
4. Because the browser stores the cookies itself, the smoke exercises the genuine cookie and CSRF path rather than synthesising tokens.

This is exactly what D0 section 8 asks for, and it is the first thing MP-3C3C jsdom tests never did.

---
## 8. Exact browser journeys (question 4)

D0 mandates the chat, approval, deny, cancel, reconnect and viewport journeys. Because D4C (Deep Research) and D5C (Multi-Agent) have **landed since D0 was written**, and because the purpose of D6 is to prove the durable flow *before MP-4 converges or deletes legacy*, the two additional modes are included as separately-scoped journeys.

**J1 — plain chat on the durable path, desktop viewport (mandatory)**
1. Log in through the real form.
2. Create a session through the UI (new session control calling `API.createSession()`).
3. Leave the mode selector at its default `auto`, which omits `mode` from the request entirely (`App.tsx:448`).
4. Send a message and capture the `/messages/stream` response.
5. Assert the response carries `X-Run-Id` (section 11).
6. Assert the deterministic fake answer streams progressively (more than one paint with partial text).
7. Assert the terminal state shows completion and the rendered text equals the fake full answer.
8. Assert zero unexpected browser console errors.

**J2 — forced SSE reconnect mid-stream (mandatory)**
1. Start the send with a longer fake answer.
2. Use Playwright route interception to **abort the SSE response after N bytes**, deterministically.
3. Assert the client re-issues the same POST with the **same `Idempotency-Key`** and a `Last-Event-ID` / `after_seq` cursor (the `useStreaming.ts` reconnect path).
4. Assert no duplicated token is rendered and the final text is complete.
5. Server-side: exactly **one** `Message` row and one run for that idempotency key.

**J3 — MCP approval, Approve resumes (mandatory)** — see section 10.

**J4 — MCP approval, Deny reaches a terminal state (mandatory)** — see section 10.

**J5 — user cancel through the durable cancel control (mandatory)**
1. Start a long send and click Cancel.
2. Assert the UI enters the `cancelling` phase and does **not** claim success locally.
3. Assert the UI ends on the server-persisted terminal `cancelled` state (`POST /api/runs/{id}/cancel`, then the persisted terminal event).
4. Assert no assistant `Message` is created for a cancelled turn.

**J6 — idempotent replay (mandatory)**
1. Capture the original submission `Idempotency-Key` and body.
2. Replay the identical POST from the page context.
3. Assert the response `X-Run-Id` equals the original run id and that no new `Run`, `Message`, `RunStep` or `RunPayload` row was created.
4. Replay with a **changed** message and the same key; assert a 409 semantic conflict.

**J7 — Deep Research durable journey (included, separately gated)**
1. Select `deep_research` in the mode selector; its capability must report `available` (`routes/settings.py:120`).
2. Send; assert `research.progress` codes arrive in order and `[W#]` citations are server-assigned from the sealed snapshot.
3. Assert the fake search and fetch adapters were called and that **no live network** occurred.

**J8 — Multi-Agent durable journey (included, separately gated)**
1. Select `multi_agent` and send.
2. Assert the six `agent_progress` codes arrive in the approved order and the UI shows friendly labels.
3. Assert exactly one assistant `Message`, no intermediate role output anywhere in the DOM, and exactly three fake provider calls.

**J9 — mobile viewport repeat (mandatory per D0)**
Re-run the shortest mandatory journey (J1 plus J5) at a mobile viewport, asserting no layout-blocked control and no new console errors.

**J10 — worker health gate (supporting)**
`GET /api/health/worker` returns HTTP 200 with `durable_path_ready: true` while the smoke backend is running. This is the cheapest, least ambiguous proof that the **real lifespan supervisor** is alive.

**Explicitly out of scope:** images and vision (blocked on an unresolved product decision), any non-MCP tool execution, hosted or multi-process behaviour, and any claim about PostgreSQL.

---

## 9. SSE reconnect and idempotency assertions (question 10)

| Property | Assertion |
|---|---|
| Durable path selected | response `X-Run-Id` is a non-empty id and a matching `runs` row exists |
| Cursor semantics | the reconnect request carries `Last-Event-ID` or `after_seq` equal to the last rendered frame id |
| No gap | concatenated replayed text equals the fake full answer byte-for-byte |
| No duplicate | no token appears twice, and exactly one `Message` row exists for the turn |
| Replay key stability | reconnect and replay reuse the original `Idempotency-Key`; a **new** send uses a new key |
| Semantic conflict | same key with a changed body yields 409 (`IdempotencyKeyConflict`) |
| Server event ordering | `run_events.run_seq` strictly increasing for the run, with no duplicate `run_seq` |
| Terminal frame | exactly one terminal frame, and the final rendered text is not provisional |

The reconnect must be induced by **aborting the SSE response**, not by restarting the backend: a restart exercises boot recovery (`outcome_unknown`) rather than the client reconnect contract, and its timing is non-deterministic. A boot-recovery smoke, if wanted later, belongs with MP-6 outcome-reconciliation work.

---

## 10. Approval-resume journey (question 4, J3 and J4)

D0 requires both the Approve and the Deny paths. Requirements:

1. The fake LLM must emit a **complete `[MCP_CALL]` marker** so the durable chat driver suspends the run for approval and parks it in `waiting_for_approval`.
2. The fake `mcp_transport` must be invoked **only** through `app.approval.gateway.dispatch`, so one-time approval consumption and the at-most-once dispatch record are genuinely exercised.
3. Approve: the dialog must show only redacted `tool_id`, `risk_level`, `purpose_redacted`, `display_summary` and `state`; approving calls the resume path and the **same run** resumes and reaches a final `Message`.
4. Deny: the run reaches a terminal non-success state and the tool must never have executed — assert the fake transport recorded **zero** invocations.
5. Exactly one decision under a double click (the `decidedRef` guard), with no auto-approve and no countdown.

Because MCP tool execution is deliberately fake, this journey proves the **approval and resume contract**, not the correctness of any real tool. The smoke report must say so explicitly rather than implying real tool execution.

---

## 11. Legacy-path exclusion proof (question 11)

Positive evidence that the **durable** branch ran, all of which existing code already emits:

1. **`X-Run-Id` response header** — set only by the durable branch (`routes/messages.py:635`). The legacy branch never sets it, and the frontend uses its presence alone to decide durable vs legacy (`useStreaming.ts:213-217`).
2. **A `runs` row** whose id equals that header value, with `status = succeeded` and a terminal `run_events` sequence.
3. **`GET /api/health/worker`** returning 200 with `durable_path_ready: true`, proving the real supervisor started and stayed alive for the turn.
4. **`run_events` rows carrying the durable-only event vocabulary** (`run.created`, `run.status`, `text`, and `agent.progress` / `research.progress`) for that run.
5. **Server-side duplicate check** — exactly one assistant `Message` for the turn, which is D0's explicit requirement.

The single decision point is `routes/messages.py:354` (`if settings.run_messages_stream_durable_enabled:`). With that variable set true in the smoke process only, the legacy generator is not reachable for these requests. The smoke should additionally assert that no `X-Run-Id`-less response was produced for a durable-mode request.

---

## 12. Security and privacy constraints

- **No production fake switch.** The seam is in-process and pre-startup only (section 6.3). D6 must not add any environment variable, config field, or route that selects a fake provider.
- **No secrets in the repository.** `RUN_IDEMPOTENCY_HMAC_KEY` and `ENCRYPTION_KEY` are generated per run, held in the process environment only, and never written to `.env`, logs, artifacts, or the DOM.
- **No raw third-party content in assertions or logs.** Fake LLM text is fixed, synthetic, and non-sensitive.
- **Canary discipline.** Deterministic fake outputs should include a canary string that must **never** appear in `run_steps.input_ref` or `output_ref`, in public `run_events` fields, in `Run.status_reason`, or in logs — reusing the assertion style already proven at `tests/test_multi_agent_worker.py:636`.
- **Approval integrity.** Nothing may bypass `app.approval.gateway.dispatch`; approvals remain one-time and bound to the normalized action hash plus user, session and run identity.
- **Flag scope.** Flags live only in the smoke process environment. Defaults stay `False`, and no `.env`, config default, or documentation tells an operator to enable them.
- **Data isolation.** The smoke must never read or write a real user database, and must not run against the live `docker-compose.yml` stack, whose volume holds real data.
- **Artifact hygiene.** Screenshots and traces must not capture real credentials; the login password is a per-run random value, and any storage-state file must be deleted in teardown and never committed.

---

## 13. Failure diagnostics and artifact retention

On failure, retain in a temp directory (never in the working tree unless the user asks):

- the Playwright trace, screenshot, or video for the failing step;
- the browser console message list — unexpected errors are themselves an acceptance failure;
- the failing request and response pair: method, path, status, presence of `X-Run-Id` and `Idempotency-Key` (**presence only, never the key value**), and the SSE frame tail;
- server-side row counts for `runs`, `run_steps`, `run_events` and `messages` for the run id;
- the worker health JSON;
- a bounded backend log tail, filtered to exclude prompts, secrets, retrieved content, and provider exception text.

On success, retain only the summary counts and the final assertions. Traces are deleted with the temp directory (section 14).

---

## 14. Cleanup behaviour

1. Stop the smoke backend process through the cooperative shutdown path, with a bounded grace, and stop the frontend server.
2. Close the browser context and any MCP browser session.
3. Delete the temp directory holding the database, uploads, data, traces, screenshots and storage state.
4. Assert the repository working tree gained **no** new files beyond those D6B intentionally creates, and that `git status --short` matches the pre-run baseline plus those intended additions.
5. Assert no orphan process remains — no stray uvicorn, node, or python children.

Cleanup must run in a `finally` so a failed smoke cannot leak a running server or a temp database. The D5C report records a prior incident of orphaned pytest processes, and the same care applies here.

---
## 15. Focused acceptance-test list

**D6B — harness plus the chat truth-bearing slice**

| # | Acceptance criterion |
|---|---|
| A1 | The smoke entrypoint boots the **real** app with the real lifespan; `GET /api/health/worker` returns 200 with `durable_path_ready: true` and `durable_run.alive` true |
| A2 | The injected fakes are used: the fake LLM records exactly the expected number of calls and **no** outbound network call occurs |
| A3 | Real registration and real browser login succeed, with server-set refresh and CSRF cookies |
| A4 | J1 passes: durable branch selected (`X-Run-Id` present), progressive rendering observed, final text equals the deterministic answer, zero unexpected console errors |
| A5 | Exactly one assistant `Message` row exists for the turn |
| A6 | The boot log shows no fail-closed condition, and all four deployment-default flags remain `False` in a **separate** `Settings()` import |
| A7 | Teardown removes the temp directory and leaves no orphan process |

**D6C — remaining journeys**

| # | Acceptance criterion |
|---|---|
| B1 | J2 reconnect: an aborted SSE stream recovers with no gap and no duplicate, reusing the same `Idempotency-Key`, producing one `Message` |
| B2 | J6 replay: same key returns the same run id with zero new rows; a changed body yields 409 |
| B3 | J3 approve: the dialog shows redacted fields only, and the same run resumes and finalizes |
| B4 | J4 deny: terminal non-success, and the fake MCP transport was invoked **zero** times |
| B5 | J5 cancel: the UI waits for the persisted terminal state, and no assistant `Message` exists |
| B6 | J7 Deep Research passes with fake search and fetch and server-assigned `[W#]` citations |
| B7 | J8 Multi-Agent passes: approved progress-code order, exactly three fake provider calls, no intermediate role text in the DOM, one `Message` |
| B8 | J9 mobile viewport repeat passes |
| B9 | The canary string is absent from refs, public events, status reason, and logs |

No assertion may be weakened to obtain a pass, and the smoke reports pass, fail and skip counts from fresh output.

---

## 16. Exact files planned for D6B

**New, test-only, outside the application package:**

| File | Purpose |
|---|---|
| `tests/smoke/__init__.py` | package marker |
| `tests/smoke/fakes.py` | deterministic fake LLM, search, fetch and MCP transports implementing the existing protocols |
| `tests/smoke/durable_smoke_app.py` | imports the real `app.main.app`, installs the test supervisor through `app.state.run_worker_supervisor`, exposes `app` |
| `tests/smoke/run_smoke.py` | orchestrates: temp directory and environment, spawn backend, spawn frontend, drive the browser journeys, assert, clean up, report |

**Possibly modified, and only with explicit user approval (section 12):**

| File | Change | Default impact |
|---|---|---|
| `frontend/vite.config.ts` | `proxy["/api"].target = process.env.VITE_PROXY_TARGET ?? "http://backend:8080"` | none — the default string is unchanged |

**Explicitly NOT touched by D6:** `backend/app/**` (no production code), all config defaults, `frontend/package.json` (no new dependency), any `.env`, any Docker file, any legacy module, and every `THUNDER_AI_PRD.md` status row other than the D6 row.

### 16.1 No migration, no public API change, no production dependency (question 13)

- **Migration:** none. The schema is created by the existing boot path into a throwaway SQLite file.
- **Public API:** nothing changed. Every endpoint used already exists.
- **Production dependency:** nothing added to the repository. The browser engine comes from harness MCP tooling, not from `frontend/package.json`.

---

## 17. Estimated commands and expected runtime

```powershell
# backend (test-only entrypoint; temp env is set by the orchestrator)
$env:PYTHONPATH="backend"; python -m uvicorn tests.smoke.durable_smoke_app:app --port 8080

# frontend dev server with the proxy pointed at the host backend
$env:VITE_PROXY_TARGET="http://127.0.0.1:8080"; npm run dev   # only if the vite change is approved

# browser journeys
python tests/smoke/run_smoke.py
```

| Step | Expected |
|---|---|
| Backend boot (migrations, schema verify, supervisor start) | about 5-15 s |
| Frontend dev server ready | about 5-10 s |
| Chat journey (J1 and J6) | about 20-40 s |
| Reconnect journey (J2) | about 15-30 s |
| Approval journeys (J3 and J4) | about 30-60 s |
| Cancel and mobile (J5 and J9) | about 20-40 s |
| Deep Research and Multi-Agent (J7 and J8) | about 40-90 s |
| **D6B subtotal** | **about 1-2 min** |
| **D6C full smoke** | **about 3-5 min** |

Services required: none external. No Docker, no Ollama, no ChromaDB, no DuckDuckGo, no paid API.

---

## 18. Stop conditions

D6B and D6C must stop and ask the user if any of the following occurs:

1. A **migration** turns out to be required — none is expected.
2. Deterministic injection cannot be achieved without a **production-accessible bypass**. The current seams make this unnecessary; if a new seam is ever needed inside `app.*`, stop rather than adding it.
3. Real-browser testing **cannot be isolated from user data**, for example if the only workable database target is a live volume.
4. The smoke would require **paid or live services** — a real LLM, live search, real fetch, or a real MCP server.
5. The plan would require **enabling a default deployment flag**. It must not.
6. **MP-4 deletion turns out to be a precondition** of a truthful smoke, meaning legacy and durable cannot be distinguished without removing legacy. Evidence says they are distinguishable through `X-Run-Id` (section 11), so this should not trigger — but it is the single most important thing to re-check before D6B starts.
7. `frontend/vite.config.ts` may not be modified and no alternative host-run frontend path works, in which case the browser smoke cannot run outside Docker and the user must choose.
8. A material product decision remains outstanding (section 12).

---

## 19. Risks that block MP-4 (question 12)

MP-4 converges and eventually deletes legacy paths. It must **not** start until the smoke proves the following, because each one is a way the durable path could silently be worse than the legacy path it replaces:

| # | Blocking failure | Why it blocks MP-4 |
|---|---|---|
| 1 | The durable branch is not actually selected for a default `auto` send — no `X-Run-Id` | Legacy would be deleted while the replacement is not the path the UI uses |
| 2 | Any duplicate `Message` after reconnect or replay | Data corruption the legacy path did not have |
| 3 | Any lost or truncated final text after a reconnect or an aborted stream | The same class of defect Stage A found in the worker; a UI-level recurrence is worse |
| 4 | Approve does not resume **the same** run, or Deny executes the tool | Approval-policy regression on an irreversible action |
| 5 | Cancel does not reach the server-persisted terminal state | Users could be told a run stopped when it did not |
| 6 | Any unexpected browser console error in a mandatory journey | Unverified client state around the durable protocol |
| 7 | Deep Research or Multi-Agent reachable in the UI while its readiness predicate is false | Enables an incomplete capability |
| 8 | Fakes silently bypass the approval gateway or the idempotency HMAC | Invalidates every conclusion the smoke draws |
| 9 | The smoke only ever runs with fakes and never once against a real free/local provider | The fakes prove protocol, not provider integration; this residual risk must be stated explicitly, and a single real-provider pass — or a documented decision to accept the risk — is required before legacy deletion |

Risk 9 is the honest limit of a deterministic offline smoke and is the strongest argument for keeping D6 subordinate to a later, explicitly approved live-provider check.

---

## 20. Should D6 be one implementation phase or split?

**Recommendation: split into D6B and D6C.**

- **D6B — deterministic smoke harness plus the chat truth-bearing slice** (J1, J6, J10 and A1-A7). This is the smallest artefact that can make a *truthful* claim, and it retires the hidden dependency D0 explicitly flagged as OPEN. It is also where the only file-level risk lives: the vite proxy decision.
- **D6C — the remaining mandated journeys** (J2-J5, J7-J9 and B1-B9). Worth building only once D6B proves the harness end to end.

Rationale: D0 calls D6 small but notes a hidden dependency; proving one mode end to end first converts that unknown into evidence, and it keeps the change surface to one config decision plus test-only modules. A single monolithic phase would couple a genuinely uncertain seam and proxy decision to a large amount of journey scripting, so a harness defect would be misread as a product defect.

MP-4 remains **not started** and must not begin until at least D6B passes and the user reviews the result.

