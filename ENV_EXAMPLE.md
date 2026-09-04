# Environment Variable Reference (Phase 5)

Generated from `backend/app/config.py` (`Settings`). Pydantic-settings maps each
field to an UPPERCASE environment variable of the same name (e.g.
`database_url` → `DATABASE_URL`). Local defaults come from `config.py`;
`.env` overrides them; see `.env.example` (dev) and `.env.production.example`
(cloud) for copy-ready templates.

Legend: **Prod** = required or strongly recommended for cloud deployment.

## Core

| Variable | Default | Prod | Example / Notes |
| --- | --- | --- | --- |
| `DATABASE_URL` | `sqlite:////data/app.db` | **Yes** | `postgresql://user:pass@host:5432/db` (managed Postgres) |
| `CHROMADB_HOST` | `localhost` | No | `chromadb` (Docker service name) |
| `CHROMADB_PORT` | `8000` | No | — |
| `OLLAMA_HOST` | `localhost` | No | `host.docker.internal` locally |
| `OLLAMA_PORT` | `11434` | No | — |
| `UPLOAD_DIR` | `/data/uploads` | No | — |
| `DATA_DIR` | `/data` | No | — |
| `FRONTEND_ORIGIN` | `http://localhost:5173` | **Yes** | `https://your-app.vercel.app` |
| `CORS_ORIGINS` | `""` | **Yes** | Comma-separated: `https://a.com,https://b.com`. Falls back to `FRONTEND_ORIGIN`. |
| `LOG_FORMAT` | `text` | Recommended | `json` for structured cloud logs |

## Memory

| Variable | Default | Prod | Notes |
| --- | --- | --- | --- |
| `ENABLE_MEMORY` | `true` | No | Also persisted per-user via settings UI |
| `MEMORY_MAX_RESULTS` | `5` | No | — |
| `MEMORY_DECAY_TTL_DAYS` | `7` | No | Days before unpinned, low-access memories are forgotten |
| `MEMORY_PIN_ACCESS_THRESHOLD` | `5` | No | — |
| `MEMORY_CONFLICT_THRESHOLD` | `0.80` | No | Cosine similarity for conflict merge |

## Auth & Security

| Variable | Default | Prod | Notes |
| --- | --- | --- | --- |
| `JWT_SECRET` | `change-me-in-production` | **Yes** | `python -c "import secrets; print(secrets.token_hex(32))"`; startup refuses default when `PRODUCTION_MODE=true` |
| `JWT_ALGORITHM` | `HS256` | No | — |
| `JWT_EXPIRY_HOURS` | `168` | No | Legacy token lifetime |
| `JWT_ACCESS_EXPIRY_MINUTES` | `15` | No | — |
| `JWT_REFRESH_EXPIRY_DAYS` | `30` | No | — |
| `PRODUCTION_MODE` | `false` | **Yes** (`true`) | Enforces secret strength |
| `COOKIE_SECURE` | `false` | **Yes** (`true`) | HTTPS required in cloud |
| `COOKIE_SAMESITE` | `lax` | Optional | `none` only for deliberate cross-site cookies + HTTPS |
| `REFRESH_COOKIE_NAME` | `research_assistant_refresh_token` | No | — |
| `CSRF_COOKIE_NAME` | `research_assistant_csrf_token` | No | — |
| `MAX_ACTIVE_SESSIONS` | `10` | No | — |
| `ENCRYPTION_KEY` | `""` | **Yes** | Fernet key; ephemeral (with warning) if unset |
| `RATE_LIMIT_MAX_ATTEMPTS` | `10` | No | — |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | No | — |
| `RATE_LIMIT_LOCKOUT_THRESHOLD` | `5` | No | — |
| `RATE_LIMIT_LOCKOUT_BASE_SECONDS` | `30` | No | — |
| `RATE_LIMIT_LOCKOUT_MAX_SECONDS` | `900` | No | — |
| `REFRESH_RATE_LIMIT_REQUESTS` | `20` | No | — |
| `REFRESH_RATE_LIMIT_WINDOW_SECONDS` | `60` | No | — |

## LLM Providers

| Variable | Default | Prod | Notes |
| --- | --- | --- | --- |
| `LLM_PROVIDER` | `glm` | Optional | `glm` \| `ollama` \| `openrouter` \| `nvidia` \| `huggingface` \| `google` \| `modelslab` \| `groq` \| `together` \| `mistral` \| `cohere` |
| `DEFAULT_MODEL` | `glm-5.3-flash` | No | — |
| `OLLAMA_TAGS_TIMEOUT` | `5` | No | Seconds |
| `GLM_ENABLE` | `true` | No | Master switch for GLM + fallback |
| `GLM_API_KEY` | `""` | Optional | `VERDENT_API_KEY` also honoured |
| `GLM_BASE_URL` | `http://127.0.0.1:8320/v1` | **Yes** (cloud) | Free Router is host-local; set a cloud endpoint or rely on fallback chain |
| `GLM_MODEL` | `glm-5.3-flash` | No | — |
| `GLM_MAX_TOKENS` | `2048` | No | — |
| `GLM_TEMPERATURE` | `0.7` | No | — |
| `VERDENT_API_KEY` | — | Optional | Env-only fallback key read by the GLM provider |

Per-provider OpenAI-compatible blocks (`*_API_KEY`, `*_BASE_URL`, `*_MODEL`,
`*_MAX_TOKENS`, `*_TEMPERATURE`) exist for: `OPENROUTER`, `NVIDIA`,
`HUGGINGFACE`, `GOOGLE`, `MODELSLAB`, `GROQ`, `TOGETHER`, `MISTRAL`,
`COHERE`. Defaults in `config.py:148-209`. Keys default to `""` (provider
disabled); in the cloud configure at least one so the GLM→free-provider→
Ollama fallback chain has a target.

## Features & Tools

| Variable | Default | Prod | Notes |
| --- | --- | --- | --- |
| `ENABLE_MULTI_AGENT` | `true` | No | Researcher → Coder → Reviewer routing |
| `ENABLE_DEEP_RESEARCH` | `true` | No | DuckDuckGo search (free, no key) |
| `DEEP_RESEARCH_MAX_RESULTS` | `5` | No | — |
| `DEEP_RESEARCH_MAX_SCRAPE` | `3` | No | Hard cap on scrape loop |
| `ENABLE_TERMINAL_TOOL` | `false` | Keep `false` | HITL shell proposals |
| `ENABLE_MCP_TOOL` | `false` | No | — |
| `MCP_CALL_TIMEOUT_S` | `30` | No | — |
| `MCP_DISCOVERY_TIMEOUT_S` | `20` | No | — |
| `SKILLS_DIR` | `""` | No | Empty = bundled `<backend>/skills` |
| `EXTRA_PATHS` | bundled skills dir | No | — |

## Thunder Skills

| Variable | Default | Notes |
| --- | --- | --- |
| `THUNDER_SKILLS_ENABLED` | `true` | Master switch |
| `THUNDER_SKILLS_PATHS` | `""` | Colon/newline-separated extra dirs |
| `THUNDER_SKILLS_DISABLED` | `""` | Comma-separated blacklist |
| `THUNDER_SKILLS_INVOCATION` | `auto` | `auto` \| `marker` \| `tool` |
| `THUNDER_SKILLS_INDEX_BUDGET_TOKENS` | `600` | L1 index prompt budget |
| `THUNDER_SKILLS_MAX_INDEXED` | `20` | Max skills in L1 index |
