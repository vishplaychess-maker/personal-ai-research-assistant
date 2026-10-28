# Phase 1B.2R — MP-3C3D3B completion report

Capability-aware frontend mode selector and request-mode propagation.
Builds directly on MP-3C3D3A's canonical request-level mode contract
(`chat` / `deep_research` / `multi_agent`, strict exact-match validation,
one authoritative `_ALLOWED_MODES`), which is **not** re-implemented or
re-audited here. This phase adds:

1. A per-message UI mode selector (Auto / Chat / Deep Research / Multi-Agent).
2. The smallest additive, authenticated capability contract needed to drive
   it truthfully (`chat_modes` on the existing `GET/PUT /api/settings`).
3. Wire-serialization and reconnect-immutability plumbing in `useStreaming`.

No durable deep-research/multi-agent handler was implemented. No frontend
selector existed before this phase (`git status` at Stage 0 showed no
`ModeSelector`/capability files). `run_messages_stream_durable_enabled` and
`run_engine_enabled` remain `False` by default throughout.

**Corrected record (closeout pass, same day):** the implementation pass's
directly-affected frontend verification command
(`ChatArea.test.tsx` + the other directly-affected suites) originally
returned **108 collected, 99 passed, 9 failed** — all 9 in `ChatArea.test.tsx`.
The prior version of this report incorrectly summarized this as "9
pre-existing failures... left untouched" without per-test evidence, and the
final recap incorrectly said all tests were verified passing. Both claims
were wrong. See "Closeout correction" below for the individual
investigation, classification, and fix of each of the 9 failures, plus the
new component-integration proof this pass added. The corrected, freshly-run
result is **zero failures** across every directly-affected frontend file.

## Skills used

- `superpowers:verification-before-completion` — gated every claim in this
  report on a command run in this session (compile, tests, tsc, diff-check).
- Ponytail (full mode, active for the session) — shaped every design choice
  below: reuse `GET /api/settings` instead of a new route; reuse the
  Radix `DropdownMenu`/`DropdownMenuRadioGroup` primitive already vendored
  in `components/ui/dropdown-menu.tsx` (used by `ApprovalDialog.tsx` and
  the chat-export menu) instead of hand-rolling a listbox; reuse the
  existing `mk_engine`/`mk_session`/`ensure_user` hermetic test fixtures
  from `tests/run_engine_helpers.py`; add a single optional trailing
  parameter to `startStream` rather than restructuring its overload
  dispatch. No frontend-testing or React-specific skill was found available
  in this environment beyond the general Superpowers skill set, so none
  beyond the above was invoked. No unrelated skill was used.

## Stage-0 evidence (this session)

```
git status --short   → dirty Phase 1B tree unchanged from prior sessions;
                        no ModeSelector/capability files present
git branch --show-current → phase-1b1-approval-domain
git rev-parse HEAD        → b2e67183d4791cbbaed9d3d98e8833983510f3ff
git diff --check          → clean (pre-existing CRLF notices only)
git stash list             → stash@{0}: 7ab31a5 (untouched throughout)
```

Confirmed before editing: the MP-3C3D3A completion report and architecture
§40 exist; no D3B selector file existed; no `advance_deep_research_runs` /
`advance_multi_agent_runs` durable handler exists; `run_engine_enabled` and
`run_messages_stream_durable_enabled` are both `False`.

## Existing UX / capability findings

1. **Where the selector lives**: `ChatArea.tsx` renders two composer blocks
   (centered empty-state input, and the bottom bar once messages exist) that
   share the same send path — `App.tsx`'s `handleSend` → `useStreaming.startStream`.
   A `ModeSelector` row was added directly above the `<textarea>` in both
   blocks (not the header, where `ModelSelector` lives) so it reads as
   per-message, not per-session.
2. **Existing authenticated settings endpoint reused**: `GET/PUT
   /api/settings` (`routes/settings.py`) already requires
   `Depends(get_current_user)` and already returns a per-user
   `SettingsResponse`. This is a truthful place to add an advisory
   `chat_modes` field — no new route, no new auth wiring.
3. **No new endpoint required** — confirmed by (2).
4. **Selected mode stability across reconnects**: `useStreaming.ts`'s
   `submissionRef` already snapshots `{sessionId, message, imageUrl}` ONCE
   in `startStream` and every reconnect (`runPass(true)`) re-reads that same
   ref, never live state. Adding `mode` to the same ref/snapshot gives it the
   identical immutability guarantee for free — no new mechanism needed.
