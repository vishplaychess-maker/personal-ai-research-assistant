# MP-3C3D6C-J1 — forced SSE reconnect (early-EOF) — COMPLETE

Date: 2026-09-17  
**Status: D6C-J1 COMPLETE — D6C IN PROGRESS (J1 complete; J2–J9 not started).**

## Fidelity statement (read this first)

The disconnect is a **deterministic test-only server-side truncated SSE / early EOF**.
It is **not** a physical network sever, browser offline emulation, TCP reset or Wi-Fi loss.
It proves the production client's documented **reconnect-on-incomplete-EOF** path plus durable
server resume semantics. Physical network-loss behaviour remains covered only by existing
lower-level tests and was not independently proven here.

## Why early EOF and not offline emulation

The previous attempt used `page.context().setOffline(true/false)`. Evidence proved that
Chromium **keeps delivering an already-established SSE response** while offline emulation is
active: tokens continued at the normal 0.45 s cadence throughout the offline window
(`t=5745/6202/6668` while offline from 5278→6792), `offlineFails: 0`, no reconnect. Offline
emulation was therefore abandoned in favour of the server-side cut you authorised.

## Test-only injector

`tests/smoke/sse_fault_injector.py` — ASGI middleware, **disabled by default**, armed only
in-process by the smoke app (`THUNDER_SMOKE_TRUNCATE_FIRST_STREAM=1`, read *only* by
`tests/smoke/durable_smoke_app.py`). Not selectable by header, query, body, production env var,
production config flag or normal startup. It never touches the DB, provider, worker, notifier or
approval gateway; it observes and re-chunks one HTTP response stream.

Behaviour: for the **first** `POST /api/sessions/{id}/messages/stream` it forwards **complete
SSE frames only**, up to and including the first complete `event: token` frame, then sends
`more_body=False` and unwinds the application coroutine via a private `BaseException`. Every
later request passes through byte-for-byte. One cut per instance.

## Focused injector tests (RED → GREEN)

`tests/smoke/test_sse_fault_injector.py` — **6 passed** (RED first: `ModuleNotFoundError`).

| Test | Proves |
|---|---|
| A | disabled injector forwards the full stream unchanged, 0 cuts |
| B | armed: forwards exactly through the first token frame, excludes terminal frames, records one cut with ids/types/bytes/timestamp |
| C | second identical request is byte-for-byte complete, cut count stays 1 |
| D | non-target route and non-POST method are never modified |
| E | split UTF-8 and split frame boundaries reassembled before the cut |
| F | no pending asyncio task after the cut |

## Browser journey evidence (real Chromium, real backend)

| ordinal | t (ms) | method | path | status | content-type | idem hash | X-Run-Id | Last-Event-ID | after_seq |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 640 | POST | `/api/sessions/1/messages/stream` | 200 | `text/event-stream` | `9697370c` | `6f7e5b0b-aca8-47bc-aac6-7607827c2c02` | — | — |
| 4 | 2725 | POST | `/api/sessions/1/messages/stream` | 200 | `text/event-stream` | `9697370c` | `6f7e5b0b-aca8-47bc-aac6-7607827c2c02` | **`4`** | **`4`** |

One logical submission, **one distinct Idempotency-Key**, **one Run across both requests**, and the
reconnect carried the correct cursor (`run_seq 4` = the first token event) on **both** transports.

Injector cut record:

```json
{"cut_at_ms": 196734, "event_ids": [null, "3", "4"], "event_types": ["start", "run_status", "token"],
 "forwarded_bytes": 8584, "path": "/api/sessions/1/messages/stream", "status": 200,
 "terminated_early": true, "token_frames_forwarded": 1}
```

Rendered progression (no gap, no duplicated prefix): 2260 → 4508 → 6758 → 9008 → 9047, with the
reconnect occurring between the first and second observations (`reqs` 1 → 2). `getReader: 2`
(one per pass), **`aborts: 0`, `cancels: 0`, `errs: []`**, `complete: 1`, composer returned to normal.

## Four-way equality (no normalisation)

| Source | chars | UTF-8 bytes | SHA-256 |
|---|---|---|---|
| provider output | 8597 | 18047 | `557b82aca8c4e93ef113cde7a8fae1546d142956c67e5fc1ab8a4e3483bdf5e1` |
| persisted text RunEvents (run_seq) | 8597 | 18047 | `557b82aca8c4e93ef113cde7a8fae1546d142956c67e5fc1ab8a4e3483bdf5e1` |
| assistant Message | 8597 | 18047 | `557b82aca8c4e93ef113cde7a8fae1546d142956c67e5fc1ab8a4e3483bdf5e1` |
| clean browser-rendered response | 9047* | 18047 | `557b82aca8c4e93ef113cde7a8fae1546d142956c67e5fc1ab8a4e3483bdf5e1` |

All four byte lengths and all four hashes are identical. (*char count is UTF-16 units vs code points.)

## DB and execution invariants

```
runs=1 run_steps=1 messages=2 run_events=9 run_payloads=1 run_signals=0
dispatch_records=0 pending_approvals=0 tool_calls=0 execution_turns=0
Run id=6f7e5b0b-... kind=chat_stream status=succeeded   (== X-Run-Id)
run_seq [1..9] strictly increasing: True
messages: 1 user (32) + 1 assistant (8597)
provider_selected=1 provider_invoked=1
cancel_requested signals: 0   tripwire.jsonl absent   canary absent (DB + log)
```

## Console

Zero manifest errors, **zero pageerror**, **zero unhandledrejection**, zero failed authenticated
requests. Only the pre-login `/api/auth/refresh` 401 (expected baseline, did not repeat during the
authenticated journey) and the pre-existing `apple-mobile-web-app-capable` deprecation warning.

## Residual risk (open, untouched)

The **Vite development-proxy concurrency defect remains OPEN**; the smoke uses the approved
`VITE_API_BASE_URL` direct-backend route, which is a workaround, not a repair.

## Source control

Branch `phase-1b1-approval-domain`, HEAD `b2e6718…`, 0 staged, `git diff --check` clean,
`stash@{0}` (`7ab31a5`) untouched, architecture document **3332 / 268044 / f4d0bbc2…** unchanged.
No commit, merge, push, stage, reset, rebase, stash, clean, discard, branch switch or worktree.
No production file was modified; changes are confined to `tests/smoke/` plus documentation.
