# Phase 1B.2R — MP-3C3C completion report

**Frontend durable stream, reconnect, and MCP approval UX**

**Date:** 2026-09-11 · **Branch:** `phase-1b1-approval-domain` · HEAD `b2e6718` ·
**all work uncommitted, not production-ready**

Updates the frontend chat flow so it works with **both**:

1. the unchanged legacy `POST /api/sessions/{id}/messages/stream` when
   `run_messages_stream_durable_enabled` is **OFF** (byte-for-byte legacy), and
2. the MP-3C3B durable contract when the flag is **ON** — `Idempotency-Key` per send,
   `X-Run-Id` capture, SSE `id:` cursor + dedupe, bounded reconnect via `Last-Event-ID`/
   `?after_seq`, explicit `POST /api/runs/{id}/cancel`, and an authenticated redacted MCP
   approve/deny modal with same-run resume.

Supports **plain chat + durable MCP approval/resume inside plain chat** only.

**Out of scope (unchanged):** deep-research / multi-agent parity, images/media on the durable
path, citation & confidence parity, enabling the flag by default, deleting legacy behaviour,
browser/terminal/Python execution, reusable grants, paid providers.

---

## Outcome

Implemented and verified at the focused level. The flag stays OFF; with it off the frontend is
byte-for-byte the legacy stream. One additive, non-wildcard CORS change on the backend
(`expose_headers` + two `allow_headers` entries) so a cross-origin frontend can drive the durable
contract. Nothing committed / merged / pushed. Mode parity, flag enablement, and legacy deletion
were **not** performed.

## Stage-0 frontend contract findings (proven from code + tests)

| Question | Finding | Consequence |
|---|---|---|
| Can the frontend get a bearer for durable calls? | Yes — in-memory access token + `getAuthHeadersAsync()` (streaming), `request<T>` (approval/cancel). | No second auth store; reuse both. |
| Is `X-Run-Id` readable? | Not cross-origin — `expose_headers` was unset. | Smallest fix: `expose_headers=["X-Run-Id"]`. |
| Can the frontend *send* `Idempotency-Key` / `Last-Event-ID` cross-origin? | No — `allow_headers` lacked them. | Add exactly those two names (still an explicit list). |
| Is the approval detail API enough for a safe modal? | Yes — `tool_id`, `state`, `risk_level`, `display_summary`/`purpose_redacted`, `created_at`. | No marker-text reconstruction needed. |
| Can one message store reconcile a single durable final `Message` without dup? | Yes — `complete` carries `message_id` (= persisted `RunStep.output_ref`); the post-run session refresh finds the same id. | Optimistic user msg + streamed draft → replaced by the one committed `Message`. |
| Reconnect strategy | Re-POST `/messages/stream` with the same `Idempotency-Key` + `Last-Event-ID`/`?after_seq` (backend already supports it) — **not** `GET /api/runs/{id}/events` (second event vocabulary). | One parser, one wire. |

No stop condition triggered: bearer + CSRF available, `X-Run-Id` needs only a targeted CORS add
(no auth/redesign), approval detail sufficient, single store reconciles without duplication.

## Files changed

| File | Change |
|---|---|
| `frontend/src/lib/sseParser.ts` **(new)** | `SSEStreamParser` incremental dual-wire parser + byte-identical legacy `parseSSEBuffer` shim. |
| `frontend/src/useStreaming.ts` **(rewritten)** | Dual-mode hook: legacy unchanged when no `X-Run-Id`; durable identity/cursor/dedupe/reconnect/cancel/approval when present. |
| `frontend/src/api.ts` | `+getApproval / approveRun / denyRun / cancelRun` via `request<T>`. |
| `frontend/src/types.ts` | `+ApprovalDetail / ApprovalDecision / RunCancelResult`. |
| `frontend/src/components/ApprovalDialog.tsx` **(new)** | Accessible Radix approve/deny modal. |
| `frontend/src/App.tsx` | Durable wiring: `ApprovalDialog`, reconnect/resume/cancel `role="status"` toast, Stop→`cancelDurableRun` when a run id exists. |
| `backend/app/main.py` | CORS: `expose_headers=["X-Run-Id"]`; `allow_headers += ["Idempotency-Key","Last-Event-ID"]`. No other backend change. |
| `tests/test_auth.py` | `+2` CORS tests (durable-header preflight, `X-Run-Id` expose). |
| `frontend/src/__tests__/{sseParser,durableStreaming}.test.ts`, `ApprovalDialog.test.tsx` **(new)** | Focused MP-3C3C tests. |

