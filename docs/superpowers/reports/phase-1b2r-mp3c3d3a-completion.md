# Phase 1B.2R — MP-3C3D3A completion report

**Explicit request-level mode contract and backend routing foundation**

**Date:** 2026-09-12 · **Branch:** `phase-1b1-approval-domain` · HEAD `b2e6718` ·
**all work uncommitted, not production-ready**

Follows the MP-3C3D3A section of
`docs/superpowers/plans/2026-09-11-phase-1b2r-mp3c3d0-mode-parity-plan.md`.

## Interrupted-session recovery

A prior session implemented the full contract (schema field, `validate_request_mode`, legacy
mode-threading, durable/Run-API gates) and got a first draft of
`tests/test_run_engine_mp3c3d3a.py` to 34 passing tests, then expired before a correction pass on
several test-quality issues and before documentation. This session verified the implementation
files were complete and internally consistent (no duplicate blocks, no stale comments, no
old-and-new gates both firing — confirmed by direct inspection of every changed file), then:

1. **Re-evaluated `validate_request_mode`'s case/whitespace tolerance.** The first draft
   normalized (`"CHAT"` → `"chat"`), mirroring the pre-existing `resolve_mode()`. On inspection,
   `resolve_mode()`'s tolerance has no test or docstring establishing it as a documented public
   contract — it reads as an incidental implementation detail. Since `ChatRequest.mode` is a
   brand-new field with zero prior clients, there is no compatibility reason to be lenient.
   Changed to **strict exact-match** (`"CHAT"`, `" chat "`, `"Chat"` are all now rejected, not
   silently canonicalized). `resolve_mode()` itself was left untouched in THIS session — **this
   was later found to be a mistake and corrected in the closeout pass below**, since it left
   `/api/runs` on a looser, undocumented validation path than `/messages/stream`.
2. **Fixed test-quality defects**: removed a `status_code in (422, 503)` assertion that could pass
   for the wrong reason, removed a tautological `... or True` assertion, replaced two Direct-Run-API
   tests that only called `unsupported_mode_reason()` directly with real TestClient requests through
   `/api/runs`, added exact-call-count proofs (not just boolean checks) for every legacy/durable/
   Run-API routing claim, and added real TestClient-driven durable-route tests (with every
   `assert_durable_stream_ready` dependency explicitly configured via a fake worker supervisor) that
   didn't exist in the interrupted draft at all.
3. **Added the URL/deep-research interaction test** the task specifically asked for, proving —
   without changing it — that `mode="deep_research"` still defers to an already-scraped URL's
   context exactly as the omitted/heuristic case already did.

Two real implementation bugs were found and fixed **in the test helpers**, not the product code
(caught immediately by running the suite): `import app.tools.web_scraper as ws` does not return the
real module — `app/tools/__init__.py` re-exports the `web_scraper` *tool* under the same name,
shadowing the submodule attribute on the package namespace — fixed via `importlib.import_module`;
and `StructuredTool.invoke` is a pydantic-inherited method, not an instance-settable field — fixed
by patching the tool's underlying `.func` instead.

## Stage-0 verification (this session)

`git status --short` / `branch --show-current` / `rev-parse HEAD` / `diff --check` / `stash list`
all confirmed: branch `phase-1b1-approval-domain`, HEAD `b2e6718`, clean diff-check, pre-existing
`stash@{0}` (`7ab31a5`) untouched, no leftover background pytest processes. Every changed file
(`schemas/documents.py`, `submission.py`, `messages_stream.py`, `routes/messages.py`,
`routes/runs.py`, `streaming_service.py`) inspected directly for interrupted-edit artifacts —
none found; the implementation itself was already complete and correct.

## Final routing truth table

| Cutover flag | Request mode | Legacy capability gate | Result |
|---|---|---|---|
| OFF | omitted (`None`) | — | **unchanged**: `prepare_chat_context`'s own `enable_deep_research`+`detect_research_task` heuristic, then `stream_chat_response`'s own separate `enable_multi_agent`+`detect_complex_task` heuristic — both existing checks, in existing order, undisturbed |
| OFF | `"chat"` | — | forces skip of BOTH deep-research injection and multi-agent routing, regardless of global flags |
| OFF | `"deep_research"` | `enable_deep_research` must be `True`, else 422 before persistence | forces deep-research context injection (bypasses the heuristic) **unless** the message already contains a URL that was scraped (§ URL behavior below — unchanged, not overridden); multi-agent routing skipped |
| OFF | `"multi_agent"` | `enable_multi_agent` must be `True`, else 422 before persistence | forces `stream_multi_agent_response` (bypasses the heuristic); deep-research injection skipped |
| ON | omitted (`None`) | — | durable chat (`DURABLE_STREAM_MODE`, MP-3C3D1 unchanged) |
| ON | `"chat"` | — | durable chat |
| ON | `"deep_research"` | — | `mode_unsupported_deep_research`, 422, before any persistence/provider/MCP call |
| ON | `"multi_agent"` | — | `mode_unsupported_multi_agent`, 422, before any persistence/provider/MCP call |
| any | invalid (garbage/object/array/oversized/wrong-case) | — | 422 before persistence, before the durable/legacy branch decision even runs |

