"""Application settings loaded from environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Non-secret runtime settings for the Backend foundation."""

    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    name: str = "ForteBank DATATHON API"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings instance per process."""

    return Settings()
