"""Jury-facing API routes."""

from __future__ import annotations

import time
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status

from fortebank_api.auth import require_api_key
from fortebank_api.jobs import JobManager, QueueCapacityError
from fortebank_api.schemas import AnalyzeAccepted, JobResponse
from fortebank_api.storage import (
    EmptyUploadError,
    UnsupportedVideoExtensionError,
    UploadTooLargeError,
    VideoStorage,
)
from fortebank_api.video_validation import (
    InvalidVideoError,
    VideoValidationUnavailableError,
    VideoValidator,
)


router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_api_key)],
)


@router.post(
    "/analyze",
    response_model=AnalyzeAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def analyze_video(
    request: Request,
    file: Annotated[UploadFile, File(...)],
) -> AnalyzeAccepted:
    storage: VideoStorage = request.app.state.storage
    validator: VideoValidator = request.app.state.video_validator
    manager: JobManager = request.app.state.job_manager
    request_started = request.scope["state"].get("request_started_perf", time.perf_counter())
    created_at = request.scope["state"].get("request_started_at", time.time())

    try:
        saved = await storage.save(file)
    except UnsupportedVideoExtensionError as exc:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)) from exc
    except EmptyUploadError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from exc

    try:
        await validator.validate(saved.path)
    except InvalidVideoError as exc:
        storage.delete(saved.path)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except VideoValidationUnavailableError as exc:
        storage.delete(saved.path)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Local video validation is unavailable",
        ) from exc
    except BaseException:
        storage.delete(saved.path)
        raise

    job_id = str(uuid4())
    try:
        await manager.submit(
            job_id=job_id,
            video_path=saved.path,
            created_at=created_at,
            upload_duration_sec=time.perf_counter() - request_started,
        )
    except QueueCapacityError as exc:
        storage.delete(saved.path)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Analysis queue is full",
        ) from exc

    return AnalyzeAccepted(job_id=job_id)


@router.get(
    "/jobs/{job_id}",
    response_model=JobResponse,
)
async def get_job(job_id: str, request: Request) -> JobResponse:
    manager: JobManager = request.app.state.job_manager
    response = manager.get_public_response(job_id)
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return response
