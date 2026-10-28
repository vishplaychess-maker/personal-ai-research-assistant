# Phase 1B.2R MP-3C3D5A — durable Multi-Agent mode: evidence-first audit and implementation plan

**DOCUMENTATION-ONLY.** No durable multi-agent code was added. No capability
was activated. No feature flag was flipped.

## 1. Executive finding

The live, frontend-reachable multi-agent implementation is NOT the 19-node
LangGraph `StateGraph` in `langgraph_workflow.py` (which is real, compiled,
and does execute terminal commands/tool calls — but is only reachable via
`POST /api/sessions/{id}/messages`, a non-streaming route the frontend never
calls). The actual live path is a much simpler, sequential, three-persona
team — Researcher → Coder → Reviewer — implemented in
`streaming_service.py::stream_multi_agent_response` (lines 416–690), invoked
from the real streaming endpoint whenever `context.mode == "multi_agent"` or
(legacy heuristic) `enable_multi_agent` + `detect_complex_task()` fires. This
matches and now re-confirms, with fresh file-anchored evidence, the
MP-3C3D0 finding and the PRD's own note ("reuses the *live* simpler
implementation, not the dead StateGraph one").

That live path today automatically executes Python code extracted from the
Coder's raw text (`[PYTHON_CODE: ...]`, no approval), automatically persists
`[SAVE_MEMORY]`/`[SAVE_DIRECTIVE]` markers found in the Reviewer's raw text
(no approval), and routes every one of its ~3–7 LLM calls through a 4-deep
implicit provider fallback chain (`build_fallback_chain`) with no per-run
call ceiling and no durable invocation-start proof. None of this is
reusable as-is for a durable, approval-gated, crash-safe implementation —
all three are identified defects to design AROUND, not port.

The smallest safe durable v1 recommended below is a **fixed, 3-role,
single-pass, text/evidence-only** team (no revision loop, no tool
execution, no live web search inside the team) that reuses D2's context
snapshot, D4C's marker-fail-closed and readiness-predicate patterns, and
the existing RunEngine/worker/SSE/citation machinery essentially unchanged.

## 2. Stage-0 evidence

```
git status --short --branch → ## phase-1b1-approval-domain (dirty tree, as expected)
git rev-parse HEAD          → b2e67183d4791cbbaed9d3d98e8833983510f3ff (= b2e6718)
git merge-base HEAD main    → b2e67183d4791cbbaed9d3d98e8833983510f3ff (branch == main)
git stash list               → stash@{0}: 7ab31a5 (untouched)
git diff --check             → clean
```

1. `docs/superpowers/reports/phase-1b2r-mp3c3d4c-completion.md` exists, 553 lines. ✓
2. Architecture §43 exists (`docs/superpowers/plans/2026-09-08-phase-1b2r-durable-run-resume-architecture.md`, one `## 43.` heading). ✓
3. `THUNDER_AI_PRD.md:118` records MP-3C3D4C as **implemented**. ✓
4. `grep -rn "advance_multi_agent_runs\|multi_agent_executable" backend/app/run_engine backend/app/routes` → no matches. No durable multi-agent worker sweep/handler exists. ✓
5. `backend/app/routes/settings.py:120-121` — durable path hardcodes `ChatModeCapability(mode="multi_agent", available=False, ...)` unconditionally. Unavailable. ✓
6. `backend/app/config.py:138,205` — `run_engine_enabled: bool = False`, `run_messages_stream_durable_enabled: bool = False`. ✓
7. `find docs/superpowers -iname "*d5*"` / `find backend tests frontend -iname "*multi_agent*"` (excluding the legacy `enable_multi_agent`/`AGENT_PERSONAS` files already inspected below) → no D5 implementation files. ✓

No conflict with expectations. Proceeding.

## 3. File-anchored legacy multi-agent trace

