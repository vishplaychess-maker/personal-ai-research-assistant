# Phase 1B.2R — MP-3C3D0: durable mode-parity and activation plan

**Type:** evidence-first, read-only planning mini-phase. **No product code changed.**
**Date:** 2026-09-11 · **Branch:** `phase-1b1-approval-domain` · **HEAD:** `b2e6718` ·
**Flag:** `run_messages_stream_durable_enabled` stays **OFF** throughout this document.

All file:line citations below come from two parallel read-only investigations of the current
repo (legacy pipeline: `routes/messages.py`, `streaming_service.py`, `langgraph_workflow.py`,
RAG/memory/image/MCP handling, frontend mode controls; durable pipeline: `run_engine/*`,
approval gateway genericity, `RunPayload`/`Message` schema). Where evidence was incomplete, that
is stated explicitly rather than filled in with assumption.

---

## 1. Executive finding

Thunder AI currently runs **three** chat execution implementations, not two:

1. `backend/app/services/langgraph_workflow.py` — a real LangGraph `StateGraph` with deep-research
   and multi-agent (Researcher/Coder/Reviewer) nodes. **Reachable only via the non-streaming
   `POST /api/sessions/{id}/messages`**, which the frontend never calls (zero matches for
   `API.sendMessage` or any deep-research/multi-agent trigger in `frontend/src`). **This is dead
   code from the UI's perspective today.**
2. `backend/app/services/streaming_service.py` — the pipeline the UI actually drives via
   `POST /messages/stream`. Its own docstring admits it **"reuses the same services... but
   bypasses LangGraph"** (streaming_service.py:1-17) — i.e. it is a second, independent
   re-implementation of deep research and multi-agent, not a call into (1).
3. `backend/app/run_engine/chat_stream.py` — the durable path. Imports neither
   `streaming_service` nor `langgraph_workflow` (zero matches); it is a **third** independent
   implementation of provider streaming, sharing only the MCP marker grammar
   (`app.tools.mcp_tool.extract_mcp_calls`/`format_mcp_result`) with legacy.

Durable chat today is honest about its limits — `citations: []`, `sources_used: false`,
`memories_used: false` are correct because chat_stream.py does not call retrieval or memory
services at all (messages_stream.py:271-274). But this also means legacy's **post-stream marker
processing** — memory write-back and directive extraction that `routes/messages.py:404-441` runs
after every `complete` event, before saving — has **no durable equivalent found anywhere in
`run_engine/`**. If the flag were flipped on today, durable "complete" turns would silently skip
memory/directive side effects that every legacy turn performs. This is the sharpest data-integrity
finding in this report.

**The single highest-priority defect for self-host viability**, independent of any parity work:
`messages_stream.py:82-92` (`unsupported_mode_code`) rejects durable requests based on the
**global** `settings.enable_deep_research` / `settings.enable_multi_agent` flags, not on anything
in the individual request. A self-hosted instance that has either feature globally enabled would
have durable chat **422 on every message, including plain chat**, the moment the durable flag is
turned on — because `resolve_and_submit` (messages_stream.py:112-150) also hardcodes
`mode=None` (lines 133-134), so no per-request mode ever reaches `submission.py`'s
already-capable `mode` field (`_ALLOWED_MODES`, submission.py:45; HMAC-covered at
submission.py:171-190). The plumbing to fix this cleanly already exists on the submission side —
only the request path and the gating check need to use it. This bug currently has **zero user
impact** (flag is off), but it must be fixed **before** any plain-chat pilot, not after, because it
can silently defeat a "plain-chat-only" pilot for exactly the deployments most likely to have
those features already turned on.

## 2. Current user-visible mode inventory (proven, not assumed)

