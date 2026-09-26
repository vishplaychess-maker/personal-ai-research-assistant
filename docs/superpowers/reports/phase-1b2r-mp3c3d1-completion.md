# Phase 1B.2R — MP-3C3D1 completion report

**Fix global-flag mode routing for durable plain chat**

**Date:** 2026-09-11 · **Branch:** `phase-1b1-approval-domain` · HEAD `b2e6718` ·
**all work uncommitted, not production-ready**

Follows the MP-3C3D1 section of
`docs/superpowers/plans/2026-09-11-phase-1b2r-mp3c3d0-mode-parity-plan.md`.

## Problem

`backend/app/run_engine/messages_stream.py:unsupported_mode_code()` rejected every durable
`/messages/stream` request whenever the **global** `settings.enable_deep_research` or
`settings.enable_multi_agent` flags were on — settings that describe what the *legacy* pipeline
is configured to do, not what a specific durable request asked for. Both flags default to `True`
in the codebase. A self-hosted deployment that left either at its default would have durable
chat 422 on **every** message, including plain chat, the instant
`run_messages_stream_durable_enabled` turned on — silently defeating a "plain-chat-only" pilot for
exactly the deployments most likely to have those features already enabled.

## Stage-0 finding

MP-3C3D0's proposed fix was confirmed accurate by direct inspection: `submission.py` already
carries a `mode` field through `ChatRunSubmission.resolved_mode`, the semantic identity HMAC
(`_canonical_bytes`, `submission.py:179`), the encrypted execution snapshot
(`build_exec_snapshot`, `chat_stream.py:231-242`), and back out again in `ChatInput.mode`
(`chat_stream.py:288-295`) for the worker to read — the plumbing was already end-to-end wired.
The only place mode-based routing was wrong was `unsupported_mode_code()`'s two lines that read
global settings instead of the request's own resolved mode. `resolve_and_submit()` was already
always passing `mode=None`, which `resolve_mode(None)` already defaulted to `"chat"` — so the
mode was already correctly `"chat"` for every durable run today; only the *gate* was checking the
wrong thing.

`ChatRequest` (`schemas/documents.py:10-12`) has no `mode` field and no extra-field passthrough
(pydantic v2 default `extra="ignore"`) — there is no existing evidence-backed per-request mode
source to conflict with the plan, and no public API redesign was needed.

## Fix

One server-owned mode constant, shared by the gate and the submission call, so they cannot drift:

```python
DURABLE_STREAM_MODE = "chat"
_SUPPORTED_DURABLE_MODES = frozenset({DURABLE_STREAM_MODE})

def unsupported_mode_code(*, has_image: bool, mode: str = DURABLE_STREAM_MODE) -> Optional[str]:
    if has_image:
        return "mode_unsupported_image"
    normalized = (mode or DURABLE_STREAM_MODE).strip().lower()
    if normalized not in _SUPPORTED_DURABLE_MODES:
        safe = "".join(c for c in normalized if c.isalnum() or c == "_")[:32] or "unknown"
        return f"mode_unsupported_{safe}"
    return None
```

`resolve_and_submit()` now passes `mode=DURABLE_STREAM_MODE` explicitly (was `mode=None` —
identical resolved value, made explicit and self-documenting). `routes/messages.py`'s call site
passes `mode=_ms.DURABLE_STREAM_MODE` explicitly for the same reason. No new request field, no
public API change — `submission.py`'s `_ALLOWED_MODES` (the durable engine's *future*
vocabulary: chat/multi_agent/deep_research) is deliberately broader than this route's *current*
business rule (`_SUPPORTED_DURABLE_MODES = {"chat"}`); an injected `"deep_research"`/
`"multi_agent"` value is a recognized-but-not-yet-executable mode, rejected identically to
garbage input — never silently downgraded to chat, never silently accepted.

## Files changed

| File | Change |
|---|---|
| `backend/app/run_engine/messages_stream.py` | `+DURABLE_STREAM_MODE`, `+_SUPPORTED_DURABLE_MODES`; `unsupported_mode_code()` rewritten to gate on the server-owned mode instead of `enable_deep_research`/`enable_multi_agent`; `resolve_and_submit()` passes `mode=DURABLE_STREAM_MODE` explicitly; `+DURABLE_STREAM_MODE` in `__all__`. |
| `backend/app/routes/messages.py` | One call site updated to pass `mode=_ms.DURABLE_STREAM_MODE` explicitly. |
| `tests/test_run_engine_mp3c3b.py` | `test_unsupported_modes_fail_before_persistence` updated: proves global flags no longer gate the durable route; proves an explicit non-chat `mode` argument is still rejected. |
| `tests/test_run_engine_mp3c3d1.py` **(new)** | 16 focused tests (below). |
| `docs/superpowers/reports/phase-1b2r-mp3c3d1-completion.md` **(new)** | this report. |
| `docs/superpowers/plans/2026-09-08-phase-1b2r-durable-run-resume-architecture.md` | new §37.12 documenting the final per-run routing rule. |
| `THUNDER_AI_PRD.md` | MP-3C3D1 status row added. |

No other file changed. No migration. No config default changed (`run_messages_stream_durable_enabled` stays `False`; `enable_deep_research`/`enable_multi_agent` default values untouched — they still govern the legacy pipeline exactly as before).

## Final mode-selection rule

