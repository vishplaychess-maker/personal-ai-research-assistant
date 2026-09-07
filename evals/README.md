# Thunder AI — evaluation harness (Phase 1A)

## What each mode actually proves

| Mode | Command | Evidence level | What it proves |
|---|---|---|---|
| **offline** (Gate A) | `python -m evals.runner --offline` | `synthetic` | The **harness** works (schema, assertions, report, exit code) and the authored regression fixtures still pass. **It does NOT verify Thunder AI product behaviour.** |
| **inprocess** (Gate B) | `PYTHONPATH=backend python -m evals.runner --inprocess` | `product_inprocess` | A **real backend function** (parser / router / validator / guard) ran; the external side-effect boundary was mocked and the mock records whether the real code *attempted* the side effect (`execution_attempted`). |
| **backend** (Gate B/C) | `PYTHONPATH=backend python -m evals.runner --backend http://localhost:8080` | `product_backend` | The **actual user-facing HTTP path** served the response: `POST /api/auth/register` → `login` (JWT + CSRF) → `POST /api/sessions` → `POST /api/sessions/{id}/messages/stream`. Needs a running backend pointed at a **free/local** provider. |
| **record** (Gate C) | `PYTHONPATH=backend python -m evals.runner --record` | `provider_recording` | Raw output from a **FREE local model** (GLM router / Ollama), captured to `evals/recordings/`. This is a provider recording, **not** the product path. Never calls a paid provider. |

Only `product_inprocess`, `product_backend`, `e2e_live` count as **verified product passes**.
Synthetic-fixture passes and placeholder fixtures are reported separately and are **never**
verified product passes.

## Outcomes (never collapsed into one another)

`pass | fail | blocked | error | unverified`

- `blocked` — the task has a `blocked_by` (a later phase). Stays blocked even with
  `--attempt-blocked` when there is no fixture/adapter to run — it does **not** become an `error`.
- `error` — the harness/adapter could not evaluate the task (missing fixture, bad schema, adapter
  crash). Never becomes a pass.
- `unverified` — no product evidence was available (placeholder fixture, or a model-quality task
  with no model). Never a pass, never a fail.

`failure_class ∈ none | assertion | grader | infrastructure | placeholder | no_evidence` separates
model-quality failures from infrastructure failures.

## Provenance

Every fixture and every result carries a `provenance` block: `fixture_kind`, `evidence_level`,
`source_commit_sha`, `captured_at`, `capture_mode`, `application_path`, `provider`, `model`,
`model_revision`, `runtime`, `dataset_version`, `prompt_version`, `evaluator_version`,
`grader_version`, `working_tree_dirty`, `source_tree_or_diff_digest`, `from_product_code`,
`tools_mocked`, `human_reviewed`. Missing required fields are listed in the report
(`provenance_gaps`).

### VCS truthfulness (dirty working trees)

A commit SHA alone is misleading when results were generated from uncommitted files. Every report
and result therefore carries:

- `source_commit_sha` — `git rev-parse HEAD`
- `working_tree_dirty` — `true` when there are staged / unstaged / untracked changes
- `source_tree_or_diff_digest` — when dirty, `sha256` over `git diff HEAD` **plus** the bytes of
  every untracked, non-ignored file; `null` on a clean tree
- `generated_at`, `dataset_version`, `evaluator_version`

**When `working_tree_dirty` is `true`, the report does NOT represent `source_commit_sha`** — the
markdown header says so explicitly and the digest identifies the actual tree state.

### Reports are build artifacts, not committed files

`evals/report.json` / `evals/report.inprocess.json` are **git-ignored**. This avoids the
self-referential problem (a committed report from commit *X* whose own bytes are part of *X*'s
tree). The authoritative baseline reports are published by CI (`.github/workflows/evals.yml`,
`actions/upload-artifact`) from a **clean checkout**, so their `working_tree_dirty` is `false` and
their `source_commit_sha` is meaningful. Local runs still write the files for inspection; they
simply carry `working_tree_dirty: true` while you have edits in flight.

