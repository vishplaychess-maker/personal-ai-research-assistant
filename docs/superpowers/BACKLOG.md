# Thunder AI - backlog and deferred items

Owner-visible, precise entries for work that is NOT part of an active slice.
Nothing here is a completion claim. The PRD is authoritative for milestone
status; this file only records the concrete items and their owning milestone.

## DEFERRED (not PASS)

### D-1 - MP-4C manual human Deny journey (browser)

- **Status: DEFERRED by the owner.** Do NOT record it as PASS and do NOT let it
  block independent development.
- What was proven: the automated and API-level denial path is green (MP-4C
  journeys 1, 3, 4 passed via Playwright-driven browser journeys; the Deny click
  produced `denied` with zero dispatch rows and zero executor calls).
- What is NOT proven: a **human** click-through of Deny in the UI against the
  disposable stack, on the owner machine.
- Unblocks when the owner runs `docs/mp4c_manual_test_guide.md` section 7.

## Open issues found during testing

### B-1 - Settings does nothing when no session is active (frontend)

- **Symptom:** clicking the sidebar Settings button while no conversation is
  active produces no dialog at all; it works once a session exists.
- **Root cause (source-pinned):** `frontend/src/App.tsx:749` renders the modal as
  `{showSettings && activeSession && (`, while the handler at
  `frontend/src/App.tsx:608` sets `showSettings(true)` unconditionally. With no
  active session the state flips and nothing renders.
- **Fix options (decide when picked up):** either render `Settings` without
  requiring `activeSession` once it is confirmed the component tolerates a null
  session id, or keep the guard and disable the trigger with an explanation.
- **Owning milestone: a frontend/UX slice.** Not MP-5 (the non-stream/scheduler
  engine path) and not MP-4C. Left un-fixed here deliberately.

### B-2 - A generic provider error hides an upstream OpenRouter 429

- **Symptom:** when OpenRouter rate-limits, the durable run reports the bounded
  reason `provider_error` and the UI shows "The model provider returned an
  error."; the operator cannot tell it was a 429 (or whether retrying helps).
- **Root cause (source-pinned):** the provider DOES carry the status
  (`backend/app/services/llm_providers/openrouter_provider.py:114,170,218,305`
  raise `OpenRouter returned HTTP {status}`), but the durable path collapses any
  provider failure to the single reason `provider_error`
  (`backend/app/run_engine/worker.py:2066,2677,3150`) and renders the fixed
  detail at `backend/app/run_engine/messages_stream.py:509`. The collapse is
  intentional (bounded, non-echoing reasons), so the fix is CLASSIFICATION, not
  echoing upstream text.
- **Fix direction:** add a bounded reason such as `provider_rate_limited` (plus a
  bounded retry hint) to the run-engine reason vocabulary and classify an
  upstream 429 into it; keep every other provider failure as `provider_error`.
- **Owning milestone: MP-6 (hardening + hosted verification / observability).**
  Not MP-5. Left un-fixed here deliberately.
- **Not diagnosed - do not act on one occurrence:** one OpenRouter browser
  journey run failed with the generic bounded reason `provider_error` (the UI
  showed "The model provider returned an error." and a Retry button, and no
  assistant message was persisted). The logs did not establish the cause; a
  direct probe of the same model returned HTTP 200. No retry logic was added.

### B-3 - The legacy `youtube_summarizer` tool is dead against the pinned library

- **Symptom:** the `@tool youtube_summarizer` returns a transcript error for
  EVERY real URL, because `backend/app/tools/youtube_summarizer.py:68` calls the
  classmethod `YouTubeTranscriptApi.get_transcript(video_id)`.
- **Evidence (live):** the installed/pinned `youtube-transcript-api==1.2.4`
  exposes only `fetch`/`list` on an INSTANCE - `hasattr(YouTubeTranscriptApi,
  "get_transcript")` is False - while `YouTubeTranscriptApi().fetch(id)` returned
  61 snippets.
- **Fixed in this slice, both paths.** `youtube_transcript_context._default_fetch`
  probes both API surfaces, and the legacy tool now DELEGATES its whole fetch to
  that module (`fetch_transcript` -> `load_transcript`) while keeping its
  free-text contract: transcript text (truncated at 4000 with the existing
  marker), `No transcript available for video <id>.`, or a bounded
  `Error fetching transcript for <id>: <ExceptionType>` (the upstream message is
  deliberately no longer echoed). No second fetch implementation exists.
- **Evidence:** `tests/test_youtube_summarizer_tool.py` (9 tests, tool path
  executed, plus a library bomb proving no direct call) and a LIVE probe
  (`secrets/yt_tool_live_probe.log`: 2089 chars, no error string).
- **Owning milestone: done in this slice.**

### B-4 - A saved provider key was invisible to session-model discovery (FIXED)

- **Symptom (reproduced live on the disposable stack):** after saving an
  OpenRouter key with `PUT /api/settings` - exactly what the Settings UI does -
  `PATCH /api/sessions/{id}/model` answered `503 Unable to verify the model
  because no provider model discovery is available`. The same key worked only
  once it was ALSO placed in a global env file.
