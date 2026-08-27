from pydantic_settings import BaseSettings
from pathlib import Path


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

    # ── Terminal tool (Phase 9 — Agent Tools) ─────────────
    # Enable the terminal executor tool for the AI agent.
    # When true, the agent can propose shell commands for user approval.
    enable_terminal_tool: bool = False

    # ── Model / LLM provider settings ───────────────────
    # Provider: "ollama" | "openrouter" | "nvidia"
    llm_provider: str = "ollama"
    default_model: str = "llama3.2:3b"
    ollama_tags_timeout: int = 5

    # OpenRouter (OpenAI-compatible API)
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "meta-llama/llama-3.2-3b-instruct:free"
    openrouter_max_tokens: int = 2048
    openrouter_temperature: float = 0.7

    # NVIDIA NIM (OpenAI-compatible API)
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "meta/llama-3.2-3b-instruct"
    nvidia_max_tokens: int = 2048
    nvidia_temperature: float = 0.7

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def chromadb_url(self) -> str:
        return f"http://{self.chromadb_host}:{self.chromadb_port}"

    @property
    def ollama_url(self) -> str:
        return f"http://{self.ollama_host}:{self.ollama_port}"


settings = Settings()
