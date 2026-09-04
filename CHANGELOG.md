# Changelog

All notable changes to the Personal AI Research Assistant.

## [Unreleased]

### Semantic Caching (RAG-based CAG)
- Upgraded the exact-match CAG cache (`backend/app/services/cache_service.py`)
  to Semantic Caching. Besides the SHA-256 exact key (fast path, zero embedding
  cost), it now keeps an in-memory list of embedding vectors with each answer.
- `find_semantic_match(session_id, query_embedding, threshold)` computes cosine
  similarity against cached answers for the same session; a match returns the
  cached answer with a `[Semantic Cache Hit]` prefix.
- `get(session_id, question)`: exact-match fast path first, then embeds the
  query (Ollama `nomic-embed-text`) and runs semantic matching.
- `set(session_id, question, answer)`: stores the exact entry and appends a
  semantic entry; TTL and `MAX_ENTRIES`/`SEMANTIC_MAX_ENTRIES` limits kept.
- Threshold tuned to `0.80` from real output (strong paraphrases ~0.76-0.94 vs
  unrelated ~0.3-0.7).
- Graceful degradation: if Ollama is offline/unreachable or the embedding model
  is missing, embedding calls are caught and the cache silently falls back to
  exact-match only — it never crashes the chat.
- Both `streaming_service.py` and `langgraph_workflow.py` call sites use the
  upgraded `get`/`set`, and no longer double-label semantic hits.
- Requires the `nomic-embed-text` Ollama model (`ollama pull nomic-embed-text`).

### Agent Skills (Claude-style SKILL.md, progressive disclosure)
- New `backend/app/skills/` package implementing Claude-style Agent Skills.
  Skills are folders containing a `SKILL.md` with YAML frontmatter (`name`,
  `description`, optional `pinned`) plus markdown instructions.
- `parser.py`: `Skill` dataclass + `parse_skill_md()` — parses frontmatter and
  body, validates skill names (`^[a-z0-9]+(-[a-z0-9]+)*$`), caps description
  at 250 chars. Defensive: returns `None` on unreadable/unparseable files.
- `loader.py`: **progressive disclosure** in two layers —
  - **L1 (cheap):** `skills_catalog()` scans the skills dir and renders ONLY
    each skill's `name` + `description` into a compact prompt block, keeping
    the system prompt small for limited-context models.
  - **L2 (on demand):** `load_skill_body(name)` returns a single skill's full
    `SKILL.md` body. The model requests it by emitting `[USE_SKILL: <name>]`
    (regex `USE_SKILL_PATTERN`, extracted by `extract_skill_calls`).
- Wired in: L1 catalog + `SKILLS_TOOL_CONTEXT` injected into `build_base_prompt`
  (`system_prompts.py`); L2 loads any skill requested in the user message
  upfront in both `streaming_service.prepare_chat_context` and
  `langgraph_workflow._build_system_prompt`. All defensive — never breaks chat.
- Config: optional `SKILLS_DIR` (`skills_dir` setting); defaults to
  `<package>/skills`.
- `manager.py`: `SkillManager` — canonical discovery engine with precedence
  (bundled `<backend>/skills` → user-global `~/.thunder/skills` → project-local
  `.thunder/skills` → `extra_paths`), `get_skill_index()` (L1) and
  `get_skill_body()` (L2). Later-listed source wins; symlink escapes guarded.
  The bundled path is resolved from the module so it works regardless of the
  process working directory (e.g. `/app/skills` in Docker).
- `tools/skill_tool.py`: native `@tool skill(name)` (L3) — returns the skill
  body and lists up to 25 resource files for the skill directory.
- **Marker protocol (free-model text fallback):** the model requests a skill by
  emitting `<skill>name</skill>`, `[USE_SKILL: name]`, or `USE SKILL: name`.
  `extract_skill_calls` detects these; `process_skill_markers` strips markers
  from the user-visible answer. Wired into `streaming_service.stream_chat_response`
  (on `done`) and `langgraph_workflow.generate_answer` (before saving to state).
- Example skills (bundled): `commit-message`, `code-review`, `web-research` —
  now in `backend/skills/` (the consolidated bundled location).
- Fix: skill marker regex now captures names containing spaces/dashes
  (`<skill>commit - message</skill>`), and `sanitize_skill_name` normalises
  them to the canonical key (spaces -> hyphens) before lookup.
- Fix: new `SkillStreamFilter` strips skill markers from the live SSE token
  stream so they never reach the UI, and the `done` handler always emits a
  `complete` event so the frontend never hangs on "Generating...".

### Deep Research Mode (DuckDuckGo web search — free, no API key)
- Added `web_search` tool and `run_deep_research` helper (`backend/app/tools/web_search.py`)
  that use DuckDuckGo for real-time web search. 100% free, no API key required. The agent
  autonomously searches, scrapes the top results, and synthesizes a cited report.
- New LangGraph `deep_research` node sits between `browse_web` and `generate_answer`:
  when the user hasn't pasted a URL and deep research is enabled, the node
  searches the web, scrapes the top results via the existing `web_scraper`, and injects
  the context into the system prompt. Bounded by `DEEP_RESEARCH_MAX_SCRAPE` (default 3).
- Added `WEB_SEARCH_TOOL_CONTEXT` to the system prompt (`system_prompts.py`) with full
  Search -> Scrape -> Synthesize -> Cite instructions.
