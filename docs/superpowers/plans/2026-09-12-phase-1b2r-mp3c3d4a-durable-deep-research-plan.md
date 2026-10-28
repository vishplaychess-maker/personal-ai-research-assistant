# Phase 1B.2R — MP-3C3D4A: durable deep-research execution contract and implementation plan

Evidence-first architecture mini-phase. **No product code was written in this
phase.** This document is the sole deliverable.

## Stage 0 — repository and phase state

```
git status --short        → dirty Phase 1B tree unchanged; no new product files
git branch --show-current → phase-1b1-approval-domain
git rev-parse HEAD        → b2e67183d4791cbbaed9d3d98e8833983510f3ff
git diff --check          → clean
git stash list            → stash@{0}: 7ab31a5 (untouched)
```

Confirmed before writing this plan:
- `docs/superpowers/reports/phase-1b2r-mp3c3d3b-completion.md` exists; architecture
  §41 exists.
- No durable deep-research worker/sweep exists (`grep` for
  `advance_deep_research_runs` in `backend/app/run_engine/` → zero matches).
- Direct Run API (`routes/runs.py:135`) and `/messages/stream`
  (`routes/messages.py:575`) both still call
  `messages_stream.unsupported_mode_reason`/`unsupported_mode_code` against
  `_SUPPORTED_DURABLE_MODES = frozenset({"chat"})` — `deep_research` is still
  rejected before persistence on both routes, unchanged.
- `routes/settings.py::_compute_chat_mode_capabilities()` still marks
  `deep_research` unavailable whenever `run_messages_stream_durable_enabled`
  is `True`, regardless of `enable_deep_research` — unchanged.
- `run_messages_stream_durable_enabled=False`, `run_engine_enabled=False`
  (fresh `app.config` import).
- No occurrence of the phrase "prior D3B commit" exists anywhere in
  `docs/` or `THUNDER_AI_PRD.md` — no wording correction was needed.

## 1. Executive finding

The legacy `run_deep_research()` is a **single synchronous, blocking,
unbounded-duration helper that returns one opaque formatted string** — no
structured sources, no citations, no per-page metadata, no cancellation
awareness, no SSRF guard, and it is injected **directly into the system
prompt** (not an untrusted user/context envelope). None of these five
properties are acceptable for a durable run, and none can be fixed by
"wrapping" the function inside `RunWorker` — the durable version needs a
genuinely different data contract (structured, sealed sources with 1:1
citation correspondence), a genuinely different trust boundary (untrusted
envelope, input-side marker neutralization — `MarkerScanner` alone does not
cover this), a genuinely different security boundary (route every fetch
through the existing-but-unwired `browser_agent.assert_url_allowed` SSRF
guard), and a genuinely different cancellation model (per-operation bounded
timeouts + between-operation cancellation checks, because DDGS/Playwright
calls cannot be safely interrupted mid-flight). All of this is buildable
with **zero database migration** — `Run.kind`, `RunStep.kind`, and
`RunPayload.payload_type` are all unconstrained string columns already
designed for new values (their own comments literally say so), and the
existing encrypted-payload/versioned-snapshot/replayable-event machinery
already proven in MP-3C1/MP-3C2/MP-3C3D2 covers every new need. Knowledge-graph
ingestion must be **deferred**, not carried over — it is fire-and-forget on a
daemon thread with its own untracked DB session, with no idempotency ledger,
which is the opposite of what a durable step requires.

## 2. File-anchored legacy pipeline inventory

| Stage | Function/file | Input | Output | External side effect | Commits internally | Retry-safe | Cancellation-aware | Structured sources |
|---|---|---|---|---|---|---|---|---|
| Mode gate | `routes/messages.py:375-380` | `resolved_mode`, `settings.enable_deep_research` | 422 or continue | none | no | n/a | n/a | n/a |
| Explicit/heuristic decision | `services/streaming_service.py:352-362` | `mode`, `web_scraped`, `user_input` | `run_deep_research_step: bool` | none | no | n/a | n/a | n/a |
| Search | `tools/web_search.py:37 search_web()` | query, max_results | `List[{url,title,snippet[:500]}]` | DuckDuckGo HTTP via `duckduckgo_search.DDGS` (3 backends, up to 3 attempts, `time.sleep(0.5)` between) | no | **conditionally** (idempotent read, but no explicit timeout — bounded only by library/network defaults) | **no** — blocking, no cancel hook | partial: url/title/snippet only, **no rank/timestamp field**, **no dedup** |
| Scrape | `tools/web_scraper.py:56 _scrape_async()` via `56-114`, invoked sync at `117 _scrape_sync()` | url | plain text, truncated to `MAX_CONTENT_CHARS=16_000` | Playwright headless Chromium navigation (`NAVIGATION_TIMEOUT_MS=30_000`, `WAIT_TIMEOUT_MS=10_000`) | no | **yes for a fresh URL** (read-only fetch), but **no SSRF guard, no redirect revalidation, no content-type/response-size cap** — only post-extraction char truncation | **no** — blocking Playwright call inside `run_coro_sync`, no cancel/interrupt path | none — raw page text only, errors embedded as text (`f"Error scraping this page: {exc}"`) inline in the returned string |
| Context assembly | `tools/deep_research.py:24-64 run_deep_research()` | query | **one formatted string** (`=== Deep Research Context ... ===` blocks) | calls the above two | no | n/a (pure aggregation) | no | **no** — everything flattened into one string, url/title only as inline text |
| Injection | `services/streaming_service.py:368-370` | `research_context` | appended to `system_parts` | none | no | n/a | n/a | n/a — **placed in the SYSTEM PROMPT, not an untrusted envelope** |
| KG ingestion | `services/knowledge_graph.py:198 ingest_text_async()` | `research_context` | graph writes | spawns a **daemon thread**, **own `SessionLocal()` session**, bounded only by a concurrency semaphore | **yes, but on an untracked background thread outside any request/run transaction** | no ledger, no dedup, no retry tracking | n/a | n/a |
| Synthesis | LLM provider call inside `stream_chat_response` (legacy path, downstream of `ChatContext`) | `system_prompt` (containing research_context) + history | streamed tokens | provider API call (same provider as any chat turn — no separate paid dependency introduced) | no (Message persisted at the very end, existing legacy behavior) | attempt-once by convention | governed by existing legacy stream cancel (client disconnect only) | n/a |
| Progress to user | none | — | — | — | — | — | — | Confirmed: **zero progress events exist for deep research today** — `_agent_status_sse` (`streaming_service.py:407`) is emitted only on the multi-agent path; deep research is a silent blocking call before token streaming starts. |