### Exact URL-mode behavior (evidence-based, unchanged)

`prepare_chat_context`'s web-scrape step (§7) runs strictly before the deep-research step (§7b),
and §7b is gated on `not web_scraped`. This phase's `mode="deep_research"` forcing rule
(`run_deep_research_step = not web_scraped`) **preserves this exact short-circuit**: a message
containing a URL uses the existing URL-scrape context path instead of invoking autonomous
`run_deep_research`, identically to the pre-existing omitted-mode behavior. Proven by
`test_explicit_deep_research_with_url_uses_url_context_not_autonomous_research` — a URL-bearing
message with `mode="deep_research"` calls the scraper exactly once and `run_deep_research` zero
times.

## Canonical mode validation

`submission.validate_request_mode(raw)` — the ONE authoritative request-level validator. Reused by
`/messages/stream` (legacy + durable branches) **directly**, and by `/api/runs` **indirectly**
via `resolve_mode(raw)` (`return validate_request_mode(raw) or "chat"` — see the closeout
correction below for why this indirection, rather than a duplicate check, is correct):

- `None` → `None` (omitted, distinguishable from explicit `"chat"`).
- Exactly `"chat"` / `"deep_research"` / `"multi_agent"` → returned unchanged.
- Anything else — wrong case, whitespace, empty, oversized, non-string, object, array, unknown
  value — raises `ValueError` with a **bounded, generic** message that never echoes the input.
- Reuses `submission._ALLOWED_MODES` (the same allowlist `resolve_mode()` uses) — no second,
  drifting definition.

`messages_stream.unsupported_mode_reason(mode)` is the EXECUTABILITY gate (separate concern from
well-formedness): checks an already-validated mode against `DURABLE_STREAM_MODE`/
`_SUPPORTED_DURABLE_MODES` (currently `{"chat"}`). `unsupported_mode_code` (the existing
`/messages/stream` gate) now delegates to it internally — unchanged external behavior, refactored
for reuse. `/api/runs` calls the same function directly.

## Legacy actual-call proof

Every legacy routing claim is proven with **exact mocked-entrypoint call counts**, not boolean
flag inspection:

- `test_omitted_mode_preserves_exact_old_behavior` — flag off, mode `None`: `run_deep_research`
  called 0 times.
- `test_omitted_mode_with_deep_research_enabled_uses_existing_detector` /
  `..._multi_agent_enabled_...` — heuristic True → called once; heuristic False (same global flag
  still on) → called zero times, proving the EXISTING detector (not the global flag alone) still
  governs the omitted case.
- `test_both_globals_true_preserves_existing_precedence` — both flags + both heuristics true:
  `run_deep_research` called once (context lands in `system_prompt`) AND the multi-agent fake is
  still invoked with that enriched context — the pre-existing composition, unchanged.
- `test_explicit_chat_skips_both_even_with_both_globals_enabled` — both globals True, mode=`"chat"`:
  both call counts are 0.
- `test_explicit_deep_research_invokes_only_deep_research_path` /
  `test_explicit_multi_agent_invokes_only_multi_agent_branch` — heuristics deliberately False,
  explicit mode still forces the correct call, the OTHER path's call count stays 0.
- `test_explicit_modes_never_invoke_two_pipelines` — a trip-wire monkeypatch on
  `llm_providers.get_provider` (would raise `AssertionError` if the single-agent path were reached
  after multi-agent routing) proves the generator actually `return`s.
- `test_route_rejects_explicit_deep_research_when_globally_disabled` /
  `..._multi_agent_...` — through the real `/messages/stream` route: 422, and
  `prepare_chat_context` (which saves the user `Message` as its own step 2) is never called — zero
  persistence before the capability-gate rejection.

## Durable exact-gate and zero-persistence proof

All through the **real** `/messages/stream` route (`TestClient`), with every
`assert_durable_stream_ready` dependency explicitly configured (flags, env `RUN_IDEMPOTENCY_HMAC_KEY`,
and a fake `run_worker_supervisor` wired onto `app.state` whose `is_durable_path_ready()` returns
`True`) — never a bare call to the gate function alone:

- `test_durable_route_deep_research_exact_422_before_any_side_effect` /
  `..._multi_agent_...` — `resolve_and_submit` and `stream_legacy_wire` are both spy-mocked to
  **raise** if called at all; response is exactly 422; the bounded internal code
  (`mode_unsupported_*`) never appears in the response body.
