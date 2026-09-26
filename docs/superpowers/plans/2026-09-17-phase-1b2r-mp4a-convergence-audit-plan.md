# Phase 1B.2R MP-4A — convergence audit and implementation plan

Date: 2026-09-17
**Status: MP-4A COMPLETE — READY FOR USER REVIEW.** Planning only: no product code, no test
code, no migration, no flag default changed.

## 1. Executive decision

MP-4 is **implementable without a new architecture decision** and **without a schema
migration**, but it is **not** a single safe edit. Repository evidence splits it into three
independently verifiable slices (section 12). The highest-risk item is not the adapters:
it is that **model-emitted Python currently executes with no approval and no flag**. MP-4
must close that boundary, and closing it requires a **new settings flag that does not yet
exist** (section 6/13) - a public-config change requiring explicit authorization.

Two findings change the naive plan:

1. **streaming_service is only PARTIALLY deletable.** The durable path itself imports
   format_sse from it (run_engine/messages_stream.py:48). The wire-format helper must be
   relocated, not deleted with the module.
2. **Python execution is ungated, not merely under-gated.** streaming_service.py:853-857
   runs run_python_code_async with no flag check, while the immediately following MCP block
   IS gated by settings.enable_mcp_tool. There is no run_engine_sandbox flag in config today.

## 2. Evidence anchors

| Anchor | What it establishes |
|---|---|
| architecture.md:1288-1294 | MP-4 scope: convergence of multi-agent + deep research + python sandbox; MultiAgentAdapter / DeepResearchAdapter; delete streaming_service duplicates once the adapter is the sole caller; run_engine_sandbox (off) routes python through PendingApproval + a tool_dispatch step; exit = stream_multi_agent_response and the graph agent_* nodes no longer both exist |
| architecture.md:1296-1301 | MP-5 scope: /messages + scheduler_service on the engine; delete run_research_workflow bespoke resume_from and the citations-JSON approval encoding |
| D6A plan:517-533 | the nine blocking risks MP-4 must not start before; risk 9 (no live-provider pass) remains an accepted residual |
| D6A plan:519 | legacy and durable must not both execute the same request |
| routes/messages.py:22,25 | production route imports run_research_workflow and streaming_service |
| run_engine/messages_stream.py:48 | the DURABLE path imports format_sse from streaming_service |
| streaming_service.py:57 | format_sse is the shared SSE wire-format primitive |
| streaming_service.py:416,696 | stream_multi_agent_response and stream_chat_response - the two legacy streaming loops |
| streaming_service.py:853-857 | UNGATED python execution on the legacy chat path |
| streaming_service.py:535-540 | _run_sandbox - UNGATED python execution on the legacy multi-agent path |
| streaming_service.py:861 | the adjacent MCP block IS gated - proves the python omission is an omission, not a convention |
| langgraph_workflow.py:79,607,1206 | a second ungated python execution path |
| tools/python_sandbox.py:42,106 | PYTHON_CODE_PATTERN and asyncio.create_subprocess_exec with preexec_fn sandbox limits |
| tools/terminal_executor.py:100 | asyncio.create_subprocess_shell - shell execution |
| tools/tool_registry.py:31 | python_sandbox is ALREADY a RegisteredTool - reuse target exists |
| config.py:82,336 | enable_terminal_tool = False, enable_browser_automation = False; NO run_engine_sandbox flag exists |
| tools/deep_research.py:24 | legacy run_deep_research(query) |
| D6C-J9 report | D6C COMPLETE J1-J9; non-chat load_context is skipped; cancelled running step is intentional at-most-once evidence |

## 3. Current call graph (public entry -> execution owner)

    POST /api/sessions/{id}/messages/stream
      -> routes/messages.py -> run_engine/messages_stream.resolve_and_submit
         -> submission.create_chat_run (Run + RunStep seq1 + sealed payload + Message)
         -> durable SSE tails persisted RunEvents ; worker owns generation
         [durable path - already converged]

    POST /api/sessions/{id}/messages            (legacy, MP-5)
      -> routes/messages.py -> services/langgraph_workflow.run_research_workflow
         -> its own graph, its own resume_from, its own persistence, UNGATED python at 607/1206

    GET  /api/runs/{id}, POST /api/runs/{id}/cancel, POST /api/approvals/{id}/approve|deny
      -> routes/runs.py, routes/approvals.py -> durable engine (already converged)

    Legacy streaming loops still imported by routes/messages.py:
      services/streaming_service.stream_chat_response        (line 696) - UNGATED python 853
      services/streaming_service.stream_multi_agent_response (line 416) - UNGATED python 535

## 4. Target ownership model (exactly one owner per responsibility)

