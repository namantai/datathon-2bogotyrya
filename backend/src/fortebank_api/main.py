"""FastAPI application entry point."""

from fastapi import FastAPI

from fortebank_api.config import get_settings
from fortebank_api.schemas import HealthResponse


def create_app() -> FastAPI:
    """Build the FastAPI application from validated runtime settings."""

    settings = get_settings()
    application = FastAPI(
        title=settings.name,
        debug=settings.debug,
        version="0.1.0",
        description="Technical foundation; video analysis is not implemented yet.",
    )

    @application.get(
        "/health",
        response_model=HealthResponse,
        tags=["diagnostics"],
        summary="Check service availability",
    )
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service=settings.name,
            environment=settings.environment,
        )

    return application


app = create_app()
