from pydantic_settings import BaseSettings
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

    # ── Deep Research (web search via DuckDuckGo — free, no API key) ──
    # When enabled, the agent can autonomously search the web, scrape the
    # top results, and synthesize a report with citations.
    enable_deep_research: bool = True
    # How many search results to fetch per research pass.
    deep_research_max_results: int = 5
    # How many of the top results to scrape per pass (hard cap — bounds the
    # search -> scrape -> synthesize loop so it can never run away).
    deep_research_max_scrape: int = 3

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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def chromadb_url(self) -> str:
        return f"http://{self.chromadb_host}:{self.chromadb_port}"

    @property
    def ollama_url(self) -> str:
        return f"http://{self.ollama_host}:{self.ollama_port}"


settings = Settings()
