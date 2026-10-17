"""Unchanged jury transport; shared services also power the private UI bridge."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, UploadFile, status

from fortebank_api.analysis_service import read_job, submit_video
from fortebank_api.auth import require_api_key
from fortebank_api.schemas import AnalyzeAccepted, JobResponse

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


@router.post("/analyze", response_model=AnalyzeAccepted, status_code=status.HTTP_202_ACCEPTED)
async def analyze_video(request: Request, file: Annotated[UploadFile, File(...)]) -> AnalyzeAccepted:
    return await submit_video(request, file)


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, request: Request) -> JobResponse:
    return read_job(job_id, request)
