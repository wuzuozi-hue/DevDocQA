import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    pg_dsn: str = "postgresql://devdocqa:devdocqa@localhost:5432/devdocqa"
    internal_key: str = ""
    data_dir: Path = Path("data/documents")
    model_mode: str = "mock"
    ollama_base_url: str = "http://host.docker.internal:11434"
    chat_model: str = "qwen3:4b"
    embed_model: str = "bge-m3"
    model_timeout_seconds: float = 90.0
    mock_delay_seconds: float = 0.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        pg_dsn=os.getenv("PG_DSN", Settings.pg_dsn),
        internal_key=os.getenv("INTERNAL_KEY", ""),
        data_dir=Path(os.getenv("DATA_DIR", "data/documents")),
        model_mode=os.getenv("MODEL_MODE", "mock"),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", Settings.ollama_base_url).rstrip("/"),
        chat_model=os.getenv("CHAT_MODEL", "qwen3:4b"),
        embed_model=os.getenv("EMBED_MODEL", "bge-m3"),
        model_timeout_seconds=float(os.getenv("MODEL_TIMEOUT_SECONDS", "90")),
        mock_delay_seconds=float(os.getenv("MOCK_DELAY_SECONDS", "0")),
    )
