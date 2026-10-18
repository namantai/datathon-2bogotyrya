"""Private same-origin browser transport, intentionally absent from jury OpenAPI."""

from datetime import datetime, timezone
import json
import time

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.responses import JSONResponse
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from fortebank_api.analysis_service import read_job, submit_video
from fortebank_api.schemas import AnalyzeAccepted, JobResponse
from fortebank_api.ui_security import COOKIE, Session

router = APIRouter(prefix="/ui-api", include_in_schema=False)


def session_info(request: Request, session: Session) -> dict:
    return {
        "csrf_token": session.csrf,
        "analyzer_mode": request.app.state.ui_analyzer_mode,
        "max_upload_bytes": request.app.state.settings.max_upload_bytes,
    }


@router.post("/session")
async def bootstrap_session(request: Request, response: Response) -> dict:
    # Origin/Fetch Metadata and rate limit are checked before body consumption.
    # No credentials, no login protocol; reject obsolete credential-bearing bodies.
    try:
        body = await request.body()
    except MultiPartException:
        raise HTTPException(request.scope["state"].get("ui_body_error", 400), "Invalid session request") from None
    if body:
        raise HTTPException(400, "Session request must have an empty body")
    security = request.app.state.ui_security
    token, session = security.bootstrap(request)
    if token is not None:
        response.set_cookie(
            COOKIE, token, max_age=security.settings.ui_session_ttl_sec,
            httponly=True, secure=security.settings.environment == "production",
            samesite="strict", path="/ui-api",
        )
    return session_info(request, session)


@router.post("/analyze", response_model=AnalyzeAccepted, status_code=202)
async def analyze(request: Request) -> AnalyzeAccepted:
    try:
        async with request.form(max_files=1, max_fields=0, max_part_size=1024) as form:
            file = form.get("file")
            if len(form) != 1 or not isinstance(file, UploadFile):
                raise HTTPException(422, "One file field is required")
            accepted = await submit_video(request, file)
            request.state.ui_session.jobs.add(accepted.job_id)
            request.state.ui_session.last_job = accepted.job_id
            return accepted
    except StarletteHTTPException as exc:
        raise HTTPException(request.scope["state"].get("ui_body_error", exc.status_code),
                            "Upload rejected" if exc.status_code == 400 else exc.detail) from None


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def job(job_id: str, request: Request, include_details: bool = False) -> JobResponse | Response:
    if job_id not in request.state.ui_session.jobs:
        raise HTTPException(404, "Job not found")
    result = read_job(job_id, request)
    if not include_details:
        return result
    record = request.app.state.job_manager.repository.get(job_id)
    if record is None:
        raise HTTPException(404, "Job not found")

    def utc(value: float | None) -> str | None:
        return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None

    now = time.time()
    payload = result.model_dump(mode="json")
    payload["timing"] = {
        "server_time": utc(now), "request_started_at": utc(record.created_at),
        "accepted_at": utc(record.queued_at), "processing_started_at": utc(record.started_at),
        "deadline": utc(record.deadline_at), "completed_at": utc(record.completed_at),
        "duration_sec": record.total_duration_sec,
        "budget_scope": "backend_request_not_browser_click",
    }
    payload["failure_reason"] = (
        "timeout" if record.error_code == "analysis_timeout"
        else "processing_failed" if record.state == "failed" else None
    )
    payload["event_details"] = json.loads(record.ui_details_json or "[]")
    # Explicit opt-in on the existing session-owned route; jury schemas remain unchanged.
    return JSONResponse(payload)
