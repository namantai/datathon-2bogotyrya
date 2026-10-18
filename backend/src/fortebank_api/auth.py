"""API-key authentication for jury routes."""

import secrets
from typing import Annotated

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader


jury_api_key = APIKeyHeader(name="X-API-Key", scheme_name="JuryAPIKey", auto_error=False,
                           description="Jury API key. Enter locally; never put it in a URL or frontend code.")


async def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Security(jury_api_key)] = None,
) -> None:
    """Authenticate without logging or exposing the configured secret."""

    configured = request.app.state.settings.api_key
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is not configured",
        )

    expected = configured.get_secret_value()
    if x_api_key is None or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )
