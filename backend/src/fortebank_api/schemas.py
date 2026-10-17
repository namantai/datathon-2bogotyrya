"""Pydantic response schemas exposed by the foundation routes."""

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Response returned by the technical health endpoint."""

    status: Literal["ok"]
    service: str
    environment: str
