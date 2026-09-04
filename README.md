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

## Production Deployment (Phase 5)

Cloud topology: **backend on Render (Docker)**, **frontend on Vercel**,
**PostgreSQL managed by your provider**. Local development keeps using
`docker-compose.yml` (SQLite, dev frontend) and is unchanged.

### 1. Backend on Render

1. Push this branch; Render → New → Web Service → deploy from the repo with
   Docker. Render detects the root of `backend/` if you set the Docker
   context/`Dockerfile` path to `backend` in `render.yaml` or the dashboard.
2. Set environment variables (template in `.env.production.example`; full
   reference in `ENV_EXAMPLE.md`):

   | Variable | Value |
   | --- | --- |
   | `DATABASE_URL` | TODO: your managed Postgres URL (`postgres://...` is auto-normalized to `postgresql://...`) |
   | `CORS_ORIGINS` | TODO: `https://your-app.vercel.app` (comma-separated for more) |
   | `FRONTEND_ORIGIN` | Same origin as above (fallback when `CORS_ORIGINS` is empty) |
   | `JWT_SECRET` | Strong random hex (startup fails without it in prod mode) |
   | `PRODUCTION_MODE` | `true` |
   | `COOKIE_SECURE` | `true` |
   | `ENCRYPTION_KEY` | Fernet key (see `.env.production.example`) |
   | `LOG_FORMAT` | `json` |
   | `GLM_BASE_URL` | TODO: the GLM Free Router is host-local — point this at your cloud GLM-compatible endpoint, or leave unset and rely on the fallback chain (GLM → free provider → Ollama) with at least one cloud provider key configured |
   | `GLM_API_KEY` / `VERDENT_API_KEY` | Optional GLM keys |

   For self-managed Docker hosts, a backend-only production compose is
   provided: `docker compose -f docker-compose.prod.yml up -d --build`
   (port 8000, health check on `/api/health`; an optional commented
   postgres service is included).
3. Health: `/api/health` now reports `database` (`connected`/`unavailable`)
   and `database_dialect` alongside `backend`, `chromadb`, `ollama`.

### 2. Frontend on Vercel

- Import the repo, set the root directory to `frontend/`. `frontend/vercel.json`
  contains SPA rewrites **including `/share/agents/:id`** (public ShareCard
  deep links) and an `/api/:path*` rewrite to the backend.
- TODO: edit `frontend/vercel.json` and replace
  `https://YOUR-RENDER-APP.onrender.com/api/:path*` with your Render URL.
- **Recommended:** keep the `/api` rewrite (same-site) — auth cookies and the
  double-submit CSRF flow keep working with no extra CORS setup. Do **not**
  set `VITE_API_BASE_URL` in this mode.
- **Alternative (cross-origin):** remove the `/api/:path*` rewrite, set
  `VITE_API_BASE_URL=https://YOUR-RENDER-APP.onrender.com` in Vercel env
  vars, and set the backend's `CORS_ORIGINS` to your Vercel domain. Cross-site
  cookies then need `COOKIE_SECURE=true` and `COOKIE_SAMESITE=none`.
- The frontend defaults to relative `/api` calls
  (`VITE_API_BASE_URL` unset = current behavior, byte-identical).

### 3. PostgreSQL notes

- First deploy on an empty database runs `Base.metadata.create_all()` — no
  manual migration step.
- Historical raw-SQL migrations are SQLite-only and are **skipped
  automatically** on PostgreSQL (dialect guard in `_migrate_database()`).
- Search uses `ILIKE` on PostgreSQL (case-insensitive) and `LIKE` on SQLite —
  behavior is unchanged locally.

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