| Mode | User-selectable? | Live entrypoint | Evidence |
|---|---|---|---|
| Plain chat | Implicitly, default | `routes/messages.py:305 stream_chat` → `streaming_service.stream_chat_response` | routes/messages.py:305 |
| Document/RAG chat | Automatic (no toggle) — runs whenever retrieval finds chunks | same as plain chat | streaming_service.py:225-233 `build_citation_list` |
| Deep research | **No frontend selector.** Automatic: global `settings.enable_deep_research` flag | `streaming_service.py:326-338` → `app.tools.deep_research.run_deep_research` | zero frontend matches for "deep research"/`enable_deep_research` |
| Multi-agent | **No frontend selector.** Automatic: global `settings.enable_multi_agent` flag **+** `detect_complex_task()` heuristic | `streaming_service.py:707-721 stream_multi_agent_response` | same — zero frontend matches |
| Web search | No standalone entrypoint found; folded into deep-research's Tavily call | — | not investigated further; low priority (see Open Decisions) |
| MCP tools | Automatic, gated by `settings.enable_mcp_tool` | legacy: `streaming_service.py:804-825`; durable: `chat_tools.py` | both route through the **same** `app.approval.gateway` (mcp_tool.py:3-10, chat_tools.py:97-159) |
| Browser / terminal / Python / desktop (agent-invoked) | **No evidence found** of an LLM-invoked execution path for any of these inside either chat pipeline | — | only MCP is a proven agentic tool-execution channel in either pipeline (see Open Decisions #3) |
| Terminal Panel | Yes, but **user-driven**, not LLM-invoked (per project memory: SSE, inspection-only allowlist, `shell=False`) | separate feature, not a chat mode | out of scope for this parity effort |
| Image input/vision | Yes — attach an image; no explicit "mode" | legacy: `streaming_service.py:164-170` embeds base64 data URL directly in the provider message | `ChatRequest.image_url` documented as base64 data URL, not a fetchable remote URL |
| Image/video generation | Yes, but via a **separate panel**, not chat (per persisted project memory: "panel-only v1, no skill executor") | not part of either chat pipeline | out of scope |
| Voice input/output | **No evidence found** in either investigation | — | mark OPEN — not investigated, no memory record either |
| Memory | Automatic | `memory_service.retrieve_relevant_memories` / `extract_memory_from_message`, streaming_service.py:28-30, invoked in context prep + post-stream extraction (`routes/messages.py:470-479`) | not found in `chat_stream.py` — inferred absent from durable (see §5) |
| Knowledge Graph | Automatic via `[KG_QUERY]` chat marker (per persisted project memory) | processed in the same post-stream marker step as memory/directives, `routes/messages.py:404-441` | not found replicated in durable — same gap class as memory |
| Citations | Automatic whenever RAG retrieval fires | `streaming_service.py:225-233`, persisted to `Message.citations` (models.py:149) | durable hardcodes empty (messages_stream.py:271-274) |
| Planning/progress events | Automatic — `plan` event (`planning_service.generate_plan_for_query`, streaming_service.py:696) and `agent_status` (multi-agent) | — | frontend (`useStreaming.ts` dispatch) already has cases for `plan`/`agent_status`/`run_status` — **ready to receive them**, but durable backend never emits them today |

## 3. Legacy / durable parity matrix

Full 22-field record for the five modes with strong evidence. Modes with insufficient evidence
(voice, non-MCP tools, KG) are covered narratively in §2/§6/§12 rather than forced into this table.

### 3.1 Plain chat

| Field | Legacy | Durable |
|---|---|---|
| User entrypoint | Send box, any message | same UI, same request when flag is on |
| Frontend control | none (default) | none (default) |
| Backend entrypoint | `routes/messages.py:305 stream_chat` | `routes/messages.py:512 _stream_chat_durable` (flag-gated) |
| Function/service | `streaming_service.stream_chat_response` (own provider-streaming impl) | `chat_stream.py` (own, separate provider-streaming impl; `submission.py`, `chat_tools.py`) |
| Provider/model routing | via `streaming_service.py`, session model / provider config | `chat_stream.py:305 app.services.llm_providers.get_provider` (direct) |
| Fallback | not traced this pass — OPEN | not traced this pass — OPEN |
| Tool/side-effect (MCP) | inline via `mcp_tool.run_mcp_calls`, gated by approval gateway | via `chat_tools.py`, **same** `extract_mcp_calls`/`format_mcp_result` grammar, same gateway |
| Approval behavior | descriptive **text appended to the stream** (mcp_tool.py:~107); no interactive dialog | `tool_approval_required` SSE event → `ApprovalDialog` (MP-3C3C), full approve/deny/resume |
| Persistence | `save_assistant_message` after marker post-processing | atomic `create_chat_run` (Message+Run+RunStep+RunPayload+events in one commit, per architecture) |
| Cancellation | `request.is_disconnected()` polled between events (routes/messages.py:381); disconnect = abort, **not saved** | `POST /api/runs/{id}/cancel`, waits for persisted terminal event; browser disconnect does **not** cancel |
| Retry/recovery | none — process death loses the turn | RunWorker atomic claim/lease/retry-safe recovery, `outcome_unknown` on crash window |
| SSE events | `start`,`plan`,`token`,`agent_status`,`complete`,`error`,`cancelled` | `token`,`tool_approval_required`,`complete`,`error`/`cancelled`/`denied` (mapped in `stream_legacy_wire`) — **no `plan`/`agent_status`/`start` emitted** |
| Final Message fields | real `citations`, `sources_used`, `memories_used` | `citations: []`, `sources_used: false`, `memories_used: false` (hardcoded, messages_stream.py:271-274) |
| Citation/source fields | structured `Citation{marker,document_id,filename,page_number,chunk_id,snippet}` | none |
| Memory reads/writes | yes, both directions | **not found** — no evidence chat_stream.py calls `memory_service` |
| Image/file inputs | supported (base64 inline) | **hard-rejected pre-persistence**, 422 (`unsupported_mode_code(has_image=...)`) |
| Existing tests | `tests/test_streaming.py` | `tests/test_run_engine_mp3c3b.py` (39, contract-level) |
| Durable equivalent today | — | text streaming + MCP approval only |
| Missing durable capability | — | citations, memory, KG/directive markers, plan/agent_status events, image support, global-flag routing bug |
| Security risk | approval-gateway-mediated MCP = low | same low for MCP; new: hardcoded mode-check reads global settings not request intent (see §1) |
| Data-integrity risk | low, mature | **medium** — silent memory/KG/directive side-effect loss if enabled without fixing marker post-processing |
| Disposition | — | **NEEDS_SMALL_ADAPTER** (citations+memory) / **OPEN_DECISION** (marker post-processing scope) |

### 3.2 RAG / document citations

- Retrieval entrypoint: inside `streaming_service.py` context prep (exact call site to retrieval
  itself not traced this pass — only the citation-building call site, streaming_service.py:225-233,
  was confirmed; **flag as a one-line verification** for whoever implements MP-3C3D-citations).
- Source structure: `Citation{marker, document_id, filename, page_number, chunk_id, snippet}`
  (`schemas/documents.py:18-26`) — real, structured, not raw URLs.
- Persistence: `Message.citations` (`models.py:149`, `Text`, JSON-serialized) — **column already
  exists**, used today by the live path. No migration needed for durable to write the same column.
- Atomicity: durable's `create_chat_run` already writes Message+Run+RunStep+RunPayload+events in
  one commit (per architecture memory) — adding citations to that same write is not a new
  atomicity primitive, just a new field in an existing commit.
- Trustworthy today: **yes** on the legacy path.
- Caveat: one dead-code wrinkle — the unused non-streaming `POST /messages` endpoint repurposes
  `Message.citations` to store **approval-interrupt state as JSON** for its own separate
  terminal-tool-approval mechanism (`routes/messages.py:234-239`). Since that endpoint is not
  used by the UI, this doesn't collide with real citation data today, but it means
  `Message.citations` has two unrelated historical uses depending on entrypoint — worth a footnote
  in the eventual migration/cleanup, not a blocker.
- Disposition: **NEEDS_SMALL_ADAPTER** — reuse existing builder + existing column; the work is
  (a) call retrieval + `build_citation_list` from `chat_stream.py`'s context-prep step, and
  (b) replace the three hardcoded literals at `messages_stream.py:271-274` with real values from
  the same Message row, in the same atomic commit.

### 3.3 Deep research

- Real implementation: `app.tools.deep_research.run_deep_research` (Tavily-backed search+scrape).
  Called from two places today — `langgraph_workflow.py:420` (dead) and `streaming_service.py:333`
  (live) — same leaf tool function, different callers.
- Live path has **no incremental progress events** — it runs as one blocking call before `start`
  is even emitted. Durable therefore doesn't need to *invent* progress semantics to match legacy;
  it should still *add* them (the wire already has `run_status`/`agent_status` types the frontend
  understands) because durable's worker/event model is well-suited to them.
- Cancellation: legacy — none, it's an uninterruptible blocking call. Durable, as a RunWorker
  handler, would inherit crash-safe step boundaries "for free" — a real improvement, not just
  parity.
- Retry safety: read-only external search calls, should be safe to retry; not independently
  verified this pass.
- Intermediate-state sealing: a new `RunPayload.payload_type` value (e.g. `"research_state"`) —
  `RunPayload` is a generic encrypted blob (`models.py:1116-1160`), **zero migration** needed.
- Disposition: **NEEDS_DURABLE_HANDLER**. Concretely: a new `advance_deep_research_runs()` sweep
  in `worker.py`, following the exact precedent of `advance_chat_runs()`/`advance_mcp_runs()`
  (worker.py:1735-1744) — **not** the generic `register_handler()` registry, which has zero
  call sites anywhere in the codebase and is unused MP-1B scaffolding. New work should follow
  the pattern that's actually load-bearing, not the pattern that looks more "correct" but was
  never wired up.

### 3.4 Multi-agent

- **Two non-identical implementations exist.** `langgraph_workflow.py:496-800` is a real
  `StateGraph` with distinct Researcher/Coder/Reviewer personas and real graph edges — but it is
  dead code from the UI. The **live** implementation, `streaming_service.py:380
  stream_multi_agent_response`, is a simpler **non-checkpointed sequential** loop over persona
  prompts in one async generator — no LangGraph, no persisted checkpoints, shared state is a
  plain in-process object.
- This means durable multi-agent does **not** need to replicate LangGraph checkpointing — it needs
  to replicate the *simpler* live behavior. But this raises a genuine product question: should the
  durable implementation canonicalize the more sophisticated (currently dead) StateGraph version
  instead, since building it once durably is the natural moment to also fix the two-pipeline
  drift? → **Open Decision #1** below; repo evidence doesn't resolve this, it's a product call.
- Progress events: `agent_status` already exists end-to-end in the wire vocabulary and the
  frontend hook (`useStreaming.ts` dispatch switch) — durable just needs to emit it.
- Shared state: would need sealing as a `RunPayload` between agent turns (new `payload_type`,
  no migration) so a worker restart mid-multi-agent-run can resume from the last completed turn.
- Cancellation: per-agent-turn boundaries map naturally onto RunWorker step/RunStep boundaries —
  a genuine improvement over legacy's whole-generator disconnect check.
- Disposition: **NEEDS_DURABLE_HANDLER**, higher complexity than deep research (multiple
  sequential LLM calls + shared-state sealing + the open product decision above).

### 3.5 Images / vision

- `image_url` is a **base64 data URL**, never a server-fetched remote URL — confirmed no SSRF
  surface in the legacy path (streaming_service.py:164-170 embeds it directly in the provider
  message; nothing fetches it).
- Whether the base64 blob is itself persisted anywhere for history/RAG replay was **not confirmed**
  this pass — OPEN, needs one more targeted read before implementation.
- Durable today: hard-blocked pre-persistence via `unsupported_mode_code(has_image=...)` — this
  is **correct, safe, fail-closed behavior**, not a gap to rush-fix.
- Design question for later: base64 images can be multi-MB; storing them in the generic encrypted
  `RunPayload` blob for every reconnect/replay could bloat a run's event log. A dedicated,
  size-capped image-payload path is worth evaluating rather than reflexively reusing the generic
  mechanism. This is a real engineering-cost question, not a security gap (current 422 behavior
  is already safe).
- Bigger open question: given the locked "model policy: free/local only" decision, do the
  currently-configured free/local models even support vision? If not, durable image support may
  be moot for the self-host-first launch — → **Open Decision #2**.
- Disposition: **DEFER** — safe as-is (fails closed), not on the critical path for a plain-chat
  pilot, blocked on a product/model-roster question this report can't answer from code alone.

### 3.6 Non-MCP tools (browser / terminal / Python / desktop)

- No evidence of an LLM-invoked execution path for any of these in either chat pipeline was found
  by either investigation. The only proven agentic tool-execution channel in the whole repo is
  MCP, gated by the shared approval gateway.
- Approval-gateway readiness: `ActionEnvelope.tool_id` is a free-form string and `mcp_server_id`
  is nullable (`approval/envelope.py:66-69,129`) — the **schema** already anticipates non-MCP
  tools. But `gateway.dispatch(ctx, mcp_name, args, execute_mcp)`'s parameter names and the
  hardcoded `source_type="mcp"` literal (`gateway.py:388`) are MCP-specific and would need
  generalizing (e.g. an explicit `source_type` parameter) before a non-MCP tool could route
  through the same at-most-once/crash-safe dispatch without bypassing MP-2's guarantees.
- Disposition: **OPEN_DECISION** — is there truly no agentic non-MCP execution surface today, or
  did the scoped investigation miss one? → **Open Decision #3**. Recommend a targeted follow-up
  grep as the zero-risk first step of whichever future mini-phase first needs this, not a blocker
  for the plan below.

## 4. Convergence recommendation

**Do not maintain three permanent chat pipelines.** Concretely:

- `langgraph_workflow.py`'s `StateGraph` (deep research + multi-agent nodes, terminal-tool
  interrupt/resume) is reachable only through a frontend-dead endpoint. Recommend a dedicated,
  narrowly-scoped **audit-before-delete** pass (outside MP-3C3D0's evidence-only scope) to confirm
  nothing else calls `create_message`/`run_research_workflow`, then retire the file. Do not delete
  it inside this plan — that's a separate, small, low-risk future task, not a parity dependency.
- `streaming_service.py` remains the **legacy-only** pipeline until durable reaches functional
  parity + passes the activation gates in §8-9. It should not gain new features going forward;
  new capability work belongs in the durable handlers below, reusing the same **leaf tool
  functions** (`run_deep_research`, agent persona prompt builders, `build_citation_list`,
  `memory_service.*`) that `streaming_service.py` already calls — not its orchestration/generator
  code, which is fused to the legacy SSE wire format and not reusable as-is.
- The durable **RunWorker** (`worker.py`) is the correct eventual canonical spine: its
  atomic-claim/lease/retry-safe-recovery machinery already gives the crash-safety LangGraph
  checkpointing exists to provide, without needing LangGraph itself. New handlers should be added
  as new `advance_<mode>_runs()` sweeps (the proven `advance_chat_runs`/`advance_mcp_runs`
  pattern), **not** via the unused generic `register_handler()` registry (zero call sites,
  confirmed dead MP-1B scaffolding) and **not** by porting LangGraph into the worker loop.

## 5. Citation truthfulness audit

- **Durable's `citations: []` / `sources_used: false` / `memories_used: false` are truthful
  today** — chat_stream.py genuinely does no retrieval or memory work, so the empty values
  don't fabricate parity for a mode that actually used sources. This is correct, not a
  violation, as of right now.
- It **becomes** a truthfulness violation the moment retrieval/memory is added to chat_stream.py
  without simultaneously fixing `messages_stream.py:271-274` — this is why that line-fix must be
  an explicit, tested acceptance criterion of the first parity slice, not an incidental side
  effect.
- Exact source passages are already durably persisted today via the legacy path's `Citation`
  schema into the existing `Message.citations` column — no new persistence primitive is needed.
- Citation IDs surviving reconnect: citations only attach to the terminal `complete` event (not
  streamed incrementally). As long as the persisted `complete` `RunEvent`'s payload carries real
  citations (not hardcoded ones), the existing replay-from-`run_events` machinery reproduces them
  faithfully on reconnect with no new reconnect-specific citation logic required.
- Whether deep-research/multi-agent modes *also* populate citations today on the legacy path
  was **not confirmed** — the fork evidence only traced `build_citation_list`'s call site inside
  the main RAG context-prep path. Flag as OPEN for whoever scopes MP-3C3D-deep-research: verify
  whether Tavily search results become `Citation` records before assuming durable deep-research
  needs to invent that wiring versus reuse it.
- **Recommendation: yes, citations should be the first durable parity slice.** It has the
  strongest evidence (existing column, existing builder, existing atomic-commit point), the
  clearest acceptance test (compare a legacy vs. durable response to the same RAG-eligible
  prompt), and is the one gap most likely to cause a real, silent "the AI just made that up"
  regression if anyone assumes durable already has parity here.

## 6. Mode selection and routing UX

No explicit frontend selector for deep research or multi-agent exists today — confirmed by zero
matches across `frontend/src` for any deep-research/multi-agent trigger. Both are invisible
global-config + (for multi-agent) text-heuristic routing. The **operational problem**: a global
admin-configured flag cannot express "durable chat only supports plain chat right now" on a
per-request basis, which is exactly the bug in §1.

Recommended contract: an explicit `mode` field on the chat request (`"chat"` default,
extensible to `"deep_research"`/`"multi_agent"`), validated server-side before persistence.
This requires:
1. A small request-schema change (`ChatRequest`/durable submission payload gains an explicit
   `mode` field — durable's `submission.py` already has `_ALLOWED_MODES` and folds `mode` into
   the idempotency HMAC's canonical dict at submission.py:171-190, so **this side is already
   built**; only the caller needs to pass it through instead of hardcoding `mode=None`).
2. `unsupported_mode_code` needs to stop reading global `settings.enable_*` flags for durable's
   gating decision and instead gate on the **request's own** `mode` field, checked against what
   durable currently supports (today: only `"chat"`).
3. New frontend UI to actually let a user pick a mode — this doesn't exist for *either* pipeline
   today, so building it is net-new product surface, not a durable-specific gap.

Whether a global auto-heuristic mode should be retired in favor of always-explicit selection, or
whether "automatic" should remain the default UX with an optional override, is a genuine product
decision the repo can't settle — **Open Decision (folded into #1 below)**.

## 7. Safe temporary coexistence strategy

| Strategy | Safety | UX | Engineering complexity | Pipeline-drift risk | Rollback | Testing burden | Recommendation |
|---|---|---|---|---|---|---|---|
| A. Durable plain chat only; explicit unsupported-mode rejection | High — is what's already built (modulo the §1 global-flag bug) | Transparent: unsupported modes fail before persistence with a clear error | Low (mostly bug-fix, not new build) | Low | Instant (flip flag) | Small, focused | **Recommended now** |
| B. Explicit per-request mode chooses durable/legacy before persistence | High, once §6 lands | Best — user/admin control, no silent fallback | Medium (needs §6's routing contract) | Low, if the spine convergence in §4 is followed | Instant | Medium | **Recommended as the next stage after A** |
| C. Complete parity before enabling durable anywhere | Highest | Delays all benefit indefinitely | contradicts "work in small evidence-backed mini-phases" | N/A (nothing shipped) | N/A | Large, all-at-once | Reject — violates locked process decision |
| D. Replace legacy immediately | Lowest | Silent feature loss (citations, memory, KG/directive markers all currently missing) | Low effort, high risk | High — no time to fix drift before commitment | Hard once legacy is removed | N/A | Reject — real data-integrity gaps proven above |

**Recommendation:** Strategy A now (once §1's bug is fixed), evolving to Strategy B once the mode
routing contract (§6) lands. Never Strategy D before full parity; never require Strategy C's
"everything first" before any pilot.

## 8. Real-browser activation gate (smallest deterministic requirement)

To be executed in a **future** mini-phase (MP-3C3D-browser-smoke below), not now. Required:

- Backend: durable flag ON, plain-chat mode only, using whatever free/local provider is already
  configured per the locked "free/local only" model policy. **Whether a deterministic/fake
  provider seam already exists in the repo was not confirmed this pass — OPEN**, check before
  building one; do not build a mock server in this planning phase (per instruction).
- Auth/session: real login flow, real bearer + CSRF (already exercised by MP-3C3C's jsdom tests,
  but never end-to-end in a live browser).
- Flows to prove live, in order: plain token streaming renders correctly → forced reconnect
  (kill/restart the backend process or throttle network mid-stream) → tool_approval_required opens
  the dialog → Approve resumes correctly → Deny reaches a terminal state correctly → Cancel via
  the durable cancel button waits for the real persisted terminal event → repeat at a mobile
  viewport and a desktop viewport → zero unexpected browser console errors → **server-side DB
  check** that exactly one `Message` row exists for the turn (no duplicate from reconnect/replay).
- Tooling: use the already-connected `claude-in-chrome` MCP tools when this gate is actually
  executed — no new browser dependency needed. Do not install Playwright.

## 9. Activation and rollback plan (flag stays OFF throughout this document)

| Stage | Flags | Supported modes | Health/readiness | Required tests | Monitoring | Rollback trigger | Rollback action | Data compatibility | In-flight runs after rollback |
|---|---|---|---|---|---|---|---|---|---|
| Development-only | `run_messages_stream_durable_enabled=true` in dev/local env only | plain chat (post §1 fix) | `/api/health/worker` | focused durable suite (existing 39 + new parity tests) | manual | any test failure | flip flag off | additive — durable and legacy Message rows coexist in the same table without schema conflict | **expected** to continue safely per RunWorker's design (flag gates only the *submission* entrypoint, not already-created runs) — **not directly verified this pass; confirm with a targeted test before relying on it operationally** |
| Self-host pilot | opt-in setting, single instance, plain chat only | plain chat | same + citation/memory parity tests green | full focused suite | operator-watched logs | elevated error rate, citation mismatch reports, `outcome_unknown` spike | flip flag off | same | same caveat as above |
| Limited-user canary (hosted) | allowlisted users/orgs | plain chat (+ deep research/multi-agent once their mini-phases land) | health checks + basic metrics | full focused suite + real-browser smoke (§8) passed | active dashboards | error-rate/latency thresholds | flip flag off for cohort | same | same caveat |
| Wider enablement | default-on for new sessions | all parity-complete modes | full monitoring | full regression (deferred to this gate, not earlier) | full | any of the above | flip flag off globally | same | same caveat |
| Legacy retirement | flag removed, legacy code deleted | all | — | full regression one last time | — | N/A (no more flag) | revert the deletion commit | one-way | N/A |

## 10. Recommended mini-phase sequence (reordered from the suggested skeleton, with reasons)

The prompt's suggested skeleton (citations → deep research → multi-agent → mode routing → browser
smoke) is close, but repo evidence supports moving the **routing/global-flag fix earlier** than
building any new handler, because the global-flag bug (§1) can make a "plain-chat-only" pilot
non-functional for any deployment that already has deep research or multi-agent enabled globally
— which is plausible given these are default product features, not experimental toggles.

**MP-3C3D1 — fix per-request mode routing (was implicit in the original D4)**
- Scope: durable submission stops gating on global `enable_deep_research`/`enable_multi_agent`
  settings; gates on an explicit per-request `mode` field instead (default `"chat"`); wire
  `resolve_and_submit` to pass the real `mode` through instead of hardcoding `None`.
- Files: `backend/app/run_engine/messages_stream.py` (`unsupported_mode_code`,
  `resolve_and_submit`), `backend/app/run_engine/submission.py` (already supports `mode`, no
  change expected there), request schema for the stream endpoint.
- Reuse: `submission.py`'s existing `_ALLOWED_MODES`/HMAC-inclusion — this is why the fix is
  small.
- Schema/migration: none.
- Acceptance: a durable request with `mode` omitted/`"chat"` succeeds regardless of global
  `enable_deep_research`/`enable_multi_agent` settings; a request naming an unsupported mode
  still 422s pre-persistence with a clear reason.
- Focused tests: extend `tests/test_run_engine_mp3c3b.py`-style tests for the new gating logic
  only.
- Security risk: low — this narrows over-broad rejection, doesn't loosen any approval/auth
  boundary.
- Explicitly out of scope: building the frontend mode selector (that's D3 below); implementing
  deep research/multi-agent durably.
- Complexity: **small**.
- Blocked by: nothing.
- Stop condition: if the request-schema change turns out to require a wider breaking change to
  the existing `ChatRequest`/idempotency contract than expected, stop and re-scope rather than
  widen this phase.

**MP-3C3D2 — durable citation + memory + marker-postprocessing foundation (was D1)**
- Scope: wire retrieval + `build_citation_list` and `memory_service` calls into `chat_stream.py`'s
  context prep; replace the hardcoded `citations:[]/sources_used:false/memories_used:false` at
  `messages_stream.py:271-274` with real values from the same Message row, same atomic commit;
  investigate and (if in scope) replicate the memory/directive/KG marker post-processing that
  `routes/messages.py:404-441` performs today, or explicitly document it as deferred with a
  named risk if not.
- Files: `backend/app/run_engine/chat_stream.py`, `backend/app/run_engine/messages_stream.py`.
- Reuse: existing `Citation` schema, existing `Message.citations` column (no migration), existing
  `memory_service`/marker-processing functions (`app.tools.memory_tool`,
  `app.tools.directive_tool`).
- Schema/migration: **none** — this is exactly why it's the first slice.
- Acceptance: identical RAG-eligible prompt on legacy vs. durable (flag on, dev-only) produces
  the same citation set and the same memory-write side effects.
- Focused tests: new durable-citation test file; do not re-run the full backend suite.
- Security risk: low.
- Data-integrity risk if skipped: **the one this report is most worried about** — silent side
  effect loss.
- Explicitly out of scope: deep research, multi-agent, image input.
- Complexity: **small-medium**.
- Blocked by: MP-3C3D1 (so parity testing isn't confused by the global-flag bug).

**MP-3C3D3 — explicit mode-routing UX + frontend controls (was D4, the UX half)**
- Scope: the full request-mode contract from §6 — frontend selector (or explicit
  automatic-with-override), request schema finalization, idempotency HMAC confirmation with a
  real non-`"chat"` mode value end-to-end.
- Files: `frontend/src/App.tsx` (or a new mode-control component), `frontend/src/api.ts`,
  `frontend/src/types.ts`, `backend/app/run_engine/submission.py` (schema finalization only).
- Reuse: submission.py's existing mode/HMAC plumbing.
- Schema/migration: request-schema change only, no DB migration.
- Acceptance: a user (or admin default) can explicitly request deep-research/multi-agent mode
  and the durable path 422s cleanly if that handler isn't implemented yet (D4/D5 below), or
  routes correctly once it is.
- Focused tests: new frontend mode-control tests, following the existing `ApprovalDialog.test.tsx`
  pattern.
- Security risk: low — this is UI/routing surface, not a new execution channel.
- Explicitly out of scope: building the deep-research/multi-agent handlers themselves.
- Complexity: **medium** (real UX design work, first mode selector Thunder AI has ever had for
  either pipeline).
- Blocked by: MP-3C3D1.
- Open dependency: needs the product decision in §12 Open Decision #1 (automatic vs. explicit
  default) resolved first.

**MP-3C3D4 — durable deep-research handler**
- Scope: new `advance_deep_research_runs()` worker sweep, reusing `run_deep_research` as the leaf
  call; seal intermediate research state via a new `RunPayload.payload_type`; emit progress via
  existing `run_status`/`agent_status` wire types; attach citations from Tavily results if legacy
  does so today (verify per §5's open item) or add that wiring here if legacy doesn't either.
- Files: `backend/app/run_engine/worker.py` (new sweep), new small handler module under
  `run_engine/`, reusing `app.tools.deep_research`.
- Reuse: `run_deep_research` tool function; `RunPayload` (no migration); MP-3C3D2's citation
  plumbing.
- Schema/migration: none (new `payload_type` value only).
- Acceptance: a durable deep-research run completes with real citations, survives a forced
  worker restart mid-search without data loss (crash-safety test), and can be cancelled
  mid-search (an improvement over legacy, which cannot).
- Focused tests: new worker-sweep test file.
- Security risk: low — read-only external search, same trust boundary as legacy.
- Explicitly out of scope: multi-agent, mode-selector UI polish.
- Complexity: **medium**.
- Blocked by: MP-3C3D1, MP-3C3D2, MP-3C3D3 (needs a real mode field to trigger on).

**MP-3C3D5 — durable multi-agent handler**
- Scope: new `advance_multi_agent_runs()` sweep replicating the **live** (simpler, non-LangGraph)
  `stream_multi_agent_response` behavior, per Open Decision #1's resolution; seal shared state
  between persona turns as `RunPayload`; emit `agent_status` per turn; per-turn cancellation via
  RunStep boundaries.
- Files: `backend/app/run_engine/worker.py`, new handler module, reusing `agent_personas.py`
  prompt builders.
- Reuse: existing persona prompts; `RunPayload` (no migration).
- Schema/migration: none.
- Acceptance: durable multi-agent run produces the same persona sequence/output shape as legacy
  for an identical prompt; survives a forced restart mid-sequence.
- Focused tests: new worker-sweep test file.
- Security risk: low, same MCP/approval boundaries as plain chat if personas invoke tools.
- Explicitly out of scope: reviving the dead `langgraph_workflow.py` StateGraph version unless
  Open Decision #1 says otherwise.
- Complexity: **large** (multiple sequential LLM calls, shared-state sealing, most product
  ambiguity of any slice).
- Blocked by: MP-3C3D1, MP-3C3D3; benefits from patterns established in MP-3C3D4.

**MP-3C3D6 — deterministic real-browser activation smoke** (was D5)
- Scope: exactly the flows in §8, executed live via `claude-in-chrome`, backend in dev-only mode
  with the durable flag on, DB checked directly for zero duplicate Messages.
- Files: none (verification only) unless a fake/deterministic provider seam needs to be added —
  check first, per §8's open item.
- Acceptance: every flow in §8 passes with zero console errors and zero duplicate Messages.
- Security risk: none (test-only).
- Complexity: **small**, but has a hidden dependency: someone must first confirm whether a
  deterministic provider seam exists (§8 Open item).
- Blocked by: MP-3C3D2 at minimum (citations must be real before a "no fabricated response" smoke
  test is meaningful); ideally after D4/D5 too if those modes are meant to ship before wider
  enablement.

**Later (explicitly deferred, not sequenced yet):** images/vision (blocked on Open Decision #2 —
does the free/local model roster support vision at all), broader non-MCP tools (blocked on Open
Decision #3 and the gateway generalization noted in §3.6), `langgraph_workflow.py` retirement
(separate small audit-then-delete task, not a parity dependency).

## 11. Scope-control recommendation

**Required before the first durable plain-chat self-host pilot:** MP-3C3D1 (routing bug fix) and
MP-3C3D2 (citation/memory truthfulness) only. Nothing else in this document should block that
pilot.

**Required before replacing legacy globally:** MP-3C3D3 through MP-3C3D6, plus the
`langgraph_workflow.py` audit-then-delete, plus a resolution of Open Decisions #1-#3.

**Required before hosted multi-user deployment specifically (not self-host):** a fix for the
`notifier.py` single-process-only limitation (confirmed at notifier.py:1,11 — "no Redis/broker,
not in scope, not claimed") — horizontal scaling of durable event delivery is unaddressed and is
a hosted-only concern, irrelevant to a single self-host instance.

**Future competitive enhancement, not coupled to anything above:** image/video generation panel
(already architecturally separate — "panel-only v1, no skill executor" per project memory),
voice input/output (no evidence found either way), broader non-MCP tool execution (no evidence
such a channel exists today at all).

## 12. Risks

1. **Silent side-effect loss** if MP-3C3D2 ships citations but not the memory/directive/KG
   marker post-processing found in `routes/messages.py:404-441` with no durable equivalent —
   flagged as the report's top data-integrity concern (§1, §5).
2. **Global-flag gating bug** (§1) — could make a nominally-safe "plain chat only" pilot
   non-functional for real deployments; must be fixed first, not discovered in production.
3. **Two live implementations of deep research and two of multi-agent already exist** before any
   durable work begins (§1, §3.3, §3.4) — building a *third* implementation of either without
   first deciding which existing one is canonical (Open Decision #1) would make the drift worse,
   not better.
4. **Approval gateway's hardcoded `source_type="mcp"`** (`gateway.py:388`) is a soft constraint,
   not a hard blocker, on ever safely adding non-MCP tools — worth generalizing early if/when that
   work starts, to avoid a rushed bypass of MP-2's guarantees under future time pressure.
5. **`RunPayload` reuse for large image blobs** could bloat run event logs if the generic
   mechanism is used without a size cap — flagged in §3.5 as a design question to resolve before
   (not during) implementation.
6. **Rollback safety for in-flight runs is architecturally expected but not directly verified**
   in this evidence pass (§9) — recommend a explicit test before relying on it in any real
   rollback runbook.

## 13. Open decisions (ask the user)

**#1 — Which multi-agent implementation is canonical?** The dead `langgraph_workflow.py`
StateGraph version is more sophisticated (real graph edges, distinct personas) than the live
`streaming_service.py` version (simple sequential loop). Should the durable multi-agent handler
(MP-3C3D5) replicate the simpler live behavior (fastest, matches what users actually experience
today), or use durable's implementation as the opportunity to finally build the more sophisticated
version for real? This also determines whether `langgraph_workflow.py` should be resurrected in
spirit or fully retired.

**#2 — Does the current free/local model roster support vision at all?** Given the locked
"free/local only" model policy, durable image/vision support (§3.5) may be moot if no
currently-supported model accepts image input. This determines whether images/vision belongs in
the "later" bucket at all, or should be dropped from the roadmap entirely pending a different
model-policy decision.

**#3 — Is MCP genuinely the only agentic tool-execution channel today, or did the scoped
investigation miss a browser/terminal/Python/desktop execution path?** Both investigations were
scoped specifically to the chat streaming pipelines; a broader repo-wide grep wasn't performed.
If such a channel exists outside the chat pipeline, the approval-gateway generalization in §3.6
becomes more urgent than this report currently estimates.

## 14. Exact next implementation prompt outline

When MP-3C3D1 is authorized to proceed, the implementation prompt should specify:

- Scope: `messages_stream.py`'s `unsupported_mode_code`/`resolve_and_submit` only; no handler
  work, no frontend work.
- Exact acceptance test: a durable plain-chat request succeeds with `enable_deep_research=true`
  and/or `enable_multi_agent=true` set globally; an explicit unsupported `mode` value still 422s
  pre-persistence.
- Explicit non-goals restated: no citation/memory work (D2), no frontend selector (D3), no new
  handlers (D4/D5).
- Source-control posture: same as this session — no commit/merge/push, dirty tree stays dirty,
  pre-existing stash untouched, until the user says otherwise.
- Flag posture: `run_messages_stream_durable_enabled` stays OFF; testing happens with the flag
  forced on only inside a focused test's local scope, never as a changed default.

---

*This plan is read-only evidence + recommendation. No files outside `docs/superpowers/plans/` and
one PRD status line were touched to produce it. See the companion PRD entry for MP-3C3D0's
audit-complete status.*