- Added `deep_research_context` / `deep_research_used` keys to `WorkflowState`
  (`langgraph_workflow.py`) and the `prepare_chat_context` path (`streaming_service.py`).
- Graceful degradation: when DuckDuckGo fails, deep research silently no-ops and the
  agent answers from its existing knowledge.
- Config: `ENABLE_DEEP_RESEARCH=true`, `DEEP_RESEARCH_MAX_RESULTS=5`,
  `DEEP_RESEARCH_MAX_SCRAPE=3` (`.env.example`, `docker-compose.yml`, `config.py`).
- Dependency: `duckduckgo-search` added to `backend/requirements.txt`.

### Quick Provider Switcher (chat header)
- Added a premium dropdown in the chat header (`frontend/src/ProviderSwitcher.tsx`) that
  shows the currently active LLM provider (branded mark + name) and lists every
  configured provider with a green dot/checkmark on the active one.
- Switching is one click: calls `PUT /api/providers/{id}` with `{ is_active: true }`
  (which deactivates all other providers for the user server-side), updates the
  trigger instantly, and bumps a `refreshKey` that tells the `ModelSelector` to
  reload the model list for the newly activated provider.
- Includes an empty state ("Go to Settings to add a provider") and graceful
  loading/error states; degrades cleanly when no providers are configured.

### F6 — Thoughtful Agent UX

#### Capability 2: Self-evaluation / confidence score (advisory)
- Added evaluation service (`backend/app/services/evaluation_service.py`) that asks the
  LLM to score how confident it is in its answer (`0-100` plus a one-line reason).
  Fully defensive: any parse/provider failure falls back to a null score and never
  breaks the chat.
- Added the `self_evaluate` LangGraph node between answer generation and save
  (`backend/app/services/langgraph_workflow.py`). Confidence is advisory and never
  blocks or alters the answer.
- Persisted `confidence` + `confidence_reason` on assistant `Message` rows
  (new nullable columns + startup migration), including on the streaming chat path.
- Added a color-coded `ConfidenceBadge` to assistant messages in the UI
  (`frontend/src/ConfidenceBadge.tsx`): green (80-100), amber (50-79), red (0-49),
  hidden when the score is null.
- New tests: `tests/test_evaluation.py` (10 passing).

#### Capability 3: Self-improving agent (persistent failure memory + dynamic directives)
- Management UI for the agent's learned "Lessons Learned":
  - New `Agent Directives` tab in Settings (`frontend/src/AgentDirectives.tsx`) that
    lists every saved directive with its content and Active/Inactive status.
  - Each directive has an Enable/Disable toggle (calls `PATCH /api/directives/{id}`)
    and a Delete button (calls `DELETE /api/directives/{id}`), with a friendly
    empty state and graceful loading/error handling.
- New backend routes (`backend/app/routes/directives.py`, registered in `main.py`):
  `GET /api/directives`, `PATCH /api/directives/{id}`, and
  `DELETE /api/directives/{id}` — all owner-scoped (a user can only see/toggle/
  delete their own directives) and CSRF-protected.
- Frontend API layer updated (`frontend/src/api.ts`) with `listDirectives`,
  `toggleDirective`, and `deleteDirective`; `AgentDirective` type added to
  `frontend/src/types.ts`.
- New `AgentDirective` table (`backend/app/models/models.py`) storing durable
  "lessons learned" per user, with a guarded startup migration in
  `backend/app/main.py` (created only if the table is missing).
- New `save_directive` tool (`backend/app/tools/directive_tool.py`) using the
  existing marker protocol: the LLM emits `[SAVE_DIRECTIVE: <lesson>]`, the
  pipeline persists an active `AgentDirective` row and strips the marker from
  the user-visible response (mirrors `[SAVE_MEMORY: ...]`). Directives shorter
  than 8 chars are skipped as trivial.
- Active directives are injected into every future system prompt under an
  `=== Active Directives ===` section (`backend/app/services/system_prompts.py`
  `directives_context`), so the agent follows them from then on. Injection is
  wired into both the streaming chat path (`streaming_service.py`) and the
  LangGraph path (`langgraph_workflow.py` `_build_system_prompt`), and always
  degrades to a no-op on failure.
- Emit instruction added to the base system prompt (`DIRECTIVES_TOOL_CONTEXT`).
- Self-reflection hook: the `self_evaluate` LangGraph node now triggers a
  best-effort reflection LLM call when confidence is below 60, and persists any
  `[SAVE_DIRECTIVE: ...]` it produces (advisory, never blocks or alters the answer).
- Fully defended so the chat is never broken: directive lookup/persistence/
  reflection all swallow errors and return clean fallbacks.

#### Capability 1: Plan-then-execute (review-only preview)
- Added plan generation (`backend/app/services/planning_service.py`) that proposes
  a step-by-step plan before an answer, surfaced to the user as a review-only
  `PlanCard` (`frontend/src/PlanCard.tsx`). Execution of the plan is a v1 no-op
  (planned for a later release).
- New `plan` SSE event in the streaming chat path.
- New tests: `tests/test_planning.py` (10 passing).

### Bug fixes
- Fixed "hi" (or any first message) getting no reply: the default Ollama chat model
  was `llama3.2:3b`, which was not installed on the host Ollama, so every request
  returned `Ollama HTTP 404: model ... not found` and no assistant message was saved.
  The default is now `dolphin-mistral`, which is actually installed
  (`backend/app/config.py`). The stream now completes and the response is persisted.