## Dual parser behaviour

* One `SSEStreamParser`. `event:` / `data:` / `id:` / `:comment` lines; strips one leading SP;
  `data:` lines joined with `\n`; unknown fields (`retry:` …) ignored; unknown **event types**
  surfaced verbatim for the consumer to ignore.
* `id:` accepted only as a **non-negative safe integer**; anything else → frame kept, no id.
* CRLF and LF treated identically for both line and frame (`\n\n`) separators.
* Multi-frame chunk → all frames emitted; a frame split across chunks → held in a bounded buffer
  (`MAX_SSE_BUFFER` = 1 MiB; a hostile no-boundary stream is truncated, never unbounded).
* UTF-8 code point split across byte chunks → reassembled (`TextDecoder(stream:true)`);
  `flush()` emits the decoder tail + a final complete frame with no trailing blank line.
* **Legacy frames (no `id:`) are unchanged** — `parseSSEBuffer(buffer)` returns the exact old
  `{events:[{type,data}], remainder}` shape, JSON-parsing each frame and dropping on failure.
* Consumer-side (durable only): a frame whose `id ≤ cursor` is parsed but has **no side effect**;
  `cursor` advances only on a strictly-greater id; a token is never emitted twice.

## Idempotency and reconnect design

* **Key:** one `crypto.randomUUID()` (RFC-4122 v4 fallback) per **explicit user send**; reused
  verbatim on every reconnect/resume/post-cancel re-open; new key on the next explicit send.
  Not derived from prompt text, not user-editable, never logged, never persisted.
* **Reconnect trigger:** streaming response ends without a terminal event, a run id is known, and
  the hook is not paused on an approval gate / cancelled / unmounted.
* **Reconnect request:** same POST, same `Idempotency-Key`, `+ Last-Event-ID: <cursor>` header
  `+ ?after_seq=<cursor>`. No restart-from-zero, no new run (same key ⇒ same run server-side),
  dedupe by `id`.
* **Backoff:** `min(500ms·2^attempt, 8s)` ± 30 % jitter. **Bounds:** 6 attempts **or** 60 s
  elapsed. Exhaustion ⇒ terminal `onError({code:"reconnect_exhausted"})` — a *recoverable*
  error (app may offer Retry; no hard failure banner for this / `RATE_LIMITED` /
  `DURABLE_UNAVAILABLE`).
* **Stops on:** terminal event, `401/403`, `cancelStream()` / `cancelDurableRun()`, unmount.
  Cleanup effect aborts the fetch and clears the pending backoff timer.
* Raw prompts/tokens are never persisted; run id + cursor live only in refs for the run's
  lifetime. (No `sessionStorage` refresh-recovery implemented in this phase.)

## Message reconciliation

* Optimistic user message on send; assistant draft rendered from `streamedContent`.
* `complete` → draft replaced by the one committed `Message` keyed on `message_id`; the post-run
  session refresh finds that same id ⇒ **no duplicate**.
* `error` / `cancelled` / `reconnect_exhausted` → draft cleared, honest state shown.
* A browser disconnect is **not** shown as a backend cancel — the durable run keeps going and
  still persists its `Message`.
* Never rendered: marker text, tool args, envelope fields, provider-error text, control metadata
  (the wire already redacts; the frontend additionally reads only known fields).
* Another `startStream` is blocked while a durable run is non-terminal.

## Cancel behaviour

* Legacy: `cancelStream()` aborts the fetch → `onCancelled`, no save (unchanged).
* Durable: Stop → `cancelDurableRun()` → `POST /api/runs/{id}/cancel`, phase `cancelling`,
  button disabled against repeat clicks. **The hook never claims cancellation locally** — it
  waits for the server-persisted `cancelled` (or other terminal) stream event, re-opening the
  stream if the reader was already idle. A `404` / already-terminal cancel response is swallowed;
  the stream still delivers the truth.