* submission/admission/idempotency: submission.create_chat_run (unchanged)
* run/step/lease/finalization: RunWorker + worker_supervisor (unchanged)
* chat generation: chat_stream (unchanged)
* Deep Research: research_acquisition + deep_research_synthesis (unchanged)
* Multi-Agent: multi_agent_orchestration + multi_agent_artifacts provider calls (unchanged)
* tool proposal + approval + dispatch: approval.gateway.dispatch (unchanged, the ONE path)
* python execution: the SAME gateway path, with the executor behind a new run_engine_sandbox flag (OFF)
* citations: deep_research_citations (web) and the context-snapshot policy (local)
* Message persistence: the finalizers only
* notifier wake-up: the existing process-local notifier; polling remains the correctness backstop

## 5. Current-versus-target ownership matrix

| Capability | Current public caller | Current exec owner | Current persist owner | Current approval owner | Target exec owner | Adapter | Legacy removable | Phase | Risk |
|---|---|---|---|---|---|---|---|---|---|
| plain chat streaming | /messages/stream | chat_stream (durable) | finalizer | n/a | unchanged | no | streaming_service.stream_chat_response | MP-4B | high (deletion) |
| reconnect/resume | /messages/stream | durable SSE | durable | n/a | unchanged | no | legacy reconnect shim | MP-4B | medium |
| cancellation | /api/runs/{id}/cancel | RunWorker | worker | n/a | unchanged | no | none | - | low |
| MCP tool proposal | /messages/stream | durable worker | finalizer | gateway | unchanged | no | legacy MCP loop | MP-4B | high |
| MCP approve/deny | /api/approvals | gateway.dispatch | finalizer | gateway | unchanged | no | none | - | low |
| Python proposal | none (marker only) | none | none | none | gateway + tool_dispatch | YES (new) | auto-exec in 2 legacy paths | MP-4C | CRITICAL |
| Python approve/deny | none | none | none | none | gateway.dispatch | YES (new) | same | MP-4C | CRITICAL |
| Deep Research | /messages/stream (durable) + legacy route | worker | finalizer | n/a | unchanged | DeepResearchAdapter only if a legacy caller remains | tools/deep_research.run_deep_research | MP-4B | medium |
| Multi-Agent | /messages/stream (durable) + legacy loop | worker | finalizer | n/a | unchanged | MultiAgentAdapter only if a legacy caller remains | streaming_service.stream_multi_agent_response + graph agent_* | MP-4B | high |
| citation persistence | durable | finalizer | finalizer | n/a | unchanged | no | duplicate citation construction | MP-4B | medium |
| memory/context reads | durable | submission (sealed once) | submission | n/a | unchanged | no | none | - | low |
| memory/KG writes | legacy markers | streaming_service / graph | legacy | NONE | gateway proposal (as with MCP) | YES | legacy direct writes | MP-4C | high |
| notifier wake-up | internal | process-local notifier | n/a | n/a | unchanged | no | duplicate call sites only | MP-4B | low |
| health/readiness | /api/health, /api/health/worker | supervisor | n/a | n/a | unchanged | no | none | - | low |
| final Message persistence | durable | finalizer only | finalizer | n/a | unchanged | no | legacy direct Message writes | MP-4B | high |

## 6. Python execution threat model (highest-risk boundary)

Each current path capable of executing code:

| Path | Marker/parser | Approval proposal | Executor | Flag gate | Executes before approval? |
|---|---|---|---|---|---|
| streaming_service.py:853-857 (legacy chat) | extract_python_code over the full model response | NONE | run_python_code_async -> create_subprocess_exec | NONE | **YES** |
| streaming_service.py:535-540 (legacy multi-agent _run_sandbox) | extract_python_code over the coder output | NONE | run_python_code_async | NONE | **YES** |
| langgraph_workflow.py:607,1206 | extract_python_code | NONE | run_python_code | NONE | **YES** |
| tools/terminal_executor.py:100 | [PROPOSED_COMMAND: ...] | separate legacy path | create_subprocess_shell | enable_terminal_tool (False) | gated today |
| durable chat/DR/MA | MarkerScanner watches ALL_WATCHED_MARKERS; a python marker trips and ABORTS | none created | none | n/a | NO - the durable path refuses |

So the durable path already fails closed on a python marker while both legacy paths execute
it silently. Target invariant to enforce in MP-4:

> No model-emitted Python/code marker may directly execute code. It may only create a bounded
> server-owned proposal. Execution requires a valid, unexpired, same-user/same-session
> approval consumed through the existing gateway and tool_dispatch path, with the executor
> itself behind run_engine_sandbox (OFF by default).

Required properties (all must be proven in MP-4C): deny executes zero code; approval replay
idempotent; approve-after-deny conflicts; changed arguments cannot reuse an approval;
unknown/revoked tool identity fails closed; code and secrets never appear in progress or
error payloads; execution stays disabled behind its flag after MP-4 ships.