**Entry/routing** (`backend/app/services/streaming_service.py:737-765`):
`stream_chat_response`'s router — `context.mode == "multi_agent"` forces
the team (bypassing the heuristic; `routes/messages.py` already rejected
this mode pre-persistence if `enable_multi_agent` is globally off, so
reaching here means it's enabled); `context.mode is None` (omitted) falls
back to the original heuristic (`settings.enable_multi_agent AND
detect_complex_task(user_input)`); `"chat"`/`"deep_research"` never route
here.

**Team execution** (`streaming_service.py:416-690`,
`stream_multi_agent_response`):

1. **CAG short-circuit** (460-489): a semantic-cache hit on the raw user
   question skips the ENTIRE team and streams the cached final answer —
   only when there's no RAG context (`not context.sources_used`).
2. **Researcher** (511-525): one `_llm()` call
   (`generate_response(messages=history, system_prompt=RESEARCHER_SYSTEM_PROMPT + "\n\n" + context.system_prompt)`).
   `context.system_prompt` is the SAME session-level context block
   (RAG/memory/web results) every other mode already builds — the
   Researcher does not itself call any search/fetch tool; it only reads
   what was already retrieved before the team started. Output: a free-form
   markdown "Research Brief" (`brief`).
3. **Coder** (527-577): one `_llm()` call with `CODER_SYSTEM_PROMPT` +
   `brief` + the skills index. If the reply contains a `[PYTHON_CODE:
   ...]` marker (`app/tools/python_sandbox.py:42,45`), it is extracted and
   **executed automatically**, no approval, in a subprocess with a 10s
   wall-clock / 8s CPU / 256MB limit (`python_sandbox.py:32-38,66-`). The
   result text is appended to the next prompt as `code_result`.
4. **Review loop** (579-664): bounded by `MAX_REVIEW_RETRIES = 2`
   (`agent_personas.py:27`) — up to 3 Reviewer calls. Each Reviewer call
   also runs `_persist_reviewer_markers` (542-568), which calls
   `process_memory_markers`/`process_use_memory_markers`/
   `process_directive_markers` directly on the Reviewer's raw text —
   **`[SAVE_MEMORY]`/`[SAVE_DIRECTIVE]` markers are executed with no
   approval gate**, on every review round including the one that becomes
   the final answer. `parse_review_verdict` (`agent_personas.py:279-301`)
   extracts one of `[REVIEW_APPROVED]` / `[REVIEW_REJECTED: ...]` /
   `[NEEDS_RESEARCH: ...]`. Rejected → Coder revises (bounded); needs
   research → Researcher re-runs ONCE (`research_used < 1`) then Coder
   revises again. On the FINAL round the Reviewer is instructed to force-
   approve (`review_round_block`, `agent_personas.py:304-320`) so the loop
   cannot deadlock.
5. **Final answer** (666-683): `final_text = verdict["cleaned"] or
   code_out` — the Reviewer's own cleaned text (verdict markers stripped)
   is what the user sees; `code_out` is a fallback only if `cleaned` is
   empty. Cached (unless code executed, so a replay never re-runs code).
   Streamed as 120-char `token` chunks, then one `complete` event carrying
   `context.citations`/`sources_used`/`memories_used` — the SAME
   session-level values computed once before the team started, not
   re-derived per role.
6. **Errors**: any exception anywhere in the team → one generic
   `AGENT_ERROR` SSE `error` event (685-690) — no partial state persisted
   by this function itself (persistence happens in the calling route,
   outside this generator, same as the single-agent legacy path).

**Personas** (`backend/app/services/agent_personas.py`, full file read):
4 personas registered (`researcher`, `coder`, `reviewer`,
`browser_operator` — the 4th, browser_operator, is defined but **never
invoked** by `stream_multi_agent_response`, dead in this path).
`detect_complex_task` (224-234): regex heuristic, code/build-phrasing only,
min 24 chars. `detect_research_task` (255-265): a SEPARATE heuristic, not
used by the multi-agent router at all (kept apart so research-keyword
prompts don't steal multi-agent priority) — confirms the team's own
Researcher role never triggers D4B/D4C's live web search machinery itself.

**Model routing** (`backend/app/services/llm_providers/__init__.py:142-209`):
`generate_response` → `generate_response_with_fallback` →
`build_fallback_chain`: up to 4 provider attempts per logical call
(primary → GLM 5.3 Flash free local router → a globally-configured free
cloud provider (OpenRouter/Groq) → Ollama `dolphin-mistral` last resort).
Every persona call in the team goes through this independently — worst
case (1 research + 1 code + 3 review + 1 re-research + 1 re-code = 7
logical calls) × up to 4 fallback attempts = up to 28 actual provider
calls for one multi-agent run, with **no durable invocation-start proof
and no hard ceiling**.

**Dead StateGraph** (`backend/app/services/langgraph_workflow.py`):
`build_workflow()` (1628-1741) wires 19 nodes including `agent_research`/
`agent_code`/`agent_review` (a SEPARATE, richer multi-agent
implementation than the streaming one), `ask_terminal_approval`/
`execute_terminal`, self-evaluation, `save_output`, `extract_memory`.
Compiled once at import (`_workflow_app = build_workflow()`, line 1745)
and invoked only inside `run_research_workflow` (`.invoke(...)` at
1910/1918), which is called ONLY from
`routes/messages.py:158,194,214` (`POST /api/sessions/{id}/messages`,
non-streaming) and `scheduler_service.py`. `frontend/src/api.ts:215`
defines `sendMessage()` against that exact route — grep across every
`.tsx`/`.ts` file for `sendMessage(` finds **zero call sites** outside its
own definition. **This entire richer graph, including its terminal
approval flow, is unreachable from the live UI today.**

**Frontend SSE plumbing** (`frontend/src/useStreaming.ts:57,392-393`):
`onAgentStatus?: (data: {agent, message}) => void` is dispatched on the
`agent_status` SSE event — but `grep "onAgentStatus" frontend/src/ChatArea.tsx`
finds no matches. **The callback is wired at the hook level but never
consumed anywhere in the UI** — no agent-status indicator is actually
rendered today, legacy or otherwise.

**Mode selector** (`frontend/src/ModeSelector.tsx:36-38,64-134`): already
reads `available`/`reason` per mode from the capability response and
disables/hides accordingly, with an auto-fallback-to-"Auto" safety net if
a previously-available mode goes unavailable mid-session. **Zero frontend
changes are needed for multi-agent to correctly stay gated, or to
correctly "light up," once the capability response reflects real
readiness** — this is already generically wired from D3B.

## 4. Verified capabilities versus prompt/marketing-only claims

| Claim | Verified? | Evidence |
|---|---|---|
| "Multi-agent Researcher/Coder/Reviewer team" | **Real, live** | `streaming_service.py:416-690`, reachable via mode selector or heuristic |
| "Sophisticated LangGraph agent workflow with terminal/browser approval, self-evaluation" | **Real code, but dead** | `langgraph_workflow.py` StateGraph only reachable via `sendMessage()`, which has zero frontend call sites |
| "Reviewer enforces quality gate" | **Partially real** | Real for a single pass; the bounded revise loop is real, but forces approval at the retry limit regardless of actual quality |
| "Coder executes code automatically" | **Real, no approval gate** | `[PYTHON_CODE:]` → `python_sandbox.py`, unconditional |
| "Reviewer applies learned directives" | **Real, no approval gate** | `_persist_reviewer_markers` executes `SAVE_MEMORY`/`SAVE_DIRECTIVE` unconditionally |
| "Browser Operator agent" | **Defined, never invoked** | `AGENT_PERSONAS["browser_operator"]` unused by `stream_multi_agent_response` |
| "Agent status shown to the user" | **Not rendered** | `onAgentStatus` wired in the hook, zero consumers in `ChatArea.tsx` |
| Any test proves real (non-mocked) team behavior | **No such test exists** | `grep -rln "stream_multi_agent_response\|AGENT_PERSONAS" tests/` → only `test_run_engine_mp3c3d3a.py`, which monkeypatches `detect_complex_task` purely to test MODE-ROUTING gating, never exercises the team's actual sequencing/sandbox/marker behavior |

## 5. Durable role-graph recommendation

**Recommended v1 (smallest safe/useful): fixed, server-owned, single-pass,
sequential Researcher → Coder → Reviewer, depth 1, no loop-back, no
parallelism, no dynamic role selection.**

This deliberately cuts the legacy bounded revision loop (Reviewer→Coder
revise, Reviewer→Researcher re-research) for the FIRST durable cut: a
fixed 3-step graph has one crash-window shape to reason about per step,
matching D4C's own "smallest truthful design" precedent (§43.3: "not
D4A's originally-proposed three steps — the smallest set the ACTUAL
persistence primitive truthfully supports"). The Reviewer still adds real
value in v1 (it still reviews and its cleaned output is still what the
user sees) — it simply cannot force a second Coder pass. This is a
genuine capability reduction versus legacy, stated honestly, with an
explicit upgrade path (§20).

**On "Coder" fitness for ordinary chat**: the team is code/build-shaped by
construction (`CODER_SYSTEM_PROMPT`, `REVIEWER_SYSTEM_PROMPT`'s security/
correctness checklist) — reasonable when routed by `detect_complex_task`,
questionable when the user explicitly picks "Multi-Agent" mode for a
non-coding request (e.g., "give me multiple perspectives on X"). No
repository evidence justifies a dynamic task-type router today (no
existing bounded contract, no tests, no prior design decision on record).
**This is flagged as an open product question (§23), not silently
resolved** — v1 reuses the existing 3 personas unchanged (Ponytail rung 1:
reuse an existing primitive) rather than inventing a router.

### Rejected alternatives

1. **Planner → Researcher → Synthesizer/Reviewer**: no such Planner
   persona exists today; would require a new prompt/persona with zero
   reuse and no evidence it improves on the existing 3-role team for a
   first cut.
2. **Bounded parallel specialists + one reviewer**: the legacy team is
   sequential by design (each role's prompt embeds the PRIOR role's
   output as context — Coder needs the brief, Reviewer needs the code).
   Genuine parallelism would require restructuring what each role reads,
   with no repository precedent for parallel RunStep execution anywhere
   in the RunEngine (`RunStep.seq` is a strict linear sequence
   throughout `worker.py`). Rejected for v1.
3. **Dynamic planner-selected roles**: no bounded contract exists
   anywhere in the repo for this; violates the task's explicit "no
   dynamic agent creation" strong default and D4C's proven "closed set,
   never model-controlled" pattern for anything server-authoritative.
   Rejected outright, not just for v1.
4. **Full legacy bounded revision loop (kept as-is)**: doubles the number
   of distinct RunStep kinds/crash windows for the first cut (a variable
   step count depending on Reviewer verdicts) for a capability
   (auto-revision) not proven necessary for a useful v1. Deferred to a
   later phase, not rejected outright (§20).

## 6. Exact Run/RunStep/RunPayload design

- `Run.kind = "multi_agent"` (fits `String(24)`, `models.py:931`).
- RunStep sequence (4 steps, folding finalization into the Reviewer step —
  same "smallest set the persistence primitive truthfully needs" reasoning
  D4C applied to acquisition+seal):

  | seq | kind | classification |
  |---|---|---|
  | 1 | `load_context` | deterministic/replayable (identical submission step every mode already has) |
  | 2 | `agent_research` | attempt-once provider invocation |
  | 3 | `agent_code` | attempt-once provider invocation (v1: text output only, sandbox execution deferred — §10) |
  | 4 | `agent_review` | attempt-once provider invocation, trailing finalization-only sub-transaction (citations + Message + terminal commit, mirrors `_finalize_deep_research_success`) |

  All kind strings fit `RunStep.kind`'s `String(32)` column (`models.py:1006`)
  with room to spare.
- Sealed `RunPayload.payload_type = "agent_artifact_v1"` (fits `String(32)`,
  `models.py:1152`) for the Researcher's and Coder's outputs, sealed
  immediately after each step's provider call succeeds — the SAME
  at-most-one authority as D4B's `UniqueConstraint("run_id","seq")` on
  `RunStep` (no new persistence primitive).
- New `agent.progress` control-event schema (closed enum, mirrors
  `research.progress`): `agent_researching` → `agent_coding` →
  `agent_reviewing` → `agent_finalizing` → terminal.
- New bounded status-reason codes: `agent_artifact_unreadable` (corrupt/
  missing sealed artifact on resume — mirrors `context_snapshot_unreadable`),
  `agent_output_empty`, `agent_marker_execution_blocked` (any tool/action
  marker in ANY role's output — mirrors deep research's marker-trip
  `tool_resume_required` handling). Every other failure code
  (`provider_error`, `provider_timeout`, `chat_input_error`,
  `context_snapshot_unreadable`, `session_revoked`, `output_persist_failed`)
  is REUSED from `CHAT_STATUS_REASONS`, unchanged — no duplicated set.
- Terminal behavior: exactly one assistant `Message` (the Reviewer's
  cleaned text), citations + `RunStep` succeeded + `Run` succeeded commit
  atomically in one transaction, identical mechanism to
  `_finalize_chat_success`/`_finalize_deep_research_success`.

## 7. Inter-agent sealed artifact schema (`agent_artifact_v1`)

```
{
  "artifact_version": 1,
  "role": "researcher" | "coder",
  "summary": str,                    # ≤ 2000 chars
  "claims": [str, ...],              # ≤ 10 items, ≤ 500 chars each
  "evidence_markers": [str, ...],    # local [N] markers actually referenced, ≤ 10
  "assumptions": [str, ...],         # ≤ 10 items, ≤ 500 chars each
  "uncertainties": [str, ...],       # ≤ 10 items, ≤ 500 chars each
  "recommended_next_action": str,    # ≤ 500 chars
  "raw_text": str                    # ≤ 8000 chars, MARKER-NEUTRALIZED
}
```

- **Marker neutralization**: raw_text is passed through the SAME
  neutralization approach D4B built for retrieved web passages
  (`research_sources.py`'s marker neutralizer, casing-preserving) before
  being sealed — a role's own free-form text is untrusted input to the
  NEXT role's prompt, exactly like a retrieved web passage is untrusted
  input to the synthesis prompt. This is the prompt-injection boundary:
  neutralized raw_text is wrapped in the SAME untrusted-envelope pattern
  `chat_stream._CONTEXT_ENVELOPE_HEADER`/`_FOOTER` already establishes,
  never concatenated as trusted instructions.
- **Validation**: every field size-capped and type-checked before sealing
  (mirrors `_validate_one_citation`'s "drop the whole item if malformed"
  discipline); an oversized/malformed artifact fails the step closed
  (`agent_output_empty` or `chat_input_error`-equivalent), never silently
  truncated-and-continued without a bound.
- **Deterministic serialization**: `json.dumps(..., sort_keys=True)`,
  matching `Message.citations`' own convention.
- **Trust boundaries, explicit**: untrusted model output = `raw_text`
  (and every list field, since the model wrote them) → authoritative
  server metadata = `role`, `artifact_version`, `Run.id`, `RunStep.seq`
  (never client- or model-supplied) → sealed evidence = the D2 context
  snapshot's own `citations`/`rag_context_text` (read-only, never
  regenerated per role) → final user-visible text = ONLY the Reviewer
  step's cleaned output, never any intermediate artifact's `raw_text`
  directly.

## 8. Provider/model and free/local-only policy

**Recommendation: ONE sealed provider/model snapshot for the entire run**
(not a per-role choice) — reuses the EXACT mechanism chat/deep-research
already use (`chat_stream.default_chat_provider`, resolved from the
sealed execution snapshot at submission time, `chat_stream.py:558-559`:
"Single provider, no fallback chain"). No per-role model selection is
introduced — no evidence any role benefits from a different model, and a
per-role snapshot would triple the sealed-state surface for no proven
gain.

Requirements enforced by construction:
- No `build_fallback_chain` reuse anywhere in the durable path — a single
  `provider.generate_response`/`generate_stream_async` call per step,
  exactly like `_run_deep_research_synthesis`'s `run_stream` call.
- Hard maximum: **3 provider calls per run** (v1's fixed 3 steps — no
  loop, so this is also the exact count, not just a ceiling).
- Provider credentials resolved live via the existing
  `get_provider(config=...)` seam at call time, never sealed/logged.
- Durable invocation-start proof before each call: each step's RunStep
  `pending`→`running` transition committed BEFORE the provider is
  invoked (same pattern as D4C's synthesis step and the `research_synthesizing`
  fix from the D4C closeout pass).
- A provider failure at any step performs exactly one attempt at that
  step and fails the run closed (`provider_error`/`provider_timeout`) —
  no automatic retry, no fallback substitution. (A future budget-aware
  phase may explicitly authorize `build_fallback_chain` reuse; not here.)
- Never infer "free" from a model name — the sealed snapshot names
  whatever provider/model the session/deployment is already configured
  with (same non-inference discipline D4C's report already established).

## 9. Tool and approval policy

**v1 is text/evidence-only. No tool execution of any kind.** Marker
sweep across every role's raw output (reusing
`chat_stream.MarkerScanner`/`ALL_WATCHED_MARKERS` unchanged, same
mechanism D4C's synthesis loop already uses):

| Marker | Legacy behavior | Durable v1 behavior |
|---|---|---|
| `[PYTHON_CODE: ...]` | Executed automatically, no approval (`python_sandbox.py`) | **Fails closed** — `agent_marker_execution_blocked`, never executed |
| `[MCP_CALL ...]` | N/A in this path (chat-only marker) | Fails closed, same |
| `[PROPOSED_COMMAND: ...]` | N/A in this path | Fails closed, same |
| `[BROWSER_ACTION: ...]` | Persona exists, unused in this path | Fails closed, same |
| `[SAVE_MEMORY: ...]` / `[SAVE_DIRECTIVE: ...]` | Executed automatically on Reviewer output, no approval | **Fails closed** — no KG/memory/directive write in this phase (explicit requirement), matches D4C's own "no KG/memory/directive write anywhere in the deep-research path" precedent |
| Any other `ALL_WATCHED_MARKERS` member | Varies | Fails closed, uniformly |

No marker is auto-executed under any circumstance in v1. If a future
phase wants tool-enabled Coder/Researcher roles, it **must** reuse the
existing centralized approval gateway (`app/approval/gateway.py`,
`registry.py` — trusted-tool registry, `identity.py` —
`execution_turn_id`/tool_call_id binding, `service.py`) and MP-2's
at-most-once dispatch (`run_engine/mcp_flow.py`), exactly as MP-3C2 did
for durable chat. **No second approval system is designed here.**
Recommendation: **defer tool-enabled multi-agent mode entirely** — v1's
value (structured, reviewed, citation-backed multi-perspective text) does
not require it, and the legacy path's two automatic-execution defects are
exactly what a durable version must not repeat.

## 10. Citation/evidence policy

Multi-agent v1 performs **no live web search or tool call inside the
team** — every role reads the SAME already-sealed D2 context snapshot
every other mode reads (confirmed: the legacy Researcher only reads
`context.system_prompt`, never calls a search/fetch adapter itself).
Therefore v1 needs **no new citation machinery at all**:

- Local `[N]` document citations: reused unchanged via
  `chat_stream.validate_citations`, same construction-time-membership
  policy as chat and deep-research's local citations (not filtered by
  whether the Reviewer's final text references the marker — the SAME
  already-approved D2 policy, not a new rule).
- No `[W#]` web-citation namespace is needed in v1 (no live research
  happens inside the team) — D4C's `deep_research_citations.py` is
  **not** reused here, correctly, since there is nothing for it to
  validate.
- No agent may invent a citation: citations come exclusively from the
  sealed D2 snapshot, never from any role's `raw_text` or artifact
  fields — the Reviewer's final Message.citations is exactly
  `chat_stream.validate_citations(ctx_snap["citations"])`, identical to
  plain chat.
- If a future phase lets the Researcher role invoke D4B's live search
  tools, THAT phase must reuse D4C's exact `[W#]` marker/validation
  machinery (`extract_cited_web_markers`/`build_web_citations`/
  `has_hallucinated_web_marker`) rather than duplicate it — explicitly
  deferred, not designed here (§20, §23).

## 11. Cancellation, lease, crash, restart, retry matrix

| Window | `load_context` | `agent_research` | `agent_code` | `agent_review`+finalize |
|---|---|---|---|---|
| Crash before invocation | Retry-safe (deterministic re-read) | Resume from sealed submission step | Resume from sealed research artifact | Resume from sealed code artifact |
| Crash during invocation | n/a | `outcome_unknown` next boot (running, uncommitted) | `outcome_unknown` | `outcome_unknown` |
| Crash after output, before seal | n/a | `outcome_unknown` (matches deep-research's synthesis window exactly) | `outcome_unknown` | `outcome_unknown` |
| Crash after seal / after Message commit | Step already `succeeded`, safely re-read | Sealed artifact reused unchanged, no re-invocation | Sealed artifact reused unchanged | Run terminal `succeeded`, never re-run |
| Lease loss | `RunLeaseLost` → abandoned, no side effect | Same | Same | Same |
| Worker restart | Resumes from last sealed step | Same | Same | Same |
| Cancellation before invocation | Zero calls made | Zero calls made | Zero calls made | Zero calls made |
| Cancellation during invocation | n/a | Aborts via `StreamAborted`/`_guard`, no Message | Same | Same, no Message |
| Session-family revocation | Fails closed `session_revoked` | Same | Same | Same |

All three provider steps are **attempt-once** — no repository evidence
that any LLM completion is safely idempotent-replayable (matches chat's
`llm_generate` and deep-research's synthesis convention exactly). A
crash after a step's `pending`→`running` commit but before its output is
sealed is `outcome_unknown`, never a second provider call — identical
mechanism, not a new one.

## 12. Progress/SSE/frontend contract

New closed-enum `agent.progress` codes, in order: `agent_researching` →
`agent_coding` → `agent_reviewing` → `agent_finalizing` → terminal.
Mapped by `messages_stream._map_event` into an additive `agent_progress`
SSE event, exactly mirroring `research.progress`'s wiring. No raw
prompts/role output/secrets/URLs/document text/exception text in any
payload — codes only. `useStreaming.ts` gains one dispatch case + an
`onAgentProgress` callback (mirrors `onResearchProgress`); no existing
"AgentStatus UI" component exists to reuse (confirmed dead/unconsumed
callback, §3) — a future, separate, purely additive UI touch would
render it, out of scope for D5B/D5C's own minimal wiring (mirrors D4C's
own deferred "friendly-label rendering"). `ModeSelector.tsx` needs **zero
changes** — it already gates `multi_agent` on `capability.available` and
will light up automatically once the readiness predicate is real
(§13, `routes/settings.py:120-121`).

## 13. One authoritative readiness predicate

`multi_agent_readiness.multi_agent_executable()` — exact structural mirror
of `deep_research_readiness.deep_research_executable()` (Ponytail rung 2:
reuse an existing pattern with a small adapter):

**Truth table** — available iff ALL of:

| Flag/dependency | Required value |
|---|---|
| `run_engine_enabled` | `True` |
| `run_worker_enabled` | `True` |
| `run_chat_stream_enabled` | `True` |
| `run_public_api_enabled` | `True` |
| `run_multi_agent_enabled` (new) | `True` |
| `RUN_IDEMPOTENCY_HMAC_KEY` | configured |

Reused unchanged by: `messages_stream.unsupported_mode_reason("multi_agent")`,
direct `/api/runs` (same call path already proven for `deep_research`),
`routes/settings._compute_chat_mode_capabilities` (replaces the
hardcoded `available=False` at `settings.py:120-121` with this
predicate, same pattern as `deep_research`'s block at 111-119), and a new
`RunWorker.advance_multi_agent_runs`'s own self-gate. **Never** advertised
merely because `run_multi_agent_enabled` is set without its full
dependency chain — same discipline D4C established.

## 14. Security and privacy threat analysis

- **Auto-executing markers** (legacy defect, §9): eliminated by design —
  v1 fails closed on every tool/action marker, never executes one.
- **Prompt injection between roles**: neutralized via marker
  neutralization + untrusted-envelope framing on every sealed artifact's
  `raw_text` (§7) — a role cannot smuggle an executable-looking marker
  into the next role's prompt and have it survive as live syntax.
  Prior neutralization precedent: D4B's `research_sources.py`.
  Consistent because both are "model/scrape-originated text used as
  another consumer's untrusted input."
- **Fallback-chain provider sprawl** (legacy defect, §8): eliminated — one
  sealed provider, one attempt per step, no fallback substitution.
- **Unbounded loop / token spend** (legacy `MAX_TOKENS_PER_TASK` budget,
  still soft): v1's fixed 3-step, no-loop design makes the call count and
  worst-case token spend a fixed, known bound rather than a soft runtime
  check.
- **Citation forgery**: impossible by construction — no agent-supplied
  citation object is ever read; all citations come from the pre-sealed
  D2 snapshot (§10).
- **Secret/credential leakage into events/logs**: same discipline as
  D4C — bounded status-reason codes only, provider credentials resolved
  live and never sealed, no raw exception text in any persisted event.
- **DNS-rebinding/TOCTOU**: not applicable to v1 — no network fetch
  happens inside the multi-agent team itself (no live search/browse).
- **KG/memory/directive write**: explicitly not activated in this phase
  (requirement honored by construction — the marker sweep fails closed on
  exactly these markers, unlike the legacy path).

## 15. Conservative resource limits

| Limit | Recommended default | Rationale |
|---|---|---|
| Max roles/provider calls per run | 3 (fixed, v1 has no loop) | §5, §8 |
| Max concurrently running multi-agent runs per user | `run_multi_agent_max_concurrent_per_user: int = 1` (new, small adapter on the existing `_active_slots` pattern, `messages_stream.py:356-375`) | Multi-agent is the most expensive mode (3 sequential calls vs. 1); reuses the existing in-process counter mechanism, no new interface |
| Per-role timeout | `agent_role_timeout_s`, default matching `run_worker_handler_timeout_s` | Reuses existing per-call timeout config shape |
| Total run deadline | `agent_total_deadline_s`, default e.g. 180s for 3 roles | Mirrors `research_total_deadline_s`'s existing pattern |
| Max sealed artifact size per role | `agent_artifact_max_chars: int = 8000` (raw_text), smaller caps per list field (§7) | Mirrors D4B's own bounded `research_*` config constants |

No Redis/Valkey introduced — the existing in-process `_active_slots`-style
dict counter is reused/adapted, consistent with the existing
single-process SQLite/worker deployment model (`run_worker_web_concurrency`
already fails closed on >1 web process for exactly this reason).

## 16. Migration decision

**No migration required.** `Run.kind` (`String(24)`), `RunStep.kind`
(`String(32)`), `RunPayload.payload_type` (`String(32)`) are unconstrained
VARCHAR columns (`models.py:931,1006,1152`) with no CHECK constraint —
confirmed by direct inspection, same as D4B/D4C's own migration-free
precedent. `"multi_agent"` (11 chars), `"agent_research"`/`"agent_code"`/
`"agent_review"` (≤14 chars), and `"agent_artifact_v1"` (18 chars) all fit
with room to spare.

## 17. D5B/D5C implementation split

- **D5B — sealed role-artifact foundation and pure orchestration
  contracts** (no worker wiring, no public route change, no capability
  activation): `agent_personas`-equivalent durable prompt assembly reusing
  the EXISTING `AGENT_PERSONAS` prompts unmodified; `agent_artifact_v1`
  schema + validator/serializer (mirrors `deep_research_citations.py`'s
  shape: pure functions, no DB, no LLM call); marker-neutralization reuse
  for inter-agent handoffs; `multi_agent_readiness.py` (new file, mirrors
  `deep_research_readiness.py`); new config flags/limits (§13, §15); new
  `Run.kind`/`RunStep.kind` constants wired additively into `engine.py`/
  `events.py`'s existing `_RUN_KINDS` sets (same additive pattern D4C
  used). Fully unit-testable with fakes, zero behavior change to any
  existing mode.
- **D5C — worker integration, synthesis/finalization, readiness
  activation, minimal frontend progress**: `RunWorker.advance_multi_agent_runs`/
  `_run_agent_research`/`_run_agent_code`/`_run_agent_review_and_finalize`
  (mirrors D4C's `_run_deep_research_acquisition`/`_run_deep_research_synthesis`
  naming/shape); wiring `multi_agent_executable()` into
  `messages_stream.unsupported_mode_reason`, `/api/runs`, and
  `routes/settings._compute_chat_mode_capabilities` (replacing the
  hardcoded `False`); `agent.progress` SSE mapping + `onAgentProgress`
  hook wiring (no UI component). Capability flips to available only when
  this phase's full dependency chain is genuinely executable.

This mirrors D4B/D4C's own split (foundation-then-integration) and avoids
unnecessary fragmentation — no evidence supports a third D5D/D5E slice.

## 18. Focused acceptance-test matrix (for D5B/D5C themselves, not run now)

**D5B** (pure, hermetic, no DB/worker):
- `multi_agent_executable()` false by default; false with any single
  prerequisite missing; true with all met (mirrors
  `test_deep_research_readiness.py`'s exact parametrized shape).
- `agent_artifact_v1` validator: accepts a well-formed artifact; rejects/
  bounds an oversized field; strips/neutralizes an embedded marker in
  `raw_text`; deterministic `json.dumps(sort_keys=True)` round-trip.
- Existing `_RUN_KINDS` sets (engine.py/events.py) accept `"multi_agent"`
  additively with zero effect on chat/deep_research.

**D5C** (hermetic worker + fake provider, no live network/LLM):
- Real route: `/api/runs` + `mode="multi_agent"` rejects pre-persistence
  when not ready (zero rows) and accepts/queues when fully ready (mirrors
  the D4C closeout pass's own two-sided real-route test).
- Full happy path: 3 sealed steps in order, one final Message, citations
  = exactly the sealed D2 set.
- Any watched marker in any role's output → run fails closed
  (`agent_marker_execution_blocked`), zero execution, zero KG/memory/
  directive write.
- Crash/resume: kill after research artifact sealed → resume re-uses it,
  zero re-invocation of the Researcher; kill mid-`agent_review` →
  `outcome_unknown`, zero second provider call.
- Cancellation before/during each of the 3 steps → zero calls / no
  Message, per the matrix in §11.
- `agent.progress` events persisted in the exact required order, no raw
  text/secrets in payload.
- Capability response: `multi_agent` unavailable until every dependency
  is true; `chat`/`deep_research` unaffected by the new flag.

## 19. Risks and explicitly deferred work

- Legacy's bounded revision loop (Reviewer↔Coder, Reviewer↔Researcher) is
  cut for v1 — a real capability reduction, stated honestly (§5). A
  fast-follow phase could reintroduce it as an additional bounded
  `RunStep` sub-sequence once the fixed-3-step version is proven.
- Tool-enabled roles (sandbox code execution, MCP) are deferred entirely
  (§9) — any future phase MUST reuse the existing approval gateway, not
  design a new one.
- Live web search inside the Researcher role (reusing D4B/D4C's `[W#]`
  machinery) is out of scope for v1 — deferred, not designed.
- Whether "Coder" framing suits non-coding multi-agent requests is an
  open product question (§5, §23), not resolved here.
- `agent.progress`'s `code` field has no bounded-count schema support —
  same pre-existing `_FieldRule` closed-enum-only limitation D4C already
  carried forward, not revisited here either.
- No frontend UI renders `agent.progress`/`onAgentProgress` in D5C's own
  scope — hook-level plumbing only, same deferral D4C used for
  `research.progress`.

## 20. Exact file plan

**D5B** (new/modified):
- `backend/app/run_engine/multi_agent_readiness.py` (new)
- `backend/app/run_engine/agent_artifacts.py` (new — schema/validator/
  serializer for `agent_artifact_v1`)
- `backend/app/config.py` (+ `run_multi_agent_enabled`,
  `run_multi_agent_max_concurrent_per_user`, `agent_role_timeout_s`,
  `agent_total_deadline_s`, `agent_artifact_max_chars`, all bounded/
  validated like the existing `research_*` config additions)
- `backend/app/run_engine/engine.py` / `events.py` (+ `"multi_agent"` to
  each existing `_RUN_KINDS` set, additive; + `agent.progress` schema)

**D5C** (new/modified):
- `backend/app/run_engine/worker.py` (+ `advance_multi_agent_runs`/
  `_advance_multi_agent_run`/3 step handlers/finalizer)
- `backend/app/run_engine/messages_stream.py` (+ `multi_agent` case in
  `unsupported_mode_reason`; `agent.progress` → SSE mapping)
- `backend/app/routes/settings.py` (replace the hardcoded
  `available=False` at lines 120-121 with `multi_agent_executable()`)
- `frontend/src/useStreaming.ts` (+ `agent_progress` dispatch case +
  `onAgentProgress` callback)

No file outside `backend/app/run_engine/*`, the 4 listed backend files,
and 1 frontend file is expected to change in D5B/D5C.

## 21. STOP conditions for D5B

Stop and ask before starting D5B if:
- The user wants the legacy bounded revision loop preserved in v1 (a
  material scope change to §5/§6).
- The user wants tool-enabled roles in the first cut (§9 explicitly
  recommends against; doing so requires designing the approval-gateway
  integration first, as its own reviewed step).
- The user wants per-role model/provider selection instead of one sealed
  snapshot (§8).
- Repository evidence at D5B-start contradicts anything in this document
  (re-verify before writing code, do not assume this plan is still
  accurate if time has passed and other phases landed).

## 22. Questions genuinely requiring a product decision

1. Keep v1 single-pass (no revision loop), or is the bounded Reviewer↔Coder
   revise loop essential to "useful" for you? (§5, §19)
2. Should multi-agent mode use the SAME Researcher/Coder/Reviewer framing
   for non-coding requests, or should role selection eventually depend on
   task type? Not designed in D5A/D5B/D5C either way (§5).
3. Is a future tool-enabled Coder role (sandbox execution via the
   approval gateway) wanted at all, or should multi-agent mode stay
   text/evidence-only permanently? (§9, §19)
4. Should the Researcher role eventually be allowed to trigger D4B/D4C's
   live web search (reusing `[W#]` citations), or should multi-agent mode
   only ever read what D2 already retrieved for the request? (§10, §19)

## 23. PRD update

`THUNDER_AI_PRD.md` row for **MP-3C3D5** was split: this MP-3C3D5A
audit/plan row added as **plan/audit complete**; the future
implementation split into **MP-3C3D5B** (foundation) and **MP-3C3D5C**
(worker integration/activation), both explicitly **not started**; durable
`multi_agent` stated as remaining unavailable. No unrelated PRD content
was touched.

### 23.1 MP-3C3D5B closeout (2026-09-13)

**MP-3C3D5B is complete** (foundation only, exactly the §17 split above:
`multi_agent_orchestration.py`, `multi_agent_artifacts.py`,
`multi_agent_readiness.py`, config bounds, additive `_RUN_KINDS`/
`agent.progress` entries, the §21 generic-worker-claim reservation). See
`docs/superpowers/reports/phase-1b2r-mp3c3d5b-completion.md` for the full
as-built record, including one genuine defect this §7 schema's own
plan did not fully specify and D5B's implementation caught and fixed:
`parent_artifact_id` needed a REAL, DB-verified parent step (not merely a
non-empty string) to make self-parenting/skipped/wrong-role/cross-run
parents structurally impossible, not merely conventionally avoided.
**MP-3C3D5C** (worker integration/activation) remains **not started** —
none of §17's D5C bullets (worker sweep, step handlers, route/capability
wiring, frontend `agent.progress` plumbing) have begun.


## MP-3C3D5C implementation closeout (2026-09-17)

D5C is implemented. The dedicated three-role worker, route/capability readiness,
transaction-serialized per-user active-run admission, marker-tail rejection, and
production frontend progress callback are wired. Deployment flags remain off by
default. This supersedes earlier D5C-not-started statements and the hook-only UI
deferral; D5B Solution Specialist naming and its six progress codes remain the
approved contract. See [D5C completion report](../reports/phase-1b2r-mp3c3d5c-completion.md)
for validation, recovery behavior, and remaining deployment boundaries.
