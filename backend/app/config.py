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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def chromadb_url(self) -> str:
        return f"http://{self.chromadb_host}:{self.chromadb_port}"

    @property
    def ollama_url(self) -> str:
        return f"http://{self.ollama_host}:{self.ollama_port}"


settings = Settings()
