# MP-3C3D6C-J6 — deterministic durable Deep Research browser journey — COMPLETE

Date: 2026-09-17  
**Status: D6C-J6 COMPLETE — D6C IN PROGRESS (J1-J6 complete; J7-J9 not started).**

## Fidelity statement (read this first)

**Real:** Chromium browser, production frontend, real ModeSelector, FastAPI app and
lifespan, authentication / Bearer / cookies / CSRF, durable /messages/stream submission,
run admission and idempotency, supervisor and worker threads, SQLite persistence
(Run / RunStep / RunPayload / RunEvent / Message / citations), SSE consumption and
rendering, readiness and capability gating, snapshot sealing, production citation
validation and final Message persistence.

**Fake (test-only, injected through pre-existing worker seams):** only the two external
research transports and the synthesis provider - search results, fetched page contents
and model output.

**HONEST BOUNDARY:** the fakes replace the network TRANSPORT. This journey therefore
proves the orchestration / snapshot / citation / persistence path, NOT live DNS or
network-level SSRF behaviour. The real SSRF, redirect, content-type and size-bound
guards remain covered by their own focused production tests (tests/test_research_search_fetch.py
and tests/test_research_acquisition.py, both green in this pass).

## Part A - authorized console-defect correction

Stage 0A of the previous pass attributed the unattributed browser error
"Unexpected token ')'" to an inline script in the served document (filename = the
document URL, line 26, col 150). The inline theme-restoration IIFE in
frontend/index.html was missing the function body's closing brace: 4 opening vs 3
closing braces, so it was a SyntaxError and NEVER RAN.

**Authorized one-character fix applied (targeted edit only):**

    - ...}}catch(e){})();</script>
    + ...}}catch(e){}})();</script>

**RED -> GREEN:** frontend/src/__tests__/indexHtmlSyntax.test.ts went from
2 failed | 1 passed (failing with SyntaxError: Unexpected token ')') to
**3 passed**. The authoritative assertion is new Function(scriptText) per inline
script; brace balance remains as a secondary diagnostic.

**Artifacts:** npm run build exits 0. frontend/dist is gitignored and untracked, so it
was NOT force-added; the build regenerated it and the built inline script now reads
catch(e){}})(); . A Node parse check reports SOURCE_INLINE=1 PARSE_FAILURES=0 and
BUILT_INLINE=1 PARSE_FAILURES=0.

**Browser gate (all 9 items pass):** indexHtmlSyntax green; new Function parses every
inline script; source and built scripts valid; build exit 0; clean reload reports ZERO
"Unexpected token ')'"; ZERO manifest syntax errors; theme restoration proven to
EXECUTE - with localStorage research-assistant-theme=dark the document is dark on load
with no manual toggle, and an isolated run of the script shows beforeRun=false ->
afterRun=true with execErr=none; tsc --noEmit -p tsconfig.json exit 0; git diff --check
clean.

## Part B - J6 journey

### B1 contract confirmed from the working tree (not redesigned)

* ONE authoritative predicate deep_research_executable(), reused by /messages/stream
  (messages_stream.unsupported_mode_reason), /api/runs, GET /api/settings and
  RunWorker.advance_deep_research_runs. It requires engine + worker + chat-stream +
  public-api + deep-research flags AND a configured idempotency HMAC key.
* Run kind: deep_research.
* Step kinds: research_fetch_extract (acquisition, sealed) and deep_research_synthesis.
* Snapshot: payload_type research_sources, in-snapshot research_snapshot_version = 1,
  ordering = "search_rank".
* Adapters: SearchAdapter.search(query, *, max_results) -> List[SearchResultCandidate(url,
  title, search_rank)]; FetchAdapter.fetch(url, *, timeout_s, max_redirects,
  max_response_bytes, max_extracted_chars, max_passages, max_passage_chars) -> FetchedPage(
  final_url, passages).
* Worker seams: research_search_adapter, research_fetch_adapter, chat_provider
  (worker.py:2049-2050 already prefers an injected adapter).
* Closed progress vocabulary: research_searching, research_fetching,
  research_sources_ready, research_synthesizing.
* research_sources_ready and the sealed snapshot are written in ONE transaction
  (worker.py:2087-2101) and commit atomically, so the event is never visible before the
  snapshot it announces.
* Citations: build_web_marker_map assigns [W1], [W2], ... in sealed passage order;
  build_web_citations keeps only markers actually referenced, in first-reference order,
  deduplicated, with server-owned url/title/snippet; has_hallucinated_web_marker is the
  fail-closed gate.

Four contract details were discovered empirically while writing the RED tests (each one
was MY expectation being wrong, not a production defect):