`/messages/stream`'s request body has no field a client can use to select a mode. The durable
route therefore has exactly one server-owned mode value, `DURABLE_STREAM_MODE = "chat"`, defined
once in `messages_stream.py` and used identically by the unsupported-mode gate and the
submission call. Global `enable_deep_research`/`enable_multi_agent` settings are never consulted
for durable routing — they remain meaningful only to the legacy pipeline. A future mode-routing
mini-phase (MP-3C3D3+ per the plan) that adds a real per-request mode field must route it through
this same `unsupported_mode_code(mode=...)` seam rather than reintroducing a global-settings
check.

## Global-flag behavior

- Legacy pipeline (`run_messages_stream_durable_enabled=False`): `enable_deep_research` /
  `enable_multi_agent` continue to control the legacy pipeline exactly as before — unchanged,
  proven by `test_durable_flag_off_preserves_legacy_and_global_flag_behavior` with both flags
  forced `True`.
- Durable pipeline (`run_messages_stream_durable_enabled=True`): these flags are **not read** by
  `unsupported_mode_code()` at all — proven for all four True/False combinations by
  `test_plain_chat_accepted_regardless_of_global_capability_flags`.

## Unsupported-mode behavior

- `has_image=True` still rejects (`mode_unsupported_image`), unconditionally, before any mode
  check — image support remains out of scope for this mini-phase.
- Any mode value other than `"chat"` — including the durable engine's own recognized-but-
  unimplemented `"deep_research"`/`"multi_agent"`, and arbitrary/garbage strings — is rejected
  with a bounded, sanitized, length-capped code (`mode_unsupported_<safe-slug>`), before any
  Run/Message/RunStep/RunPayload/event is created. No fallback to legacy after the durable branch
  begins (unchanged from MP-3C3B).

## Idempotency / privacy evidence

- `mode="chat"` is included in `_canonical_bytes`/`semantic_identity` (unchanged code — already
  true since MP-3C1's `chat-run/v2` contract) — proven by
  `test_semantic_identity_includes_resolved_mode` (`mode=None` and `mode="chat"` produce an
  identical identity hash — this route's change is a no-op for existing runs, not a behavior
  change) and `test_behavior_changing_mode_cannot_reuse_identity` (a same-key submission with a
  different `mode` value raises `IdempotencyKeyConflict`, never silently reused).
- `test_identical_retry_reuses_one_run` — same key + same message ⇒ one `Run`, one `RunStep`, no
  duplicate rows.
- `test_unknown_extra_body_field_cannot_override_mode` — `ChatRequest.model_validate` with an
  injected `"mode"` body field proves pydantic drops it; the field doesn't exist on the model at
  all, so there is no path for a client to influence server-owned routing through the body.
- `test_injected_mode_canary_never_reaches_bounded_code` — a canary value passed as `mode` never
  appears in the returned code or in logs (the route only ever logs the bounded code, never the
  raw mode value).
- No executor call added anywhere in this diff — `resolve_and_submit` still only builds and
  persists a submission; `test_submission_creates_no_execution` (zero assistant `Message`, zero
  text `RunEvent`) and `test_durable_flag_off_preserves_legacy_and_global_flag_behavior`
  (`called["durable"] == 0`) both confirm no execution happens in the request thread.

## Exact focused tests

`tests/test_run_engine_mp3c3d1.py` — **16/16 pass**:
`test_plain_chat_accepted_regardless_of_global_capability_flags` (×4 parametrized),
`test_run_step_snapshot_records_mode_chat`, `test_worker_receives_snapshotted_mode_chat`,
`test_identical_retry_reuses_one_run`, `test_semantic_identity_includes_resolved_mode`,
`test_behavior_changing_mode_cannot_reuse_identity`, `test_injected_mode_rejected_before_persistence`,
`test_injected_mode_canary_never_reaches_bounded_code`, `test_unknown_extra_body_field_cannot_override_mode`,
`test_image_url_remains_rejected_before_persistence`, `test_unhealthy_worker_returns_503_before_persistence`,
`test_submission_creates_no_execution`, `test_durable_flag_off_preserves_legacy_and_global_flag_behavior`.

`tests/test_run_engine_mp3c3b.py::test_unsupported_modes_fail_before_persistence` (updated) —
**pass**. Also spot-checked (unchanged, still pass, since `resolve_and_submit`'s
`mode=None`→`mode="chat"` change is identity-preserving):
`test_identical_retry_same_run_no_dup_rows`, `test_changed_semantic_input_conflicts`,
`test_submit_creates_no_execution`, `test_bad_session_is_404`.

`py_compile` clean on all four changed Python files. `git diff --check` clean. `import app.main`
not run — no imports/config/main changed this mini-phase.

Not run (per scope): the remaining 34 MP-3C3B tests, MP-3C3A's 41, MP-3C2's 111, the 119-test
group, 613-test regression, frontend suite/build, Docker, PostgreSQL, Ollama, ChromaDB, live
providers.

## Remaining blockers

None for MP-3C3D1's own scope. MP-3C3D2 (citation/memory/marker-postprocessing foundation) is the
next planned slice per the approved plan.

## Confirmation

- `run_messages_stream_durable_enabled` remains `False`.
- Durable mode support remains **chat only** — `_SUPPORTED_DURABLE_MODES = {"chat"}`.
- Global `enable_deep_research`/`enable_multi_agent` no longer block durable plain chat.
- No deep-research or multi-agent execution was implemented. No citations/memory parity was
  implemented. MP-3C3D2+ were not started.
