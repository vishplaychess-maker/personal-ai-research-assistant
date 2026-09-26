# Phase 1B.2R — MP-3C3D4C completion report

End-to-end durable Deep Research worker, cited synthesis, replayable
progress/SSE, and capability activation — behind an OFF-by-default feature
flag (`run_deep_research_enabled`, default `False`). Builds directly on
D4B's tested acquisition/persistence foundation; D4B's own modules are
reused unmodified except for one small, evidence-driven correction (see
"Corrections made during integration" below).

## Closeout correction pass (resumed session)

The session that produced the body of this report hit its limit
immediately after beginning the PRD update, before the PRD row was
finished and before its own claims below were re-verified against fresh
evidence. A separate resume-and-closeout pass re-audited the interrupted
state against the actual repository (never trusting this report's own
prior text) and found and fixed **three real defects**, plus proved one
previously source-inspection-only claim at the real HTTP-route level:

1. **Dead code removed, not a real gate**: `submission.py` defined
   `assert_deep_research_executable()`, and this report's original "Files
   changed" table (below) listed it as if it were a wired safeguard. It
   was never called from any route, worker, or test — confirmed by
   `grep -rn "assert_deep_research_executable" backend/app/ tests/` before
   removal (one hit: its own definition). The actual, ONLY authoritative
   gate for both `/api/runs` (`routes/runs.py:135`) and `/messages/stream`
   is `messages_stream.unsupported_mode_reason`, which was already
   correctly wired before this pass — the dead function was removed
   rather than given a second, duplicate call site.
2. **`research_synthesizing` was schema-defined and SSE-mapped, but never
   actually emitted** — this report's own "SSE / frontend behavior"
   section (below) claimed the closed `code` enum "mapped by
   `messages_stream._map_event`" without ever showing where the worker
   persisted one. It didn't: `_run_deep_research_synthesis` went straight
   from the pending→running `RunStep` transition into the provider call
   with no `research.progress` event allocated at all. Fixed by allocating
   the event atomically in the SAME commit as that transition (before the
   provider is invoked) — see "SSE / frontend behavior" below and
   `tests/test_deep_research_worker.py::test_progress_events_in_order_closed_set_only`,
   updated to assert the code is now present (an earlier revision of that
   test encoded the omission as expected behavior).
3. **Corrupt sealed local-citation state was silently swallowed, not
   failed closed**: `_run_deep_research_synthesis` caught `Exception`
   around `chat_stream.read_context_snapshot` and defaulted to
   `local_citations = []`, continuing to a normal success. This directly
   contradicts `read_context_snapshot`'s own docstring contract ("a caller
   must fail closed... never default to {}") and the plain-chat
   finalization code a few hundred lines above in the SAME file, which
   correctly fails the run closed (`context_snapshot_unreadable`) on the
   identical exception. Fixed to match the chat path exactly; covered by
   new test `test_corrupt_local_context_snapshot_fails_closed_no_message`.
4. **Real-route proof added for the direct Run API's rejection AND
   acceptance of `deep_research`**: `test_research_activation_boundary.py`
   proved rejection only by `inspect.getsource()` string-matching
   `routes/runs.py`, never a real request. `test_run_engine_mp3c3d3a.py`
   already had a genuine `TestClient` proof of rejection-when-not-ready
   (`test_direct_run_api_rejects_deep_research_exact_422_before_persistence`),
   but nothing anywhere proved acceptance-when-ready at the real-route
   level. Added
   `test_deep_research_readiness.py::test_direct_run_api_accepts_deep_research_when_fully_ready`,
   which configures every prerequisite true and asserts a real `201` with
   `Run.kind == "deep_research"` persisted.

One item flagged as a likely defect was investigated and found to be
**correct as designed, not a bug** — see "Citation filtering policy" below
for the evidence.

All test counts elsewhere in this report below the line are from this
pass's own fresh command runs, not reused from the interrupted session's
arithmetic (which was internally inconsistent: its own sub-totals summed
to 268, not the 345 it claimed).