Blocking gap: **there is no run_engine_sandbox flag in config today** - MP-4C must add one
(default False) and gate the executor on it. That is a public-config change and is listed as
a required user decision.

## 7. Adapter plan

| Legacy caller | Decision | Signature / flow |
|---|---|---|
| streaming_service.stream_chat_response | **delete** after caller migration | no adapter; routes/messages.py already has the durable path |
| streaming_service.stream_multi_agent_response | **delete** after parity, or thin MultiAgentAdapter if a route must keep it | adapter(chat_input, ctx) -> resolve_and_submit(mode="multi_agent") and return the durable SSE stream |
| streaming_service.format_sse | **relocate, never delete blindly** | move to a neutral module and re-export; durable path depends on it |
| tools/deep_research.run_deep_research | **delete** if unreferenced after MP-4B, else DeepResearchAdapter | adapter(query, ctx) -> resolve_and_submit(mode="deep_research") |
| langgraph_workflow.run_research_workflow | **defer to MP-5** (explicitly MP-5 in architecture:1298) | none in MP-4 |
| python_sandbox execution | **approval adapter (new, MP-4C)** | proposal -> PendingApproval -> gateway tool_dispatch -> executor, gated by run_engine_sandbox |

Every adapter must go through the canonical mode validator, reuse the idempotency contract,
create no duplicate Run/Message, invoke no provider itself, persist no final Message itself,
execute no tool itself, and add no second citation/context implementation.

## 8. Deletion plan (per module)

Sequence: add adapter -> migrate one caller -> verify parity -> migrate remaining callers ->
prove zero references -> delete duplicate -> rerun focused regression.

| Module | Production imports | Test imports | Flag refs | Replacement | Preconditions | Phase |
|---|---|---|---|---|---|---|
| streaming_service.stream_chat_response | routes/messages.py:25 | to confirm in MP-4B | enable_mcp_tool, enable_terminal_tool | durable chat path | durable parity for all journeys; format_sse relocated | MP-4B/D |
| streaming_service.stream_multi_agent_response | routes/messages.py:25 | to confirm | same | durable multi_agent | MA parity | MP-4B/D |
| streaming_service.format_sse | run_engine/messages_stream.py:48 | many | none | relocated helper | relocation merged first | MP-4B (must precede deletion) |
| tools/deep_research.run_deep_research | none found in backend/app | to confirm | none | durable deep_research | zero references proven | MP-4B/D |
| graph agent_* nodes | langgraph_workflow (route-level) | to confirm | none | durable multi_agent | MP-5 owns run_research_workflow retirement; MP-4 may only prove no second implementation remains | boundary with MP-5 |
| python auto-exec blocks (3 sites) | streaming_service x2, langgraph x2 | to confirm | none | approval-gated dispatch | MP-4C complete | MP-4C |

No single-step bulk deletion is proposed.

## 9. Notifier convergence boundary

Evidence: wake-ups are process-local; polling is the correctness backstop; the architecture
does not assign shared-store work to MP-4. Therefore MP-4B should only **remove duplicate
notifier call sites** so exactly one owner emits wake-ups, and MP-6 owns any shared store.
No Redis/PostgreSQL pub-sub is proposed.

## 10. Feature-flag truth table (target, all defaults OFF during implementation)

| run_engine | run_worker | chat_stream | public_api | deep_research | multi_agent | run_engine_sandbox (NEW) | Result |
|---|---|---|---|---|---|---|---|
| F | * | * | * | * | * | * | legacy only; durable routes 404; unchanged from today |
| T | T | T | T | F | F | F | durable chat; DR/MA report unavailable; python refused |
| T | T | T | T | T | F | F | + durable Deep Research |
| T | T | T | T | F | T | F | + durable Multi-Agent |
| T | T | T | T | T | T | F | all modes; python still refused |
| T | T | T | T | * | * | T | python proposals may be approved and dispatched through the gateway (opt-in, still approval-gated) |
| * | * | * | T | * | * | * | worker not ready -> submission refused before persistence (existing contract) |

No combination enables an unimplemented mode, and none permits execution without approval.

## 11. Migration decision

**No schema migration and no data backfill are required for MP-4B and MP-4D.** RunStep.kind
and RunStep.status are unconstrained documented strings; the needed kinds (tool_dispatch,
sub_agent steps) and the terminal skipped state already exist. **MP-4C adds one settings
field with a False default; that is a config addition, not a schema migration.** Payload
versioning is unchanged. This was checked against models.py (RunStep.kind String(32),
status unconstrained) and config.py, not assumed.

## 12. Recommended implementation slices

