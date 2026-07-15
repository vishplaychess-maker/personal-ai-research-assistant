# Personal AI Research Assistant 🧠

A local-first research assistant that combines a FastAPI backend, React frontend, ChromaDB vector storage, and Ollama-powered AI — all orchestrated with Docker.

> **Phase 1** — Project skeleton and service health monitoring.

---

## Architecture

```
┌───────────────────────┐     ┌───────────────────────┐
│   Frontend (React)    │     │   Ollama (Windows)     │
│  127.0.0.1:5173       │     │   host.docker.internal:11434
└──────┬────────────────┘     └────────▲──────────────┘
       │ /api/* (proxied)              │
       ▼                                │
┌───────────────────────┐              │
│   Backend (FastAPI)   │──────────────┘
│  127.0.0.1:8080       │
└──┬────────┬───────────┘
   │        │
   ▼        ▼
┌────────┐ ┌──────────────┐
│ SQLite │ │ ChromaDB      │
│ .db    │ │ (Docker)      │
└────────┘ └──────────────┘
```

### Services

| Service   | Port                  | Access             |
| --------- | --------------------- | ------------------ |
| **Frontend**  | `127.0.0.1:5173` | Browser            |
| **Backend**   | `127.0.0.1:8080` | API / browser      |
| **ChromaDB**  | internal only         | Backend (Docker network) |
| **Ollama**    | `host.docker.internal:11434` | Backend via host |

---

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows)
- [Ollama](https://ollama.com/) running natively on Windows (not in Docker)

---

## Quick Start

### 1. Start the services

```bash
docker compose up --build
```

This builds and starts three containers:

- `research-assistant-chromadb`
- `research-assistant-backend`
- `research-assistant-frontend`

### 2. Open the dashboard

Visit [http://127.0.0.1:5173](http://127.0.0.1:5173)

You should see three health-status cards showing:

| Service  | Expected status           |
| -------- | ------------------------- |
| Backend  | ✅ Online                 |
| ChromaDB | ✅ Online (if running)    |
| Ollama   | ✅ Online (if running)    |

If Ollama or ChromaDB are unavailable, the dashboard will show **Unavailable** — the endpoint still responds with HTTP 200.

### 3. Test the API directly

```bash
curl http://127.0.0.1:8080/api/health
```

Expected response:

```json
{
  "backend": "ok",
  "chromadb": "ok",
  "ollama": "ok"
}
```

### 4. Run smoke tests (with services running)

```bash
pip install httpx pytest
pytest tests/test_health.py -v
```

---

## Stopping & Cleaning Up

```bash
# Stop containers
docker compose down

# Stop and delete volumes (destroys SQLite / ChromaDB data)
docker compose down -v
```

---

## Project Structure

```
.
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py              # FastAPI app & health endpoint
│   │   ├── config.py            # Settings (env vars)
│   │   ├── database.py          # SQLAlchemy setup
│   │   └── models/
│   │       ├── __init__.py
│   │       └── models.py        # SQLAlchemy ORM models
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── main.tsx             # React entry point
│   │   ├── App.tsx              # Health dashboard
│   │   ├── App.css
│   │   ├── index.css
│   │   └── vite-env.d.ts
│   ├── index.html
│   ├── package.json
│   ├── tsconfig.json
│   ├── tsconfig.node.json
│   ├── vite.config.ts
│   └── Dockerfile
├── tests/
│   ├── __init__.py
│   └── test_health.py           # Smoke tests
├── docker-compose.yml
├── .env.example
├── .gitignore
└── README.md
```

---

## Environment Variables

| Variable        | Default                                      | Description            |
| --------------- | -------------------------------------------- | ---------------------- |
| `DATABASE_URL`  | `sqlite:///./data/research_assistant.db`     | SQLite database path   |
| `CHROMADB_HOST` | `chromadb` (Docker service name)             | ChromaDB host          |
| `CHROMADB_PORT` | `8000`                                       | ChromaDB port          |
| `OLLAMA_HOST`   | `host.docker.internal`                       | Ollama host            |
| `OLLAMA_PORT`   | `11434`                                      | Ollama port            |

Copy `.env.example` to `.env` to override defaults.

---

## Phase 1 Checklist

- [x] Docker Compose with 3 services (backend, frontend, ChromaDB)
- [x] FastAPI backend with SQLAlchemy models (User, Session, Message, Document, Chunk, Memory)
- [x] Automatic table creation and default user on startup
- [x] `GET /api/health` endpoint checking Backend, ChromaDB, and Ollama
- [x] Graceful degradation (still responds 200 when ChromaDB/Ollama down)
- [x] React health dashboard with cards, loading state, and connection-error state
- [x] Persistent volumes for SQLite, uploads, and ChromaDB
- [x] Localhost-only port bindings (`127.0.0.1`)
- [x] Smoke tests for the health endpoint
- [x] `.env.example`, `.gitignore`, `README.md`

---

## Next Steps (Phase 2+)

- Chat interface with message persistence
- Document upload and chunking
- Vector embedding with ChromaDB
- AI-powered research queries via Ollama
- Session management and history