1. The search adapter itself normalizes with the production normalize_result_url,
   drops unsupported schemes and duplicates, and assigns CONTIGUOUS 1-based ranks; the
   orchestrator's dedupe is explicitly defense-in-depth over already-normalized URLs.
2. passage_id is "s{source}p{position}" over the ORIGINAL extracted lists, so dropping a
   duplicate passage does not renumber survivors (observed: s1p0 and s1p2).
3. The SEALED snapshot carries snapshot-local markers [1]..[N]; the collision-free [W#]
   web namespace is assigned later at synthesis time.
4. research_fetching carries a bounded candidate_count; progress payloads are bounded
   counts only, never query/url/title/content.

### B2 fixtures (tests/smoke/research_fakes.py)

Deterministic search adapter, fetch adapter and synthesis provider, injected ONLY
through the existing RunWorker seams. Activation is tests/smoke-scoped
(THUNDER_SMOKE_SCENARIO=deep_research, read only by durable_smoke_app.py); there is no
public header/query/body switch and nothing in backend/app reads it. Deep Research is
enabled for that scenario only, in the smoke child env; Multi-Agent stays disabled.
Records safe metadata only: invocation ordinals, counts, normalized URLs, ranks,
passage counts, prompt length/sha256 prefix, and BOOLEANS for marker/canary presence -
never raw query text, never prompt text, never canary values.

### B3-B4 real vs fake, smoke flags

The smoke child sets engine/worker/public-api/chat-stream true (already the case) and
deep_research true only for this scenario. A fresh normal Settings() instance still
reports all seven durable defaults False (verified).

## B5-B6 browser journey

Run 5820c1ba-b9a8-48af-919b-7f43fe084637, kind deep_research, status **succeeded**.

| Item | Evidence |
|---|---|
| one /messages/stream POST | exactly 1 |
| distinct Idempotency-Key | one per submission (journey 1: 06dc5cf6-9a20-41f7-9f0c-8a10f7ac6dea) |
| X-Run-Id header | e18d363c-... (journey 1) - equals the persisted Run id |
| persisted Run id equals X-Run-Id | yes |
| wire mode value | request body exactly {"message":"D6C-J6 deterministic deep research request","mode":"deep_research"} |
| no reconnect / duplicate / abort / cancellation | 1 submission, 0 run_signals |
| terminal | one complete; composer returned to normal |

Progress order (persisted run_events, run_seq 4-7):
research_searching -> research_fetching -> research_sources_ready -> research_synthesizing,
then terminal succeeded. No unknown code; every payload is {"code": ...} or a bounded
candidate_count - no raw query, URL, title, passage content, exception, credential or
canary.

## B7 acquisition and sealed snapshot (opened with the PRODUCTION helper)

Read through open_research_snapshot(db, step, *, expected_tenant_id, expected_user_id,
expected_run_id) - the context-bound production envelope.

| Property | Observed |
|---|---|
| research_snapshot_version | 1 |
| query_count | 1 |
| ordering | search_rank |
| sources | rank 1 https://alpha.example.invalid/a "Alpha Source" (2 passages); rank 2 https://beta.example.invalid/b "Beta Source" (2 passages) |
| passage ids | s0p0, s0p1, s1p0, s1p2 (duplicate dropped, survivors not renumbered) |
| content hashes | sha256:5d91f9d4..., d0630918..., b15d2aa7..., abd76a7f... recomputed and matched |
| search invocations | 1 |
| fetch invocations | 2 (one per deduplicated eligible source) |
| rejected URL | ftp://gamma.example.invalid/c dropped by production normalization |
| canonical duplicate | HTTPS://Alpha.Example.Invalid:443/a#section collapsed onto URL_ALPHA, fetched once |
| **marker neutralization** | sealed passage text reads "[\u200bMCP_CALL: mcp__smoke_..." - a ZERO-WIDTH SPACE breaks the actionable opener while the text survives as inert data; ACTIONABLE_MARKER_IN_SEALED = False |
| step output_ref | {"passage_count": 4, "source_count": 2, "truncated": false} - bounded counts, no url/title/plaintext |
| snapshot reuse | the dedicated test test_replay_reads_existing_snapshot_without_any_adapter proves a replay reads the existing sealed snapshot and creates no second one |
| canary | present in the SEALED PLAINTEXT (it is retrieved page content, encrypted at rest) and ABSENT from every unencrypted column, from the ciphertext, from progress events and from the DOM |

Ciphertext scan: 3396 bytes, contains neither the canary, nor "[MCP_CALL", nor
"https://". PLAINTEXT_LEAKS across every unencrypted text column = [] .

## B8 citation integrity

The assistant Message carries exactly two citations, both server-built:

* marker [W1], url https://alpha.example.invalid/a, filename "Alpha Source", snippet =
  the sealed alpha passage
* marker [W3], url https://beta.example.invalid/b, filename "Beta Source", snippet = the
  sealed beta passage

document_id and chunk_id are null (web source), no model-supplied url/title exists
anywhere, the repeated [W1] in the answer produced ONE citation (first-reference dedupe),
and has_hallucinated_web_marker("[W99]") is True in the focused test (fail-closed).
Referenced vs unreferenced: 2 of 4 sealed passages are referenced ([W1], [W3]); [W2]
(marker-shaped passage) and [W4] (canary passage) are sealed but NOT cited. The
production contract does not require every sealed passage to be cited, so no such
requirement is asserted.

## B9 exact counts and terminal states

| Table | Count |
|---|---|
| runs | 1 (deep_research, succeeded) |
| run_steps | 3 - load_context pending, research_fetch_extract succeeded, deep_research_synthesis succeeded |
| run_payloads | 2 - other/fernet.v1/345 B, research_sources/fernet.v1/1434 B |
| run_events | 9 - 3 control, 4 research.progress, 1 text, 1 terminal control |
| messages | 2 - one user (42 chars), one assistant (146 chars) |
| citations | 2 (JSON column on the assistant Message; no citations table exists) |
| run_signals | 0 |
| pending_approvals | 0 |
| dispatch_records | 0 |
| execution_turns | 0 |
| tool_calls | 0 |

Invocations: search exactly 1; synthesis provider exactly 1; fetch exactly 2. No implicit
fallback provider (the injected provider is the only one and its ordinal guard fails
closed). No MCP/tool/approval/dispatch execution. Tripwire silent - no outbound-network
event was recorded at any point.

## B10 browser, console and artefacts

Console on the authenticated page: Errors 1, Warnings 1 - the single error is the
pre-login POST /api/auth/refresh 401/403 bootstrap baseline, which did NOT repeat during
the authenticated turn. ZERO "Unexpected token ')'", ZERO manifest errors, ZERO page
errors, ZERO unhandled rejections, ZERO authenticated-turn application errors.

DOM: the answer renders with [W1] ... [W3]; marker leak false; canary leak false; two
bubbles (one user, one assistant - no duplicate assistant bubble). After reload the
session auto-hydrates: GET /api/sessions/1/messages => 200 issued with no click, the
answer renders, the counter reads "2 messages", and citations are preserved.

Screenshots (outside the repository):
C:\\deepseek-harness\\d6c-j6-answer-with-citations.png and
C:\\deepseek-harness\\d6c-j6-postreload-hydrated.png.

**I cannot visually inspect images** - the executing model declares no image input.
DOM probes, accessibility snapshots and browser counters are the evidence.

## Focused verification (Part C)

| Command | Result |
|---|---|
| pytest tests/smoke + research acquisition / snapshot persistence / readiness / activation boundary / deep-research worker / sources / search-fetch / config / mode capability | **240 passed** |
| vitest indexHtmlSyntax, ModeSelector, ModePropagation, useStreaming, sseParser, sessionHydration | **79 passed (6 files)** |
| tests/smoke/test_research_fakes.py | 14 passed |
| import app.main | IMPORT_OK |
| py_compile (changed python) | exit 0 |
| npx tsc --noEmit -p tsconfig.json | exit 0 |
| npm run build | exit 0 |
| git diff --check | clean |

## Cleanup

After the journey: 0 smoke processes, 0 waiting orchestrators, 0 bound ports,
0 thunder_d6b_*/thunder_d6c_* temp roots, 0 pending fixture tasks, no binary artifact in
the repository, no .playwright-mcp directory in the repository. Evidence archived to
C:\\deepseek-harness\\d6c-j6-evidence (smoke.db, the three fixture JSONLs, manifest,
backend log). The throwaway per-run encryption key file was deliberately NOT archived.

Repository state is unchanged: branch phase-1b1-approval-domain, HEAD
b2e67183d4791cbbaed9d3d98e8833983510f3ff, 0 staged, stash@{0} untouched. No commit,
stage, merge, push, reset, rebase, stash, clean, discard, branch switch or worktree
operation was performed.

## Open items

1. The Vite dev-proxy concurrency defect (D6B-R5) remains **OPEN** and was not fixed or
   re-tested here; the smoke bypass remains a harness workaround only.
2. load_context (the submission step) remains status "pending" after a successful Deep
   Research run. This mirrors the J4 observation that the submission step is not driven
   to a terminal status by the worker; it is recorded as an observation, not a claimed
   defect, and no production change was made.
3. The pre-login /api/auth/refresh 401/403 is an established bootstrap baseline.
4. J7-J9 and MP-4-MP-6 are not started.