**MP-4B — call-graph convergence and duplicate-loop deletion.** Objective: make the durable
engine the sole execution owner for every public entry while keeping all defaults OFF.
Files likely changed: routes/messages.py, run_engine/messages_stream.py (format_sse
relocation), a new small wire-format module, tests. Untouched: submission.py, worker.py,
config defaults. RED first: an adapter/parity test that fails while routes still import the
legacy loops. Order: relocate format_sse -> add adapters -> migrate one caller -> parity ->
migrate the rest -> prove zero references -> delete. Acceptance: zero production imports of
the deleted symbols; identical Run id under replay; no duplicate Message. Runtime: medium.
Browser evidence: yes (one durable journey per mode).

**MP-4C — approval-gated Python proposal/dispatch.** Objective: no marker executes code;
proposal only; execution only via gateway with run_engine_sandbox OFF. Files likely changed:
config.py (one new field, default False), a python proposal adapter, tool_registry wiring,
tests. Untouched: the executor implementation and its resource limits. RED first: an
ungated-execution regression that currently FAILS (proving the defect) plus deny-executes-zero.
Acceptance: the threat-model properties in section 6. **STOP before implementing: adding the
flag and routing proposals changes security semantics and needs authorization.** Browser
evidence: yes (approve/deny journeys).

**MP-4D — zero-reference deletion and closeout.** Objective: delete only what is proven
unreferenced after 4B/4C. Preconditions: import/call-site proofs, no dead flags, focused
regression green. Rollback: revert the deletion commit (no data repair). Runtime: small.

## 13. Test matrix (future)

Adapters: same auth/CSRF/validation/idempotency semantics; same Run id; no duplicate Message;
no direct provider call. Legacy retirement: zero production imports; zero route references;
no dead flag; no duplicate streaming loop; no duplicate citation builder; no duplicate final
Message persistence. Python approval: marker detected but not rendered; proposal only;
approve executes once; deny executes zero; replay idempotent; changed args conflict;
cross-user/session/run mismatch rejected; revoked identity rejected; timeout/cancel;
action-log chain; secret/code canary absence. Durable regressions: reconnect, cancel,
approval, Deep Research, Multi-Agent, health, worker recovery, terminal persistence.
Source inspection: no direct exec/eval/subprocess reachable from a model-controlled route;
no background daemon write; no legacy provider loop reachable after cutover. PostgreSQL,
hosted, Docker and live providers remain MP-6.

## 14. Rollback

Every slice reverts by flag configuration or a single revert; none requires a data repair.
MP-4B/4D touch no persisted schema. MP-4C's new flag defaults to False, so reverting the
code restores exactly today's behaviour.

## 15. STOP conditions (require authorization before coding)

1. any public API behaviour change;
2. deleting an externally imported module (format_sse in particular);
3. enabling Python execution, or adding the run_engine_sandbox flag;
4. adding a new sandbox/runtime or a paid/hosted dependency;
5. any schema migration;
6. shared notifier infrastructure;
7. changing approval semantics;
8. removing a feature still reachable from UI/API;
9. changing any default flag;
10. breaking backward compatibility of /messages/stream.

## 16. Required user decisions

1. **Authorize the new run_engine_sandbox flag (default False)** - without it MP-4C cannot
   close the ungated-python boundary through the gateway.
2. **Confirm MP-4C scope includes the memory/KG direct writes** found on the legacy paths
   (they share the marker pattern) or explicitly defer them.
3. **Confirm the MP-4B/MP-5 boundary for the graph agent_* nodes** - architecture:1298 puts
   run_research_workflow retirement in MP-5 while MP-4 exit criteria mention the agent_* nodes.
4. **Accept or schedule the D6A risk-9 residual** (no live-provider pass) before any legacy
   deletion, as D6A:531 requires.

## 17. Residual-risk ownership

| Risk | Owner |
|---|---|
| Vite dev-proxy concurrent-SSE defect | MP-6 (hosted/deployment) - still OPEN, not fixed |
| one historical intermittent resume, root cause unknown | MP-6 (hosted multi-process verification) |
| long unbroken content clipped internally | MP-6 UX polish (no CSS fix authorized) |
| mobile touch targets below ~44x44 | MP-6 UX polish (no CSS fix authorized) |
| process-local notifier | MP-6 shared store; MP-4B only removes duplicate call sites |
| no PostgreSQL concurrency matrix | MP-6 |
| no hosted multi-process verification | MP-6 |
| DNS rebinding / TOCTOU in fetch guard | MP-4B review + MP-6 hardening |
| fakes do not prove live integrations | MP-6 / explicit user acceptance |
| architecture doc needs controlled convergence for the non-chat load_context -> skipped contract | MP-4D or MP-6 documentation convergence (protected doc must not be edited ad hoc) |
| ungated python execution in two legacy paths | **MP-4C (CRITICAL)** |
| duplicate streaming/citation/Message-persistence implementations | **MP-4B/MP-4D** |
