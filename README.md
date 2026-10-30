# ⚡ Thunder AI

**A Next-Gen, Production-Ready AI Operating System with Multi-Agent Swarms, Self-Evolving Memory, and Secure Automation.**

Thunder AI is a local-first AI workspace: a FastAPI backend, a React dashboard, a multi-provider LLM layer, autonomous agents with real-time streaming, and a memory stack that combines semantic retrieval, document RAG, and a browsable long-term context database.

---

## 🚀 Features

- 🧠 **Multi-Agent Swarm Collaboration**: Sequential Planner → Researcher → Synthesizer workflows with guarded fallbacks — any swarm failure transparently degrades to the single-agent pipeline.
- 🕸️ **OpenViking Self-Evolving Memory**: Sidecar context database for long-term memory and skill registry (`viking://` tree), fail-open and timeout-guarded: when the sidecar is down the app behaves as if it were never enabled.
- 🔬 **REA Integration (229 Tools)**: Secure, workspace-confined reverse engineering and binary analysis. Only a 20-tool read-only allowlist is reachable, and every path argument is confined to the workspace root.
- 📄 **Bulletproof Document Upload & RAG**: Magic-byte validated PDF/DOCX/image parsing, strict context-window management, and source citations. Unparsable files surface an explicit error instead of silently indexing binary garbage.
- 🤖 **OpenBot Autonomous Agent**: Real-time SSE event streaming, workspace confinement, and LLM-driven smart synthesis over a deterministic fallback template.
- 🧩 **Custom Skill Marketplace**: Dynamic plugin loading for user-defined Python skills, wired into both chat and agent execution with per-user enablement.
- 🌐 **Secure Built-in Browser**: X-Frame-Options / CSP framability probing with sandboxed iframe rendering.
- ⚙️ **Open Interpreter Integration**: LLM-driven code execution with strict **Ask / Auto / Full** permission controls.
- 🎨 **Premium UI/UX**: Split-view settings dashboard, modern capability cards, and a dedicated Plugins management tab.

---

## 🏗️ Architecture

```
┌────────────────────────────┐        ┌────────────────────────────┐
│  Frontend — React + Vite   │  /api  │  Backend — FastAPI         │
│  http://127.0.0.1:5173     │ ─────▶ │  http://127.0.0.1:8080     │
└────────────────────────────┘        └───┬──────┬──────┬──────────┘
                                          │      │      │
                        ┌─────────────────┘      │      └──────────────────┐
                        ▼                        ▼                         ▼
              ┌──────────────────┐    ┌──────────────────┐    ┌───────────────────────┐
              │ SQLite / Postgres│    │ ChromaDB (RAG)   │    │ Ollama (local LLMs)    │
              │ users, memory,   │    │ document chunks  │    │ chat + nomic-embed-text│
              │ tasks, prefs     │    └──────────────────┘    └───────────────────────┘
              └──────────────────┘
                        ▲
        ┌───────────────┴───────────────────────────────────────────┐
        │ Mem0 mirror (semantic memory ranking, embedded Qdrant)    │
        │ OpenViking sidecar (long-term context, port 1933)         │
        │ REA MCP server (workspace-confined analysis)              │
        └───────────────────────────────────────────────────────────┘
```

| Layer | Technology | Role |
|---|---|---|
| Backend | Python 3.10+, FastAPI + SQLAlchemy | REST API, SSE streams, task workers |
| Frontend | React 18, Vite, TypeScript, Tailwind, Radix | Dashboard, chat, settings |
| Semantic memory | Mem0 + embedded Qdrant + Ollama embeddings | Ranks approved memories per user |
| Document RAG | ChromaDB HTTP | Chunk-level retrieval for uploaded docs |
| Structured context | OpenViking sidecar (`:1933`) | Browsable long-term memory and skill registry |
| Local AI | Ollama (`nomic-embed-text`, chat/VLM models) | Embeddings and offline inference |
| Isolation | Separate sidecar venv on `D:\openviking` | Heavy dependencies never touch the app venv |

**Design principles**

- **Fail-open enhancements**: Mem0, OpenViking, REA, and the swarm are strictly additive. Every call is timeout-guarded and degrades to a safe default — they can slow a feature down, never take the app down.
- **Per-user isolation**: memory, tasks, skills, and capability toggles are scoped by `user_id`; cross-user reads are indistinguishable from 404s.
- **Workspace confinement**: filesystem tools reject `..`, absolute paths outside `WORKSPACE_DIR`, symlink escapes, and sensitive filenames (`.env`, `.git`, keys).

---

## 🛠️ Setup & Installation

### 1. Prerequisites