5. **Omitted Auto preserves existing behavior**: `ChatRequest.mode` /
   `SubmitChatRunBody.mode` are already `Optional[str]` (MP-3C3D3A); the
   body is built with `JSON.stringify({..., mode: sub.mode})`, and
   `JSON.stringify` drops an `undefined`-valued key entirely — so Auto
   (`mode: undefined`) produces the exact same wire body as before this
   phase existed.

No STOP condition was triggered: one authenticated settings endpoint could
truthfully describe mode availability with an additive field; there is one
send path (`startStream`) that already snapshots into one immutable ref; no
durable execution needed to be enabled; explicit legacy routing is already
reachable from the frontend via the existing omitted/explicit `mode` field
D3A added; no migration was needed; capability truth is computable from
already-non-secret booleans; no approval/reconnect behavior needed weakening.

## Capability truth table

Computed once, in `routes/settings.py::_compute_chat_mode_capabilities()`,
by reusing exactly the two signals the existing D3A/D1 routing gates
already check — never a new or duplicated allowlist:

| `run_messages_stream_durable_enabled` | `enable_deep_research` | `enable_multi_agent` | `chat` | `deep_research` | `multi_agent` |
|---|---|---|---|---|---|
| False | True  | True  | available | available | available |
| False | False | True  | available | **unavailable** ("Not enabled on this deployment.") | available |
| False | True  | False | available | available | **unavailable** ("Not enabled on this deployment.") |
| True  | any   | any   | available | **unavailable** ("Not yet available for durable chat.") | **unavailable** ("Not yet available for durable chat.") |

"Auto" is never a backend mode and never appears in `chat_modes` — it is
the frontend-only choice to omit `mode`. A mode being *recognized* by
`submission.validate_request_mode` is not sufficient to advertise it; only
the executability checks above are. Existing backend validation/routing
(`validate_request_mode`, `unsupported_mode_reason`, `resolve_mode`) is
**unchanged** and remains the sole authority — this table is advisory UX
only, and a forged/stale request is still rejected exactly as before.

## Selector behavior and accessibility

- Component: `frontend/src/ModeSelector.tsx`, built on the existing vendored
  `components/ui/dropdown-menu.tsx` (`@radix-ui/react-dropdown-menu`,
  already a dependency — no new icon or UI library added; icons are
  additional imports from the already-installed `lucide-react`).
- Default display: **Auto**. Options: Auto, Chat, Deep Research,
  Multi-Agent, each with an icon plus a text label (not color-only).
- Unavailable modes remain visible, `disabled`, and show a short bounded
  reason string (e.g. "Not enabled on this deployment.") — never an
  internal slug like `mode_unsupported_deep_research`.
- Keyboard accessible: the trigger is a real `<button type="button">`
  (verified — never submits an enclosing form); Radix's
  `DropdownMenuTrigger`/`DropdownMenuContent`/`DropdownMenuRadioGroup`
  provide native keyboard open (Enter/Space/ArrowDown), roving focus that
  skips disabled items, and `Escape` to close — proven in
  `ModeSelector.test.tsx` via a `keyDown` Enter test.
  `DropdownMenuRadioItem` renders `role="menuitemradio"` with Radix-managed
  `aria-checked`/`data-disabled`, giving accessible selected-state semantics
  for free.
- `aria-label` on the trigger states the current mode and that activating
  it changes it.
- Small-screen safe: the selector sits in its own toolbar row above the
  `<textarea>` (not squeezed into the same flex row as the send/attach
  buttons), so it can never shrink the message input.
- Selecting a mode never submits the message — proven by the trigger's
  `type="button"` and by `ModeSelector.test.tsx`'s selection test only
  invoking `onModeChange`, never `onSend`.
- Frozen while streaming: `ChatArea` passes `disabled={isStreaming}` into
  `ModeSelector`, matching the existing pattern already used on the
  `<textarea>` and attach buttons.
- Capability refresh invalidating the current explicit selection resets it
  to Auto and shows a small, auto-dismissing (`role="status"`) non-
  destructive notice — proven in `ModeSelector.test.tsx`.
- A capability-fetch failure falls back to a safe default (`chat` available,
  `deep_research`/`multi_agent` disabled) — never speculative enablement —
  proven in `ModeSelector.test.tsx`.

## Exact request serialization

