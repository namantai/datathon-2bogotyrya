"""Contract, security, upload, queue, and lifecycle tests."""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from fortebank_api.analyzers import AnalysisEvent, AnalysisResult, FakeAnalyzer
from fortebank_api.config import Settings
from fortebank_api.jobs import JobRepository
from fortebank_api.main import create_app
from fortebank_api.schemas import EvidenceFrame
from fortebank_api.video_validation import FFprobeVideoValidator
from tests.support import ASGIResponse, SYNTHETIC_JPEG_BASE64, multipart_file, request


API_KEY = "synthetic-test-api-key-32-characters"
AUTH_HEADERS = {"X-API-Key": API_KEY}


class AcceptingVideoValidator:
    """Test-only validator used when FFprobe behavior is not under test."""

    async def validate(self, path: Path) -> None:
        assert path.is_file()


class SyntheticEvidenceExtractor:
    """Return generated JPEG fixtures only inside automated tests."""

    async def extract(
        self,
        video_path: Path,
        timestamps_sec: tuple[float, ...],
    ) -> list[EvidenceFrame]:
        assert video_path.is_file()
        return [
            EvidenceFrame(
                timestamp_sec=timestamp,
                image_base64=SYNTHETIC_JPEG_BASE64,
            )
            for timestamp in timestamps_sec
        ]


class InvalidEvidenceExtractor:
    """Inject malformed evidence to verify fail-closed result validation."""

    def __init__(self, image_base64: str) -> None:
        self.image_base64 = image_base64

    async def extract(
        self,
        video_path: Path,
        timestamps_sec: tuple[float, ...],
    ) -> list[Any]:
        assert video_path.is_file()
        return [
            {
                "timestamp_sec": timestamps_sec[0],
                "image_base64": self.image_base64,
            }
        ]


def synthetic_event(
    timestamp_sec: float,
    *,
    index: int = 0,
    evidence_timestamps_sec: tuple[float, ...] | None = None,
) -> AnalysisEvent:
    return AnalysisEvent(
        class_id=f"synthetic-class-{index}",
        start_sec=max(0.0, timestamp_sec - 0.1),
        end_sec=timestamp_sec + 0.1,
        timecode_sec=timestamp_sec,
        confidence=0.8,
        observation="Синтетическое наблюдение только для интеграционного теста",
        rule_id=f"synthetic-rule-{index}",
        evidence_timestamps_sec=evidence_timestamps_sec or (timestamp_sec,),
    )


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "api_key": API_KEY,
        "analyzer_mode": "fake",
        "storage_dir": tmp_path / "storage",
        "database_path": tmp_path / "storage" / "jobs.sqlite3",
        "max_upload_bytes": 2 * 1024 * 1024,
        "upload_chunk_bytes": 1024,
        "ffprobe_path": shutil.which("ffprobe") or "ffprobe",
        "ffprobe_timeout_sec": 5.0,
        "ffmpeg_path": shutil.which("ffmpeg") or "ffmpeg",
        "evidence_timeout_sec": 5.0,
        "evidence_max_width": 640,
        "evidence_max_height": 480,
        "evidence_max_jpeg_bytes": 1024 * 1024,
        "evidence_jpeg_quality": 3,
        "evidence_endpoint_scan_sec": 1.0,
        "max_queue_size": 8,
        "analysis_timeout_sec": 2.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


async def post_file(
    app: Any,
    content: bytes,
    *,
    filename: str = "synthetic.mov",
    field_name: str = "file",
    content_type: str = "video/quicktime",
    headers: dict[str, str] | None = None,
) -> ASGIResponse:
    body, multipart_type = multipart_file(
        content,
        filename=filename,
        field_name=field_name,
        content_type=content_type,
    )
    request_headers = {"Content-Type": multipart_type}
    request_headers.update(AUTH_HEADERS if headers is None else headers)
    return await request(app, "POST", "/api/v1/analyze", headers=request_headers, body=body)


async def get_job(app: Any, job_id: str) -> ASGIResponse:
    return await request(
        app,
        "GET",
        f"/api/v1/jobs/{job_id}",
        headers=AUTH_HEADERS,
    )