## 3. Answers to the required evidence questions

1. **Return shape**: one formatted string only (`deep_research.py:24` return
   type is `str`). No structured objects survive past this function.
2. **Exact passages preserved?** No — the full page text (up to 16 000 chars)
   is embedded verbatim in the string, but nothing tracks which sentence/
   passage the model actually used, so no citation can be traced back to an
   exact excerpt.
3. **URLs validated before fetch?** No. `web_scraper.py:147` only checks the
   scheme prefix (`http://`/`https://`); no host/IP validation exists in this
   file at all.
4. **Private/link-local/metadata/mapped-IP forms blocked?** No, in
   `web_scraper.py`. Yes, but **only in a different, unused-by-deep-research
   module** — `services/browser_agent.py:89 assert_url_allowed()` does real
   `socket.getaddrinfo` resolution and rejects loopback/link-local/private/
   reserved/multicast/unspecified addresses plus the AWS/GCP metadata IPs
   (`_METADATA_IPS`), with an explicit allowlist
   (`settings.browser_ssrf_allowlist`). It is called only from
   `BrowserSession.navigate` (`browser_agent.py:199`) — never from
   `web_scraper.py` or `deep_research.py`.
5. **Redirects revalidated?** No, anywhere in the repo. Playwright's
   `page.goto` follows redirects internally with no interception hook in
   either `web_scraper.py` or `browser_agent.py`. This is a pre-existing gap,
   not something this phase can silently inherit as "safe."
6. **Page size / content-type / extraction size bounded?** Only extraction
   size, and only after the fact: `web_scraper.py:34 MAX_CONTENT_CHARS=16_000`
   truncates the extracted TEXT post-render. Nothing bounds the raw response
   size Chromium downloads/renders before that truncation, and nothing checks
   `Content-Type` before rendering.
7. **Search results deduplicated?** No — `search_web` returns `DDGS().text()`
   results as-is (`web_search.py:74-83`); `run_deep_research` takes
   `results[:scrape_limit]` with no URL/content dedup.
8. **Freshness/publication timestamps retained?** No — no timestamp field
   exists anywhere in the search or scrape result shapes.
9. **Source rank retained?** Only implicitly as list order; no explicit rank
   field is stored or passed forward.
10. **Disagreement/conflict between sources represented?** No — sources are
    concatenated as opaque text blocks; nothing structures agreement/conflict.
11. **Can a retrieved page inject instructions into the model context?**
    Yes, unmitigated today — raw scraped text is appended straight into the
    system prompt with no delimiter, no "this is data not instructions"
    framing, and no marker neutralization.
12. **System prompt or untrusted envelope?** **System prompt**
    (`streaming_service.py:370 system_parts.append(research_context)`) — the
    single most important finding this plan acts on. MP-3C3D2 already
    established the correct pattern for durable retrieval (RAG/memory
    context spliced as one delimited **user**-role message via
    `chat_stream._build_context_envelope`, `chat_stream.py:395-420`) — the
    durable deep-research plan reuses that exact envelope mechanism instead
    of the legacy system-prompt pattern.
13. **Does KG ingestion write asynchronously outside the request
    transaction?** Yes — confirmed at `knowledge_graph.py:198-216`: a daemon
    thread with its own `SessionLocal()`, gated only by a concurrency
    semaphore, no idempotency/dedup ledger.
14. **Can browser/search work be stopped on cancellation?** No — nothing in
    `web_search.py` or `web_scraper.py` accepts a cancellation token, checks
    an event, or is otherwise interruptible mid-call.
15. **Does any helper start its own event loop/thread/browser
    singleton/DB session?** Yes, three separate ones: `web_scraper.py`'s
    `run_coro_sync` bridges into `_scrape_async` via whatever loop-bridging
    strategy `app.services.async_bridge` implements (a fresh Playwright
    `async_playwright()`/Chromium launch **per call**, not shared);
    `browser_agent.py` maintains a **separate**, shared, session-scoped
    Chromium context (used only by the unrelated browser-agent/multi-agent
    feature, not deep research); `knowledge_graph.ingest_text_async` starts a
    daemon thread with its own session. None of these three are the same
    browser instance or session as each other.
16. **Crash-window behavior today (informal, since there is no durable run
    at all yet)**: the ENTIRE deep-research + chat turn is one in-process,
    synchronous call chain inside a single legacy HTTP request handler. A
    process crash at ANY point (before search, mid-search, mid-scrape, mid-
    synthesis, or after streaming some tokens but before the final `Message`
    commit) loses everything — there is no persisted intermediate state at
    all today. This is the baseline the durable design must strictly improve
    on, not match.
17. **Existing telemetry/status events available?** Only `agent_status`
    (`streaming_service.py:407-411`), and only on the **multi-agent** path —
    deep research emits nothing. The durable engine's own generic
    `run_status` `RunEvent` (already persisted, sequenced, and replay-safe —
    proven in MP-3C3B/3C3C) is the correct existing mechanism to carry new,
    bounded research-progress codes; the frontend's `useStreaming.ts`
    dispatcher already safely ignores unrecognized `run_status` values
    (`default: return false`), so new codes are additive-safe without a
    required frontend change for D4A/B.