### Redaction

Reports and payloads are run through `redact()`:

- Dict keys are matched **exactly (case-insensitive) or by anchored suffix** — never a bare
  substring — so token-usage metadata (`token_count`, `input_tokens`, `output_tokens`,
  `total_tokens`, `max_tokens`, `tokenizer`, `prompt_tokens`, `completion_tokens`) stays visible
  while `api_key`, `authorization`, `password`, `secret`, `client_secret`, `access_token`,
  `refresh_token`, `id_token`, `cookie`, `set-cookie`, `*_api_key`, `*_secret`, `*_password`, … are
  replaced with `[REDACTED]`.
- Every string value is additionally scrubbed for JWTs, Fernet blobs, `Bearer …` headers, and
  provider-key formats (`sk-or-v1-…`, `sk-…`).
- A value already masked with `****` is kept so masking assertions can still run.

## Backend adapter safety (`--backend`, Gate C)

The backend adapter registers a user, creates a session, and *may* make a **live model call**. It
is **default-closed**, with **two independent authorization gates** —
`evals/adapters/backend_http.py`:

### GATE 1 — target authorization (`_authorise_target()`)

*May the eval client contact this backend at all, for ANY request?* Health / register / login /
create-session invoke **no model** and are gated only by this.

| Rule | Behaviour |
|---|---|
| URL scheme | `http` / `https` only; anything else refused |
| Local target (`localhost`, loopback, or a host in `EVAL_BACKEND_ALLOW_HOSTS`) | allowed by default |
| Non-local target | requires `EVAL_ALLOW_REMOTE_BACKEND=true`, then run through the repo SSRF/URL guard (`app.services.browser_agent.assert_url_allowed`, guarded import; stdlib fallback) — still cannot be a private / link-local / reserved / cloud-metadata address |
| Redirects | not followed (`follow_redirects=False`) |
| **Independent of** | `EVAL_ALLOW_LLM_CALLS`, `PAID_ENABLED` — an authorized staging/test backend runs every non-model check with paid calls still disabled |

### GATE 2 — live-model authorization (`_authorise_live_model()`)

*May THIS run trigger a live model invocation (the SSE stream)?*

| Rule | Behaviour |
|---|---|
| Always | requires `EVAL_ALLOW_LLM_CALLS=true`. `EVAL_ALLOW_REMOTE_BACKEND=true` does **not** authorize a model call |
| Local target | `EVAL_ALLOW_LLM_CALLS=true` is the operator's assertion that their own local stack (GLM router / Ollama) is free — allowed |
| Remote target | allowed **only** when the effective model is *verifiable* free/local: model id known **and** provider known **and** `app.schemas.sessions.is_free_model(model, provider)` **and** id in `EVAL_FREE_MODEL_ALLOWLIST` (explicit, comma-separated). Never inferred from the name alone; the allowlist is always also required |
| Paid | disabled by default; **never a silent fallback** — there is no code path that substitutes a provider/model |
| Gate C | stays manual (`workflow_dispatch`) and non-gating |

**Known limitation (documented, not worked around).** Thunder AI's `GET /api/sessions/{id}/model`
returns only a bare model string — **no provider, no routing / fallback policy**. So for a remote
backend the effective provider is unknown to the adapter and **GATE 2 refuses the live-model
portion as `unverified`**; GATE 1 checks (connectivity / auth / session) still run and pass. Remote
live-model evaluation is therefore not achievable against the current API and is honestly reported
as unverified — no guarantee is invented.

### Credentials, timeouts, redaction

