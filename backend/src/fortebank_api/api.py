"""Unchanged jury transport; shared services also power the private UI bridge."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, UploadFile, status

from fortebank_api.analysis_service import read_job, submit_video
from fortebank_api.auth import require_api_key
from fortebank_api.schemas import AnalyzeAccepted, JobResponse

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)], tags=["jury"])


@router.post("/analyze", response_model=AnalyzeAccepted, status_code=status.HTTP_202_ACCEPTED,
    summary="Submit one MOV video for local analysis",
    description="Upload one file in multipart field `file`. Returns job_id; poll GET /api/v1/jobs/{job_id}. "
                "Binary Logistic v4 reports generic possible_violation events for human review.",
    responses={400: {"description": "Empty or invalid video"}, 401: {"description": "Missing or invalid API key"},
               408: {"description": "Upload timeout"}, 413: {"description": "Upload too large"},
               415: {"description": "Unsupported file extension; MOV required"},
               503: {"description": "Queue full or local service unavailable"}})
async def analyze_video(request: Request, file: Annotated[UploadFile, File(
    ..., json_schema_extra={"format": "binary"},
)]) -> AnalyzeAccepted:
    return await submit_video(request, file)


@router.get("/jobs/{job_id}", response_model=JobResponse, summary="Read processing, done or failed job",
    description="A done result contains violation/no_violation and violations. Each event has 1–3 original "
                "JPEG evidence frames with actual decoded timestamps. Failed is never a no_violation verdict. "
                "The binary model does not identify specific actions or legal rules.",
    responses={401: {"description": "Missing or invalid API key"}, 404: {"description": "Job not found"}})
async def get_job(job_id: str, request: Request) -> JobResponse:
    return read_job(job_id, request)