## Skills used

- `superpowers:verification-before-completion` — every claim below is
  backed by a command run in this session. This is what caught the
  "acquisition and synthesis must run in the same worker call" bug during
  integration testing (a naive claim of "done" after seeing the acquisition
  step seal successfully would have missed that the run never progressed
  to synthesis on any subsequent sweep).
- Ponytail (full mode) — kept the implementation to the smallest truthful
  design: two RunStep kinds (not three — D4A's `research_search` step was
  folded away since D4B's actual persistence primitive only ever supports
  one sealed acquisition step), reused every existing chat-driver primitive
  possible (`MarkerScanner`, `TextEventBuffer`, `_ChatHeartbeat`,
  `chat_stream.run_stream`/`default_chat_provider`, `_precheck`,
  `_live_cancel_id`, `_consume_cancel`, `_finalize_terminal`,
  `_apply_transition`) rather than re-deriving parallel machinery, and
  reused the exact D4A/D4B web-citation-marker design (`[W#]`) instead of
  inventing a new scheme.
- No unrelated skill invoked. No parallel forks (the whole task is one
  tightly coupled integration; forking would have fragmented the context
  needed to keep the design consistent across ~10 touched files).

## Stage 0 evidence

```
git status --short        → dirty tree unchanged except this session's new/modified files
git branch --show-current → phase-1b1-approval-domain
git rev-parse HEAD        → b2e67183d4791cbbaed9d3d98e8833983510f3ff
git diff --check          → clean
git stash list            → stash@{0}: 7ab31a5 (untouched)
```

Confirmed before editing: D4B completion report + architecture §42 exist;
D4B's five modules (`research_sources.py`, `research_search.py`,
`research_fetch.py`, `research_acquisition.py`, `research_snapshot.py`) all
compile; no `advance_deep_research_runs()` existed; both public durable
routes rejected `deep_research`; capability marked it unavailable under
durable cutover; no synthesis/provider driver existed; no multi-agent
handler exists; all flags OFF.

## File-anchored implementation map (read before editing)

- `routes/runs.py:92` (`assert_chat_stream_executable()` unconditional gate)
  and `messages_stream.assert_durable_stream_ready` (both require
  `run_chat_stream_enabled`) — confirmed this is a CORRECT shared
  prerequisite for deep research too, since synthesis reuses the identical
  generation bridge. **Zero changes needed to either gate.**
- `routes/runs.py:135` / `routes/messages.py:575` both already call
  `messages_stream.unsupported_mode_reason(mode)` generically for ANY
  mode — **zero route changes needed** beyond making that one function
  mode-aware for `"deep_research"`.
- `submission.create_chat_run` (line ~286) had `kind="chat_stream"`
  hardcoded — the ONE necessary submission-path change: derive `Run.kind`
  from `submission.resolved_mode`.
- `chat_stream.build_chat_input` already splices the D2 context envelope
  generically for any mode — reused unmodified; a NEW function
  (`deep_research_synthesis.build_deep_research_input`) wraps it, adding
  the web-source envelope on top.
