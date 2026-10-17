"""Tests for the public technical health endpoint."""

import asyncio

from fortebank_api.main import app
from tests.support import request


def test_health_returns_service_status() -> None:
    async def scenario() -> None:
        response = await request(app, "GET", "/health")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "service": app.title,
            "environment": "development",
        }

    asyncio.run(scenario())
