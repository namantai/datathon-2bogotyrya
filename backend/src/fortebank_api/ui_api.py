"""Private same-origin browser transport, intentionally absent from jury OpenAPI."""

from fastapi import APIRouter, HTTPException, Request, Response
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
async def login(request: Request, response: Response) -> dict:
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "JSON required")
    try:
        payload = await request.json()
    except (ValueError, UnicodeError, MultiPartException):
        raise HTTPException(request.scope["state"].get("ui_body_error", 400), "Invalid login request") from None
    if not isinstance(payload, dict) or set(payload) != {"access_code"}:
        raise HTTPException(400, "Invalid login request")
    security = request.app.state.ui_security
    token, session = security.login(request, payload["access_code"])
    response.set_cookie(
        COOKIE, token, max_age=security.settings.ui_session_ttl_sec,
        httponly=True, secure=security.settings.environment == "production",
        samesite="strict", path="/ui-api",
    )
    return session_info(request, session)


@router.get("/session")
async def current_session(request: Request) -> dict:
    return session_info(request, request.state.ui_session)


@router.delete("/session", status_code=204)
async def logout(request: Request) -> Response:
    request.app.state.ui_security.revoke(request)
    response = Response(status_code=204)
    response.delete_cookie(COOKIE, path="/ui-api", httponly=True, samesite="strict",
                           secure=request.app.state.settings.environment == "production")
    return response


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
async def job(job_id: str, request: Request) -> JobResponse:
    if job_id not in request.state.ui_session.jobs:
        raise HTTPException(404, "Job not found")
    return read_job(job_id, request)
