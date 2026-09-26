# MP-3C3D6C-J8 — desktop/mobile UX, responsive layout, accessibility and reload-hydration verification — COMPLETE

Date: 2026-09-17
**Status: D6C-J8 COMPLETE — D6C IN PROGRESS (J1-J8 complete; J9 not started).**

Consolidates the J8, J8-R1, J8-R2 and J8-R3 passes. **Evidence is labelled by pass**,
because the measurements were not all taken in one run.

## Numbering reconciliation (read first)

| Current D6C label | D6A journey |
|---|---|
| D6C-J6 | D6A J7 - Deep Research |
| D6C-J7 | D6A J8 - Multi-Agent |
| D6C-J8 | D6A J9 - mandatory mobile viewport repeat |
| D6C-J9 | D6A J10 - worker-health gate |

Historical reports were not renamed. The D6A J8 (Multi-Agent) requirements were all
proven under the D6C-J7 report; this report covers the D6A J9 mobile-viewport requirement
plus its desktop counterpart.

## Real-versus-fake matrix

Real: Chromium, production frontend, real responsive CSS, real sidebar/navigation, real
ModeSelector, real composer and submit control, FastAPI lifespan, auth / cookies / Bearer
/ CSRF, durable /messages/stream, readiness and capability checks, supervisor and worker,
SQLite, SSE consumption, progress rendering, Message and citation persistence, reload
hydration.

Fake (reused, unchanged): only the deterministic external-operation fixtures already
verified in J6 (search / fetch / synthesis) and J7 (three-role provider plus the single
rag_service.retrieve_chunks patch). No DuckDuckGo, live fetch, Ollama, ChromaDB,
OpenRouter, paid provider, external MCP or code execution. The outbound-network tripwire
was armed for every run and was silent throughout.

## Desktop journey (J8-R3, 1440x900, Deep Research)

Captured directly from the browser network record BEFORE any reload:

| Item | Value |
|---|---|
| ordinal / method / path | 603 / POST / /api/sessions/1/messages/stream |
| query string | (none) |
| status | 200 |
| request content type | application/json |
| response content type | text/event-stream; charset=utf-8 |
| raw JSON body | {"message":"D6C-J8R3 desktop deep research request","mode":"deep_research"} |
| exact mode | deep_research |
| idempotency key | 3e88573f-6907-44c9-9b70-c17cc6491a43 |
| X-Run-Id | fff29f3e-c085-471f-89b6-ebc89a67826e |
| SSE / persisted events | 9 run_events; terminal complete once |
| terminal complete count | 1 |
| reconnect count | 0 |
| abort/cancel count | 0 |

Proven: one logical submission, one distinct idempotency key, one Run, X-Run-Id equals the
persisted Run id, one user Message, one assistant Message, one terminal complete, no
duplicate bubble, no reconnect.

### Desktop DB/UI alignment (J8-R3)

| Table | Count |
|---|---|
| runs | 1 (deep_research, succeeded) |
| run_steps | 3 - load_context pending, research_fetch_extract succeeded, deep_research_synthesis succeeded |
| run_payloads | 2 |
| run_events | 9 |
| messages | 2 (user 38 chars, assistant 146 chars) |
| citations | [W1], [W3] |
| run_signals / pending_approvals / dispatch_records / execution_turns / tool_calls | 0 each |
| search / fetch invocations | 1 / 2 |
| tripwire | silent |

DB Message count 2 == UI "2 messages"; terminal DB state == terminal UI state; reload
created no new row.

## Dark theme (J8-R3)

Desktop 1440x900 and mobile 390x844, after setting the documented localStorage theme key
to dark and reloading:

* body background rgb(8, 9, 12), body foreground rgb(255, 255, 255) - dark applied before
  normal interaction at both viewports
* primary navigation fg rgb(255,255,255) on rgb(8,9,12)
* mode-selector trigger fg rgb(255,255,255) on rgba(255,255,255,0.05)
* composer, progress/status, assistant answer and citation UI all fg rgb(255,255,255)
* zero inline-script errors at either viewport
* automatic session/message hydration occurred WITHOUT clicking ("2 messages", answer
  rendered)
* no document/body horizontal overflow

**No WCAG contrast ratios are reported** - no contrast analyzer was run and none were
fabricated; only computed colour values are recorded.

## Mobile journeys and reconnect history

| Pass | Scenario | Stream POSTs | terminal_sent | more_body | complete | reconnect |
|---|---|---|---|---|---|---|
| J8 (first) | multi_agent | 2 (initial + after_seq=10 with Last-Event-ID) | not traced | n/a | 1 | 1, unexplained |
| J8-R1 | multi_agent_longtokens | 1 | True | False | 1 | 0 |
| J8-R2 | multi_agent_longtokens | 1 | True | False | 1 | 0 |

J8-R1 and J8-R2 are two consecutive clean traced runs. Mobile provider invocations stayed
exactly three in the locked order (researcher, solution_specialist, reviewer); one Run,
one user Message, one assistant Message each.

## Mode-selector evidence

Keyboard selection and non-submission (J8, desktop): trigger is a real button with
type=button and an accessible name; ArrowDown opened the menu; arrow keys moved
Auto -> Chat -> Deep Research; Enter selected it. Selection produced ZERO
/messages/stream POSTs and left the textarea untouched.

Escape close and focus return (J8-R1, mobile): menu opened (aria-expanded=true), arrow to
another item, Escape -> menu closed, aria-expanded=false, document.activeElement is the
trigger (BUTTON), no submission.