`useStreaming.ts`'s `startStream` gained one optional trailing parameter,
`mode?: BackendChatMode` (no existing call site — 30+ across the test suite
— needed to change, since all of them already omit trailing args):

```ts
startStream(sessionId, message, imageUrlOrCallbacks, callbacks?, mode?)
```

`App.tsx`'s `handleSend` computes `modeSnapshot = chatMode === "auto" ?
undefined : chatMode` and passes it as the 5th argument at the moment of
send — a synchronous read of the (mutable, changeable-for-the-next-message)
UI state into an immutable local value.

`submissionRef.current = { sessionId, message, imageUrl, mode }` freezes
that value for the entire logical send. The POST body is built as:

```ts
JSON.stringify({ message: sub.message, image_url: sub.imageUrl, mode: sub.mode })
```

- `mode: undefined` (Auto) → `JSON.stringify` drops the key — the body has
  **no** `mode` property at all.
- `mode: "chat"` → `{"mode":"chat", ...}`.
- `mode: "deep_research"` → `{"mode":"deep_research", ...}`.
- `mode: "multi_agent"` → `{"mode":"multi_agent", ...}`.
- Never `mode: null`, never `mode: "auto"`, never a display label
  ("Chat", "Deep Research", "Multi-Agent") — proven by
  `durableStreaming.test.ts`'s new serialization tests, which assert on the
  raw JSON string.

## Reconnect / idempotency proof

- `submissionRef` (containing `mode`) and `idemKeyRef` (one
  `crypto.randomUUID()`) are both set exactly once per `startStream()` call
  and never mutated until the logical send reaches a terminal state.
- Every reconnect pass (`runPass(true)`, scheduled by `scheduleReconnect`)
  and every post-approval resume (`resumeAfterApproval` → `scheduleReconnect`)
  reads the SAME `submissionRef.current` / `idemKeyRef.current` — proven by
  `durableStreaming.test.ts`'s new tests: an explicit-mode send that
  reconnects sends the identical `mode` on both the initial POST and the
  reconnect POST; an Auto send's reconnect still omits `mode` on both calls.
- A later, separate `startStream()` call (a new message, possibly with a
  new UI mode selection) mints a fresh `idemKeyRef` and a fresh
  `submissionRef` — proven: two sequential sends with different modes
  produce two different `Idempotency-Key` values, each carrying its own
  mode.
- `Last-Event-ID` / `?after_seq` cursor dedupe (MP-3C3C) is untouched — the
  body-construction change is additive (one more key), the header/cursor
  logic in `runPass` was not touched.
- Cancel (`cancelStream`/`cancelDurableRun`), approval
  (`onApprovalRequired`/`resumeAfterApproval`), and legacy/durable SSE
  parsing (`SSEStreamParser`) are all unchanged code paths — confirmed by
  the full existing `durableStreaming.test.ts` + `useStreaming.test.ts` +
  `sseParser.test.ts` suites passing unmodified alongside the new tests.

## Files changed

| File | Change |
|---|---|
| `backend/app/routes/settings.py` | Added `ChatModeCapability` schema, `_compute_chat_mode_capabilities()`, and a `chat_modes` field on `SettingsResponse` (both GET and PUT responses). Reuses `messages_stream._SUPPORTED_DURABLE_MODES` and `config.settings.{enable_deep_research,enable_multi_agent,run_messages_stream_durable_enabled}` — no new allowlist. |
| `tests/test_settings_mode_capability.py` | New — 9 focused backend tests for the capability contract. |
| `frontend/src/types.ts` | Added `BackendChatMode`, `UIChatMode`, `ChatModeCapability`; added optional `chat_modes` to `UserSettings`. |
| `frontend/src/useStreaming.ts` | `startStream` gained an optional trailing `mode?: BackendChatMode` parameter; `submissionRef` carries it; the POST body includes `mode` (dropped by `JSON.stringify` when `undefined`). |
| `frontend/src/ModeSelector.tsx` | New — the selector component. |
| `frontend/src/ChatArea.tsx` | Added `chatMode`/`onChatModeChange` props; renders `ModeSelector` above both composer textareas. |
| `frontend/src/App.tsx` | Added `chatMode` state (default `"auto"`); `handleSend` snapshots it and passes it to `startStream`; wired into `<ChatArea>`. |
| `frontend/src/__tests__/ModeSelector.test.tsx` | New — 9 focused UI tests. |
| `frontend/src/__tests__/durableStreaming.test.ts` | +8 focused serialization/reconnect-immutability tests. |
| `frontend/src/__tests__/ChatArea.test.tsx` | `baseProps` extended with `chatMode`/`onChatModeChange` (2-line addition, implementation pass). **Closeout correction pass**: 9 stale assertions repaired to match current, valid, D3B-unrelated product copy — see "Closeout correction" below. |
| `frontend/src/__tests__/ModePropagation.test.tsx` | New (**closeout correction pass**) — 11 component-integration tests proving mode selection reaches the real `App.handleSend` → `useStreaming.startStream` boundary through a real `<App/>` render. |

No migration. No new dependency. No frontend file outside the above was
touched. `resolve_mode`/`validate_request_mode` (MP-3C3D3A) were **not**
modified.

## Backend focused test commands and counts

```
PYTHONPATH=backend .venv/Scripts/python.exe -m pytest \
  tests/test_settings_mode_capability.py \
  tests/test_run_engine_mp3c3d3a.py \
  tests/test_run_engine_mp3c3d1.py -q
