"""FastAPI application entry point."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from fortebank_api.analyzers import Analyzer, DisabledAnalyzer, FakeAnalyzer
from fortebank_api.api import router as api_router
from fortebank_api.config import Settings, get_settings
from fortebank_api.jobs import (
    EvidenceExtractor,
    JobManager,
    JobRepository,
    UnavailableEvidenceExtractor,
)
from fortebank_api.schemas import HealthResponse
from fortebank_api.storage import VideoStorage
from fortebank_api.video_validation import FFprobeVideoValidator, VideoValidator


class RequestTimingMiddleware:
    """Record request start times without exposing them in public responses."""

    def __init__(self, application: ASGIApp) -> None:
        self.application = application

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            state = scope.setdefault("state", {})
            state["request_started_perf"] = time.perf_counter()
            state["request_started_at"] = time.time()
        await self.application(scope, receive, send)


def create_app(
    settings: Settings | None = None,
    *,
    analyzer: Analyzer | None = None,
    video_validator: VideoValidator | None = None,
    evidence_extractor: EvidenceExtractor | None = None,
) -> FastAPI:
    """Build the FastAPI application from validated runtime settings."""

    runtime_settings = settings or get_settings()
    storage = VideoStorage(
        runtime_settings.storage_dir,
        runtime_settings.max_upload_bytes,
        runtime_settings.upload_chunk_bytes,
    )
    repository = JobRepository(runtime_settings.database_path)
    selected_analyzer = analyzer
    if selected_analyzer is None:
        selected_analyzer = (
            FakeAnalyzer()
            if runtime_settings.analyzer_mode == "fake"
            else DisabledAnalyzer()
        )
    selected_validator = video_validator or FFprobeVideoValidator(
        runtime_settings.ffprobe_path,
        runtime_settings.ffprobe_timeout_sec,
    )
    manager = JobManager(
        repository=repository,
        storage=storage,
        analyzer=selected_analyzer,
        evidence_extractor=evidence_extractor or UnavailableEvidenceExtractor(),
        max_queue_size=runtime_settings.max_queue_size,
        analysis_timeout_sec=runtime_settings.analysis_timeout_sec,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    application = FastAPI(
        title=runtime_settings.name,
        debug=runtime_settings.debug,
        version="0.2.0",
        description="Secure local video-analysis API; real ML is not integrated yet.",
        lifespan=lifespan,
    )
    application.add_middleware(RequestTimingMiddleware)
    application.state.settings = runtime_settings
    application.state.storage = storage
    application.state.video_validator = selected_validator
    application.state.job_manager = manager
    application.include_router(api_router)

    @application.get(
        "/health",
        response_model=HealthResponse,
        tags=["diagnostics"],
        summary="Check service availability",
    )
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service=runtime_settings.name,
            environment=runtime_settings.environment,
        )

    return application


app = create_app()