- **Root cause (source-pinned):** `fetch_available_chat_models`
  (`backend/app/routes/sessions.py:36`) read `list_user_providers` (the
  `user_providers` table) and then fell back to the GLOBAL provider. The Settings
  UI writes the legacy `user_settings` row via `save_user_settings`, so that
  saved key was never consulted even though the run path could use it.
- **Fix:** discovery now also resolves the authenticated user saved key through
  `get_user_llm_config(db, user_id)` - the SAME secure resolver the run path uses
  (active `user_providers` row -> legacy `user_settings` row, per user,
  decrypted, with the intentional env-key fallback). The global/env path is
  unchanged and still works. No key is returned or logged.
- **Evidence:** `tests/test_session_model_discovery_saved_provider.py` (8 tests):
  saved-key success, another user isolation, missing credentials -> 503,
  contained discovery failure, global-env path preserved, and the raw key absent
  from every response and log. RED proof: with the fix disabled the two
  saved-key tests fail with the original 503 (2 failed, 6 passed); GREEN: 8
  passed.
- **Owning milestone: done in this slice.**

## MP-5 - Non-streaming path + scheduler on the engine

Authoritative scope: `docs/superpowers/plans/2026-09-08-phase-1b2r-durable-run-resume-architecture.md:1296-1302`.

Slice status:

1. **Sync wait-for-terminal adapter - DONE (additive, no behaviour change).**
   `backend/app/run_engine/sync_wait.py` + `tests/test_mp5_messages_sync_adapter.py`.
   It creates/advances nothing, reuses the one terminal vocabulary, and fails
   closed on its deadline.
2. **`/messages` cutover behind a new default-False flag - NOT STARTED.** It is
   blocked on retiring the legacy terminal-approval encoding
   (`backend/app/routes/messages.py:142-174`, the bespoke `resume_from` resume and
   `_check_pending_approval`), which MP-4A gates on MP-4C + MP-4D. Splitting it
   away from that retirement would leave two approval authorities, so it is
   deliberately not done yet.
3. **`scheduler_service` on the engine - NOT STARTED.** Same blocker plus the
   "scheduled task reaches `waiting_for_approval` and can be approved" test.
4. **Deletions (`run_research_workflow` bespoke `resume_from`, citations-JSON
   approval encoding, `langgraph_workflow` retirement) - NOT STARTED, gated on
   MP-4C completion + MP-4D.**

## Not scheduled here

- YouTube transcript feature - **PARTIAL SLICE, IN PROGRESS, not complete.** The
  live durable `/messages/stream` path now resolves a supported YouTube URL
  pasted into the message into a bounded, timestamped, untrusted transcript
  context block (`backend/app/tools/youtube_transcript_context.py`,
  `backend/app/run_engine/messages_stream.py`,
  `chat_stream.build_context_snapshot`), proven by
  `tests/test_youtube_transcript_submission_wiring.py` plus a LIVE fetch probe.
- **Browser gate - DONE (automated, no manual step).** On the rebuilt
  disposable `mp4c-uitest` stack (Ollama `llama3.2:3b`, local/free) the UI at
  `http://127.0.0.1:15173` was driven by Playwright: (1) a real supported URL
  plus a question whose answer is in the transcript produced the correct lyric
  and real `[mm:ss]` citations; (2) a supported-looking URL whose transcript
  cannot be read produced a plain "could not retrieve any content or transcript"
  answer with nothing invented. The fetch was LIVE, not mocked. Evidence:
  `secrets/yt_browser_journey_evidence.log`, `secrets/yt_transcript_evidence.log`,
  `secrets/yt_run_evidence.log` (sealed `transcript_status=ok` / `fetch_failed`
  on the two real Runs). The MP-5 `/messages` cutover is still NOT required for
  this and is untouched.
- The Vite dev-proxy SSE concurrency defect is FIXED and regression-tested; the
  previous branch-failure classification was replaced by a proven root cause.

## MP-4D - zero-reference deletion and closeout

Authoritative scope: `docs/superpowers/plans/2026-09-17-phase-1b2r-mp4a-convergence-audit-plan.md:211-213`.

- **Status: BLOCKED, no deletion performed.** MP-4D deletes "only what is proven
  unreferenced after 4B/4C", and its preconditions are "import/call-site proofs,
  no dead flags, focused regression green". MP-4C is NOT complete and its Deny
  evidence is DEFERRED, so the "after 4C" precondition is unmet.
- Additional unmet gates from the same plan: STOP conditions 2/8/9/10 require
  authorization before deleting an externally imported module or anything still
  reachable from the UI/API, and required decision 4 says the D6A risk-9 residual
  must be accepted or scheduled **before any legacy deletion**.
- **Delivered instead (additive, deletes nothing):
  `tests/test_mp4d_retirement_census.py`** - the reference census and the
  NO-DEAD-FLAGS proof that MP-4D names as its own preconditions. It records which
  modules reference each legacy retirement candidate
  (`run_research_workflow`, `stream_chat_response`, `stream_multi_agent_response`),
  proves none is reachable from `backend/app/run_engine/` (the MP-4B boundary
  MP-5 depends on), and proves no `run_*` Settings flag is dead.
- To unblock the deletion slice: complete MP-4C formally, decide required
  decisions 3 and 4, and authorize a deletion slice under STOP condition 2.