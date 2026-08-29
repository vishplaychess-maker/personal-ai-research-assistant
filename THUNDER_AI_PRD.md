# Thunder AI — Project Source of Truth

> **Read this file first at the start of every session, or any time context is lost,
> before making any changes.** This is the core memory and single source of truth
> for the "Viral Expansion" work on Thunder AI.

---

## Strict Working Rules (always in force)

1. **Skill selection & consistency.** Review the PRD thoroughly. Identify and select
   the necessary skills/tools for the work, and use them consistently until a
   feature set is fully implemented. Committed skill set for this project:
   - `superpowers:brainstorming` — before each phase's design
   - `superpowers:writing-plans` — turn each phase into a reviewable step plan
   - `context7` — verify real library/framework APIs before writing code
   - `claude-api` — for Anthropic-facing surfaces (Computer Use, MCP)
   - `superpowers:test-driven-development` — per feature, before implementation code
   - `frontend-design` — Swarm UI (F3) and the no-code canvas (F4)
   - `ponytail` — keep every feature to the smallest thing that works
2. **No hallucination.** Do NOT invent APIs, library functions, or architecture
   patterns. Read the actual files and verify library APIs before writing code.
3. **Ask, don't assume.** On any doubt, roadblock, or question about the existing
   codebase or intent — STOP and ask directly. Do not guess.

---

## PRD: Thunder AI — Viral Features Expansion

### 1. Objective
Upgrade Thunder AI from a powerful tool into a highly engaging, viral product by
introducing visual automation, decentralized integrations, and shareable workflows.

### 2. Current Tech Stack (Context)
- **Backend:** FastAPI, Python 3.11/3.12, LangGraph, SQLAlchemy, ChromaDB.
- **Frontend:** React 18, Vite, TypeScript, Tailwind CSS, shadcn/ui.
- **Existing Tools:** Playwright (headless), Python Sandbox, Terminal access, APScheduler.

### 3. New Feature Requirements

#### Feature 1: True Browser Automation (Visible Action)
- Upgrade the existing headless Playwright scraper to support live, visible browser
  automation using the Anthropic Computer Use API or an open-source alternative
  like `browser-use`.
- The frontend must stream the visual progress (or step-by-step logs) of the agent
  interacting with web pages (clicking, typing, navigating).

#### Feature 2: Model Context Protocol (MCP) Integration
- Implement an MCP Client in the FastAPI backend.
- Allow the user to connect external MCP servers (e.g., local file systems, GitHub,
  Slack) directly through the Settings UI.
- Thunder AI's routing agent should dynamically register tools exposed by the
  connected MCP servers.

#### Feature 3: Multi-Agent Teams (Swarm Intelligence UI)
- Expand the LangGraph backend to support multiple specialized agents
  (e.g., Coder, Reviewer, Researcher).
- Frontend: Create a chat UI mode where the user can see agents conversing with
  each other in real-time to solve a complex prompt before delivering the final
  answer to the user.

#### Feature 4: Visual Workflow Builder (No-Code Canvas)
- Implement a drag-and-drop canvas in the frontend (using a library like React Flow).
- Allow users to visually connect LLM nodes, tools (Scraper, Terminal, Sandbox),
  and logical conditions.
- The backend must parse this visual graph JSON and convert it into an executable
  LangGraph StateGraph.

#### Feature 5: 1-Click Shareable Agents
- Create an export/import mechanism for custom workflows and scheduled tasks.
- Generate a shareable JSON config or unique URL parameters.
- When a new user imports this config, it automatically sets up the system prompt,
  tools, and schedule in their local Thunder AI instance.

### 4. Implementation Phases
- **Phase 1:** MCP Integration (highest utility, easiest architectural addition).
- **Phase 2:** True Browser Automation & Multi-Agent UI.
- **Phase 3:** Visual Workflow Builder & Export capabilities.

---

## Phase 1 — MCP Integration: agreed constraints

- **Branch:** `feat/mcp-integration`, cut from `feat/playwright-web-tool` after
  committing the accumulated fixes.
- **Transport scope (first cut):** `stdio` MCP servers only (local filesystem,
  SQLite, etc.). Remote/SSE servers come later.
- **Tool registry:** there is no central dynamic tool-registration layer yet —
  tools are imported and bound directly in
  `backend/app/services/langgraph_workflow.py`. Phase 1 introduces a minimal,
  scalable registry that handles both native Python tools and dynamic MCP tools.
- **Settings UI:** users add/remove `stdio` MCP servers via the existing Settings
  screen (CRUD, mirroring the multi-LLM provider manager pattern).

---

## Project architecture notes (verified, current)

- **Backend code is baked into the Docker image** (no source bind-mount; only
  `app_data:/data`). Any backend change requires
  `docker compose up -d --build backend` + container recreate.
  Frontend has `./frontend:/app` + Vite HMR, so `src/` and `index.html` hot-reload;
  `vite.config.ts` changes need a container restart.
- **Schema migrations are hand-rolled** in `_migrate_database()` in
  `backend/app/main.py`, run on startup in `lifespan`. `create_all()` only creates
  missing tables, never adds columns — every new model column needs a guarded
  `ALTER TABLE ADD COLUMN` block there.
- **DB tables:** `users`, `research_sessions`, `messages`, `documents`, `memories`,
  `user_providers`, `scheduled_tasks` (SQLite).
- **Two chat paths:** streaming SSE (`/api/sessions/{id}/messages/stream` →
  `streaming_service.py`, used by the React UI) and non-streaming
  (`/api/sessions/{id}/messages` → `langgraph_workflow.run_research_workflow`).
- **Existing agent tools:** `web_scraper` (Playwright), `youtube_summarizer`,
  Python sandbox, terminal executor (HITL via `[PROPOSED_COMMAND: ...]`).
  Wired into `browse_web` / `generate_answer` nodes and the streaming service.
- **CAG answer cache:** `backend/app/services/cache_service.py` — session-scoped
  in-process TTL cache, wired into both chat paths.
