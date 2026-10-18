"""Application settings loaded from environment variables."""

from functools import lru_cache
import math
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

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
        hide_input_in_errors=True,
    )

    name: str = "ForteBank DATATHON API"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    api_key: SecretStr | None = None
    ui_allowed_origins: list[str] = ["http://localhost:5173"]
    ui_session_ttl_sec: int = 1800
    analyzer_mode: Literal["disabled", "fake"] = "disabled"
    storage_dir: Path = Path("./storage")
    database_path: Path = Path("./storage/jobs.sqlite3")
    max_upload_bytes: int = 128 * 1024 * 1024
    upload_chunk_bytes: int = 1024 * 1024
    jury_upload_timeout_sec: float = 60.0
    max_jury_uploads: int = 4
    job_retention_sec: float = 86400.0
    cleanup_interval_sec: float = 300.0
    ffprobe_path: str = "ffprobe"
    ffprobe_timeout_sec: float = 10.0
    ffmpeg_path: str = "ffmpeg"
    evidence_timeout_sec: float = 10.0
    evidence_max_width: int = 1280
    evidence_max_height: int = 720
    evidence_max_jpeg_bytes: int = 4 * 1024 * 1024
    evidence_jpeg_quality: int = 3
    evidence_endpoint_scan_sec: float = 2.0
    max_queue_size: int = 8
    analysis_timeout_sec: float = 240.0
    job_timeout_sec: float = 300.0

    @model_validator(mode="after")
    def validate_security_and_limits(self) -> "Settings":
        """Reject unsafe production configuration and invalid resource limits."""

        if self.max_upload_bytes <= 0:
            raise ValueError("APP_MAX_UPLOAD_BYTES must be positive")
        if self.upload_chunk_bytes <= 0:
            raise ValueError("APP_UPLOAD_CHUNK_BYTES must be positive")
        if self.max_jury_uploads <= 0:
            raise ValueError("APP_MAX_JURY_UPLOADS must be positive")
        for value in (self.jury_upload_timeout_sec, self.job_retention_sec, self.cleanup_interval_sec):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Upload timeout, retention and cleanup interval must be finite and positive")
        if self.ffprobe_timeout_sec <= 0:
            raise ValueError("APP_FFPROBE_TIMEOUT_SEC must be positive")
        if self.evidence_timeout_sec <= 0:
            raise ValueError("APP_EVIDENCE_TIMEOUT_SEC must be positive")
        if self.evidence_max_width < 2 or self.evidence_max_height < 2:
            raise ValueError("Evidence dimensions must be at least 2 pixels")
        if self.evidence_max_jpeg_bytes <= 0:
            raise ValueError("APP_EVIDENCE_MAX_JPEG_BYTES must be positive")
        if not 2 <= self.evidence_jpeg_quality <= 31:
            raise ValueError("APP_EVIDENCE_JPEG_QUALITY must be between 2 and 31")
        if self.evidence_endpoint_scan_sec <= 0:
            raise ValueError("APP_EVIDENCE_ENDPOINT_SCAN_SEC must be positive")
        if self.max_queue_size <= 0:
            raise ValueError("APP_MAX_QUEUE_SIZE must be positive")
        if not math.isfinite(self.analysis_timeout_sec) or self.analysis_timeout_sec <= 0:
            raise ValueError("APP_ANALYSIS_TIMEOUT_SEC must be finite and positive")
        if not math.isfinite(self.job_timeout_sec) or not 0 < self.job_timeout_sec <= 300:
            raise ValueError("APP_JOB_TIMEOUT_SEC must be finite and within (0, 300]")

        raw_key = self.api_key.get_secret_value() if self.api_key is not None else ""
        if not 60 <= self.ui_session_ttl_sec <= 3600:
            raise ValueError("UI session lifetime must be 60–3600 seconds")
        if not self.ui_allowed_origins:
            raise ValueError("Explicit UI origins are required")
        for origin in self.ui_allowed_origins:
            parsed = urlsplit(origin)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.path
                    or parsed.query or parsed.fragment or "*" in origin):
                raise ValueError("UI origins must be exact HTTP(S) origins without paths")
            if self.environment == "production" and parsed.scheme != "https":
                raise ValueError("Production UI requires HTTPS origins")
        if self.api_key is not None:
            if not raw_key or raw_key != raw_key.strip():
                raise ValueError("APP_API_KEY cannot be empty or surrounded by whitespace")
            if len(raw_key) < 16:
                raise ValueError("APP_API_KEY must contain at least 16 characters")

        if self.environment == "production":
            if self.debug:
                raise ValueError("Debug must be disabled in production")
            if not raw_key or raw_key.lower() in _PRODUCTION_PLACEHOLDERS:
                raise ValueError("A non-placeholder APP_API_KEY is required in production")
            if self.analyzer_mode == "fake":
                raise ValueError("FakeAnalyzer cannot run in production")

        return self


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings instance per process."""

    return Settings()
