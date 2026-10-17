"""Application settings loaded from environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_PRODUCTION_PLACEHOLDERS = {
    "change-me",
    "replace-me",
    "replace-with-a-long-random-local-secret",
}


class Settings(BaseSettings):
    """Validated runtime settings loaded from ``APP_*`` environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    name: str = "ForteBank DATATHON API"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    api_key: SecretStr | None = None
    analyzer_mode: Literal["disabled", "fake"] = "disabled"
    storage_dir: Path = Path("./storage")
    database_path: Path = Path("./storage/jobs.sqlite3")
    max_upload_bytes: int = 128 * 1024 * 1024
    upload_chunk_bytes: int = 1024 * 1024
    ffprobe_path: str = "ffprobe"
    ffprobe_timeout_sec: float = 10.0
    max_queue_size: int = 8
    analysis_timeout_sec: float = 240.0

    @model_validator(mode="after")
    def validate_security_and_limits(self) -> "Settings":
        """Reject unsafe production configuration and invalid resource limits."""

        if self.max_upload_bytes <= 0:
            raise ValueError("APP_MAX_UPLOAD_BYTES must be positive")
        if self.upload_chunk_bytes <= 0:
            raise ValueError("APP_UPLOAD_CHUNK_BYTES must be positive")
        if self.ffprobe_timeout_sec <= 0:
            raise ValueError("APP_FFPROBE_TIMEOUT_SEC must be positive")
        if self.max_queue_size <= 0:
            raise ValueError("APP_MAX_QUEUE_SIZE must be positive")
        if self.analysis_timeout_sec <= 0:
            raise ValueError("APP_ANALYSIS_TIMEOUT_SEC must be positive")

        raw_key = self.api_key.get_secret_value() if self.api_key is not None else ""
        if self.api_key is not None:
            if not raw_key or raw_key != raw_key.strip():
                raise ValueError("APP_API_KEY cannot be empty or surrounded by whitespace")
            if len(raw_key) < 16:
                raise ValueError("APP_API_KEY must contain at least 16 characters")

        if self.environment == "production":
            if not raw_key or raw_key.lower() in _PRODUCTION_PLACEHOLDERS:
                raise ValueError("A non-placeholder APP_API_KEY is required in production")
            if self.analyzer_mode == "fake":
                raise ValueError("FakeAnalyzer cannot run in production")

        return self


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings instance per process."""

    return Settings()
