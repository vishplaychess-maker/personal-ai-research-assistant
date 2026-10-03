from pydantic_settings import BaseSettings
from pydantic import model_validator
from pathlib import Path
from typing import List

# Bundled skills directory (<backend>/skills, e.g. /app/skills in Docker).
# The SkillManager discovers this as its primary "bundled" location regardless
# of the process working directory.
_DEFAULT_SKILLS_DIR = str(Path(__file__).resolve().parent.parent / "skills")

class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    database_url: str = "sqlite:////data/app.db"
    chromadb_host: str = "localhost"
    chromadb_port: int = 8000
    ollama_host: str = "localhost"
    ollama_port: int = 11434
    upload_dir: str = "/data/uploads"
    data_dir: str = "/data"

    # ── Memory settings ─────────────────────────────────
    enable_memory: bool = True
    memory_max_results: int = 5
    # Memory decay: memories not accessed within this many days are forgotten,
    # unless they are pinned or highly accessed (access_count > threshold).
    memory_decay_ttl_days: int = 7
    memory_pin_access_threshold: int = 5
    # Semantic conflict-resolution threshold for save_memory (0-1 cosine sim).
    # Tuned from nomic-embed-text output: a changed preference on the same
    # subject ("bullet points" -> "paragraphs") lands ~0.83; genuinely different
    # facts score far lower. 0.80 merges conflicting preferences while keeping
    # unrelated facts as separate memories.
    memory_conflict_threshold: float = 0.80

    # ── Auth settings ───────────────────────────────────
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expiry_hours: int = 168  # 7 days (backward compat for existing tokens)
    # Phase 7B: access and refresh token lifetimes
    jwt_access_expiry_minutes: int = 15
    jwt_refresh_expiry_days: int = 30

    # ── Rate limiting (Phase 7A) ──────────────────────────
    rate_limit_max_attempts: int = 10
    rate_limit_window_seconds: int = 60
    rate_limit_lockout_threshold: int = 5
    rate_limit_lockout_base_seconds: int = 30
    rate_limit_lockout_max_seconds: int = 900  # 15 min
    production_mode: bool = False

    # Phase 7B: refresh endpoint rate limiting
    # (env var REFRESH_RATE_LIMIT_REQUESTS)
    refresh_rate_limit_requests: int = 20
    refresh_rate_limit_window_seconds: int = 60

    # ── Session management (Phase 7B) ─────────────────────
    max_active_sessions: int = 10

    # ── Cookies & CSRF (Phase 7C) ─────────────────────────
    # HttpOnly refresh cookie set by the backend on login/refresh.
    refresh_cookie_name: str = "research_assistant_refresh_token"
    # Non-HttpOnly double-submit CSRF cookie.
    csrf_cookie_name: str = "research_assistant_csrf_token"
    # Secure flag: false for localhost, true in production (HTTPS required).
    cookie_secure: bool = False
    # SameSite attribute for the refresh cookie (lax for local dev).
    cookie_samesite: str = "lax"
    # Allowed CORS origin for the frontend (never "*" with credentials).
    frontend_origin: str = "http://localhost:5173"
    # Phase 5 (cloud): comma-separated list of extra CORS origins for
    # production, e.g. "https://app.example.com,https://www.example.com".
    # Falls back to frontend_origin when empty.
    cors_origins: str = ""
    # Log output format: "text" (default, current behavior) or "json"
    # (structured one-JSON-per-line logging for cloud log drains).
    log_format: str = "text"

    # ── Terminal tool (Phase 9 — Agent Tools) ─────────────
    # Enable the terminal executor tool for the AI agent.
    # When true, the agent can propose shell commands for user approval.
    enable_terminal_tool: bool = False

    # ── Multi-agent collaboration (Phase 3) ──────────────
    # When true, complex build/code tasks are routed through the
    # Researcher -> Coder -> Reviewer team (max 2 review retries).
    # Simple questions always use the normal single-agent path.
    enable_multi_agent: bool = True

    # ── Semantic cache (ChromaDB-backed CAG layer 2) ─────
    # Second cache layer behind the exact-match CAG (cache_service): embeds
    # the query (Ollama nomic-embed-text) and serves a previously cached
    # FINAL response when cosine similarity >= threshold, scoped to the
    # active provider+model. Embedding/store failures degrade silently to
    # exact-match-only and never break a chat request.
    semantic_cache_enabled: bool = True
    semantic_cache_threshold: float = 0.85
    semantic_cache_max_entries: int = 1000

    # ── MCP tools (Phase 1) ──────────────────────────────
    enable_mcp_tool: bool = False
    mcp_call_timeout_s: int = 30
    mcp_discovery_timeout_s: int = 20

    # ── Approval gateway (Phase 1B.2) ────────────────────
    # Required by default for enabled MCP execution: when enable_mcp_tool is on
    # and this is true, MCP calls route through the trusted approval gateway
    # (app.approval.gateway) — a call with no valid one-time approval / matching
    # SessionGrant returns a structured `approval_required` result and does NOT
    # execute. Setting this False disables risky MCP execution; it NEVER
    # restores the legacy zero-approval marker auto-execution.
    approval_gateway_required: bool = True
    # Operational kill-switch: when False the gateway is treated as UNAVAILABLE
    # and MCP execution is blocked (fail closed). Distinct from
    # approval_gateway_required (a policy toggle) — this models an outage.
    approval_gateway_available: bool = True
    # Bounded active-proposal caps (Phase 1B.2 correction). "Active" = a
    # PendingApproval still in `requested` or `approved` (it can still consume
    # resources / be acted on). The gateway refuses to create a NEW proposal
    # once any of these ceilings is reached for the attempt's scope — it fails
    # closed with reason_code `active_proposal_limit`. A retry of an ALREADY
    # pending logical call is never blocked by these caps (it reuses its row).
    # The client cannot raise or disable them; they are validated as >= 1 at
    # startup. Shared distributed (multi-worker / PostgreSQL) enforcement is
    # still subject to the hosted-deployment PostgreSQL verification gate.
    max_active_proposals_per_turn: int = 3      # == the MCP marker cap per response
    max_active_proposals_per_session: int = 10
    max_active_proposals_per_user: int = 20
    # Hard cap on [MCP_CALL] markers parsed from ONE generated assistant
    # response (bounds the parse -> propose fan-out). Validated as >= 1.
    max_mcp_calls_per_response: int = 3

    # ── Durable run engine (Phase 1B.2R — MP-0 schema foundation only) ──
    # MP-0 adds the durable run ledger schema + state machine + encrypted
    # payload store. NO worker, route, streaming adapter, approval resume, or
    # tool execution is wired in MP-0. These are configuration *seams* — the
    # master flag is OFF and unused until a later mini-phase turns a flow on.
    run_engine_enabled: bool = False
    # Max plaintext bytes for a single encrypted `run_payloads` row (sealed
    # with the repo's Fernet service — AES-128-CBC + HMAC-SHA256). A larger
    # payload is rejected at seal time (fail closed). Validated as >= 1.
    run_payload_max_bytes: int = 65536
    # Text-delta batching for `run_events` (D7): a future writer flushes a text
    # batch at ~this many bytes OR ~this many ms, whichever first. Control /
    # security / lifecycle events are ALWAYS written immediately and never wait
    # for a text batch. MP-0 only validates these (>= 1); no batching logic yet.
    run_event_text_flush_bytes: int = 4096
    run_event_text_flush_ms: int = 100
    # ── Phase 1B.2R MP-1A — RunEngine core transactional-service safety limits ──
    # Server-side only. A client cannot raise or disable them; they are
    # validated as >= 1 at startup. Conservative, repo-evidenced defaults:
    #   * key length  == the `runs.idempotency_key VARCHAR(80)` column width
    #   * event bytes == the text-flush byte budget (control `data` is tiny JSON)
    #   * read page   == a bounded control-event replay page
    run_idempotency_key_max_bytes: int = 80
    run_control_event_max_bytes: int = 4096
    run_event_read_max_page: int = 200
    # ── Phase 1B.2R MP-1B — RunWorker (claim / lease / recovery core) ──
    # The worker is a MECHANISM only in MP-1B. Phase 1B.2R MP-3C3A wires it into
    # the FastAPI app lifespan through `RunWorkerSupervisor` — it still starts
    # ONLY when BOTH `run_engine_enabled` and `run_worker_enabled` are true (both
    # off by default), and ONLY after every boot check (migrations + schema
    # verification + HMAC key) has succeeded. The handler registry ships EMPTY —
    # an unknown `run.kind` fails the run closed. The client cannot set any of
    # these; the integer knobs are validated as >= 1.
    run_worker_enabled: bool = False
    run_worker_id: str = ""                    # blank -> a per-process id is generated
    run_worker_lease_ttl_s: int = 60          # lease lifetime granted per claim / renew
    run_worker_poll_interval_s: int = 5       # sleep between sweeps in run_forever()
    run_worker_max_attempts: int = 3          # × handler timeout = the recovery wall-clock budget
    run_worker_handler_timeout_s: int = 300   # per-run handler wall-clock budget
    run_worker_recover_batch: int = 20        # max expired-lease runs handled per sweep
    # ── Phase 1B.2R MP-3C3A — embedded worker lifespan supervisor ──
    # The embedded worker's SUPPORTED topology is self-host, exactly ONE web
    # process, SQLite or PostgreSQL. Set this to the real web-process count. If it
    # is > 1 while `database_url` is SQLite AND the worker is enabled, startup is
    # HALTED (multiple embedded workers against one SQLite file is unsafe until
    # the MP-6 PostgreSQL concurrency verification). A dedicated worker process is
    # a future (MP-6) topology; the `RunWorker` interface already supports it.
    run_worker_web_concurrency: int = 1
    # Bounded graceful period (seconds) the lifespan shutdown waits for the
    # in-flight sweep to unwind after the cooperative stop signal is set. When it
    # expires the supervisor records a bounded operational error and returns; it
    # NEVER force-kills a thread and NEVER marks a run cancelled or succeeded.
    # Keep it comfortably above `run_worker_lease_ttl_s // 3` (the stalled-provider
    # idle-tick cadence) so a stalled generation is noticed and closed in time.
    run_worker_shutdown_grace_s: int = 30
    # Supervisor-level bounded exponential backoff bounds (seconds) after an
    # unexpected sweep failure. Backoff resets to the minimum after any
    # successful sweep; the stop signal interrupts a backoff sleep promptly.
    run_worker_backoff_min_s: int = 2
    run_worker_backoff_max_s: int = 60
    # ── Phase 1B.2R MP-3C3B — durable /messages/stream backend cutover ──
    # OFF by default. When on, POST /api/sessions/{id}/messages/stream submits (or
    # reuses) a durable `chat_stream` run and streams its PERSISTED `run_events`,
    # adapted into the exact legacy SSE wire format. When off the route is the
    # byte-for-byte legacy path and NOTHING durable (Run/Message/RunStep/RunPayload/
    # event) is created. Enabling it also requires `run_engine_enabled`,
    # `run_worker_enabled`, `run_chat_stream_enabled`, `run_public_api_enabled`, a
    # valid `RUN_IDEMPOTENCY_HMAC_KEY`, a live/ready `RunWorkerSupervisor`, and
    # `enable_deep_research` + `enable_multi_agent` BOTH off (the durable engine
    # persists neither citations/confidence nor deep-research / multi-agent
    # progress — those request modes fail closed BEFORE any persistence). NOT a
    # frontend cutover; NOT enabled in any default / deployment config.
    run_messages_stream_durable_enabled: bool = False
    # Phase 1B.2R MP-3C3D4C — master flag for the END-TO-END durable Deep
    # Research worker (acquisition + cited synthesis + finalization). OFF by
    # default; never set true in any default/deployment config. Executability
    # additionally requires run_engine_enabled + run_worker_enabled +
    # run_chat_stream_enabled (deep-research synthesis reuses the SAME
    # generation bridge as durable chat) + run_public_api_enabled + a
    # configured RUN_IDEMPOTENCY_HMAC_KEY — see
    # app.run_engine.deep_research_readiness.deep_research_executable(), the
    # ONE authoritative predicate every route/capability/worker gate reuses.
    run_deep_research_enabled: bool = False
    # Phase 1B.2R MP-3C3D5B — master flag for the FUTURE end-to-end durable
    # Multi-Agent worker. OFF by default; never set true in any default/
    # deployment config. This flag alone activates NOTHING in this phase —
    # D5B wires no public route, no capability response, and no worker
    # sweep to it at all (see app.run_engine.multi_agent_readiness, whose
    # own predicate additionally cannot become true until D5C flips its
    # explicit implementation-readiness gate). D5C will require this
    # flag AND run_engine_enabled + run_worker_enabled + run_chat_stream_enabled
    # + run_public_api_enabled + a configured RUN_IDEMPOTENCY_HMAC_KEY, the
    # same shape as run_deep_research_enabled's own prerequisite chain.
    run_multi_agent_enabled: bool = False
    # Phase 1B.2R MP-5 (scheduler-on-engine, first slice) - OFF by default.
    # When ON, the cron execution path submits through the canonical RunEngine
    # (build_chat_submission -> create_chat_run) instead of running the legacy
    # LangGraph workflow inline, then observes the run to a truthful outcome:
    # succeeded / failed / timed_out / cancelled / denied, or pending_approval
    # when the run parks awaiting a REAL user decision (never auto-approved),
    # or pending when it is still queued/running after the wait deadline.
    # Prerequisites: the durable flags above (run_engine_enabled,
    # run_worker_enabled, run_chat_stream_enabled, run_public_api_enabled), a
    # valid RUN_IDEMPOTENCY_HMAC_KEY, a running worker, AND a task row carrying
    # auth_session_ref (captured at creation) - without it the job fails closed
    # with reason scheduler_no_session_binding and creates no Run.
    # This does NOT cut over /messages, delete legacy helpers, or add an
    # approval authority: approvals stay with the MP-2 gateway/dispatch path.
    run_scheduler_engine_enabled: bool = False
    # Bounded wait for a scheduled run to reach a terminal state. A non-terminal
    # observation (still running, or parked for approval) is reported truthfully,
    # never as success.
    run_scheduler_engine_wait_deadline_s: float = 180.0
    # Phase 1B.2R MP-5 - REVERSIBLE non-streaming /messages cutover. OFF by
    # default. When ON, a non-streaming chat submission whose session has NO
    # unresolved approval is submitted through the canonical RunEngine (the
    # nonstream adapter) and answered from the persisted assistant Message.
    # Authority rule: while ANY approval is unresolved in the session (legacy
    # citations marker OR canonical PendingApproval) NO new Run is started - a
    # bounded, actionable parked response is returned instead, and the existing
    # approval stays decidable ONLY through its own authority. Flag OFF is
    # byte-for-byte the legacy behaviour.
    messages_nonstream_engine_enabled: bool = False
    # Bounded wait for the canonical non-streaming turn. A non-terminal
    # observation is reported as still-running - never as an answer.
    messages_nonstream_wait_deadline_s: float = 30.0
    # Fail-fast consistency guards, not deployer tuning knobs: the durable
    # multi-agent role graph is a FIXED 3-role, 3-provider-call sequence
    # (app.run_engine.multi_agent_orchestration.ROLE_ORDER /
    # MAX_PROVIDER_CALLS) — these two settings exist only so a future
    # accidental drift between that hardcoded contract and configuration
    # is caught at startup, never silently. Client cannot override; a
    # deployer changing either away from 3 fails config validation below.
    multi_agent_max_roles: int = 3
    multi_agent_max_provider_calls: int = 3
    # Per-role provider-call wall-clock timeout and the run's total deadline
    # (mirrors research_fetch_timeout_s / research_total_deadline_s's exact
    # shape — validated below to keep role_timeout strictly under the total).
    multi_agent_role_timeout_s: float = 120.0
    multi_agent_total_deadline_s: float = 360.0
    # Sealed inter-agent artifact bounds (app.run_engine.multi_agent_artifacts).
    # multi_agent_max_artifact_bytes must never exceed run_payload_max_bytes —
    # the sealed-payload envelope's own hard cap (validated below).
    multi_agent_max_artifact_bytes: int = 65536
    multi_agent_max_content_chars: int = 24_000
    multi_agent_max_claims: int = 32
    multi_agent_max_assumptions: int = 16
    multi_agent_max_uncertainties: int = 16
    # Phase 1B.2R MP-3C3D5B — FOUNDATION-ONLY setting: D5B defines and
    # validates this bound but enforces NOTHING (no worker sweep exists to
    # count or reject against it yet). D5C is the exact enforcement point:
    # its submission path must count the caller's own non-terminal
    # `Run.kind == "multi_agent"` rows and reject a new one transactionally
    # once this ceiling is reached — the SAME shape as
    # `max_active_proposals_per_user` above, not the unrelated
    # `run_messages_stream_max_concurrent_per_user` (which bounds concurrently
    # OPEN SSE streams, not concurrently ACTIVE runs, and a client can close a
    # stream and reopen it against the same still-running run).
    multi_agent_max_active_runs_per_user: int = 2
    # Max concurrently open durable message streams per user (bounded in-process;
    # a new stream past this returns 429). Client cannot raise it. Validated >= 1.
    run_messages_stream_max_concurrent_per_user: int = 4
    # Rate limit (submissions per minute per user) for the durable submit path,
    # reusing the existing in-memory rate-limiter seam. Validated >= 1.
    run_messages_stream_submits_per_min: int = 30
    # ── Phase 1B.2R MP-3A — durable Run public API + persisted text events + SSE ──
    # The additive `/api/runs` router is registered always but every route calls
    # `require_run_public_api()` first: 404 when this flag is off (disabled by
    # default), 503 when the flag is on but `RUN_IDEMPOTENCY_HMAC_KEY` (env only,
    # no default, never logged) is missing — fail closed. No execution happens in
    # any route. `RUN_IDEMPOTENCY_HMAC_KEY` is deliberately NOT a Settings field.
    run_public_api_enabled: bool = False
    run_sse_poll_interval_s: int = 2          # DB poll cadence for the SSE tail
    run_sse_heartbeat_s: int = 15             # `: heartbeat` comment cadence (no fake events)
    run_sse_max_seconds: int = 1800          # hard SSE connection lifetime cap
    run_text_event_max_bytes: int = 8192     # per `text` run_events row (UTF-8, whole codepoints)
    # ── Phase 1B.2R MP-3B — worker-backed durable chat generation ──
    # OFF by default. When on (AND `run_engine_enabled`), the RunWorker's chat
    # driver claims/advances durable `chat_stream` runs itself (they are then
    # excluded from the generic claim/recover paths), invokes the existing
    # LLMProvider streaming seam once, persists text through TextEventBuffer,
    # heartbeats its lease, and creates the final assistant Message exactly once.
    # Still NOT a `/messages/stream` cutover (MP-3C). Provider attempts = 1.
    run_chat_stream_enabled: bool = False
    # ── Phase 1B.2R MP-3C2 — durable chat -> MCP approval -> same-run resume ──
    # Rides `run_chat_stream_enabled` (no new flag). When a durable chat segment
    # emits a COMPLETE [MCP_CALL] marker the chat run parks for approval via the
    # existing MP-2 gateway and, on approval, the SAME run resumes: the MCP call
    # executes at-most-once and generation continues with the sealed tool result.
    # Server-side only; the client never supplies any identity / hash / dedupe key.
    #   * marker-capture hard cap — a captured [MCP_CALL] block larger than this
    #     (UTF-8 bytes) fails closed (`tool_marker_oversized`); bounds the
    #     confidential capture buffer. Validated as >= 1.
    run_chat_tool_marker_max_bytes: int = 4096
    #   * max sequential MCP tool cycles per chat run — enforced from durable
    #     `run_steps` (count of tool_dispatch steps), never an in-memory counter.
    #     A request for cycle N+1 fails closed (`tool_cycle_limit`) with NO new
    #     proposal / turn / dispatch. Validated as >= 1.
    run_chat_max_tool_cycles: int = 3
    #   * max UTF-8 bytes of a tool-result envelope fed back to the provider as
    #     an untrusted, clearly-delimited data message (never the system prompt).
    #     A larger result is middle-truncated. Validated as >= 1.
    run_chat_tool_result_max_bytes: int = 8192

    # ── Phase 1B.2R MP-4C — approval-gated Python execution ──
    # OFF by default; never set true in any default/deployment config, and
    # never enabled in .env, Docker, or tests globally. This flag authorizes
    # ONLY the creation of a server-owned Python execution PROPOSAL (an
    # approval request bound to the run / turn / tool-call identity). It
    # NEVER authorizes execution: no flag combination permits a Python
    # subprocess, a model-directed memory write, or a knowledge-graph
    # mutation before a valid server-owned approval has been consumed.
    # When False, every model-emitted [PYTHON_CODE: …] marker fails closed —
    # no proposal, no subprocess, no success Message, and no marker text in
    # user-visible output. Legacy direct auto-execution is unreachable in
    # EVERY configuration, including True (see services/streaming_service.py
    # and services/langgraph_workflow.py, which now fail closed rather than
    # run model-emitted code).
    run_engine_sandbox: bool = False
    # Hard maximum UTF-8 size of ONE captured [PYTHON_CODE: …] body; a larger
    # body fails closed as oversized (bounded slug, never the code itself).
    run_engine_sandbox_max_code_bytes: int = 8192
    # Wall-clock seconds allowed for the approval-gated sandbox subprocess.
    run_engine_sandbox_timeout_s: int = 10
    # Captured stdout/stderr is truncated to this many bytes before sealing.
    run_engine_sandbox_max_output_bytes: int = 4000

    # -- Phase 1B.2R MP-4C -- approval-gated model memory/directive writes --
    # OFF by default. This flag authorizes ONLY the creation of a server-owned
    # PROPOSAL for a model-emitted [SAVE_MEMORY]/[SAVE_DIRECTIVE] marker in the
    # durable chat engine, plus the exact-once approved write through the
    # existing approval gateway + DispatchRecord ledger. It NEVER authorizes a
    # write without a consumed, owner-bound approval: with the flag off both
    # markers keep their historical fail-closed behaviour (zero proposals, zero
    # writes, bounded notice). Legacy marker processors stay fail-closed in
    # EVERY configuration. Validated as >= 1.
    run_engine_memory_write_enabled: bool = False
    # Maximum UTF-8 CHARACTER length of ONE captured save body. A larger body
    # fails closed (save_content_too_large) with NO proposal created, and the
    # approver preview refuses rather than truncating, so an approver always
    # sees the ENTIRE content that would be written. Validated as >= 1.
    run_engine_memory_write_max_chars: int = 2000

    # -- Phase 1B.2R MP-4C -- approval-gated model memory/directive writes --
    # OFF by default. This flag authorizes ONLY the creation of a server-owned
    # PROPOSAL for a model-emitted [SAVE_MEMORY]/[SAVE_DIRECTIVE] marker in the
    # durable chat engine, plus the exact-once approved write through the
    # existing approval gateway + DispatchRecord ledger. It NEVER authorizes a
    # write without a consumed, owner-bound approval: with the flag off both
    # markers keep their historical fail-closed behaviour (zero proposals, zero
    # writes, bounded notice). Legacy marker processors stay fail-closed in
    # EVERY configuration. Validated as >= 1.
    run_engine_memory_write_enabled: bool = False
    # Maximum UTF-8 CHARACTER length of ONE captured save body. A larger body
    # fails closed (save_content_too_large) with NO proposal created, and the
    # approver preview refuses rather than truncating, so an approver always
    # sees the ENTIRE content that would be written. Validated as >= 1.
    run_engine_memory_write_max_chars: int = 2000

    # ── YouTube Knowledge Ingestion — Phase 1 (MVP, backend-only) ──
    # OFF by default. When False a pasted YouTube link is IGNORED entirely: no
    # transcript fetch, no extraction call, no memory write, no notice — plain
    # chat behaviour, byte-for-byte identical to before this feature.
    # When True, a user-pasted supported YouTube URL in a chat message is
    # ingested: transcript -> bounded chunks -> LLM fact extraction -> memory
    # rows carrying source attribution (kind "youtube_knowledge").
    # This is USER-INITIATED ingest: the pasted link is the explicit consent, so
    # the write is direct and does NOT create an approval. MODEL-initiated saves
    # ([SAVE_MEMORY]/[SAVE_DIRECTIVE]) remain approval-gated per MP-4C — the two
    # paths stay separate and this flag never unlocks the model-initiated one.
    youtube_ingest_enabled: bool = False
    # Hard cap on the TRANSCRIPT characters accepted for ingestion. A longer
    # transcript is truncated to this bound (with a bounded notice), never
    # rejected outright. Validated as >= 1.
    youtube_ingest_max_chars: int = 120000
    # Transcript is split into chunks of at most this many characters, so one
    # oversized transcript becomes several bounded extraction calls instead of
    # one unbounded prompt. Validated as >= 1 and <= youtube_ingest_max_chars.
    youtube_ingest_chunk_size: int = 3000
    # Hard ceiling on the number of facts SAVED per video. Extraction stops
    # once this many new facts have been persisted. Validated as >= 1.
    youtube_ingest_max_facts_per_video: int = 20
    # Wall-clock bound (seconds) for the YouTube oEmbed title/channel lookup.
    # Metadata is BEST-EFFORT: a miss/timeout degrades to source_title=NULL and
    # never fails the ingest. Validated as a finite number > 0.
    youtube_ingest_oembed_timeout_s: float = 5.0

    # ── Local Vision (ZERO API cost) — moondream via Ollama ─────────────
    # OFF by default. When False an attached image is IGNORED by the local
    # vision path entirely: no Ollama call, no context injection, no notice —
    # plain chat behaviour, byte-for-byte identical to before this feature.
    # When True, an attached image (JPEG / PNG / WebP, <= local_vision_max_bytes)
    # is described by a LOCAL Ollama vision model. NO paid API is ever called.
    # Fail-closed: every failure is a bounded notice; the chat never breaks.
    local_vision_enabled: bool = False
    # The local Ollama model used for image description. "moondream" is a small
    # (~1.8B) CPU-friendly vision model that fits a 16GB RAM host with no GPU.
    local_vision_model: str = "moondream"
    # Hard cap on the raw image bytes accepted for a single description. A
    # larger image fails closed (bounded notice), never truncated silently.
    # Validated as >= 1.
    local_vision_max_bytes: int = 5242880
    # Wall-clock bound (seconds) for ONE local description call. A CPU-only
    # moondream run can take tens of seconds; a timeout fails closed with a
    # bounded notice. Validated as a finite number > 0.
    local_vision_timeout_s: float = 60.0

    # ── In-app Terminal panel (dev tool, separate from agent HITL) ─
    # OFF by default. When on, POST /api/terminal/execute streams the output
    # of an allowlisted command (argv[0] must be in the list), run with
    # shell=False, a fixed cwd, a minimal env, and a hard timeout. This is
    # NOT the agent-proposed-command approval flow ([PROPOSED_COMMAND]) —
    # that is untouched.
    #
    # The allowlist is a security boundary, so it holds ONLY commands that do
    # not themselves execute arbitrary code. Language interpreters and build
    # tools (python, node, pip, npm, make, go, cargo, pytest, …) are
    # deliberately excluded — `python -c`, `node -e`, `find -exec`,
    # `make <target>`, `pip install` (setup.py) are all RCE and no blocklist
    # can enclose them. `find` and `git` are allowed but arg-guarded in
    # validate_command() (no -exec/-delete; read-only git subcommands, no -c).
    # Adding an interpreter here re-opens full RCE for anyone who can reach
    # the endpoint — do it only on a trusted single-user box and know the cost.
    enable_terminal_panel: bool = False
    terminal_panel_timeout_s: int = 30
    terminal_panel_max_output_bytes: int = 200_000
    terminal_panel_allowlist: str = (
        "echo,printf,ls,pwd,cat,head,tail,wc,grep,rg,find,tree,stat,file,realpath,"
        "basename,dirname,sort,uniq,cut,nl,diff,cmp,sleep,true,false,"
        "git,df,du,uname,whoami,id,date,env,printenv,which,hostname,uptime,free"
    )

    # ── Browser automation (multi-agent Browser Operator) ─
    # OFF by default: lets the LangGraph team drive a real Chromium (click,
    # type, navigate) via [BROWSER_ACTION: ...]. Risky actions (login,
    # payment, delete, download) pause for user approval. SSRF allowlist is a
    # comma-separated host list that bypasses the private/loopback/metadata
    # block (e.g. "intranet.corp,10.0.0.5").
    enable_browser_automation: bool = False
    browser_ssrf_allowlist: str = ""
    browser_session_idle_seconds: int = 600

    # ── Deep Research (web search via DuckDuckGo — free, no API key) ──
    # When enabled, the agent can autonomously search the web, scrape the
    # top results, and synthesize a report with citations.
    enable_deep_research: bool = True
    # How many search results to fetch per research pass.
    deep_research_max_results: int = 5
    # How many of the top results to scrape per pass (hard cap — bounds the
    # search -> scrape -> synthesize loop so it can never run away).
    deep_research_max_scrape: int = 3

    # ── Durable Deep Research acquisition (Phase 1B.2R MP-3C3D4B) ──
    # Bounds for the safe, free/local-only search+fetch acquisition
    # primitive. Server-owned only — never client-overridable. Reuses
    # `deep_research_max_results` (results per query) and
    # `deep_research_max_scrape` (pages fetched) above; only NEW bounds are
    # added here.
    research_max_queries: int = 1
    research_max_query_chars: int = 300
    research_max_result_urls: int = 10
    research_max_redirects: int = 3
    research_fetch_timeout_s: float = 10.0
    research_total_deadline_s: float = 45.0
    research_max_response_bytes: int = 2_000_000
    research_max_extracted_chars: int = 20_000

    # ── Media Generation (Image/Video) ───────────────────────
    # Master switch for media generation endpoints.
    enable_media_generation: bool = True
    # Primary media provider: "venice" | "nexa" | "agnes"
    # Falls back in order: Venice -> Nexa -> Agnes
    media_provider_primary: str = "venice"
    # Default image model (provider-specific)
    media_default_image_model: str = "flux-schnell"
    # Default video model (provider-specific)
    media_default_video_model: str = "seedance-2-0-text-to-video-basic"
    # Video polling interval (seconds)
    media_video_poll_interval_s: int = 5
    # Video generation timeout (seconds)
    media_video_timeout_s: int = 300
    # Rate limit for media endpoints (requests per minute per user)
    media_rate_limit_per_minute: int = 10

    # ── Agent skills (Claude-style SKILL.md, progressive disclosure) ──
    # Directory containing one folder per skill, each with a SKILL.md file.
    # Only name+description are injected (L1); full bodies load on demand via
    # [USE_SKILL: <name>] (L2). Defaults to <package>/skills.
    skills_dir: str = ""
    # Extra directories to search for skills (in addition to the bundled
    # "skills/", user-global, and project-local locations the SkillManager
    # discovers by default). Defaults to the package <app>/skills directory so
    # bundled skills are always discoverable.
    extra_paths: List[str] = [_DEFAULT_SKILLS_DIR]

    # ── Thunder skills (progressive disclosure) ───────────────
    # Master switch for the skills feature.
    thunder_skills_enabled: bool = True
    # Extra skill paths, colon/newline separated (merged into discovery).
    thunder_skills_paths: str = ""
    # Comma-separated list of skill names to disable/blacklist.
    thunder_skills_disabled: str = ""
    # Invocation mode: "auto" (manager decides) | "marker" (<skill> text) | "tool".
    # Default "auto" lets free models use the text marker and capable models
    # use the native `skill` tool.
    thunder_skills_invocation: str = "auto"
    # Max tokens the L1 skill index may consume in the system prompt.
    thunder_skills_index_budget_tokens: int = 600
    # Max number of skills shown in the L1 index.
    thunder_skills_max_indexed: int = 20

    # ── Model / LLM provider settings ───────────────────
    # Provider: "glm" | "ollama" | "openrouter" | "nvidia" | "huggingface" | "google" | "modelslab"
    #           "groq" | "together" | "mistral" | "cohere"
    llm_provider: str = "glm"
    default_model: str = "glm-5.3-flash"
    ollama_tags_timeout: int = 5

    # ── Secrets encryption at rest (Phase — security overhaul) ──
    # URL-safe base64 Fernet key (32 bytes) used to encrypt API keys before
    # they are stored. Generate with: python -c "from cryptography.fernet
    # import Fernet; print(Fernet.generate_key().decode())". If unset, an
    # ephemeral key is generated per process with a loud log warning.
    encryption_key: str = ""

    # GLM 5.3 Flash — local Verdent Free Router (OpenAI-compatible, keyless).
    # Default free model: used directly and as the automatic fallback whenever
    # a user's configured provider fails (call error / timeout / non-200).
    glm_enable: bool = True
    glm_api_key: str = ""  # optional; VERDENT_API_KEY env var also honoured
    glm_base_url: str = "http://127.0.0.1:8320/v1"
    glm_model: str = "glm-5.3-flash"
    glm_max_tokens: int = 2048
    glm_temperature: float = 0.7

    # OpenRouter (OpenAI-compatible API)
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "meta-llama/llama-3.2-3b-instruct:free"
    openrouter_max_tokens: int = 2048
    openrouter_temperature: float = 0.7

    # NVIDIA NIM (OpenAI-compatible API)
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "nvidia/llama-3.1-nemotron-70b-instruct"
    nvidia_max_tokens: int = 2048
    nvidia_temperature: float = 0.7

    # Hugging Face (OpenAI-compatible Serverless Inference API)
    huggingface_api_key: str = ""
    huggingface_base_url: str = "https://router.huggingface.co/v1"
    huggingface_model: str = "meta-llama/Meta-Llama-3-8B-Instruct"
    huggingface_max_tokens: int = 2048
    huggingface_temperature: float = 0.7

    # Google AI Studio / Gemini (OpenAI-compatible API)
    google_api_key: str = ""
    google_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    google_model: str = "gemini-2.0-flash"
    google_max_tokens: int = 2048
    google_temperature: float = 0.7

    # ModelsLab (OpenAI-compatible API - assumed)
    modelslab_api_key: str = ""
    modelslab_base_url: str = "https://modelslab.com/api/v1"
    modelslab_model: str = "meta-llama/Llama-3.3-70B-Instruct"
    modelslab_max_tokens: int = 2048
    modelslab_temperature: float = 0.7

    # Groq (OpenAI-compatible API)
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "llama-3.3-70b-versatile"
    groq_max_tokens: int = 2048
    groq_temperature: float = 0.7

    # Together AI (OpenAI-compatible API)
    together_api_key: str = ""
    together_base_url: str = "https://api.together.xyz/v1"
    together_model: str = "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"
    together_max_tokens: int = 2048
    together_temperature: float = 0.7

    # Mistral (OpenAI-compatible API)
    mistral_api_key: str = ""
    mistral_base_url: str = "https://api.mistral.ai/v1"
    mistral_model: str = "mistral-small-latest"
    mistral_max_tokens: int = 2048
    mistral_temperature: float = 0.7

    # Cohere (OpenAI-compatible API)
    cohere_api_key: str = ""
    cohere_base_url: str = "https://api.cohere.ai/v1"
    cohere_model: str = "command-r-plus"
    cohere_max_tokens: int = 2048
    cohere_temperature: float = 0.7

    # Venice AI (OpenAI-compatible API) — primary media provider
    venice_api_key: str = ""
    venice_base_url: str = "https://api.venice.ai/api/v1"
    venice_image_model: str = "flux-schnell"
    venice_video_model: str = "seedance-2-0-text-to-video-basic"

    # NexaAPI (OpenAI-compatible API) — fallback media provider
    nexa_api_key: str = ""
    nexa_base_url: str = "https://nexa-api.com/v1"
    nexa_image_model: str = "flux-schnell"
    nexa_video_model: str = "kling-video"

    # Agnes AI (OpenAI-compatible API) — free fallback media provider
    agnes_api_key: str = ""
    agnes_base_url: str = "https://apihub.agnes-ai.com/v1"
    agnes_image_model: str = "agnes-image-2.1-flash"
    agnes_video_model: str = "agnes-video-2.5-flash"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @model_validator(mode="after")
    def _validate_approval_limits(self) -> "Settings":
        """Fail-fast on a nonsensical self-host limit config (Phase 1B.2 / 1B.2R)."""
        for name in (
            "max_active_proposals_per_turn",
            "max_active_proposals_per_session",
            "max_active_proposals_per_user",
            "max_mcp_calls_per_response",
            "run_payload_max_bytes",
            "run_event_text_flush_bytes",
            "run_event_text_flush_ms",
            "run_idempotency_key_max_bytes",
            "run_control_event_max_bytes",
            "run_event_read_max_page",
            "run_worker_lease_ttl_s",
            "run_worker_poll_interval_s",
            "run_worker_max_attempts",
            "run_worker_handler_timeout_s",
            "run_worker_recover_batch",
            "run_sse_poll_interval_s",
            "run_sse_heartbeat_s",
            "run_sse_max_seconds",
            "run_text_event_max_bytes",
            "run_chat_tool_marker_max_bytes",
            "run_chat_max_tool_cycles",
            "run_chat_tool_result_max_bytes",
            # Phase 1B.2R MP-4C — approval-gated Python execution bounds.
            "run_engine_sandbox_max_code_bytes",
            "run_engine_sandbox_timeout_s",
            "run_engine_sandbox_max_output_bytes",
            # Phase 1B.2R MP-4C -- approval-gated memory/directive write bound.
            "run_engine_memory_write_max_chars",
            "run_worker_web_concurrency",
            "run_worker_shutdown_grace_s",
            "run_worker_backoff_min_s",
            "run_worker_backoff_max_s",
            "run_messages_stream_max_concurrent_per_user",
            "run_messages_stream_submits_per_min",
            # Phase 1B.2R MP-3C3D4B — durable deep-research acquisition bounds
            # (int-valued only; the two float timeout/deadline fields are
            # validated separately below, since a plain `isinstance(x, int)`
            # check would wrongly reject a valid float).
            "research_max_queries",
            "research_max_query_chars",
            "research_max_result_urls",
            "research_max_redirects",
            "research_max_response_bytes",
            "research_max_extracted_chars",
            # Phase 1B.2R MP-3C3D5B — durable multi-agent artifact bounds
            # (int-valued only; the two float timeout/deadline fields, and
            # the ==3 / artifact-byte-cap consistency checks, are validated
            # separately below).
            "multi_agent_max_artifact_bytes",
            "multi_agent_max_content_chars",
            "multi_agent_max_claims",
            "multi_agent_max_assumptions",
            "multi_agent_max_uncertainties",
            "multi_agent_max_active_runs_per_user",
            # YouTube Knowledge Ingestion (Phase 1) — integer bounds.
            "youtube_ingest_max_chars",
            "youtube_ingest_chunk_size",
            "youtube_ingest_max_facts_per_video",
            # Local Vision (ZERO API cost) — image byte cap.
            "local_vision_max_bytes",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be an integer >= 1 (got {value!r})")
        return self

    @model_validator(mode="after")
    def _validate_youtube_ingest_limits(self) -> "Settings":
        """Fail-fast on a nonsensical YouTube-ingest config (Phase 1). The
        integer bounds are covered by `_validate_approval_limits`; the two
        relationships between them, and the float oEmbed timeout, are not."""
        import math

        value = self.youtube_ingest_oembed_timeout_s
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or math.isnan(value) or math.isinf(value) or value <= 0):
            raise ValueError(
                f"youtube_ingest_oembed_timeout_s must be a finite number > 0 "
                f"(got {value!r})"
            )
        if self.youtube_ingest_chunk_size > self.youtube_ingest_max_chars:
            raise ValueError(
                "youtube_ingest_chunk_size must not exceed "
                "youtube_ingest_max_chars (a chunk larger than the whole "
                "accepted transcript would make the chunk bound meaningless)"
            )
        return self

    @model_validator(mode="after")
    def _validate_local_vision_timeout(self) -> "Settings":
        """Fail-fast on a nonsensical local-vision timeout (Local Vision,
        ZERO API cost). The byte cap is covered by `_validate_approval_limits`;
        the float timeout is not."""
        import math

        value = self.local_vision_timeout_s
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or math.isnan(value) or math.isinf(value) or value <= 0):
            raise ValueError(
                f"local_vision_timeout_s must be a finite number > 0 "
                f"(got {value!r})"
            )
        return self

    @model_validator(mode="after")
    def _validate_research_timeouts(self) -> "Settings":
        """Fail-fast on a nonsensical durable deep-research timeout config
        (Phase 1B.2R MP-3C3D4B). Both fields are floats (seconds), so they
        cannot share `_validate_approval_limits`'s integer-only check above.
        Server-owned only — never client-overridable."""
        import math

        for name in ("research_fetch_timeout_s", "research_total_deadline_s"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or math.isnan(value) or math.isinf(value) or value <= 0):
                raise ValueError(f"{name} must be a finite number > 0 (got {value!r})")
        if self.research_fetch_timeout_s >= self.research_total_deadline_s:
            raise ValueError(
                "research_fetch_timeout_s must be strictly less than "
                "research_total_deadline_s (a single fetch may never be "
                "allowed to consume the entire acquisition deadline)"
            )
        return self

    @model_validator(mode="after")
    def _validate_multi_agent_limits(self) -> "Settings":
        """Fail-fast on a nonsensical durable multi-agent config
        (Phase 1B.2R MP-3C3D5B). Server-owned only — never client-
        overridable."""
        import math

        if self.multi_agent_max_roles != 3 or self.multi_agent_max_provider_calls != 3:
            raise ValueError(
                "multi_agent_max_roles and multi_agent_max_provider_calls must "
                "both be exactly 3 — the durable multi-agent role graph "
                "(app.run_engine.multi_agent_orchestration.ROLE_ORDER) is a "
                "fixed 3-role, 3-call sequence, not a tunable value"
            )
        for name in ("multi_agent_role_timeout_s", "multi_agent_total_deadline_s"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or math.isnan(value) or math.isinf(value) or value <= 0):
                raise ValueError(f"{name} must be a finite number > 0 (got {value!r})")
        if self.multi_agent_role_timeout_s >= self.multi_agent_total_deadline_s:
            raise ValueError(
                "multi_agent_role_timeout_s must be strictly less than "
                "multi_agent_total_deadline_s (a single role's provider call "
                "may never be allowed to consume the entire run deadline)"
            )
        if self.multi_agent_max_artifact_bytes > self.run_payload_max_bytes:
            raise ValueError(
                "multi_agent_max_artifact_bytes must not exceed "
                "run_payload_max_bytes (the sealed-payload envelope's own "
                "hard cap)"
            )
        return self

    @property
    def chromadb_url(self) -> str:
        return f"http://{self.chromadb_host}:{self.chromadb_port}"

    @property
    def ollama_url(self) -> str:
        return f"http://{self.ollama_host}:{self.ollama_port}"


settings = Settings()