18. **Raw exception text/page content in errors?** Partially yes, and it
    matters: `deep_research.py:57-62` embeds `f"Error scraping this page:
    {exc}"` **directly into the string sent to the LLM** (not a log, not a
    UI event — but still an uncontrolled channel). Logs elsewhere
    (`web_scraper.py:157`, `web_search.py:63`) use `logger.warning`/`.error`
    with the exception object, which is acceptable for server logs but must
    NOT be the pattern reused for any durable `status_reason`/event field —
    those must stay bounded slugs, exactly as `messages_stream.py`'s
    `unsupported_mode_reason` already does for mode errors.

## 4. Durable target contract

**Request mode**: `deep_research` (already canonical, MP-3C3D3A/B — no
change).

**Durable run kind**: `Run.kind = "deep_research"` — no new column needed.
`Run.kind` is `Column(String(24), nullable=False, default="chat_stream")`
with a comment enumerating `chat_stream|chat_sync|scheduled|sub_run` as a
**convention, not a DB constraint** (`models.py:931`); `"deep_research"` (14
chars) fits with no migration, and reusing the SAME column keeps one
worker-claim query path rather than a parallel run table. No client-supplied
handler/run-kind name is ever accepted — the kind is set server-side from
the already-validated, already-canonical `resolved_mode`, exactly as
`chat_stream` is today (`submission.py:286`).

### Proposed durable steps (smallest boundary that preserves crash safety)