```
→ **71 passed** (9 new capability + 46 MP-3C3D3A + 16 MP-3C3D1 — the smallest
set that proves the new import (`messages_stream._SUPPORTED_DURABLE_MODES`)
and the additive schema field caused no regression in mode-aware routing).
`py_compile` on `routes/settings.py` + the new test file: clean.
`git diff --check`: clean. `import app.main`: OK.
The full 613-test / PostgreSQL / Ollama / ChromaDB / provider / Docker /
eval suites were **not** run (out of scope per this phase's credit-economics
instructions; `routes/messages.py`/`messages_stream.py` were not modified).

## Frontend focused test commands and counts (post-closeout-correction)

```
npx vitest run src/__tests__/ChatArea.test.tsx --reporter=verbose
```
→ **25 passed, 0 failed** (fresh run, post-correction).

```
npx vitest run \
  src/__tests__/ModeSelector.test.tsx \
  src/__tests__/durableStreaming.test.ts \
  src/__tests__/useStreaming.test.ts \
  src/__tests__/sseParser.test.ts \
  src/__tests__/RetryIntegration.test.tsx \
  src/__tests__/ApprovalDialog.test.tsx \
  src/__tests__/ChatArea.test.tsx \
  src/__tests__/ModePropagation.test.tsx
```
→ **8 files, 119 total, 119 passed, 0 failed** (the original 108 across the
7 implementation-pass suites, all now green, + 11 new component-integration
tests in `ModePropagation.test.tsx`). See "Closeout correction" below for
exactly what each of the originally-failing 9 tests was and how it was
fixed.

`npx tsc --noEmit`: clean, 0 errors. No lint script exists in `package.json`
(unchanged from the MP-3C3C finding). Full `npm run build` was **not** run —
no bundler-level import or config change was made and `tsc --noEmit`
already establishes build correctness for this phase's changes.

## Closeout correction (2026-09-12): the 9 `ChatArea.test.tsx` failures, investigated and fixed

**What was wrong with the original report:** it asserted the 9 failures were
"pre-existing" and "unrelated," and the final recap said all tests were
verified passing — neither claim was backed by per-test evidence, which is
not acceptable given `ChatArea.tsx` was materially modified in this phase.

**Evidence gathered:** `npx vitest run src/__tests__/ChatArea.test.tsx
--reporter=verbose`, captured in full, plus `git diff -- frontend/src/ChatArea.tsx`
(the exact 4 hunks this phase added: the `ModeSelector` import, the
`chatMode`/`onChatModeChange` prop additions, and the two `<ModeSelector>`
insertions above the composer textareas — nothing else) and `grep` of the
component's current markup for each failing assertion's target string.

**Classification of all 9 (per the required triage — category 3, "genuine
pre-existing failure demonstrably unrelated," still repaired per the rule
that a directly-affected file's tests are always worth fixing):**

| # | Test | Expected (stale) | Actual current product text | Evidence it predates D3B |
|---|---|---|---|---|
| 1 | shows empty state when no session is selected | `"Select or create a session"` | Two-node heading: `"Start a conversation with Thunder AI"` + `"Select a session from the sidebar or create a new one to begin."` | Lines 367-380 (the `!activeSession` early return) — outside every D3B diff hunk |
| 2 | shows start conversation prompt when session selected, no messages | `"Start a conversation"` | `"Ask Thunder AI anything"` (centered composer heading) | Line 528 — outside every D3B diff hunk |
| 3 | shows message count | `"2 msgs"` | `"2 messages"` (`{messages.length} message{s}`) | Line 410 — outside every D3B diff hunk |
| 4 | renders memory status | `"🧠 On"` (visible text) | No visible status text at all — an icon-only toggle whose `title` reflects state (`"Disable memory"` when enabled) | Lines 486-495, "Memory Controls - Minimal" — outside every D3B diff hunk |
| 5 | shows RAG badge for messages with sources | `"📄 RAG"` | Icon + plain text node `"RAG"` (no emoji) | Line 714 — outside every D3B diff hunk |
| 6 | shows Memory badge for messages with memories used | `"🧠 Memory"` | Icon + plain text node `"Memory"` (no emoji) | Line 720 — outside every D3B diff hunk |
| 7 | shows streaming indicator when isStreaming | `"⏳ Generating…"` | Two separate nodes both containing `"Generating…"` (the echoed `streamedContent` + a fixed caption) — no emoji, and the original test's own `streamedContent="Generating…"` value collided with the fixed caption | Lines 767-786 — outside every D3B diff hunk |
| 8 | shows retry button when retryTarget is set | `getByText("🔄 Retry")` | Icon + plain text node `"Retry"` (no emoji) | Line 814 — outside every D3B diff hunk |
| 9 | calls onRetry when retry button is clicked | `getByText("🔄 Retry")` | same as #8 | same as #8 |

Every one of these strings sits in JSX regions the `git diff` for this
phase never touches — confirmed line-by-line, not inferred. All 9 are
**category 3**: genuine pre-existing test/product drift (an emoji-removal
and copy pass that happened earlier in this same uncommitted working tree,
unrelated to mode selection), demonstrated with the actual diff and actual
current markup rather than asserted.

**Fixes applied (test-only; zero product-code changes were needed for any
of the 9):**
- #1, #2, #3, #5, #6: exact-string assertions updated to the current,
  correct product copy (no regex, no `toBeTruthy()` loosening).
- #4: changed from a nonexistent visible-text query to
  `screen.getByTitle("Disable memory")` — the actual current accessible
  mechanism for surfacing memory status (an icon button's `title`), same
  behavioral intent (prove the "on" state is exposed), not a weaker check.
- #7: changed the injected `streamedContent` fixture value from
  `"Generating…"` (which collided with the fixed caption below it) to
  `"Hello there"`, then asserted on both nodes by exact text
  (`screen.getByText("Hello there")` and `screen.getByText("Generating…")`)
  — strictly more coverage than the original single assertion, not less.
- #8, #9: changed from a raw emoji-text query to
  `screen.getByRole("button", { name: "Retry" })` — a stable accessible-role
  query, per the instruction to prefer roles/names over brittle text when a
  new visible label could otherwise cause ambiguity (not the actual cause
  here, but the more robust query regardless).

None of `.skip`, `.todo`, `|| true`, broad hiding regexes, snapshot
replacement, status-range assertions, or removed accessibility checks were
used anywhere in these fixes.

**Fresh result:** `npx vitest run src/__tests__/ChatArea.test.tsx
--reporter=verbose` → **25 passed, 0 failed** (all 9 confirmed fixed by
name in the verbose output, not merely inferred from a headline count).

## Mode-selector component-integration proof (closeout correction addition)

The implementation pass's isolated `ModeSelector.test.tsx` and hook-level
`durableStreaming.test.ts` tests did not, by themselves, prove that a
selection made through the real `ChatArea`/`ModeSelector` UI inside the
real `App` component reaches the real `App.handleSend` →
`useStreaming.startStream` call boundary. New file
`frontend/src/__tests__/ModePropagation.test.tsx` (11 tests) closes that
gap, rendering a real `<ThemeProvider><App/></ThemeProvider>` tree (matching
`main.tsx`'s real production wrapping — `App.test.tsx`'s own bare
`render(<App/>)` has a separate, pre-existing, unrelated `ThemeProvider`
harness gap that crashes `Sidebar.tsx`'s `useTheme()` call on all 9 of its
session-dependent tests; this file avoids inheriting that gap rather than
fixing App.test.tsx's pre-existing harness, which is out of this closeout's
scope) with `useStreaming` mocked only in this new file (so the mock cannot
affect any other test file):

| Required proof | Test name |
|---|---|
| 1. selector renders in the composer area; 2. defaults to Auto | `1. ChatArea renders the selector in the composer area, 2. defaulting to Auto` |
| 3. Auto reaches startStream with no mode | `3. an Auto send reaches startStream with no mode argument` |
| 4-6. Chat/Deep Research/Multi-Agent send exact canonical values | `it.each` — `4-6. selecting {Chat,Deep Research,Multi-Agent} then sending reaches startStream with mode={chat,deep_research,multi_agent}` |
| 7. disabled mode cannot be selected | `7. a disabled mode cannot be selected — the next send stays Auto (no mode argument)` |
| 8. selecting does not submit | `8. selecting an option does not submit the message` |
| 9. frozen while streaming | `9. while streaming, the selector is disabled/frozen` |
| 10-13. existing send/Enter/attachment/cancel behavior intact | already proven by the now-green `ChatArea.test.tsx` (25/25, unmodified by ModeSelector's addition) — not duplicated here |
| 14. capability failure preserves Auto/Chat only | `14. a capability-fetch failure preserves Auto/Chat and never enables the advanced modes` |
| 15. capability invalidation resets to Auto with a notice | already proven at the component level in `ModeSelector.test.tsx` ("resets a now-unavailable explicit selection to Auto...") — not duplicated here |
| 16. no UI path sends `"auto"`/`null`/labels/internal values | `16. no UI path sends "auto", null, a display label, or an internal value` |
| 17. no duplicate startStream() calls | `17. the selector does not create duplicate startStream() calls for one send` |
| 18. mobile/composer layout intact | unchanged JSX/classes — the selector is its own toolbar row, not squeezed into the existing button row; no new test needed beyond the existing `ChatArea.test.tsx` render assertions |
| Reconnect reuses the exact snapshot; later UI changes can't mutate an in-flight send | already proven at the hook level in `durableStreaming.test.ts`'s "MP-3C3D3B — mode serialization" block: `"a durable reconnect reuses the SAME explicit mode as the initial send"`, `"a durable reconnect of an Auto send keeps omitting mode"`, `"a later explicit send after a mode change gets a new Idempotency-Key and its own mode"` — not duplicated here |

A real bug in the test harness (not the product) was found and fixed while
building this: `App.tsx`'s session-auto-select effect
(`sessions[0]` when nothing is selected) does **not** call `loadMessages` —
only the explicit `handleSelectSession` click handler does. So a test
needing `hasMessages` to be genuinely true (to reach the bottom-bar composer
while `isStreaming`) must explicitly click the session, not rely on
auto-select; `renderReady()`'s `seedMessages` option does this correctly.

Fresh result: `npx vitest run src/__tests__/ModePropagation.test.tsx
--reporter=verbose` → **11 passed, 0 failed**.

## Deferred work and remaining blockers

- Durable deep-research/multi-agent handlers (`advance_deep_research_runs`,
  `advance_multi_agent_runs`) — **not started**, per instructions.
- `App.test.tsx`'s own pre-existing, unrelated `ThemeProvider` harness gap
  (9 of its 11 tests crash in `Sidebar.tsx`'s `useTheme()` because that
  file's `render(<App/>)` never wraps `<App/>` in a `ThemeProvider`, unlike
  `main.tsx`) — confirmed pre-existing and out of this closeout's scope;
  `ModePropagation.test.tsx` correctly wraps its own renders instead of
  inheriting or fixing that gap.
- Per-user persisted mode preference — deliberately not built; component
  state (`App.tsx`'s `chatMode`) is sufficient per this phase's scope, and
  no existing per-session UI-preference mechanism was found to reuse
  without a migration.
- MP-3C3D4/D5 (durable deep-research/multi-agent worker sweeps) and MP-3C3D6
  (real-browser activation smoke) remain exactly as sequenced in MP-3C3D0's
  plan — unaffected by this phase.

## Confirmation

- No durable deep-research/multi-agent handler was implemented in this
  session — confirmed via `grep` (`advance_deep_research_runs` /
  `advance_multi_agent_runs`: zero matches in `backend/app/run_engine/`).
- `run_messages_stream_durable_enabled=False`, `run_engine_enabled=False`,
  confirmed via a fresh `app.config` import after all edits.
- Source control: branch `phase-1b1-approval-domain`, HEAD
  `b2e67183d4791cbbaed9d3d98e8833983510f3ff` (unchanged). No stage, commit,
  merge, push, reset, rebase, stash, clean, discard, or branch switch was
  performed. `stash@{0}` (`7ab31a5`) untouched throughout. The working tree
  remains fully dirty, plus this phase's additive edits.