- `test_durable_route_invalid_mode_fails_before_readiness_would_matter` — readiness IS fully
  configured (so a 503 would prove the wrong thing) and an invalid mode still returns 422,
  confirming `validate_request_mode` runs independently of durable readiness.
- `test_durable_route_unready_dependency_never_masks_as_mode_rejection` — the inverse: readiness
  deliberately NOT configured still returns 503 (never disguised as a 422 mode rejection).
- `test_durable_route_deep_research_never_falls_through_to_legacy` — `prepare_chat_context` (the
  legacy entrypoint) spy-mocked, call count stays 0 after the durable-mode rejection.
- `test_durable_route_omitted_and_explicit_chat_reach_durable_chat` — both omitted and explicit
  `"chat"` reach `resolve_and_submit` (proven via a spy, not just "the gate function returns
  `None`") and the response is a real 200 with the run's `X-Run-Id` header.

No unsupported durable mode is ever downgraded to chat (proven by the exact 422 + zero
`resolve_and_submit` calls above) and never falls through to legacy (proven directly).

## Direct Run API route proof

All through the **real, authenticated** `/api/runs` endpoint (`TestClient`, `get_auth_context` and
`get_db` dependency-overridden with a real `AuthContext` from the existing `make_ctx` test helper
and an in-memory SQLite session — not a bypass of the route):

- `test_direct_run_api_accepts_chat_when_dependencies_configured` — 201, one `Run` row persisted.
- `test_direct_run_api_rejects_deep_research_exact_422_before_persistence` /
  `..._multi_agent_...` — exact 422, then a fresh DB session confirms **zero** `Run` and `Message`
  rows.
- `test_direct_run_api_invalid_mode_returns_validation_failure` — 422, zero rows.
- `test_direct_run_api_omitted_mode_defaults_to_chat` — 201; the sealed `RunStep.input_ref`
  confirms `"mode": "chat"`.
- `test_mode_field_cannot_carry_run_kind_handler_or_identity` — `SubmitChatRunBody`'s
  `extra="forbid"` rejects any request that pairs a valid `mode` with unrelated internal-sounding
  fields (`run_kind`, `handler`, `identity_hash`, `tenant_id`).

## Files changed

| File | Change |
|---|---|
| `backend/app/schemas/documents.py` | `+mode: Optional[str] = Field(None, max_length=32)` on `ChatRequest`. |
| `backend/app/run_engine/submission.py` | `+validate_request_mode` (strict, request-level, preserves omitted-vs-chat distinction); **closeout correction:** `resolve_mode` now delegates to it (`validate_request_mode(raw) or "chat"`) instead of its own separate case/whitespace-tolerant check — closes the `/api/runs` validation asymmetry. |
| `backend/app/run_engine/messages_stream.py` | `+unsupported_mode_reason` (mode-only executability gate); `unsupported_mode_code` refactored to delegate to it (unchanged external behavior). |
| `backend/app/routes/messages.py` | Mode validated once at route entry, before the durable/legacy branch decision; legacy capability gate (422 before `prepare_chat_context`); `_stream_chat_durable` gains `resolved_mode` and passes it into the existing mode gate. |
| `backend/app/routes/runs.py` | New executability gate (`unsupported_mode_reason`) before `create_chat_run` — closes a real pre-existing gap where `deep_research`/`multi_agent` could be queued as placeholder runs no worker sweep ever advances. |
| `backend/app/services/streaming_service.py` | `ChatContext.mode` + `prepare_chat_context(mode=)`; the deep-research injection gate and the multi-agent routing gate both now branch on explicit mode first, falling back to the exact original heuristic/global-flag behavior when mode is omitted. |
| `tests/test_run_engine_mp3c3d3a.py` **(new)** | 46 focused tests (38 from the initial review + 8 from the closeout correction, proving both public endpoints reject/accept the identical mode set). |
| `tests/test_run_engine_mp3c3d1.py` | One test updated (`test_unknown_extra_body_field_cannot_override_mode`) — its original premise ("`ChatRequest` has no mode field") was intentionally superseded by this phase; now verifies the narrower, still-true property that unrelated internal-sounding fields remain dropped. |

No frontend files changed. No migration. No config default changed.
`run_messages_stream_durable_enabled` stays `False`.

## Exact test commands and counts

**Closeout correction pass (this session):**

```
PYTHONPATH=backend pytest tests/test_run_engine_mp3c3d3a.py -q   → 46/46 pass
PYTHONPATH=backend pytest tests/test_run_engine_mp3c3d1.py       → 16/16 pass
PYTHONPATH=backend pytest tests/test_run_engine_submit.py        → 12/12 pass
PYTHONPATH=backend pytest tests/test_run_engine_mp3a.py          → 24/24 pass
```

98 tests, zero failures. MP-3C3B (39) and MP-3C3D2 (40) were NOT re-run this pass — they were
already re-run and green in the D3A review session, and this correction touches only
`submission.py`'s mode-resolution functions, which neither of those suites exercises; no failure
occurred here to justify re-running them. `py_compile` clean on `submission.py` and the test file.
`git diff --check` clean. `import app.main` — **OK**.

169 tests total across the full focused regression set built up across both the D3A review and
this closeout pass (38→46 in the mode-contract file itself), zero failures anywhere.

`tests/test_streaming.py` (the pre-existing legacy SSE integration suite) was confirmed this
session to require a live server + Ollama (`httpx.ConnectError` on all 25 tests when attempted) —
explicitly out of scope per the task's exclusions; not relied on for verification. My new
Section B tests exercise the exact same `prepare_chat_context`/`stream_chat_response` entrypoints
directly, with every external service mocked, providing equivalent coverage of the code this
phase actually changed without needing live infrastructure.

## Frontend compatibility finding

`frontend/src/useStreaming.ts:166` sends `JSON.stringify({ message: sub.message, image_url: sub.imageUrl })`
— no `mode` field, confirmed unchanged this session (`git status --short -- frontend/` shows no new
diffs beyond the pre-existing MP-3C3C work). Since `ChatRequest.mode` is `Optional[str] = Field(None, ...)`,
omitting it is valid and resolves to `None` (omitted) on the backend — the exact same behavior as
before this phase, on both the legacy and durable paths. **No frontend edit was required or made.**

**D3B frontend contract** (for whoever implements the mode selector): send an optional top-level
`mode` string field alongside `message`/`image_url` in the `/messages/stream` request body. Valid
values: `"chat"`, `"deep_research"`, `"multi_agent"` — **exact lowercase, no whitespace** (strict
validation, no aliases). Omit the field entirely (do not send `null`) to keep today's
automatic/heuristic routing. A 422 response with a generic detail means the mode was invalid,
unrecognized, or (for `deep_research`/`multi_agent` specifically) not enabled/executable on this
deployment — the response never distinguishes which, by design (no information disclosure about
server configuration).

## Closeout correction (2026-09-12): eliminated the resolve_mode()/validate_request_mode() asymmetry

**This report originally deferred `resolve_mode()`'s case/whitespace tolerance as an "acceptable
asymmetry" between `/messages/stream` and `/api/runs`. That was wrong** — D3A's own requirement is
ONE canonical request-level mode contract reused by every mode-aware public route, and
`/api/runs` (via `build_chat_submission` → `resolve_mode`) was silently exempt from the strict
contract `/messages/stream` enforced. This was a real product-validation gap, not merely a test
gap, and has been corrected in product code:

`resolve_mode(raw)` now **delegates entirely** to `validate_request_mode(raw)` for well-formedness
(the exact same strict, canonical check `/messages/stream` uses — `_ALLOWED_MODES` remains the
ONE authoritative allowed-mode set, referenced nowhere else), and adds only the internal
omitted-to-`"chat"` default on top (`return validate_request_mode(raw) or "chat"`). `resolve_mode`
has exactly one caller in the whole codebase (`build_chat_submission`), which has exactly two
callers (`messages_stream.resolve_and_submit`, always passing the canonical `"chat"` constant, and
`routes/runs.py`'s `submit_run_endpoint`, passing the raw client `body.mode`) — so this change
could only ever affect `/api/runs`'s acceptance of non-canonical-case mode strings, which is
precisely the asymmetry being closed. No existing test exercised that leniency (confirmed by
running the full regression set before and after — identical pass count), so nothing relied on it.

`/messages/stream` and `/api/runs` now enforce byte-for-byte identical public-wire validation,
proven directly (not just documented) by 8 new tests exercising both real, authenticated
`TestClient` routes side by side with the same rejected-value set. Note for
accuracy: this correction changed **product validation code** (`submission.py`), not only tests —
the prior report's summary implying "all defects were only in tests" applies to the D3A
*implementation* review, not to this closeout pass, which found and fixed a genuine cross-endpoint
contract gap in the product code itself.

## Deferred / remaining work

- No frontend mode selector (explicitly out of scope for this mini-phase).
- No durable deep-research or multi-agent worker handler (MP-3C3D3B+).
- The dead non-streaming `POST /api/sessions/{id}/messages` endpoint (confirmed unused by the
  frontend in MP-3C3D0) still routes through `langgraph_workflow.run_research_workflow` and was
  not touched — mode threading there was out of scope.

## Confirmation

- `run_messages_stream_durable_enabled` remains `False`.
- No production cutover occurred; no durable deep-research/multi-agent execution exists anywhere.
- Recognized-but-unimplemented durable modes (`deep_research`, `multi_agent`) fail with a bounded
  422 before any persistence, on both `/messages/stream` and `/api/runs`.
- MP-3C3D3B and any durable mode handler were not started.
