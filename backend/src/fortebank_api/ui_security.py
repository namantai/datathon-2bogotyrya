"""Small single-process UI security boundary. No account database or API key forwarding."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import HTTPException, Request
from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from fortebank_api.config import Settings

COOKIE = "forteguard_session"


@dataclass
class Session:
    csrf: str
    expires: float
    jobs: set[str] = field(default_factory=set)
    last_job: str | None = None
    uploading: bool = False
    reviewer_id: str | None = None


class UISecurity:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sessions: dict[str, Session] = {}
        self.bootstraps: deque[float] = deque()
        self.uploads: deque[float] = deque()
        self.review_logins: deque[float] = deque()
        self.active_uploads = 0

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def consume(history: deque[float], limit: int) -> None:
        now = time.monotonic()
        while history and history[0] <= now - 60:
            history.popleft()
        if len(history) >= limit:
            raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": "60"})
        history.append(now)

    def prune(self) -> None:
        now = time.monotonic()
        self.sessions = {key: value for key, value in self.sessions.items() if value.expires > now}

    def session(self, request: Request) -> Session:
        self.prune()
        token = request.cookies.get(COOKIE, "")
        session = self.sessions.get(self.digest(token)) if len(token) <= 128 else None
        if session is None:
            raise HTTPException(401, "UI session required")
        return session

    def origin(self, request: Request, *, required: bool) -> None:
        origin = request.headers.get("origin")
        if (required or origin is not None) and origin not in self.settings.ui_allowed_origins:
            raise HTTPException(403, "Origin rejected")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Cross-site request rejected")

    def bootstrap(self, request: Request) -> tuple[str | None, Session]:
        """Reuse a visitor's session without rotating its token or extending its TTL."""
        self.prune()
        token = request.cookies.get(COOKIE, "")
        existing = self.sessions.get(self.digest(token)) if len(token) <= 128 else None
        if existing is not None:
            return None, existing
        if len(self.sessions) >= 64:
            raise HTTPException(429, "Session capacity reached")
        token = secrets.token_urlsafe(32)
        session = Session(secrets.token_urlsafe(32), time.monotonic() + self.settings.ui_session_ttl_sec)
        self.sessions[self.digest(token)] = session
        return token, session


class UIBoundaryMiddleware:
    """Authorize before consuming a body; cap streamed bytes, time and concurrent uploads."""

    def __init__(self, application: ASGIApp, security: UISecurity) -> None:
        self.application = application
        self.security = security

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/ui-api/"):
            await self.application(scope, receive, send)
            return
        request = Request(scope)
        security = self.security
        session: Session | None = None
        uploading = False
        started = False

        async def private_send(message: dict) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                ]
            await send(message)

        try:
            if scope["path"] == "/ui-api/session" and request.method != "POST":
                raise HTTPException(405, "Method not allowed", headers={"Allow": "POST"})
            modifying = request.method not in {"GET", "HEAD"}
            security.origin(request, required=modifying)
            bootstrap = scope["path"] == "/ui-api/session" and request.method == "POST"
            if bootstrap:
                # A global bounded limiter is deliberately independent of spoofable proxy headers.
                security.consume(security.bootstraps, 10)
            else:
                session = security.session(request)
                scope.setdefault("state", {})["ui_session"] = session
                if modifying and not secrets.compare_digest(
                    request.headers.get("x-csrf-token", "").encode(), session.csrf.encode()
                ):
                    raise HTTPException(403, "CSRF token rejected")

            upload = scope["path"] == "/ui-api/analyze" and request.method == "POST"
            if upload:
                assert session is not None
                manager = scope["app"].state.job_manager
                last = manager.repository.get(session.last_job) if session.last_job else None
                if (session.uploading or security.active_uploads >= 2 or len(session.jobs) >= 64
                        or (last is not None and last.state in {"queued", "running"})):
                    raise HTTPException(429, "Wait for the active upload or job")
                security.consume(security.uploads, 6)
                security.active_uploads += 1
                session.uploading = uploading = True
            limit = security.settings.max_upload_bytes + 65536 if upload else 4096
            length = request.headers.get("content-length")
            if length is not None:
                if not length.isdecimal():
                    raise HTTPException(400, "Invalid content length")
                if int(length) > limit:
                    raise HTTPException(413, "Request too large")
            consumed = 0
            deadline = time.monotonic() + 60

            async def bounded_receive() -> dict:
                nonlocal consumed
                try:
                    async with asyncio.timeout(max(0, deadline - time.monotonic())):
                        message = await receive()
                except TimeoutError:
                    scope.setdefault("state", {})["ui_body_error"] = 408
                    raise MultiPartException("Upload timeout") from None
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    scope.setdefault("state", {})["ui_body_error"] = 413
                    # Starlette closes already-spooled multipart files on this exception.
                    raise MultiPartException("Request too large")
                return message

            await self.application(scope, bounded_receive, private_send)
        except HTTPException as exc:
            if started:
                raise
            await JSONResponse({"detail": exc.detail}, exc.status_code, headers=exc.headers)(scope, receive, private_send)
        except Exception:
            if started:
                raise
            await JSONResponse({"detail": "UI request failed"}, 500)(scope, receive, private_send)
        finally:
            if uploading and session is not None:
                session.uploading = False
                security.active_uploads -= 1
