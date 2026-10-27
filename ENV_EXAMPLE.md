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

### Tests never use this database

`tests/conftest.py` points `DATABASE_URL` at `THUNDER_TEST_DB_DIR/thunder-test.db`
(system temp dir by default) before any `app` import, seeds the schema and the
`default` user there, and deletes the file at session end. A run that would
resolve to the repo's `data/thunder.db` is refused at session start.

| Variable | Default | Notes |
| --- | --- | --- |
| `THUNDER_TEST_DB_DIR` | system temp dir | Directory for the throwaway test database; point it at a fast local disk (e.g. `D:/tmp/thunder-tests`) |
| `DATABASE_URL` (in the test process) | the throwaway file | An operator-set value that is not the repo database is honoured (e.g. Postgres in CI); a value pointing at `data/thunder.db` is redirected |

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
| `ENCRYPTION_KEY` | `""` | **Yes** | Fernet key (`python scripts/generate_encryption_key.py`); without it, saving provider keys returns 500 and runtime calls fall back to env provider keys |
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
| `LLM_PROVIDER` | `glm` | Optional | `glm` \| `ollama` \| `openrouter` \| `nvidia` \| `huggingface` \| `google` \| `modelslab` \| `groq` \| `grok` \| `together` \| `mistral` \| `cohere` |
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
`HUGGINGFACE`, `GOOGLE`, `MODELSLAB`, `GROQ`, `GROK`, `TOGETHER`, `MISTRAL`,
`COHERE`. Defaults in `config.py:148-209`. Keys default to `""` (provider
disabled); in the cloud configure at least one so the GLM→free-provider→
Ollama fallback chain has a target.

### Grok (xAI) subscription sign-in

`GROK_API_KEY` buys tokens on `api.x.ai`; the keys below instead let a
SuperGrok / X Premium+ subscriber sign in with OAuth and spend the
subscription. Off by default — nothing changes until you opt in.

| Variable | Default | Prod | Notes |
| --- | --- | --- | --- |
| `GROK_OAUTH_ENABLED` | `false` | No | Master switch for the sign-in routes and the subscription provider |
| `GROK_OAUTH_SUBSCRIPTION_BASE_URL` | `https://cli-chat-proxy.grok.com/v1` | No | The Grok CLI chat proxy; a subscription bearer is refused by `api.x.ai` (402/403) |
| `GROK_OAUTH_DEVICE_AUTH_URL` | `https://auth.x.ai/oauth2/device/code` | No | From xAI's OIDC discovery document; must stay `https://` |
| `GROK_OAUTH_TOKEN_URL` | `https://auth.x.ai/oauth2/token` | No | Must stay `https://` |
| `GROK_OAUTH_USERINFO_URL` | `https://auth.x.ai/oauth2/userinfo` | No | Must stay `https://` |
| `GROK_OAUTH_REVOCATION_URL` | `https://auth.x.ai/oauth2/revoke` | No | Must stay `https://` |
| `GROK_OAUTH_CLIENT_ID` | `""` | No | Public Grok-CLI client id is baked in; set only if xAI rotates it. **There is no client secret** |
| `GROK_OAUTH_SCOPES` | `""` | No | Space- or comma-separated; empty means the default Grok-CLI grant |
| `GROK_OAUTH_POLL_INTERVAL_S` | `5` | No | Floor for status polling; the server's own `interval` wins |
| `GROK_OAUTH_EXPIRY_SKEW_S` | `300` | No | Refresh an access token this many seconds before it expires |

Requires `ENCRYPTION_KEY`: access and refresh tokens are Fernet-encrypted in
`provider_oauth_tokens`, and a sign-in is refused (HTTP 500) rather than stored
unencrypted. See `docs/grok_oauth_signin.md`.

## Features & Tools

| Variable | Default | Prod | Notes |
| --- | --- | --- | --- |
| `ENABLE_MULTI_AGENT` | `true` | No | Researcher → Coder → Reviewer routing |
| `SEMANTIC_CACHE_ENABLED` | `true` | No | ChromaDB-backed semantic cache (CAG layer 2) |
| `SEMANTIC_CACHE_THRESHOLD` | `0.85` | No | Min cosine similarity for a semantic hit |
| `SEMANTIC_CACHE_MAX_ENTRIES` | `1000` | No | Oldest entries evicted beyond the cap |
| `ENABLE_DEEP_RESEARCH` | `true` | No | DuckDuckGo search (free, no key) |
| `DEEP_RESEARCH_MAX_RESULTS` | `5` | No | — |
| `DEEP_RESEARCH_MAX_SCRAPE` | `3` | No | Hard cap on scrape loop |
| `ENABLE_TERMINAL_TOOL` | `false` | Keep `false` | HITL shell proposals |
| `GROK_OAUTH_ENABLED` | `false` | No | Grok subscription sign-in; see **LLM Providers → Grok** above and `docs/grok_oauth_signin.md` |
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
