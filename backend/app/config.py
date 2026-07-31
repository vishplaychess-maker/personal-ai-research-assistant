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

    # ── Model settings ──────────────────────────────────
    default_model: str = "llama3.2:3b"
    ollama_tags_timeout: int = 5

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def chromadb_url(self) -> str:
        return f"http://{self.chromadb_host}:{self.chromadb_port}"

    @property
    def ollama_url(self) -> str:
        return f"http://{self.ollama_host}:{self.ollama_port}"


settings = Settings()
