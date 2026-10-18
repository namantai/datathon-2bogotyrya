"""Shared upload and job services; authorization lives in transports."""

from __future__ import annotations

import time
from uuid import uuid4

from fastapi import HTTPException, Request, UploadFile, status
from starlette.datastructures import UploadFile as StarletteUploadFile

from fortebank_api.jobs import JobManager, QueueCapacityError, WorkerUnavailableError
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


async def submit_video(
    request: Request,
    file: UploadFile,
) -> AnalyzeAccepted:
    form = await request.form()
    uploads = [(name, value) for name, value in form.multi_items() if isinstance(value, StarletteUploadFile)]
    if len(uploads) != 1 or uploads[0][0] != "file":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Exactly one MOV file is required")
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
            upload_sha256=saved.sha256,
            created_at=created_at,
            upload_duration_sec=time.perf_counter() - request_started,
        )
    except (QueueCapacityError, WorkerUnavailableError) as exc:
        storage.delete(saved.path)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Local worker is unavailable" if isinstance(exc, WorkerUnavailableError) else "Analysis queue is full",
        ) from exc
    except BaseException:
        storage.delete(saved.path)
        raise

    return AnalyzeAccepted(job_id=job_id)


def read_job(job_id: str, request: Request) -> JobResponse:
    manager: JobManager = request.app.state.job_manager
    response = manager.get_public_response(job_id)
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return response