async def wait_for_terminal(app: Any, job_id: str) -> ASGIResponse:
    for _ in range(200):
        response = await get_job(app, job_id)
        if response.json()["status"] != "processing":
            return response
        await asyncio.sleep(0.01)
    raise AssertionError("Job did not reach a terminal state")


def test_missing_api_key_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(make_settings(tmp_path), video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            get_response = await request(app, "GET", "/api/v1/jobs/missing")
            post_response = await post_file(app, b"synthetic", headers={})
        assert get_response.status_code == post_response.status_code == 401
        assert get_response.json() == post_response.json() == {"detail": "Unauthorized"}

    asyncio.run(scenario())


def test_wrong_api_key_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(make_settings(tmp_path), video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            wrong_headers = {"X-API-Key": "wrong-but-long-enough-key"}
            get_response = await request(
                app,
                "GET",
                "/api/v1/jobs/missing",
                headers=wrong_headers,
            )
            post_response = await post_file(app, b"synthetic", headers=wrong_headers)
        assert get_response.status_code == post_response.status_code == 401

    asyncio.run(scenario())


def test_correct_api_key_reaches_route(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(make_settings(tmp_path), video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            response = await get_job(app, "missing")
        assert response.status_code == 404
        assert response.json() == {"detail": "Job not found"}

    asyncio.run(scenario())


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(make_settings(tmp_path), video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            missing_response = await request(
                app,
                "POST",
                "/api/v1/analyze",
                headers=AUTH_HEADERS,
            )
            wrong_field_response = await post_file(
                app,
                b"synthetic",
                field_name="video",
            )
        assert missing_response.status_code == wrong_field_response.status_code == 422

    asyncio.run(scenario())


def test_unsupported_extension_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(make_settings(tmp_path), video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            response = await post_file(app, b"synthetic", filename="video.mp4")
        assert response.status_code == 415
        assert not list((tmp_path / "storage").glob("*.mov"))

    asyncio.run(scenario())


def test_corrupted_mov_is_rejected_and_deleted(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = make_settings(tmp_path)
        app = create_app(
            settings,
            video_validator=FFprobeVideoValidator(settings.ffprobe_path, 5.0),
        )
        async with app.router.lifespan_context(app):
            response = await post_file(app, b"not-a-video")
        assert response.status_code == 400
        assert not list((tmp_path / "storage").glob("*.mov"))

    asyncio.run(scenario())


def test_empty_mov_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(make_settings(tmp_path), video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            response = await post_file(app, b"")
        assert response.status_code == 400
        assert not list((tmp_path / "storage").glob("*.mov"))

    asyncio.run(scenario())


def test_upload_limit_is_enforced(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = make_settings(tmp_path, max_upload_bytes=4, upload_chunk_bytes=2)
        app = create_app(settings, video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            response = await post_file(app, b"12345")
        assert response.status_code == 413
        assert not list((tmp_path / "storage").glob("*.mov"))

    asyncio.run(scenario())


def test_client_filename_cannot_escape_storage(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(
            make_settings(tmp_path),
            analyzer=FakeAnalyzer(delay_sec=0.1),
            video_validator=AcceptingVideoValidator(),
        )
        async with app.router.lifespan_context(app):
            accepted = await post_file(app, b"synthetic", filename="../../outside.mov")
            record = app.state.job_manager.repository.get(accepted.json()["job_id"])
            assert record is not None
            assert record.video_path.parent == (tmp_path / "storage").resolve()
            assert record.video_path.name != "outside.mov"
            await wait_for_terminal(app, accepted.json()["job_id"])
        assert not (tmp_path / "outside.mov").exists()

    asyncio.run(scenario())


def test_missing_ffprobe_is_controlled_and_file_is_deleted(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = make_settings(tmp_path, ffprobe_path="/missing/ffprobe")
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            response = await post_file(app, b"synthetic-container")
        assert response.status_code == 503
        assert response.json() == {"detail": "Local video validation is unavailable"}
        assert not list((tmp_path / "storage").glob("*.mov"))

    asyncio.run(scenario())


def test_valid_synthetic_mov_completes_without_violations(
    tmp_path: Path,
    synthetic_mov: bytes,
) -> None:
    async def scenario() -> None:
        settings = make_settings(tmp_path)
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            accepted = await post_file(
                app,
                synthetic_mov,
                content_type="application/octet-stream",
            )
            assert accepted.status_code == 202
            assert set(accepted.json()) == {"job_id"}
            finished = await wait_for_terminal(app, accepted.json()["job_id"])
            record = app.state.job_manager.repository.get(accepted.json()["job_id"])

        assert finished.status_code == 200
        assert finished.json() == {
            "job_id": accepted.json()["job_id"],
            "status": "done",
            "verdict": "no_violation",
            "violations": [],
        }
        assert record is not None
        assert record.upload_duration_sec >= 0
        assert record.queue_duration_sec is not None and record.queue_duration_sec >= 0
        assert record.analyzer_duration_sec is not None and record.analyzer_duration_sec >= 0
        assert record.evidence_duration_sec is not None and record.evidence_duration_sec >= 0
        assert record.total_duration_sec is not None and record.total_duration_sec >= 0
        assert not list((tmp_path / "storage").glob("*.mov"))

    asyncio.run(scenario())


def test_no_events_do_not_invoke_unavailable_ffmpeg(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(
            make_settings(tmp_path, ffmpeg_path="/missing/ffmpeg"),
            video_validator=AcceptingVideoValidator(),
        )
        async with app.router.lifespan_context(app):
            accepted = await post_file(app, b"synthetic")
            finished = await wait_for_terminal(app, accepted.json()["job_id"])
        assert finished.json()["status"] == "done"
        assert finished.json()["verdict"] == "no_violation"
        assert finished.json()["violations"] == []

    asyncio.run(scenario())


def test_job_id_is_unique(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(make_settings(tmp_path), video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            first = await post_file(app, b"first")
            second = await post_file(app, b"second")
            assert first.status_code == second.status_code == 202
            assert first.json()["job_id"] != second.json()["job_id"]
            await wait_for_terminal(app, first.json()["job_id"])
            await wait_for_terminal(app, second.json()["job_id"])

    asyncio.run(scenario())


def test_processing_is_observable(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(
            make_settings(tmp_path),
            analyzer=FakeAnalyzer(delay_sec=0.2),
            video_validator=AcceptingVideoValidator(),
        )
        async with app.router.lifespan_context(app):
            accepted = await post_file(app, b"synthetic")
            current = await get_job(app, accepted.json()["job_id"])
            assert current.json() == {"status": "processing"}
            await wait_for_terminal(app, accepted.json()["job_id"])

    asyncio.run(scenario())


def test_analyzer_error_results_in_failed(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(
            make_settings(tmp_path),
            analyzer=FakeAnalyzer(error=RuntimeError("synthetic failure")),
            video_validator=AcceptingVideoValidator(),
        )
        async with app.router.lifespan_context(app):
            accepted = await post_file(app, b"synthetic")
            finished = await wait_for_terminal(app, accepted.json()["job_id"])
        assert finished.json() == {"status": "failed"}

    asyncio.run(scenario())


def test_unknown_job_id_returns_404(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(make_settings(tmp_path), video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            response = await get_job(app, "does-not-exist")
        assert response.status_code == 404

    asyncio.run(scenario())


def test_parallel_posts_create_distinct_jobs(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(
            make_settings(tmp_path),
            analyzer=FakeAnalyzer(delay_sec=0.03),
            video_validator=AcceptingVideoValidator(),
        )
        async with app.router.lifespan_context(app):
            responses = await asyncio.gather(
                *(post_file(app, f"synthetic-{index}".encode()) for index in range(4))
            )
            assert all(response.status_code == 202 for response in responses)
            job_ids = {response.json()["job_id"] for response in responses}
            assert len(job_ids) == 4
            terminal = await asyncio.gather(
                *(wait_for_terminal(app, job_id) for job_id in job_ids)
            )
            assert all(response.json()["status"] == "done" for response in terminal)

    asyncio.run(scenario())


def test_predefined_synthetic_events_complete_with_multiple_violations(
    tmp_path: Path,
) -> None:
    events = tuple(
        AnalysisEvent(
            class_id=f"synthetic-class-{index}",
            start_sec=float(index),
            end_sec=float(index + 1),
            timecode_sec=float(index) + 0.5,
            confidence=0.8,
            observation="Синтетическое наблюдение только для теста",
            rule_id=f"synthetic-rule-{index}",
            evidence_timestamps_sec=(float(index) + 0.5,),
        )
        for index in range(2)
    )

    async def scenario() -> None:
        app = create_app(
            make_settings(tmp_path),
            analyzer=FakeAnalyzer(AnalysisResult(events=events)),
            video_validator=AcceptingVideoValidator(),
            evidence_extractor=SyntheticEvidenceExtractor(),
        )
        async with app.router.lifespan_context(app):
            accepted = await post_file(app, b"synthetic")
            finished = await wait_for_terminal(app, accepted.json()["job_id"])

        payload = finished.json()
        assert payload["status"] == "done"
        assert payload["verdict"] == "violation"
        assert len(payload["violations"]) == 2
        assert all(len(event["evidence_frames"]) == 1 for event in payload["violations"])

    asyncio.run(scenario())


def test_real_evidence_e2e_for_one_event(
    tmp_path: Path,
    synthetic_mov: bytes,
) -> None:
    async def scenario() -> None:
        analyzer = FakeAnalyzer(AnalysisResult(events=(synthetic_event(0.5),)))
        app = create_app(make_settings(tmp_path), analyzer=analyzer)
        async with app.router.lifespan_context(app):
            accepted = await post_file(app, synthetic_mov)
            finished = await wait_for_terminal(app, accepted.json()["job_id"])
            record = app.state.job_manager.repository.get(accepted.json()["job_id"])

        payload = finished.json()
        assert accepted.status_code == 202
        assert payload["status"] == "done"
        assert payload["verdict"] == "violation"
        assert len(payload["violations"]) == 1
        frame = payload["violations"][0]["evidence_frames"][0]
        assert set(frame) == {"timestamp_sec", "image_base64"}
        assert not frame["image_base64"].startswith("data:")
        assert record is not None
        assert record.evidence_duration_sec is not None
        assert record.evidence_duration_sec > 0
        assert not list((tmp_path / "storage").glob("*.mov"))

    asyncio.run(scenario())


def test_real_evidence_e2e_for_two_events_and_multiple_frames(
    tmp_path: Path,
    synthetic_mov: bytes,
) -> None:
    events = (
        synthetic_event(0.2, index=1, evidence_timestamps_sec=(0.1, 0.2, 0.3)),
        synthetic_event(0.7, index=2),
    )

    async def scenario() -> None:
        app = create_app(
            make_settings(tmp_path),
            analyzer=FakeAnalyzer(AnalysisResult(events=events)),
        )
        async with app.router.lifespan_context(app):
            accepted = await post_file(app, synthetic_mov)
            finished = await wait_for_terminal(app, accepted.json()["job_id"])

        payload = finished.json()
        assert payload["status"] == "done"
        assert payload["verdict"] == "violation"
        assert len(payload["violations"]) == 2
        assert len(payload["violations"][0]["evidence_frames"]) == 3
        assert len(payload["violations"][1]["evidence_frames"]) == 1

    asyncio.run(scenario())


def test_out_of_range_evidence_timestamp_fails_without_path_leak(
    tmp_path: Path,
    synthetic_mov: bytes,
) -> None:
    async def scenario() -> None:
        app = create_app(
            make_settings(tmp_path),
            analyzer=FakeAnalyzer(AnalysisResult(events=(synthetic_event(2.0),))),
        )
        async with app.router.lifespan_context(app):
            accepted = await post_file(app, synthetic_mov)
            finished = await wait_for_terminal(app, accepted.json()["job_id"])
            record = app.state.job_manager.repository.get(accepted.json()["job_id"])

        assert finished.json() == {"status": "failed"}
        assert str(tmp_path) not in finished.content.decode("utf-8")
        assert record is not None
        assert record.error_code == "EvidenceExtractionError"
        assert not list((tmp_path / "storage").glob("*.mov"))

    asyncio.run(scenario())


def test_ffmpeg_error_fails_job_instead_of_returning_no_violation(
    tmp_path: Path,
    synthetic_mov: bytes,
) -> None:
    async def scenario() -> None:
        app = create_app(
            make_settings(tmp_path, ffmpeg_path="/missing/ffmpeg"),
            analyzer=FakeAnalyzer(AnalysisResult(events=(synthetic_event(0.5),))),
        )
        async with app.router.lifespan_context(app):
            accepted = await post_file(app, synthetic_mov)
            finished = await wait_for_terminal(app, accepted.json()["job_id"])
        assert finished.json() == {"status": "failed"}

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", ["not-base64", "bm90LWEtanBlZw=="])
def test_invalid_evidence_payload_fails_job(
    tmp_path: Path,
    payload: str,
) -> None:
    async def scenario() -> None:
        app = create_app(
            make_settings(tmp_path),
            analyzer=FakeAnalyzer(AnalysisResult(events=(synthetic_event(0.5),))),
            video_validator=AcceptingVideoValidator(),
            evidence_extractor=InvalidEvidenceExtractor(payload),
        )
        async with app.router.lifespan_context(app):
            accepted = await post_file(app, b"synthetic")
            finished = await wait_for_terminal(app, accepted.json()["job_id"])
        assert finished.json() == {"status": "failed"}

    asyncio.run(scenario())


def test_events_fail_closed_without_real_evidence_extractor(tmp_path: Path) -> None:
    event = AnalysisEvent(
        class_id="synthetic-class",
        start_sec=0,
        end_sec=1,
        timecode_sec=0.5,
        confidence=0.8,
        observation="Синтетическое наблюдение",
        rule_id="synthetic-rule",
        evidence_timestamps_sec=(0.5,),
    )

    async def scenario() -> None:
        app = create_app(
            make_settings(tmp_path),
            analyzer=FakeAnalyzer(AnalysisResult(events=(event,))),
            video_validator=AcceptingVideoValidator(),
        )
        async with app.router.lifespan_context(app):
            accepted = await post_file(app, b"synthetic")
            finished = await wait_for_terminal(app, accepted.json()["job_id"])
        assert finished.json() == {"status": "failed"}

    asyncio.run(scenario())


def test_queue_capacity_is_bounded(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(
            make_settings(tmp_path, max_queue_size=1, analysis_timeout_sec=2.0),
            analyzer=FakeAnalyzer(delay_sec=0.2),
            video_validator=AcceptingVideoValidator(),
        )
        async with app.router.lifespan_context(app):
            first = await post_file(app, b"first")
            for _ in range(50):
                first_record = app.state.job_manager.repository.get(first.json()["job_id"])
                if first_record is not None and first_record.state == "running":
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("First job was not picked up by the worker")
            second = await post_file(app, b"second")
            third = await post_file(app, b"third")
            assert first.status_code == second.status_code == 202
            assert third.status_code == 503
            assert third.json() == {"detail": "Analysis queue is full"}
            await wait_for_terminal(app, first.json()["job_id"])
            await wait_for_terminal(app, second.json()["job_id"])
        assert not list((tmp_path / "storage").glob("*.mov"))

    asyncio.run(scenario())


def test_analyzer_timeout_results_in_failed(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(
            make_settings(tmp_path, analysis_timeout_sec=0.01),
            analyzer=FakeAnalyzer(delay_sec=0.2),
            video_validator=AcceptingVideoValidator(),
        )
        async with app.router.lifespan_context(app):
            accepted = await post_file(app, b"synthetic")
            finished = await wait_for_terminal(app, accepted.json()["job_id"])
            record = app.state.job_manager.repository.get(accepted.json()["job_id"])
        assert finished.json() == {"status": "failed"}
        assert record is not None
        assert record.error_code == "analysis_timeout"

    asyncio.run(scenario())


def test_unfinished_job_is_failed_after_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    storage_dir = Path(settings.storage_dir)
    storage_dir.mkdir(parents=True)
    video_path = storage_dir / "interrupted.mov"
    video_path.write_bytes(b"synthetic")
    repository = JobRepository(Path(settings.database_path))
    repository.initialize()
    repository.create(
        job_id="interrupted-job",
        video_path=video_path,
        created_at=time.time() - 1,
        queued_at=time.time() - 1,
        upload_duration_sec=0.01,
    )

    async def scenario() -> None:
        app = create_app(settings, video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            response = await get_job(app, "interrupted-job")
            record = app.state.job_manager.repository.get("interrupted-job")
        assert response.json() == {"status": "failed"}
        assert record is not None
        assert record.error_code == "interrupted_restart"
        assert record.total_duration_sec is not None
        assert not video_path.exists()

    asyncio.run(scenario())
