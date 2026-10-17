"""Authenticate jury requests before multipart parsing and bound ingress resources."""

import asyncio
import time

from fastapi import HTTPException, Request
from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fortebank_api.auth import require_api_key
from fortebank_api.config import Settings


class JuryIngressMiddleware:
    def __init__(self, application: ASGIApp, settings: Settings) -> None:
        self.application = application
        self.settings = settings
        self.active_uploads = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/v1/"):
            await self.application(scope, receive, send)
            return
        request = Request(scope)
        uploading = False
        body_error: int | None = None
        started = False

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if body_error is not None:
                # FastAPI converts parser failures to 400; return the actual bounded-body error.
                return
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = list(message.get("headers", [])) + [(b"cache-control", b"no-store")]
            await send(message)

        try:
            await require_api_key(request, request.headers.get("x-api-key"))
            upload = scope["path"].rstrip("/") == "/api/v1/analyze" and request.method == "POST"
            if upload:
                if self.active_uploads >= self.settings.max_jury_uploads:
                    raise HTTPException(503, "Upload capacity reached", headers={"Retry-After": "5"})
                self.active_uploads += 1
                uploading = True
            limit = self.settings.max_upload_bytes + 65536 if upload else 4096
            length = request.headers.get("content-length")
            if length is not None:
                if not length.isdecimal() or len(length) > 20:
                    raise HTTPException(400, "Invalid content length")
                if int(length) > limit:
                    raise HTTPException(413, "Request too large")
            consumed = 0
            deadline = time.monotonic() + self.settings.jury_upload_timeout_sec

            async def bounded_receive() -> Message:
                nonlocal consumed, body_error
                try:
                    async with asyncio.timeout(max(0, deadline - time.monotonic())):
                        message = await receive()
                except TimeoutError:
                    body_error = 408
                    raise MultiPartException("Upload timeout") from None
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    body_error = 413
                    # MultiPartException makes Starlette close partially spooled files.
                    raise MultiPartException("Request too large")
                return message

            await self.application(scope, bounded_receive, guarded_send)
            if body_error is not None:
                await JSONResponse({"detail": "Upload rejected"}, body_error,
                                   headers={"Cache-Control": "no-store"})(scope, receive, send)
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, exc.status_code,
                               headers={"Cache-Control": "no-store", **(exc.headers or {})})(scope, receive, send)
        except Exception:
            if started:
                raise
            await JSONResponse({"detail": "API request failed"}, 503,
                               headers={"Cache-Control": "no-store"})(scope, receive, send)
        finally:
            if uploading:
                self.active_uploads -= 1