Freeze/unlock (J8-R2, mobile, passive 100 ms attribute observer installed before
submission): 23 samples with disabled=true during the non-terminal window; the menu never
opened while disabled; the selected label never changed; after terminal completion
disabled=false with 171 post-unlock samples, and the DB still held exactly one Run.

Capability (J6/J7, unchanged): available modes match GET /api/settings; an unavailable
mode is exposed disabled with a human-readable reason, never an internal slug.

## Responsive layout and long-token measurements (J8-R2, mobile 390x844)

Fixture: default J7 reviewer output byte-for-byte unchanged at 224 chars; the
tests/smoke-scoped variant produces 612 chars with a 142-char unbroken ASCII token and a
168+-char unbroken URL.

| Measurement | Value |
|---|---|
| innerWidth / clientWidth | 390 / 390 |
| documentElement.scrollWidth | 390 (passes) |
| body.scrollWidth | 390 (passes) |
| user / assistant bubbles | 1 / 1 |
| citations rendered | [1], [2], [1] |
| composer / mode / send | reachable / reachable / reachable |
| horizontal scrolling needed | no |

## Accessibility and touch targets

One clear main conversation region; navigation is a complementary landmark (ASIDE);
navigation controls, mode trigger and send control have accessible names; mode choices
expose menuitemradio selection semantics with exactly one checked; disabled modes are
exposed disabled with a readable reason; focus order follows the visible UI; focus is
visible; keyboard-only submission works; no duplicate focusable hidden control was
observed between layouts.

Touch-target residual risk (measured in J8 and J8-R3): mode trigger about 133x30 (93x30
with the shorter label in J8-R3 mobile), send about 40x40. Both remain named, visible,
focusable where applicable, reachable and hit-testable, but are below the commonly cited
~44x44 guidance. The repository documents no explicit target size. Recorded as a
residual risk; no production CSS change was authorized or applied. This does not block the
narrow D6A requirement that mobile controls remain operable and not layout-blocked.

## Console and network classification

| | Desktop | Mobile |
|---|---|---|
| Unexpected token ')' | 0 | 0 |
| manifest syntax errors | 0 | 0 |
| page errors | 0 | 0 |
| unhandled rejections | 0 | 0 |
| authenticated-turn application errors | 0 | 0 |
| pre-login refresh 401/403 | baseline only, did not repeat after login | same |
| warnings | 1 | 1 |

The single warning at both viewports is the pre-existing Apple mobile web-app meta
deprecation (apple-mobile-web-app-capable); reported honestly, not hidden.

## Residual risks (preserved, not repaired)

Historical intermittent reconnect:

    One historical intermittent resume was observed.
    It was not reproduced in two consecutive ASGI-traced clean mobile runs.
    Both traced runs delivered terminal completion with more_body=False.
    Root cause was not established.
    No product defect is claimed.

Long unbroken content:

* Page-level overflow criteria pass: documentElement and body do not widen.
* Critical controls remain reachable and no horizontal scrolling is required.
* The long token/URL paragraph content box overflows internally (clientWidth 296,
  scrollWidth 1164, overflow-x visible, no wrapping rule) and is clipped by an ancestor.
* This is a readability/responsiveness residual risk, not a proven page-overflow failure.
* A future improvement could use overflow-wrap:anywhere or word-break:break-word.
* No fix is authorized or applied in J8.

Touch targets:

* Mode trigger about 133x30; send button about 40x40.
* Both remain named, visible, focusable where applicable, reachable and hit-testable.
* They remain below the commonly cited ~44x44 guidance.
* No production CSS fix is authorized or applied.

## Focused verification (J8-R3)

| Command | Result |
|---|---|
| pytest tests/smoke -q | 91 passed |
| vitest ModeSelector, ModePropagation, useStreaming, useStreamingSafety, useStreamingStrictMode, durableStreaming, sseParser, sessionHydration, ChatArea, indexHtmlSyntax | 133 passed (10 files), 0 failed |
| npx tsc --noEmit -p tsconfig.json | exit 0 |
| npm run build | exit 0 |
| py_compile (changed smoke python) | exit 0 |
| import app.main | IMPORT_OK |
| git diff --check | clean |

## Cleanup

After every run: 0 smoke processes, 0 waiting orchestrators, 0 bound ports, 0 thunder_d6*
temp roots, 0 pending observer/instrumentation tasks, and no binary screenshots or
.playwright-mcp directory inside the repository. Evidence archived outside the repository
under C:/deepseek-harness (d6c-j8-desktop, d6c-j8-mobile, d6c-j8r1-desktop, d6c-j8r2,
d6c-j8r3).

Repository state is unchanged: branch phase-1b1-approval-domain, HEAD
b2e67183d4791cbbaed9d3d98e8833983510f3ff, 0 staged, stash@{0} untouched. No commit, stage,
merge, push, reset, rebase, stash, clean, discard, branch switch or worktree operation was
performed. The protected architecture document is unmodified at 3332 lines / 268044 bytes
/ f4d0bbc24f20529addb32ac10bf12e197c7ad747. A fresh Settings() instance still reports all
seven durable production defaults as False.

## Open items

1. The Vite dev-proxy concurrency defect (D6B-R5) remains OPEN; the smoke continues to use
   the approved direct-backend workaround. Not fixed, not claimed fixed.
2. load_context remains pending after successful runs - recorded, not classified; the
   lifecycle question is preserved for J9.
3. The historical mobile resume, the long-token clipping and the sub-44px touch targets
   remain documented residuals.
4. J9 and MP-4-MP-6 are not started.