Evaluated all seven candidates; **recommend five**, folding `research_plan`
into `search` (a query-generation sub-call, if used at all, is cheap,
attempt-once, and does not need its own crash-recoverable boundary — it can
be re-derived deterministically from the user's question on any restart
before search has produced results) and folding `source_normalize` into
`fetch_extract` (normalization is a pure, in-memory, deterministic
transformation of already-fetched content — it does not perform its own
external I/O and does not need an independent RunStep to be crash-safe; it
runs as the last sub-stage of `fetch_extract` before that step's own commit).
`optional tool approval/resume` is **not** included as a distinct step for
D4B/C: deep research has no MCP/tool-approval surface in the legacy pipeline
today (confirmed — no `[MCP_CALL]` or similar marker appears anywhere in
`deep_research.py`/`web_search.py`/`web_scraper.py`), so adding one would be
speculative, not evidence-based.

| RunStep kind | Input payload | Output payload | Encrypted vs non-secret | Started when | Retry class | Max attempts | Timeout/deadline | Cancel points | Lease heartbeat | Failure code | Crash window result | Restart resumes? | Output reused? |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `research_search` | `{query}` (from the user's message, resolved server-side, no client override) | sealed `RunPayload(payload_type="research_search_results_v1")`: bounded list of `{url,title,snippet,rank}` (see §5) | non-secret input; output sealed (query text is user-authored, not a secret, but still sealed for consistency with every other run payload) | on first worker claim of this step | **retry-safe** (idempotent read; DuckDuckGo has no auth/session state) | bounded (reuse existing worker backoff/ceiling pattern, e.g. 3, same class as `advance_chat_runs`' operational-error ceiling) | per-call bound = `search_web`'s own 3-backend×0.5s-backoff loop + network; a hard wall-clock deadline (new config, e.g. `research_search_timeout_s`) wraps the whole call so one slow backend can't stall the step indefinitely | before starting; between backend attempts | must renew before starting the blocking call, cannot renew during it — lease TTL for this step must exceed the hard deadline with margin | `research_search_failed` (bounded slug, no raw exception) | run stays `running`, step stays without a completed output row → next claim safely retries (no partial DB write to un-do — the whole call is a pure read) | yes — nothing partial to resume from; a safe full retry | no — a fresh search on retry is correct (results may legitimately differ; this step's output is not yet sealed as "final" until `fetch_extract` completes and the sealed snapshot is written) |
| `research_fetch_extract` | sealed search results from the prior step (read back, not re-fetched) | sealed `RunPayload(payload_type="research_sources_v1")` — the versioned, bounded structured-source snapshot (§5), **THIS is the "sealed" boundary the rest of the run must never recompute past** | output sealed/encrypted (may contain page text — user-adjacent, bounded, but still routed through the same envelope as every other RunPayload) | on first worker claim of this step | **partially retry-safe** — each individual page fetch is an idempotent read, but the STEP as a whole must be resumable per-page (see below), not restarted from page 1 on every retry | bounded per page (e.g. 2 attempts/page, same operational-error class) | hard per-page deadline = existing `NAVIGATION_TIMEOUT_MS(30s)+WAIT_TIMEOUT_MS(10s)` margin, PLUS a total-step deadline (new config, e.g. `research_fetch_total_timeout_s`) bounding the whole loop across all pages | before each page fetch; the loop MUST check cancellation between pages (never mid-Playwright-call, since Playwright's own call cannot be safely interrupted from Python without an unsafe hard thread-kill) | must renew before EACH page's blocking call, not during it — same lease-TTL-margin requirement as `research_search`, and additionally: partial per-page progress (which URLs are already fetched) must be checkpointed into the payload incrementally (or recomputed idempotently) so a restart does not re-fetch already-succeeded pages | `research_fetch_failed` / `research_fetch_partial` (if some but not all pages succeeded — see §5 "fetch outcome" field; a partial result with at least one successful page is still usable and sealed, not a hard failure) | if the crash is DURING a page fetch, that specific page's outcome is simply missing/unsealed on restart — the step re-claims and continues with the remaining pages, never re-fetching a URL whose outcome was already durably recorded | yes, incrementally, per already-recorded page outcome | **the FINAL sealed snapshot from this step is reused, never recomputed**, by every later step/restart/reconnect |
| `research_answer_synthesis` | sealed sources from `research_fetch_extract` (read-only) | streamed tokens + final citations list, atomically finalized with the `Message` (identical mechanism to MP-3C3D2's chat citation finalization) | non-secret (same trust level as any chat completion) | on first worker claim, only after sources are sealed | **attempt-once** — an LLM completion is not safely retry-idempotent (a partial stream cannot be blindly resumed without risking a duplicated/garbled answer; this matches the existing "attempt-once" convention already used for `llm_generate` in plain chat) | 1 (the existing chat-generation attempt-once convention, reused unchanged) | governed by the existing provider-call timeout/backoff already used for `llm_generate` — no new config needed | before starting; NOT mid-stream (matches existing chat-stream cancellation granularity — a cancel mid-generation already aborts as `"shutdown"`/`cancelled` per the existing MP-3C1/MP-1B contract, reused unchanged) | inherited unchanged from the existing chat generation loop's heartbeat behavior (frequent yields per streamed token) | inherited unchanged (existing bounded provider-error/timeout codes) | inherited unchanged from existing chat-stream crash handling → `outcome_unknown` next boot, same as today | yes, using the SAME sealed sources (never re-searched/re-fetched) | n/a — one attempt |
| *(atomic finalization)* | final assistant text + validated citations + sealed sources reference | `Message.citations` + `Run→succeeded`, one commit | non-secret (citations are user-facing) | — | n/a (part of the same commit as synthesis's terminal transition) | n/a | n/a | n/a | n/a | n/a | if the crash is AFTER streamed text but BEFORE this commit, the run stays non-terminal (`outcome_unknown` next boot) — **never a false success**, exactly like plain chat today | n/a | n/a |

Total: **3 RunStep kinds** (`research_search`, `research_fetch_extract`,
`research_answer_synthesis`), with finalization folded into synthesis's own
commit exactly as `_finalize_chat_success` already does for plain chat — no
fourth "finalize" step needed, matching the existing pattern precisely
rather than inventing a new one.

## 5. Structured source schema

New sealed `RunPayload(payload_type="research_sources_v1")` — a **versioned**
JSON envelope (same encrypted-Fernet-envelope mechanism as
`chat_stream.build_context_snapshot`, reusing `CONTEXT_SNAPSHOT_VERSION`-style
versioning under its own constant, e.g. `RESEARCH_SNAPSHOT_VERSION = 1`):

```
{
  "research_snapshot_version": 1,
  "query": "<the search query actually used>",
  "sources": [
    {
      "source_id": "s1",                  # stable within this run, used as the citation join key
      "url": "https://…",                  # validated via assert_url_allowed BEFORE this snapshot is sealed
      "title": "…",                        # bounded length
      "domain": "example.com",             # derived from url, for display without re-parsing
      "search_rank": 1,                    # 1-based position in that query's results
      "retrieved_at": "2026-…Z",           # server clock, always known
      "published_at": null,                # only if the page itself exposed one; NEVER guessed
      "fetch_outcome": "ok",               # ok | http_error | timeout | blocked | error (bounded enum)
      "passages": [
        {"passage_id": "s1p1", "text": "…", "content_hash": "sha256:…"}
      ]
    }
  ]
}
```

Fields **deliberately excluded** (considered, rejected — not "sounds
useful"): raw page HTML (no repository evidence anything downstream needs
it — the existing scraper already discards it after `inner_text`
extraction; keeping it would only grow the encrypted payload and the attack
surface for prompt injection review); a paid-model-derived relevance/
confidence score (violates free/local-only — see §8; a **cheap, local**
score such as "matched the query terms in title" is allowed if evidence
later shows it is useful, but is NOT designed here since nothing requires
it); source "disagreement" as a separate structured field (the model can
represent disagreement in its own synthesized answer text — structuring it
server-side would require a second LLM call to detect disagreement, which
this plan does not introduce).

**Bounded limits** (all as new `config.py` settings, following the exact
naming style of `deep_research_max_results`/`deep_research_max_scrape`):
`research_max_queries=1` (D4B/C do not need multi-query planning — the
user's own message is the query; evidence: legacy never generates multiple
queries either), reuse `deep_research_max_results` (search result count) and
`deep_research_max_scrape` (pages fetched) unchanged, `research_max_passages_per_page`
(new, e.g. 3), `research_max_passage_chars` (new, e.g. 1000 — passages are
excerpts, not full pages), `research_max_total_context_bytes` (new, a hard
ceiling on the serialized snapshot passed to synthesis), `research_max_citations`
(reuse the existing `_CITATION_MAX_COUNT = 10` pattern from `chat_stream.py`).

**Requirements mapping**:
1. Every passage supplied to synthesis has one `passage_id` → the
   synthesis prompt template must number/reference passages by that id so
   the model can only cite what it was actually given.
2/3. `chat_stream.validate_citations`' existing dedupe/bounds/schema-check
   pattern is reused, extended to require every emitted citation's
   `passage_id` to exist in the sealed snapshot — a citation to a
   non-existent `passage_id` is dropped, exactly like an uncited-passage
   filter today; **the model outputting a bare URL string never creates a
   citation** — citations are only ever constructed server-side from the
   sealed snapshot plus a validated `passage_id` reference the model
   returned, never from arbitrary model-generated text.
4/5. Sealed at the end of `research_fetch_extract`; every reconnect/restart
   of `research_answer_synthesis` re-reads the same sealed `RunPayload` —
   never re-searches/re-fetches, mirroring `read_context_snapshot`'s
   existing reuse pattern exactly.
6. Limits above.
7. Raw HTML excluded (see above) — no evidence anything downstream needs it.
8. Query/page content must never appear in `RunEvent.data`, `status_reason`,
   or logs — only bounded slugs, exactly as the existing mode-error and
   context-snapshot-error codes already do.
9. Citation `url` fields shown to the user come **only** from a
   server-validated `sources[].url` in the sealed snapshot — never from raw
   model output.
10. Citations are stored on `Message.citations` (existing `Text`/JSON
    column, unchanged shape once the `Citation` schema gains an optional
    `url`) **in the same atomic commit** as the run's terminal transition —
    identical mechanism `_finalize_chat_success` already uses for RAG
    citations today; no new atomicity primitive needed.

**Citation schema note (evidence, not a decision made here):**
`app.schemas.documents.Citation` (`schemas/documents.py:26-34`) currently
requires `document_id: int` and `chunk_id: int` — fields that make sense for
uploaded RAG documents but not web sources, and it has **no `url` field**.
`chat_stream.py:427-436` already anticipated this exact gap in its own
comment: *"Deliberately NO url field... a citation URL needs a PROVEN SSRF
guard... When a URL-bearing citation source is added, route its URL through
`assert_url_allowed` BEFORE sealing the context snapshot at submission time
(outside any atomic transaction), never re-implement URL validation here."*
D4B must extend this Pydantic schema (optional `url: Optional[str] = None`,
and relax `document_id`/`chunk_id` to `Optional[int]`) — this is a
**schema-only** change (no DB migration; the column stays `Text`/JSON) and
is explicitly deferred to D4B's own implementation, not decided in this
planning document beyond confirming it is additive/backward-compatible.

## 6. Trust/security boundaries

**Untrusted envelope, not system prompt.** Reuse
`chat_stream._build_context_envelope`'s exact pattern
(`chat_stream.py:395-420`, `_CONTEXT_ENVELOPE_HEADER`/`_FOOTER`) — research
passages are spliced into the SAME kind of delimited **user**-role message
already used for RAG/memory context, never the system prompt. This is a
direct, evidenced fix for the single worst legacy defect found in §3 Q12.

**MarkerScanner is not sufficient — confirmed by evidence, not assumption.**
`chat_stream.MarkerScanner.feed()` (`chat_stream.py:175`) consumes
**streaming MODEL OUTPUT** chunks; it has no code path that ever sees
retrieved/fetched INPUT text before it reaches the model. A page containing
literal marker-shaped substrings (e.g. a page whose text contains
`[MCP_CALL: ...]` verbatim, quoting documentation or an attacker-planted
string) would reach the model as ordinary context, UNSCANNED, and if the
model were tricked into echoing it back, only THEN would MarkerScanner see
it — too late to prevent the model from having been influenced, and
irrelevant to whether the page's raw text should have been neutralized on
the way in. **Recommendation**: a separate, new input-side sanitization
step applied to every passage before it enters the envelope — neutralize
(not destructively delete) any substring matching the SAME
`ACTION_MARKERS`/`_REPO_MARKER_OPENS` opener set already defined in
`chat_stream.py:78`, by inserting a zero-width break or escaping the
bracket (e.g. `[MCP_CALL` → `[MCP_CALL` with a inserted marker that breaks
the exact-match, documented inline as intentional) so the literal marker
opener can never survive verbatim into the envelope, while leaving all
other page text completely intact — this is encoding/neutralization, not
deletion of ordinary content, per the explicit instruction not to
destructively delete text without evidence it's necessary.

**Web content can never trigger actions.** Already true by construction once
the envelope pattern above is used: the worker's marker detection
(`MarkerScanner`) only ever scans the MODEL's own output stream, and
`chat_stream`'s existing execution-marker dispatch
(`extract_mcp_calls`/tool dispatch) only fires on markers the scanner itself
captured from that output stream — retrieved page text, being routed through
the untrusted user-envelope and never through the model's own output
channel, cannot itself open an execution marker. The existing approval/
gateway rules (MP-2) remain the only path to any tool execution, unchanged.

## 7. Retry/cancel/crash matrix (see also §4's per-step table)

- **Fresh session-family validation**: reuse the existing
  `validate_session_binding` check already performed at claim time and
  before each external stage in `messages_stream.py`/`worker.py` — no new
  mechanism, just applied at the start of each of the 3 new RunStep kinds.
- **Cancel-before-invocation ⇒ zero external calls**: achieved by checking
  the existing cooperative-stop mechanism (`RunWorker.attach_stop`/`_stopping()`)
  immediately before each search call and before each page fetch — identical
  pattern to the existing `claim_next_queued`/`advance_chat_runs` checks.
- **Cancellation granularity**: between queries, between pages, and before
  synthesis starts — **never mid-DDGS-call, never mid-Playwright-call**,
  because neither library exposes a safe interrupt from Python (confirmed by
  code inspection: no cancellation token, event, or interrupt parameter
  exists on either `DDGS().text()` or `page.goto()`). This is an explicit,
  evidenced limitation, not an oversight.
- **Lease heartbeat during blocking work**: cannot be renewed DURING a single
  blocking call (the worker thread is synchronously blocked). Mitigation:
  bound every external call with a hard wall-clock deadline strictly less
  than the run's lease TTL (new config, with a safety margin), so the lease
  cannot expire mid-call under normal operation; if a call somehow still
  overruns (a hung DNS resolution the library's own timeout didn't catch),
  the worker does **not** claim cancellation succeeded — it marks the step
  `outcome_unknown` on restart, exactly like the existing "uncooperative
  sync provider" limitation already documented for MP-3C3A (residual,
  accepted, future MP-6 item: a separate worker process would fix this fully).
- **No second invocation on a possibly-still-running call**: the worker never
  re-claims and re-starts `research_search`/`research_answer_synthesis` for a
  run whose lease merely expired without independent evidence the prior
  attempt is truly dead (matches the existing claim/lease-owner/version-CAS
  logic in `worker.py` unchanged — this plan does not weaken it).
- **Read operations are retry-safe by explicit reasoning**: `research_search`
  and each individual page fetch inside `research_fetch_extract` are pure
  reads with no side effects on the target server beyond a normal HTTP GET —
  reusing a URL/query never mutates external state, so re-attempting is safe;
  this reasoning does NOT apply to synthesis (an LLM call, attempt-once).
- **Persisted snapshot prevents result drift on retry**: once
  `research_fetch_extract` seals `research_sources_v1`, EVERY subsequent
  read (synthesis, reconnect, restart) uses that exact sealed snapshot —
  search results are never re-fetched to "refresh" them, so a retried
  synthesis never sees different sources than the first attempt would have.
- **Disconnect never cancels** (unchanged existing durable-run invariant —
  reused, not modified).
- **User cancel is a durable signal** — reuse `POST /api/runs/{id}/cancel` +
  the existing `RunSignal`/cancel-requested mechanism unchanged; the only new
  work is checking it at the additional cancellation points listed above.

## 8. Free/local-only provider inventory

| Component | Provider | Cost/auth | Verdict |
|---|---|---|---|
| Search | DuckDuckGo via `duckduckgo-search` PyPI package | No API key, no auth (`web_search.py:4-5`, confirmed) | **Free/local-compliant** |
| Scrape | Local headless Chromium via Playwright | Runs on the server's own machine, no external service | **Free/local-compliant** |
| Synthesis | Whatever LLM provider the run already resolves (`resolved_provider`/`sess.model`, same mechanism as any `chat_stream` run — `messages_stream.py` already documents this) | Governed entirely by the user's/deployment's EXISTING provider configuration (Ollama local, or a configured OpenRouter/etc. key) | **No NEW paid dependency introduced by deep research itself** — it reuses whatever the deployment already uses for plain chat, unchanged |

No hidden paid search API, no implicit paid-model fallback, and no external
telemetry SaaS is introduced anywhwere in this plan. **Failure policy when
DuckDuckGo rate-limits or fails**: `search_web` already degrades to an empty
list (`web_search.py:70-72`) — the durable `research_search` step must treat
an empty/failed search result as a **truthful bounded failure**
(`research_search_no_results` — not an error, a legitimate "nothing found"
outcome the model can still respond to honestly) rather than silently
falling back to a paid search API or a paid model. This must be stated
explicitly in D4B's implementation so a future contributor is not tempted to
add a "fallback" provider.

## 9. Progress/SSE contract

Reuse the existing `RunEvent`/`run_status`-style mechanism (already
persisted, sequenced, and replay-safe per MP-3C3B/3C3C) with five new
bounded, server-owned status codes:
`research_planning` (the brief pre-search moment when the run has claimed
the step but not yet issued a query — folded per §4's decision, so brief
this may not need its own code if `research_searching` starts immediately;
D4B decides based on measured latency), `research_searching`,
`research_fetching`, `research_synthesizing`, `research_completed`. None of
these carry a raw query, URL, page title, page content, provider exception,
or secret — exactly the same bounded-slug discipline as
`unsupported_mode_reason`. No percentage is emitted (no real denominator
exists — the number of pages to fetch is only known after search completes,
and even then a partial-fetch outcome makes "N of M done" misleading rather
than informative). Sequenced via the existing `run_seq`/replay mechanism —
no new ordering primitive. The frontend's existing `useStreaming.ts`
`run_status` dispatch already safely ignores codes it doesn't recognize
(`default: return false`), so **no required frontend change** for these new
codes to be safely ignored in D4B; a friendly-label mapping is a D4C
nice-to-have, not a correctness requirement.

**MP-3C3D3B capability change needed?** Not until the handler actually
exists and is enabled. `routes/settings.py::_compute_chat_mode_capabilities()`
must gain exactly one new condition: `deep_research` becomes available (when
durable cutover is ON) only once a NEW, explicit "handler ready" signal is
true — e.g. a dedicated flag/constant analogous to
`_SUPPORTED_DURABLE_MODES` being extended to include `"deep_research"` only
after D4C ships and is deliberately turned on. **This capability change is
explicitly D4C's job, not D4A's or D4B's** — D4B ships with `deep_research`
still reported unavailable under durable cutover, truthfully, because the
handler is not yet fully executable end-to-end.

## 10. Knowledge-graph and memory decision

**Defer KG ingestion.** Evidence: `ingest_text_async`
(`knowledge_graph.py:198-216`) is fire-and-forget on a daemon thread with its
own untracked `SessionLocal()` session, gated only by a concurrency
semaphore — no idempotency ledger, no dedup, no retry tracking, and no
transactional relationship to the run at all. It cannot be safely
represented as a durable, crash-recoverable, at-most-once step without
building a brand-new ingestion ledger, which is unevidenced scope creep for
this feature. Per the explicit instruction ("if it cannot be transactionally
retried/deduplicated, recommend deferral"), KG ingestion for durable deep
research is **deferred** — not silently dropped, but explicitly out of scope
for D4B/C, to be revisited only if a future phase builds a genuinely durable
KG-ingestion mechanism generally (not deep-research-specific). No
SAVE_MEMORY/SAVE_DIRECTIVE write activation is added here, matching the same
posture MP-3C3D2 already took for those markers.

## 11. Migration decision

**No migration.** Every new need is representable with existing columns:
- `Run.kind = "deep_research"` — existing unconstrained `String(24)` column,
  comment already anticipates new values (`models.py:931`).
- `RunStep.kind ∈ {research_search, research_fetch_extract,
  research_answer_synthesis}` — existing unconstrained `String(32)` column
  (`models.py:1006`), same convention-not-constraint pattern.
- `RunPayload.payload_type = "research_sources_v1"` (and
  `"research_search_results_v1"`) — the existing generic encrypted-envelope
  store (`models.py:1116+`) already keys on `(run_id, step_id, payload_type,
  action_hash)`; adding new payload-type strings requires no schema change,
  exactly like `CONTEXT_SNAPSHOT_VERSION`'s sealed context snapshot did for
  MP-3C3D2.
- `RunEvent.category="control"` with new bounded status codes — existing
  column, no new category needed.
- `Message.citations` — existing `Text`/JSON column
  (`models.py:149`), unchanged shape once `Citation` gains an optional `url`
  (a Pydantic schema change, not a database migration).

The ONLY schema-adjacent change is the Pydantic `Citation` model gaining an
optional `url` field and relaxing `document_id`/`chunk_id` to optional —
this is an in-memory validation schema, not a database table, and is
explicitly D4B's job to implement and test, not decided further here beyond
confirming it needs no Alembic-style migration.

## 12. D4B/C implementation plan

Two phases, per the instruction to prefer two over three. The suggested
D4B/D4C split from the task is evaluated and **accepted as proposed** — it
does not create an unfinishable/placeholder run: D4B's runs are real,
complete, terminal (`succeeded`/`failed`/`cancelled`) durable runs that stop
BEFORE the public-facing synthesis/citation surface is turned on for real
traffic (they are exercised only via direct, flag-gated internal testing,
never reachable through the public capability contract until D4C flips it),
so no run is ever left in a state nothing can terminate.

### MP-3C3D4B — structured source acquisition + sealed snapshot + durable progress/cancel/recovery (not yet publicly executable)

- **Objective**: implement `research_search` and `research_fetch_extract`
  as real `RunStep`s with sealed `research_sources_v1` output, full
  cancellation/lease/crash-recovery behavior, and bounded progress events —
  but `deep_research` remains rejected by
  `unsupported_mode_reason`/capability advertising exactly as today (the new
  sweep function exists and is unit-tested directly, not yet wired into the
  public routing gate).
- **Expected files**: `backend/app/tools/research_search.py` (new —
  wraps `search_web` with SSRF-safe validation deferred to fetch, bounded
  timeout, bounded result shape), `backend/app/tools/research_fetch.py`
  (new — wraps `web_scraper`'s core scrape logic with `assert_url_allowed`
  called per URL BEFORE fetch, per-page timeout, passage extraction/bounding,
  content-hash, marker neutralization), `backend/app/run_engine/research.py`
  (new — the sealed-snapshot builder/reader, mirroring
  `chat_stream.build_context_snapshot`/`read_context_snapshot`'s exact
  pattern), `backend/app/run_engine/worker.py` (add
  `advance_research_runs()` sweep function, mirroring `advance_chat_runs`'s
  claim/lease/heartbeat/cancel structure), `backend/app/schemas/documents.py`
  (extend `Citation` with optional `url`), `backend/app/config.py` (new
  bounded-limit settings, all defaulting to safe small values).
- **Schema/migration impact**: none (see §11).
- **Acceptance tests** (see §13 for the full matrix — D4B's slice): request
  validation/zero-persistence for invalid input, one run per idempotency
  key, cancel before search, cancel between fetches, stalled fetch with
  lease heartbeat, expired lease while a call may still be running, crash
  before/after source seal, restart reuses sealed sources, bounded
  page/context sizes, source-conflict representation is NOT attempted here
  (deferred to D4C's synthesis), raw error/content privacy, revoked session
  before/during research, free-only/no-paid-fallback enforcement.
- **Security tests**: invalid/unsafe URL rejection (SSRF), redirect-to-
  private-address rejection (to the extent Playwright's own redirect
  handling can be tested — if it cannot be intercepted, this test documents
  the known gap rather than asserting a false guarantee), prompt-injection
  content cannot trigger tools, marker strings in web content remain inert.
- **Stop conditions**: if `assert_url_allowed`'s live DNS resolution cannot
  be safely called outside the atomic staging area (it must be — this is
  already the documented pattern from `chat_stream.py:430-436`; if evidence
  during implementation contradicts this, STOP and ask); if Playwright
  offers no way to bound total response size before the content leaves the
  browser process (STOP and ask whether a bounded-size proxy/interceptor is
  in scope or whether the existing post-truncation approach is accepted as
  the residual known gap).
- **What remains disabled**: public routing (`unsupported_mode_reason` still
  rejects `deep_research`), capability advertising (still `unavailable`),
  KG ingestion (deferred), synthesis (D4C).
- **Focused verification** (to run when D4B is implemented, NOT in D4A):
  `PYTHONPATH=backend pytest tests/test_run_engine_mp3c3d4b.py -q`,
  `compileall` on changed files, `git diff --check`, `import app.main`.
- **Estimated risk**: **medium** — the SSRF/redirect/timeout work touches a
  genuinely unguarded surface today, but it reuses proven primitives
  (`assert_url_allowed`, the sealed-payload pattern, the worker sweep
  pattern) rather than inventing new ones.

### MP-3C3D4C — synthesis streaming + exact citations + public capability activation

- **Objective**: implement `research_answer_synthesis` (reusing the
  existing chat-generation/streaming/finalization machinery against the
  sealed sources via the untrusted envelope), wire `deep_research` through
  `unsupported_mode_reason`'s allowlist, and update
  `_compute_chat_mode_capabilities()` to advertise it — only once D4B is
  proven solid.
- **Expected files**: `backend/app/run_engine/messages_stream.py` (extend
  `_SUPPORTED_DURABLE_MODES`), `backend/app/run_engine/worker.py` (finalize
  synthesis + citations in the existing atomic commit path), `backend/app/
  routes/settings.py` (capability truth table gains the "handler ready"
  condition from §9), `backend/app/run_engine/chat_stream.py` (extend
  `validate_citations`/context envelope building to accept research
  passages alongside RAG/memory, reusing the same envelope function).
- **Schema/migration impact**: none.
- **Acceptance tests**: no second synthesis call on retry, exact
  passage/citation correspondence, final Message/citations atomicity,
  provider error/timeout (reusing the existing chat-generation test
  patterns), SSE replay with no duplicate progress/text, capability remains
  unavailable until every D4C dependency is actually ready (test this
  negatively too — flag-partial states must still report unavailable).
- **Security tests**: source-conflict representation does not fabricate
  false certainty, citation URL never comes from raw model output.
- **Stop conditions**: if reusing the existing attempt-once
  `llm_generate`-style retry semantics for synthesis proves unsafe for the
  longer/larger research context (e.g. token-budget concerns not present in
  plain chat), STOP and ask before silently chunking or summarizing sources
  server-side beyond the bounds already specified.
- **What remains disabled**: nothing new — this is the phase that turns the
  feature on end-to-end, still behind `run_messages_stream_durable_enabled`
  (OFF by default) exactly like every other durable mode.
- **Focused verification**: `PYTHONPATH=backend pytest
  tests/test_run_engine_mp3c3d4c.py -q`, capability tests extended, full
  D4B+D4C regression subset, `compileall`, `git diff --check`,
  `import app.main`.
- **Estimated risk**: **medium-high** — this is the phase that makes
  scraped web content reach a real LLM call in a durable context; the
  untrusted-envelope/marker-neutralization work from §6 must be verified
  here with adversarial content, not assumed correct from D4B's plumbing
  alone.

No D4D is recommended — two phases are sufficient; a third phase was
considered only for KG ingestion, which is deferred entirely (§10), removing
the need for it.

## 13. Focused test matrix (specified now, executed in D4B/C — none run in D4A)

| # | Test | Phase |
|---|---|---|
| 1 | request validation and zero persistence for invalid input | D4B |
| 2 | one run per idempotency key | D4B |
| 3 | cancel before search → zero external calls | D4B |
| 4 | cancel between fetches → no further pages fetched | D4B |
| 5 | stalled fetch with lease heartbeat renewed before the call, not during | D4B |
| 6 | expired lease while the underlying call may still be running → no double-invocation | D4B |
| 7 | crash before source seal → restart safely retries | D4B |
| 8 | crash after source seal → restart reuses sealed sources, never re-fetches | D4B |
| 9 | restart reuses sealed sources (explicit byte-for-byte snapshot equality check) | D4B |
| 10 | no second synthesis call on any retry/reconnect | D4C |
| 11 | exact passage/citation correspondence (every citation ↔ a supplied passage, no orphans) | D4C |
| 12 | invalid/unsafe URL rejection (loopback/link-local/private/metadata) | D4B |
| 13 | redirect-to-private-address rejection (or documented as a known residual gap if untestable) | D4B |
| 14 | prompt-injection content cannot trigger tools (MCP/python/shell/browser/memory/directive/computer) | D4C |
| 15 | marker strings in web content remain inert (neutralized, not deleted) | D4B/C |
| 16 | bounded page/context sizes enforced (size caps, passage caps, citation caps) | D4B |
| 17 | source conflict representation (model may express disagreement; no fabricated consensus) | D4C |
| 18 | raw error/content privacy (no raw exception/page text in events/logs/refs) | D4B |
| 19 | revoked session before/during research → fails closed | D4B |
| 20 | SSE replay with no duplicate progress/text events | D4B/C |
| 21 | final Message/citations atomicity (single commit, never partial) | D4C |
| 22 | provider error/timeout during synthesis handled truthfully | D4C |
| 23 | free-only/no-paid-fallback enforcement (empty search result ≠ silent paid fallback) | D4B |
| 24 | capability remains unavailable until the handler is fully executable (including partial-readiness states) | D4C |

## 14. Risks and stop conditions

- **Highest risk**: the untrusted-envelope + marker-neutralization boundary
  (§6) is new design, not a direct port of an existing mechanism — it must
  be built and adversarially tested in D4B/C, not assumed safe by analogy
  to MarkerScanner (which this plan proves does not cover the input side).
- **Second risk**: Playwright/DDGS calls cannot be safely interrupted
  mid-flight — the lease-heartbeat/cancellation design in §7 depends on
  every external call having a hard bounded timeout strictly under the
  lease TTL; if a future dependency upgrade removes or weakens those
  timeouts, the safety argument breaks silently. D4B should assert these
  timeouts explicitly in tests, not just rely on the current constants.
- **Third risk**: redirect revalidation is a known, currently-unaddressed
  gap in the existing codebase (not introduced by this plan) — D4B must
  either close it or explicitly document it as an accepted residual gap;
  it must never be described as solved without evidence.
- **Stop condition already flagged in §12**: if `assert_url_allowed`
  cannot safely run outside the atomic staging area, or if Playwright
  offers no bounded-size guarantee before content leaves the browser
  process.

## 15. Exact unresolved questions requiring a user decision

None block this plan from being accepted as-is. Two implementation-detail
choices are flagged for confirmation before D4B begins coding (not
blocking this plan's approval):

1. Whether `research_planning` needs its own SSE status code or can be
   folded into `research_searching` (§9) — a measurement-driven decision
   best made when D4B has real latency numbers, not now.
2. Whether the "redirect-to-private-address rejection" gap (§3 Q5, §14)
   should be closed in D4B (adding redirect interception/re-validation) or
   explicitly accepted as a documented residual gap shared with the
   pre-existing `browser_agent.py` — this affects D4B's estimated risk and
   scope and is worth an explicit decision before implementation starts.