- `RunPayload` has no DB uniqueness constraint; `RunStep` DOES
  (`uq_run_steps_run_seq`) — confirmed in D4B, reused as the at-most-one
  authority for the acquisition step (unchanged from D4B's own design).
- `_ChatHeartbeat`, `MarkerScanner`, `TextEventBuffer`, `_precheck`,
  `_live_cancel_id`, `_consume_cancel`, `_finalize_terminal`,
  `_apply_transition`, `_alloc_event` — all `RunWorker` instance
  methods/helpers already proven for chat — reused unchanged for deep
  research's own driver methods, added to the SAME class.
- `events.CONTROL_EVENT_SCHEMAS` — closed per-event schema registry;
  needed one new entry (`research.progress`) since none of the existing
  five event types fit a progress signal.
- `worker.poll_once()` is the ONE integration point the supervisor calls
  (`worker_supervisor.py:259` → `self._worker.poll_once()`) — adding
  `advance_deep_research_runs()` there required **zero supervisor changes**.

## Corrections made during integration

1. **Acquisition and synthesis must complete in ONE worker-call, not two
   separate sweeps.** The initial design returned early after sealing the
   snapshot, expecting a LATER `advance_deep_research_runs()` sweep to pick
   up synthesis. That sweep's own query only selects runs with a `NULL` or
   EXPIRED lease — but the run's lease was just freshly minted (30s TTL) by
   the SAME sweep that sealed the snapshot, so it would never be
   re-selected until the lease actually expired 30 seconds later. Fixed:
   `_run_deep_research_acquisition` now calls `_run_deep_research_synthesis`
   directly, in the same call, immediately after a successful seal —
   mirroring `advance_chat_runs`'s own single-call-drives-the-whole-
   generation pattern exactly (there is no "waiting on a human" reason to
   split acquisition and synthesis across two claims, unlike the MCP
   approval sub-state-machine, which genuinely does wait on a human).
2. **`research_sources_ready` was being emitted twice, and the D4B
   orchestrator's own emission was ORDER-WRONG.** The orchestrator
   (`research_acquisition.py`) originally emitted `research_sources_ready`
   itself, before returning the snapshot to the caller — but the
   orchestrator has no DB access and cannot know whether the caller's
   subsequent seal will actually succeed (a concurrent-seal race, a DB
   error). Per the explicit requirement ("emit `research_sources_ready`
   only after authoritative snapshot persistence commits"), this event
   must be the CALLER's exclusive responsibility. Fixed: removed the
   orchestrator's own emission entirely (`research_acquisition.py`); the
   worker's own emission (added alongside the seal, in the same commit)
   is now the ONLY source of this event. D4B's own
   `test_research_acquisition.py` test was updated to match (the
   orchestrator's progress sequence is now `["research_searching",
   "research_fetching"]` only).

Both were caught by fresh test runs during this session (not asserted from
memory), consistent with the verification discipline this report follows.

## Feature flag and readiness gate

`config.run_deep_research_enabled: bool = False` (new; never set `True` in
any default/deployment config). ONE authoritative predicate,
`deep_research_readiness.deep_research_executable()`, reused unchanged by
every mode-aware surface:

```python
def deep_research_executable() -> bool:
    if not (settings.run_engine_enabled and settings.run_worker_enabled
            and settings.run_chat_stream_enabled and settings.run_public_api_enabled
            and settings.run_deep_research_enabled):
        return False
    try:
        from app.run_engine.submission import _hmac_key
        _hmac_key()
    except Exception:
        return False
    return True
```

`run_chat_stream_enabled` is a genuine, correct prerequisite (not
accidental coupling) — deep-research synthesis reuses the IDENTICAL
generation bridge (`chat_stream.run_stream`, `default_chat_provider`,
`MarkerScanner`) as durable chat.

Reused by (no second/duplicated copy anywhere):
- `messages_stream.unsupported_mode_reason("deep_research")` — delegates
  to `deep_research_unavailable_reason()`.
- `/api/runs` (`routes/runs.py:135`, unchanged call site — already generic).
- `/messages/stream` (`routes/messages.py:575`, unchanged call site).
- `routes/settings._compute_chat_mode_capabilities()` — when durable
  cutover is on, `deep_research`'s `available` field is now
  `deep_research_executable()` (was hardcoded `False`).
- `RunWorker.advance_deep_research_runs()` — the sweep itself is a no-op
  unless the predicate is true.

## Files changed

| File | Change |
|---|---|
| `backend/app/config.py` | + `run_deep_research_enabled: bool = False`. |
| `backend/app/run_engine/engine.py` | + `"deep_research"` to `_RUN_KINDS` (additive). |
| `backend/app/run_engine/events.py` | + `"deep_research"` to its OWN separate `_RUN_KINDS` (additive); + new `"research.progress"` control-event schema (closed `code` enum, no count field — `_FieldRule` only supports enums today). |
| `backend/app/run_engine/submission.py` | `create_chat_run` derives `Run.kind` from `submission.resolved_mode` (was hardcoded `"chat_stream"`). **Closeout correction**: an `assert_deep_research_executable()` added during the interrupted session was never called from any route and has been removed — see "Closeout correction pass" above; the actual authoritative gate is `messages_stream.unsupported_mode_reason`, already correctly wired. |
| `backend/app/run_engine/deep_research_readiness.py` | New — the one authoritative predicate. |
| `backend/app/run_engine/deep_research_citations.py` | New — `[W#]` web-citation marker namespace, server-authoritative extraction/validation, collision-free with local `[1]`/`[2]` markers. |
| `backend/app/run_engine/deep_research_synthesis.py` | New — builds the synthesis `ChatInput` from D2 context (reusing `chat_stream.build_chat_input` unmodified) + the sealed web-source snapshot. |
| `backend/app/run_engine/research_acquisition.py` | D4B module, corrected: removed its own (order-wrong, duplicated) `research_sources_ready` emission. |
| `backend/app/run_engine/messages_stream.py` | `unsupported_mode_reason` special-cases `"deep_research"` via the readiness predicate; `resolve_and_submit` gained an optional `mode` param (was hardcoded to "chat"); `_map_event` gained a `research.progress` → SSE `research_progress` mapping. |
| `backend/app/run_engine/worker.py` | + `_DEEP_RESEARCH_RUN_KIND` import/exclusion in `claim_next_queued`/`recover_expired`; + `advance_deep_research_runs`/`_advance_deep_research_run`/`_run_deep_research_acquisition`/`_run_deep_research_synthesis`/`_finalize_deep_research_success`; + injectable `research_search_adapter`/`research_fetch_adapter` constructor params (mirrors the existing `chat_provider` injection pattern); wired into `poll_once()`. |
| `backend/app/routes/messages.py` | Threads `mode=resolved_mode` into the existing `resolve_and_submit` call (1-line addition). |
| `backend/app/routes/settings.py` | Capability computation consults `deep_research_executable()` when durable is on. |
| `backend/app/schemas/documents.py` | `Citation.document_id`/`chunk_id` → `Optional`; + `url: Optional[str] = None` (additive, backward compatible). |
| `frontend/src/useStreaming.ts` | + `onResearchProgress?` callback + `case "research_progress"` dispatch (additive; an older build already safely ignores the unrecognized event name by default). |
| `tests/test_deep_research_readiness.py` | New — 12 tests. |
| `tests/test_deep_research_worker.py` | New — 19 tests. |
| `tests/test_research_acquisition.py` | D4B test corrected for the removed orchestrator-level progress emission. |
| `tests/test_research_activation_boundary.py` | 2 now-obsolete D4B-era "nothing exists yet" assertions updated to the correct D4C-era "exists, self-gates" boundary. |
| `tests/test_run_engine_mp3c3d3a.py` | 1-line test-only fix: a fake `resolve_and_submit` stub updated to accept the new `mode` kwarg. |
| `tests/test_run_engine_mp3c3d1.py` | 1 test updated: `"deep_research"`'s bounded reason is now `deep_research_not_executable`, not the generic `mode_unsupported_` prefix. |
| `tests/test_run_engine_mp3c3b.py` | Same correction as above, in this file's own copy of the assertion. |
| `frontend/src/__tests__/durableStreaming.test.ts` | + 2 new tests for the `research_progress` SSE dispatch. |

No migration. No new runtime dependency. `ChatArea.tsx`/`ModeSelector.tsx`/
`App.tsx` unchanged — the existing D3B mode selector already sends
`mode:"deep_research"` and already reads capability from `GET
/api/settings`; nothing about its OWN logic needed to change for D4C.

## Run/step state machine

**Run kind**: `deep_research` (server-derived from `resolved_mode`, never
client-supplied). **RunStep kinds** — the smallest truthful set D4B's
actual persistence interfaces support (two, not three — D4A's
`research_search` step was folded into `research_fetch_extract`, since
D4B only ever built ONE seal primitive for the acquisition stage — search
results are held in memory only, never separately persisted):

1. `load_context` (seq 1, created at submission — identical to chat's own
   first step; seals the exec snapshot + D2 context).
2. `research_fetch_extract` (seq 2, created and sealed atomically by
   `research_snapshot.seal_research_snapshot` — ALWAYS inserted already
   `succeeded`, by construction; no pending/running intermediate state is
   possible for this step).
3. `deep_research_synthesis` (seq 3, created `pending` → `running`
   [attempt-1 durable proof, committed BEFORE any provider call] →
   `succeeded`/`failed`/`incomplete`).

**Retry classification**: acquisition (steps 1→2) is retry-safe pre-seal
(idempotent reads) — a crash before a `research_fetch_extract` row exists
is always safely retryable from scratch, proven by the step's own
all-or-nothing seal semantics. Synthesis (step 3) is attempt-once — a
crash after the `running` transition commits is `outcome_unknown`, never a
second provider call.

**Lease/version ownership**: identical CAS mechanism to chat
(`lease_owner`/`version` on `Run`), reused unchanged. **Bounded error
codes**: `research_no_sources`, `citation_validation_failed`,
`research_citations_missing` (new); every other failure mode
(`provider_error`, `provider_timeout`, `empty_generation`,
`chat_input_error`, `context_snapshot_unreadable`, `session_revoked`,
`tool_resume_required`, `output_persist_failed`) is REUSED from
`CHAT_STATUS_REASONS` — synthesis shares the identical generation bridge
and can hit the identical failure modes; no duplicated reason-code set.

## Submission / idempotency

Reuses `submission.build_chat_submission`/`create_chat_run` completely
unmodified except for the one-line `kind` derivation. Same
`RUN_IDEMPOTENCY_HMAC_KEY`-keyed semantic identity (`chat-run/v2`) already
covers `mode` as a behavior-changing field — a changed prompt/model/
provider/mode/options under the same Idempotency-Key deterministically
conflicts (`IdempotencyKeyConflict`), unchanged. Replay returns the
existing run with zero new Message/Run/Step/Payload/Event — proven by
`test_replay_with_same_key_creates_zero_duplicates`. Submission performs
zero search/fetch/provider calls — proven by
`test_submission_performs_zero_external_calls` (monkeypatches both real
adapter classes to raise `AssertionError` if ever called during
submission).

## Acquisition / replay / crash policy

`RunWorker._run_deep_research_acquisition` constructs real
`AcquisitionSeams` (cancel/session/deadline/heartbeat/progress) from the
SAME worker primitives `_run_chat_generation` already uses
(`_ChatHeartbeat`, `_live_cancel_id`, `validate_session_binding`), then
calls D4B's `run_research_acquisition` unmodified. Zero sources → bounded
`research_no_sources`, provider never called (proven:
`test_zero_sources_terminates_without_calling_provider`). A partial page
failure with at least one valid remaining source still proceeds (proven:
`test_partial_page_failure_with_one_valid_source_still_proceeds`). After a
successful seal, a completely independent restart reclaims via the
expired-lease path and performs ZERO new search/fetch calls, resuming
directly at synthesis — proven with search/fetch adapters that raise
`AssertionError` if invoked at all
(`test_restart_after_sealed_snapshot_performs_zero_new_network_calls`,
which simulates a genuine crash window by directly sealing a snapshot and
force-expiring the run's lease, then handing a second worker instance
adapters that would fail the test if ever touched).

## Synthesis / provider policy

`deep_research_synthesis.build_deep_research_input` builds the provider
input from EXCLUSIVELY sealed state: `chat_stream.build_chat_input`
(unmodified) supplies history + D2 local context envelope; the web-source
envelope (`deep_research_citations.render_web_source_block`) is spliced in
as ONE MORE untrusted user-role message immediately before the final
question — explicitly instructing the model that the content is untrusted
reference data, never instructions, and that it must cite only via the
supplied `[W#]` markers, never invent a URL or marker, and never emit
tool/memory/directive markers. Single configured provider/model from the
sealed execution snapshot, attempt-once, no fallback chain — identical
policy to chat (`default_chat_provider`, reused unmodified). Any marker
tripping `MarkerScanner` during synthesis (D4A did not approve an MCP
continuation inside deep research) fails closed exactly like an
unsupported marker in plain chat — never auto-executed.

## Citation provenance and validation

`[W1]`, `[W2]`, ... assigned deterministically, once, in the sealed
snapshot's own immutable order — stable across every restart/reconnect,
never renumbered. Server-authoritative validation
(`deep_research_citations.py`): a `[W#]` marker in the final text that
does not exist in the sealed map → `has_hallucinated_web_marker` →
`citation_validation_failed`, fail closed (proven:
`test_hallucinated_marker_fails_closed`). Zero valid web citations in an
otherwise-successful generation → `research_citations_missing`, fail
closed rather than silently claiming a fully-researched answer (proven:
`test_no_citation_marker_fails_research_citations_missing`). Citations are
extracted in first-reference order, deduplicated (proven:
`test_duplicate_marker_dedupes_in_final_citations`), and persisted with
the SAME dict shape as a local/document citation (`document_id`/
`chunk_id` present but `None`) so `Message.citations` stays one
homogeneous list with no special-casing needed downstream. Local/document
citations (`[1]`/`[2]`) are read from the SAME sealed D2 context snapshot
every chat run already reads, via `chat_stream.validate_citations`,
completely unmodified — the two namespaces cannot collide by
construction (`[W...]` vs plain digits).

### Citation filtering policy: two DELIBERATELY different rules, not one bug

The closeout pass specifically re-audited whether local citations should
be filtered to "only markers the model actually referenced in
`final_text`," the same rule web citations already follow, and found the
current behavior (persist every sealed local citation regardless of
whether its `[N]` marker appears in the model's answer) is **correct as
designed**, not a bug:

- **Web `[W#]` citations** are filtered to exactly what `final_text`
  references (`extract_cited_web_markers`/`build_web_citations`) because
  their membership is decided by the MODEL, at generation time, from a
  set of live, untrusted, just-fetched web passages it may or may not
  end up drawing on.
- **Local `[N]` citations** follow the pre-existing, documented D2 chat
  policy (`chat_stream._validate_one_citation`'s docstring: "the single
  source of the passage/citation one-to-one guarantee"): membership is
  decided at CONSTRUCTION time — a passage is a citation if and only if
  it was actually included in the sealed `rag_context_text` the model
  was given, never re-decided afterward by whether the model's own
  output happens to reference its marker. This is the exact same rule
  plain chat already applies to every one of its own answers today; deep
  research reuses it unchanged rather than inventing a stricter or looser
  one for itself alone.

Both rules independently satisfy "never attach an unrelated/model-invented
source" — they simply decide relatedness at two different, already-approved
points (construction-time for local, reference-time for web) for two
different trust levels of source (pre-vetted uploaded documents vs. live
web search this run just performed).

## Cancellation / session / lease behavior

Checked before AND after every search call and every individual page
fetch (D4B's own seam contract, reused); before AND during synthesis
(mirrors `_run_chat_generation`'s own `_guard()` pattern exactly). Cancel
before acquisition → zero search/fetch/provider calls
(`test_cancel_before_search_yields_zero_search_and_fetch_calls`, D4B
orchestrator-level; the worker wiring is exercised end-to-end by the
happy-path tests reaching the SAME seam calls). Cancel during synthesis
closes the provider iterator via the SAME `StreamAborted`/`_guard`
mechanism chat already uses — no new mechanism. A stale/expired lease is
detected via the SAME `_precheck`/version-CAS check chat uses; a live
worker cannot be reclaimed by a second worker (proven at the D4B
persistence layer: `test_second_seal_for_same_run_and_seq_is_rejected_by_the_real_unique_constraint`,
a real SQL `UniqueConstraint`, not a Python check).

## SSE / frontend behavior

New `"research.progress"` control-event schema (closed `code` enum:
`research_searching`/`research_fetching`/`research_sources_ready`/
`research_synthesizing` — no `research_planning`, D4-DECISION-1) mapped by
`messages_stream._map_event` into an additive SSE event
`event: research_progress\ndata: {"code": "..."}`. Persisted in the same
`run_seq` sequence as every other event — reconnect/replay is gap/
duplicate-free by the SAME existing mechanism (`read_run_events`,
`after_seq` cursor) with zero new replay logic. `useStreaming.ts` gained
one new dispatch case + an optional `onResearchProgress` callback — an
older/unaware frontend build already safely ignores the unrecognized
event name via its existing `default:` case (proven:
`"an unrecognized event name is safely ignored"` test). Disconnect
behavior, cancel behavior, and citation rendering are completely
unchanged — deep research's completion event carries citations through
the EXACT SAME `complete` event / `Message.citations` field every other
mode already uses.

**Closeout correction**: `research_synthesizing` is now actually allocated
(see "Closeout correction pass" above) — it previously existed only in
this schema/mapping, never in a persisted `RunEvent`, so a reconnecting
client had no signal that synthesis had begun.

### Provisional streaming before citation validation — stated honestly

Synthesis tokens are published live (`TextEventBuffer.publish`,
persisted as `category="text"` `RunEvent`s) WHILE the provider streams,
and citation validation
(`has_hallucinated_web_marker`/`build_web_citations`/the
zero-web-citations check) only runs AFTER the full stream completes. So
yes: text can reach a connected client via SSE (and via reconnect replay)
BEFORE it is known whether that text will ultimately pass citation
validation. This was checked against the approved D4A plan rather than
decided ad hoc — the plan's own synthesis-stage table (D4A plan, "Whatever
LLM provider..." row and its finalization row) states this stage's
citations are "atomically finalized with the `Message` (identical
mechanism to MP-3C3D2's chat citation finalization)" and that a crash
between streamed text and that commit leaves the run non-terminal, "never
a false success, exactly like plain chat today." In other words: this is
not a new risk D4C introduced — it is the SAME provisional-streaming
design plain chat has always used, reused unchanged, and explicitly
pre-approved for deep research by D4A. The failure mode this could enable
(a client sees fluent, citation-marker-shaped prose, then the run fails
citation validation) is bounded and already honest at the transport
level: `messages_stream._map_event`/the terminal-status mapping emits an
SSE `complete` event ONLY when `Run.status == "succeeded"`; any other
terminal status (including both `citation_validation_failed` and
`research_citations_missing`) maps to a bounded `error` event instead, and
no `Message` row is ever created on a failure path — so a citation
validation failure can never be presented to the client as a completed,
citation-backed answer, even though some of its text may have been
visible in-flight. No redesign was made here: buffering all synthesis
text until after validation would be a real UX change to the existing,
approved, chat-identical streaming contract, unsupported by any evidence
that D4A/D4C's approved design requires it.

## Exact focused test commands and counts

**Closeout correction**: the interrupted session's counts below this line
(345 / 124 / 71) were never independently re-verified in THIS pass and
are internally inconsistent on their own face (the "345" claim's own
cited sub-totals, 19+12+132+105, sum to 268) — per this pass's explicit
instructions they are replaced with fresh command output from this
session only, scoped to what this pass's corrections could actually
affect (readiness/routing, the worker, and D4B's directly-reused
acquisition/snapshot modules). The broader worker-integration regression
suite and the frontend suite were NOT re-run this session — neither
`chat`/`mcp`/generic-worker code nor any frontend file was touched by
this pass's corrections, so re-running them would be outside the
"smallest meaningful focused set" this pass was scoped to; their prior
counts are explicitly NOT re-asserted here and should be treated as
unverified until a pass that actually touches that code re-confirms them.

```
PYTHONPATH=backend .venv/Scripts/python.exe -m pytest \
  tests/test_deep_research_readiness.py \
  tests/test_deep_research_worker.py \
  tests/test_research_activation_boundary.py \
  tests/test_run_engine_mp3c3d3a.py \
  tests/test_research_snapshot_persistence.py \
  tests/test_research_acquisition.py \
  tests/test_settings_mode_capability.py \
  -q
```
→ **115 passed, 0 failed, 0 skipped** (13 readiness [12 pre-existing + 1
new real-route positive-path test] + 18 worker [17 pre-existing, one
corrected for the `research_synthesizing` fix, + 1 new fail-closed
context-snapshot test] + 7 D4B activation-boundary + 46 D3A mode-routing
+ 11 D4B snapshot-persistence + 11 D4B acquisition + 9 settings-capability
= 115).

`py_compile` on every file touched this session
(`backend/app/run_engine/submission.py`, `backend/app/run_engine/worker.py`,
`tests/test_deep_research_readiness.py`, `tests/test_deep_research_worker.py`):
clean. `git diff --check`: clean. `import app.main`: OK (fresh, this
session).

Frontend: **not re-run this session** — no frontend file was touched by
this correction pass (per the focused-verification instructions, `tsc
--noEmit`/vitest are only run when frontend TypeScript actually changed).
The prior session's "4 files, 71 passed" claim is carried forward
UNVERIFIED, not re-confirmed.

## Residual risks and deferred verification

- **Live network/PostgreSQL**: not exercised (fakes only, per instructions;
  D4B's own report already documents the DNS-rebinding/TOCTOU residual gap
  and the at-most-one-authority proof resting on a real, portable SQL
  `UniqueConstraint` — unchanged, not re-verified against a live
  PostgreSQL server in this session either).
- **Hosted/multi-user egress control**: D4-DECISION-9 explicitly requires
  hosted activation to wait for a future external egress-control gate —
  nothing in this phase claims that gate exists; the flag stays OFF by
  default and this report makes no production-readiness claim.
- **`research.progress`'s `code` field has no bounded count** — a
  deliberate D4B-era decision (the schema validator only supports closed
  enums, not integer ranges) carried forward unchanged; not revisited in
  D4C.
- **Frontend friendly-label rendering** — `onResearchProgress` is wired
  and tested at the hook level; no UI component was changed to actually
  *display* a friendly label for each code (out of the smallest-safe-change
  scope for this pass — the callback exists and is available for a future,
  separate, purely additive UI touch).
- **KG/memory/directive writes**: not implemented, as required — confirmed
  by direct source inspection
  (`test_no_llm_mcp_kg_memory_directive_execution_paths_in_deep_research_methods`).

## Confirmation

- No paid search/model fallback: the worker's injectable adapters default
  to the SAME free `DuckDuckGoSearchAdapter`/`GuardedHttpFetchAdapter` D4B
  already proved free/local-only; synthesis reuses `default_chat_provider`
  unchanged (single configured provider, no fallback chain).
- No knowledge-graph, memory, or directive write exists anywhere in the
  deep-research code path (confirmed by source inspection, not assumption).
- No multi-agent durable implementation was started.
- `run_deep_research_enabled=False`, `run_messages_stream_durable_enabled=False`,
  `run_engine_enabled=False` — confirmed via a fresh `app.config` import
  after all edits.
- Source control: branch `phase-1b1-approval-domain`, HEAD
  `b2e67183d4791cbbaed9d3d98e8833983510f3ff` (unchanged). No stage,
  commit, merge, push, reset, rebase, stash, clean, discard, or branch
  switch was performed. `stash@{0}` (`7ab31a5`) untouched throughout.
- All of the above re-confirmed fresh by the resume-and-closeout pass
  (branch/HEAD/merge-base-with-main/stash list re-run, flags re-checked
  via a fresh `app.config` import) — not reused from this report's own
  prior text.