| | |
|---|---|
| Credentials | `EVAL_BACKEND_USERNAME` / `EVAL_BACKEND_PASSWORD` when both set; otherwise an ephemeral `eval-ephemeral-<utc-ts>-<rand>` account |
| Timeouts | connect 5s / read 30s / write 10s / pool 5s; SSE stream idle-gap 45s; SSE total wall-clock 180s |
| Redaction | Bearer/CSRF headers and any backend error body redacted before they can reach a report; every failure message names the reason and never echoes an env value or token |

**Ephemeral account cleanup.** The API has no user-delete endpoint, so ephemeral rows persist. An
operator running Gate C against a long-lived dev DB should periodically prune
`users WHERE username LIKE 'eval-ephemeral-%'` and their sessions/messages. Prefer setting
`EVAL_BACKEND_USERNAME` / `EVAL_BACKEND_PASSWORD` to one pre-provisioned evaluation account.

Example (local free stack — the supported live path):

```bash
EVAL_ALLOW_LLM_CALLS=true PYTHONPATH=backend \
  python -m evals.runner --backend http://127.0.0.1:8080
```

## Fixture kinds

- `synthetic_regression` — authored from a **verified reading** of the current product code
  (Phase 0 audit). Runs in Gate A only. NOT a product verification.
- `authored_placeholder` — a stand-in for a model-quality judgement (currently **T1, T4**). Always
  reported as `unverified`; excluded from every pass count and every threshold. Replace via
  `--record` / `--backend`.
- `inprocess_capture` / `backend_capture` / `provider_capture` — produced by an adapter.

## Gates & thresholds (do not weaken)

- **Gate A** (offline): fails if any synthetic safety fixture (`tool_approvals_safety` /
  `approval_gateway` / `budget_policy`) regressed, or any task `fail`/`error`.
- **Gate B** (inprocess / backend): additionally fails if any **unblocked** safety task lacks a
  **verified** product pass. A synthetic-only pass does **not** satisfy this gate.
- A PR may not decrease `verified_pass` at the same gate, or turn a `blocked` task into a silent
  `error`/`pass`.
- If a task fails, fix the fixture or the assertion — **never** change product behaviour to make an
  eval pass.

## Current baseline (this branch)

| Gate | verified product passes | synthetic passes | placeholder | unverified | blocked | fail | error | exit |
|---|---|---|---|---|---|---|---|---|
| A (offline) | 0 | 15 | 2 | 2 | 28 | 0 | 0 | 0 |
| B (inprocess) | **13** | 0 | 0 | 4 | 28 | 0 | 0 | 0 |

The 13 verified in-process passes execute real code in:
`app.tools.mcp_tool` (T7, T9, T10), `app.services.rag_service.build_citation_list` (T11, T17),
`app.tools.terminal_executor` + `langgraph_workflow._route_after_generate` (T27),
`app.services.browser_agent` (T29 classify + pre-approval gate, T30 SSRF guard),
`app.services.terminal_panel.validate_command` (T31),
`app.services.streaming_service.prepare_chat_context` (T32),
`app.services.cache_service` (T33), `app.main.health_check` (T34),
`app.services.encryption.mask_api_key` (T35).

The 4 `unverified` in Gate B (T1, T4, T12, T14) are model-quality / synthesis tasks — they need
`--record` or `--backend` with a free model and are honestly reported as not verified.

The 28 blocked tasks map to phases 1B, 2A, 2B, 3A, 4B, 4C, 6, and `record-mode`.

## Add a task

Append one line to `evals/dataset.jsonl` with `evidence_level` (one of `evals.provenance.EVIDENCE_LEVELS`)
and `product_target` (the real function/route it evaluates). If offline-evaluable, add
`evals/fixtures/T46.json` with a `provenance` block. If it needs real code, add a target function to
`evals/adapters/inprocess.py` (`_TARGETS`). Assert kinds: `contains`, `not_contains`, `regex`,
`json_path_exists`, `citation_present`, `marker_absent`, `status_code`. `in:` is a path
(`response_json.api_key`, `citations[0].filename`, `$json`).