- **Node.js ≥ 24.22.1** (24.22.2 recommended; a portable install such as `D:\nodejs\node-v24.22.2-win-x64` works fine). Required by the frontend and by the REA tooling.
- **Python 3.10+** with a virtual environment at `.venv`.
- **Ollama** running locally at `http://localhost:11434` with the embedding model:
  ```bash
  ollama pull nomic-embed-text
  ```
  (add any chat model you plan to use locally, e.g. `ollama pull llama3.2:1b`).
- **Docker Desktop** — only if you want the containerised ChromaDB (`docker compose up -d chromadb`). A local Chroma server on port 8000 works equally well.
- **Optional integrations**: REA (`rea-agents` via Node ≥ 24.22), Open Interpreter, Tailscale/Cloudflare Tunnel for remote access.

### 2. Quick Start

```bash
# 1) Backend environment
python -m venv .venv
.venv\Scripts\activate
pip install -r backend/requirements.txt

# 2) Required secrets (.env in backend/)
python scripts/generate_encryption_key.py   # prints a Fernet key
#   -> backend/.env  :  ENCRYPTION_KEY=<printed value>

# 3) Vector store (choose one)
docker compose up -d chromadb               # OR a local Chroma HTTP server on :8000

# 4) Backend — run from the backend/ directory
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8080

# 5) Frontend — new terminal
cd frontend
npm install
npm run dev                                 # http://localhost:5173
```

Optional sidecar (recommended for long-term memory):

```bash
start_openviking.bat                        # OpenViking server on http://127.0.0.1:1933
```

Then start the backend with `OPENVIKING_ENABLED=true` to let OpenBot tasks query structured context. The sidecar is opt-in at both the server level (`OPENVIKING_ENABLED`) and per user (Settings → Plugins → OpenViking Memory).

> Note: the repository ships `start_openviking.bat` for the memory sidecar. There is no combined backend+frontend launcher script; run the two commands above (or wire them into your own `.bat`).

### 3. Verification

1. Open **http://localhost:5173** and create an account.
2. Go to **Settings → Plugins** and enable the capabilities you want (OpenBot, OpenViking, Custom Skills, REA, Open Interpreter…).
3. Configure an LLM provider under **Settings → Providers** (keys are encrypted at rest with `ENCRYPTION_KEY`).
4. Upload a PDF in chat and ask a question about it; launch an OpenBot task from the agent dashboard (toggle **Swarm** for the three-agent pipeline).

### Key environment variables

| Variable | Default | Notes |
|---|---|---|
| `ENCRYPTION_KEY` | — | **Required** for saving provider keys (Fernet, see script above) |
| `DATABASE_URL` | local SQLite | Postgres supported (`postgresql://…`) |
| `OLLAMA_HOST` / `OLLAMA_PORT` | `localhost` / `11434` | Local model server |
| `CHROMADB_HOST` / `CHROMADB_PORT` | `localhost` / `8000` | Document RAG store |
| `WORKSPACE_DIR` | app workspace | Root that all filesystem tools are confined to |
| `OPENVIKING_ENABLED` / `OPENVIKING_URL` | `false` / `http://127.0.0.1:1933` | Structured-context sidecar |
| `REA_MCP_ENABLED` | `false` | REA MCP tool allowlist |
| `OPENBOT_SYNTHESIS_LLM_ENABLED` | `true` | LLM report synthesis (deterministic fallback) |

---

## 🧪 Testing & Quality Assurance

```bash
# Backend — from the repository root
PYTHONPATH=backend .venv/Scripts/python.exe -m pytest tests/ -v

# Targeted suites (fast, no external services required)
PYTHONPATH=backend .venv/Scripts/python.exe -m pytest \
  tests/test_swarm_manager.py tests/test_openviking_service.py \
  tests/test_workspace_search.py tests/test_custom_skill_service.py -v

# Frontend — 361 tests across 30 files (Vitest)
cd frontend && npm run test

# Type safety and production build
npx tsc --noEmit
npm run build:local
```

Notes:

- Backend tests are hermetic by default: a throwaway SQLite file is used and Ollama warm-up is skipped/guarded. A few groups are environment-bound (Docker Postgres, live provider keys) and skip or fail without those services.
- The swarm, OpenViking, REA, workspace-search, and custom-skill suites mock every external dependency, including timeouts and connection failures.

---

## 📜 License

- **Application code**: MIT.
- **OpenViking sidecar**: the upstream `openviking` server is licensed **AGPL-3.0**. Thunder AI talks to it over HTTP as a separate process (`D:\openviking` venv) and does not link against it; review the upstream license before redistributing the sidecar with a packaged build.

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.