## Approval UX

* `event: tool_approval_required {approval_id, tool_id}` → phase `waiting_approval`,
  `onApprovalRequired`. **No auto-reconnect while paused** (human think-time is not spent from
  the reconnect budget).
* `ApprovalDialog` fetches `GET /api/approvals/{id}` (bearer + CSRF) and renders **only**
  server-returned redacted fields: `tool_id`, `risk_level` (spelled out — "high risk" — not
  colour-only), `purpose_redacted` / `display_summary`, `created_at`, `state`. **Never** renders
  or logs API keys, action hashes, session-family ids, raw provider metadata, envelope fields.
* Explicit separate **Approve** / **Deny**; no countdown, no auto-approve, no auto mode.
  `decidedRef` + `disabled` ⇒ exactly one decision even on a double click. Approve →
  `.../approve`, Deny → `.../deny`. **The approval API call never executes MCP in the browser.**
* Approve → `resumeAfterApproval()` → phase `resuming` → stream re-opened (same key + cursor);
  status: "Approved — resuming…". Deny → terminal "Denied. The tool call was not run."
  Stale / consumed / already-decided / fetch-failed → safe non-actionable "error" + Close;
  the stream still delivers the real outcome.
* a11y: Radix `role="dialog"` + `aria-modal`; `DialogTitle`/`DialogDescription` wire the
  accessible name/description; focus moves in and is restored on close; Escape/outside-click do
  not dismiss a pending decision; `role="status" aria-live="polite"` narrates every state;
  risk is text + colour, never colour alone.

## Auth / security behaviour

* Reuses the in-memory access token + `getAuthHeadersAsync()` (stream) and `request<T>`
  (approval/cancel) — **no second auth store**. Tokens never in a URL, never logged.
* CSRF double-submit header on every state-changing call.
* `getReader()` guarded: a disturbed/locked body **fails closed** (durable ⇒ bounded reconnect;
  legacy ⇒ `onError`) — never a hanging pass or unhandled rejection.
* `401/403` on a durable pass ⇒ terminal `onError`, reconnect stops (no loop against a revoked
  session family).

## Unsupported-mode behaviour

Backend returns these **before any persistence**; the frontend surfaces them and **does not
silently retry through the legacy path**, and fabricates no citations / progress / confidence /
agent status:

| HTTP | `onError.code` | UX |
|---|---|---|
| 422 | `VALIDATION_ERROR` | generic "not supported on the durable path" (image / deep-research / multi-agent / bad Idempotency-Key) |
| 503 | `DURABLE_UNAVAILABLE` | "durable path unavailable" — recoverable, offer Retry |
| 429 | `RATE_LIMITED` | "too many requests / streams" — recoverable |
| 404 | `SESSION_NOT_FOUND` | session gone |
| other non-2xx | `HTTP_ERROR` | generic |

## React performance decisions

* Token → `contentRef` + one `setStreamedContent` per token. **No rAF batching added** —
  YAGNI until a real profile shows jank; the seam is a single call site if needed later.
* All reconnect / cancel / approval callbacks read `cbRef` / `runIdRef` / `cursorRef` /
  `terminalRef` / `awaitingApprovalRef` from **refs** — no stale closures; `useCallback` deps
  minimal; functional `setState` where prior state matters.
* Transient run identity in refs only; cleanup effect clears the backoff timer + aborts the
  fetch on unmount.
* No new global dependency, state library, or bundle bloat.

## Unit / component / typecheck / build results

| Check | Result |
|---|---|
| `frontend/src/__tests__/sseParser.test.ts` | **13 / 13 pass** |
| `frontend/src/__tests__/durableStreaming.test.ts` | **12 / 12 pass** |
| `frontend/src/__tests__/ApprovalDialog.test.tsx` | **6 / 6 pass** |
| `frontend/src/__tests__/useStreaming.test.ts` (existing) | **29 / 29 pass** (was 12/29 at HEAD — the 17 failures were a stale shared-mock + signature-detection bug, now fixed) |
| `frontend/src/__tests__/RetryIntegration.test.tsx` | back to green (its 1 HEAD failure was the shared unhandled-rejection, now gone) |
| `npx tsc --noEmit` (whole frontend) | **clean** |
| `npm run build` (`tsc && vite build`) | **green** (pre-existing 500 kB chunk-size warning only) |
| Full frontend suite | **230 passed / 48 failed** (pristine HEAD: **181 / 66**). All 48 remaining = `useTheme must be used within a ThemeProvider` in `Sidebar` / `ModelSelector` / `ChatArea` / `App` test files — a **pre-existing** harness gap in components MP-3C3C did not touch (identical set at HEAD). |
| `tests/test_auth.py::TestPhase7CCORS` | **5 / 5 pass** (2 new) |
| `tests/test_run_engine_mp3c3b.py` (MP-3C3B contract) | **39 / 39 pass** |
| MP-3C3A (41) / MP-3C3B (39) full re-run | **not re-run** — contracts unchanged (per the focused policy) |
| `git diff --check` | clean |
| lint | no ESLint script in `frontend/package.json` — nothing to run |

## Rendered browser validation (env + viewport)

* **No real browser.** The repo has no Playwright / E2E harness and none was installed
  (session MCP browsers were not used — there is no served app + backend to point them at, and
  the phase requires a mocked backend).
* Rendered surface = **jsdom** (`@testing-library/react`, `vitest` 4.1.10, `jsdom` env; no
  meaningful viewport — jsdom has no layout). `ApprovalDialog.test.tsx` mounts the **real Radix
  modal** and asserts: `role="dialog"` with an accessible name, `role="status"` live region,
  focusable one-shot Approve/Deny, `denyRun`/`approveRun` each called exactly once under a double
  click, stale/error states show a Close path, and no sensitive field
  (`sk-…`, action hash, session-family id, `/etc/shadow`) ever reaches `document.body.innerHTML`.
* Backend **mocked** (`vi.mock("../api")`, mocked `fetch`) and labelled as such. **No
  live-provider / real-browser E2E claim.** No screenshots, traces, or browser scripts saved in
  the repo.

## Recovery pass (2026-09-11, post-interruption)

Session interrupted mid-close-out (usage limit) before this report's final read-through was
delivered. Re-verified from repo state rather than trusted: structural read of
`sseParser.ts`/`useStreaming.ts`/`api.ts`/`types.ts`/`ApprovalDialog.tsx`/`App.tsx` diff/CORS
diff, all counts below re-run fresh (not copied from the interrupted transcript).

One correction applied: `newIdempotencyKey()`'s RFC-4122 fallback dereferenced
`crypto.getRandomValues` **outside** the `try`, with no `typeof crypto !== "undefined"` guard —
on an environment where the `crypto` global itself is absent this threw an unguarded
`ReferenceError` (not a graceful failure), and since the call is synchronous inside `startStream`
before `startPendingRef` is ever cleared, an uncaught throw there would have permanently wedged
the double-submit guard. Fixed by guarding the fallback behind the same `typeof` check and
throwing an explicit, intentional error when no secure RNG is available (never falling back to
`Math.random`). No behavior change on any real browser (all evergreen browsers have
`crypto.getRandomValues`); the fix only changes what happens on an environment that lacks it.

## Remaining blockers

* None for MP-3C3C's scope. The pre-existing `ThemeProvider` test-harness gap (48 failures in
  untouched components) is out of scope and was left as-is.
* Deep-research / multi-agent / image / citation & confidence parity, enabling
  `run_messages_stream_durable_enabled`, retiring the legacy stream, and multi-process notifier
  delivery remain for MP-3C3D+ / MP-6.

## Confirmation

Mode parity — **not** implemented. Default flag — **not** enabled
(`run_messages_stream_durable_enabled` stays OFF). Legacy behaviour — **not** deleted. Commit /
merge / push — **not** performed.

## Source-control confirmation

Branch `phase-1b1-approval-domain`, HEAD `b2e6718`. All MP-3C3C work is uncommitted in the
working tree alongside the earlier uncommitted 1B.1 / 1B.2 / 1B.2R changes. The pre-existing
`stash@{0}` ("WIP on main: 7ab31a5 …") is untouched. No branch switch, rebase, reset, or clean.
