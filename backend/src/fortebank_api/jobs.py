"""Persistent job repository and bounded single-worker processing."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fortebank_api.analyzers import AnalysisEvent, AnalysisResult, Analyzer
from fortebank_api.schemas import (
    DoneJobResponse,
    EvidenceFrame,
    FailedJobResponse,
    JobResponse,
    ProcessingJobResponse,
    Violation,
)
from fortebank_api.storage import VideoStorage


logger = logging.getLogger(__name__)


class QueueCapacityError(RuntimeError):
    """Raised before registration when the bounded queue is full."""


class WorkerUnavailableError(RuntimeError):
    """Raised when no running worker can process a newly submitted job."""


class InvalidAnalysisEventError(ValueError):
    """Raised before extraction when an analyzer event is structurally unsafe."""


class EvidenceExtractor(Protocol):
    """Backend-owned boundary for extracting original frames by timestamp."""

    async def extract(
        self,
        video_path: Path,
        timestamps_sec: tuple[float, ...],
    ) -> list[EvidenceFrame]:
        """Return validated JPEG frames for one event."""


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    state: str
    video_path: Path
    result_json: str | None
    error_code: str | None
    upload_duration_sec: float
    queue_duration_sec: float | None
    analyzer_duration_sec: float | None
    evidence_duration_sec: float | None
    total_duration_sec: float | None
    upload_sha256: str | None = None
    diagnostics_json: str | None = None


class JobRepository:
    """Small SQLite repository using one short-lived connection per operation."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'done', 'failed')),
                    video_path TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    queued_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    result_json TEXT,
                    error_code TEXT,
                    upload_duration_sec REAL NOT NULL,
                    queue_duration_sec REAL,
                    analyzer_duration_sec REAL,
                    evidence_duration_sec REAL,
                    total_duration_sec REAL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "evidence_duration_sec" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN evidence_duration_sec REAL"
                )
            for name in ("upload_sha256", "diagnostics_json"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_terminal_age ON jobs(completed_at) "
                "WHERE state IN ('done', 'failed')"
            )

    def delete_expired(self, cutoff: float, *, batch_size: int = 100) -> int:
        """Delete a bounded batch of terminal results only; active jobs are never eligible."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM jobs WHERE job_id IN (SELECT job_id FROM jobs "
                "WHERE state IN ('done', 'failed') AND completed_at < ? "
                "ORDER BY completed_at LIMIT ?)", (cutoff, batch_size),
            )
            return cursor.rowcount

    def create(
        self,
        *,
        job_id: str,
        video_path: Path,
        created_at: float,
        queued_at: float,
        upload_duration_sec: float,
        upload_sha256: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, state, video_path, created_at, queued_at, upload_duration_sec, upload_sha256
                ) VALUES (?, 'queued', ?, ?, ?, ?, ?)
                """,
                (job_id, str(video_path), created_at, queued_at, upload_duration_sec, upload_sha256),
            )

    def mark_running(self, job_id: str, started_at: float) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = 'running', started_at = ?, queue_duration_sec = ? - queued_at
                WHERE job_id = ? AND state = 'queued'
                """,
                (started_at, started_at, job_id),
            )
            return cursor.rowcount == 1

    def save_diagnostics(self, job_id: str, diagnostics: dict) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE jobs SET diagnostics_json=? WHERE job_id=? AND state='running'",
                               (json.dumps(diagnostics, allow_nan=False), job_id))

    def mark_done(
        self,
        job_id: str,
        result: DoneJobResponse,
        *,
        completed_at: float,
        analyzer_duration_sec: float,
        evidence_duration_sec: float,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET state = 'done', completed_at = ?, result_json = ?, error_code = NULL,
                    analyzer_duration_sec = ?, evidence_duration_sec = ?,
                    total_duration_sec = ? - created_at
                WHERE job_id = ? AND state = 'running'
                """,
                (
                    completed_at,
                    result.model_dump_json(),
                    analyzer_duration_sec,
                    evidence_duration_sec,
                    completed_at,
                    job_id,
                ),
            )

    def mark_failed(self, job_id: str, error_code: str, completed_at: float) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET state = 'failed', completed_at = ?, error_code = ?,
                    total_duration_sec = ? - created_at
                WHERE job_id = ? AND state IN ('queued', 'running')
                """,
                (completed_at, error_code, completed_at, job_id),
            )

    def recover_unfinished(self, completed_at: float) -> list[Path]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT video_path FROM jobs WHERE state IN ('queued', 'running')"
            ).fetchall()
            connection.execute(
                """
                UPDATE jobs
                SET state = 'failed', completed_at = ?, error_code = 'interrupted_restart',
                    total_duration_sec = ? - created_at
                WHERE state IN ('queued', 'running')
                """,
                (completed_at, completed_at),
            )
        return [Path(row["video_path"]) for row in rows]

    def get(self, job_id: str) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT job_id, state, video_path, result_json, error_code,
                       upload_duration_sec, queue_duration_sec,
                       analyzer_duration_sec, evidence_duration_sec,
                       total_duration_sec, upload_sha256, diagnostics_json
                FROM jobs WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return JobRecord(
            job_id=row["job_id"],
            state=row["state"],
            video_path=Path(row["video_path"]),
            result_json=row["result_json"],
            error_code=row["error_code"],
            upload_duration_sec=row["upload_duration_sec"],
            queue_duration_sec=row["queue_duration_sec"],
            analyzer_duration_sec=row["analyzer_duration_sec"],
            evidence_duration_sec=row["evidence_duration_sec"],
            total_duration_sec=row["total_duration_sec"],
            upload_sha256=row["upload_sha256"], diagnostics_json=row["diagnostics_json"],
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA secure_delete = ON")
            with connection:
                yield connection
        finally:
            connection.close()


class JobManager:
    """Persist jobs and process them sequentially in one bounded local worker."""

    def __init__(
        self,
        *,
        repository: JobRepository,
        storage: VideoStorage,
        analyzer: Analyzer,
        evidence_extractor: EvidenceExtractor,
        max_queue_size: int,
        analysis_timeout_sec: float,
        job_retention_sec: float = 86400.0,
        cleanup_interval_sec: float = 300.0,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.analyzer = analyzer
        self.evidence_extractor = evidence_extractor
        self.analysis_timeout_sec = analysis_timeout_sec
        self.job_retention_sec = job_retention_sec
        self.cleanup_interval_sec = cleanup_interval_sec
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_queue_size)
        self._submit_lock = asyncio.Lock()
        self._worker_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._worker_task is not None and not self._worker_task.done()

    async def start(self) -> None:
        self.storage.initialize()
        self.repository.initialize()
        for path in self.repository.recover_unfinished(time.time()):
            self.storage.delete(path)
        self.storage.clean_startup_orphans()
        self.repository.delete_expired(time.time() - self.job_retention_sec)
        self._worker_task = asyncio.create_task(self._worker(), name="analysis-worker")
        self._cleanup_task = asyncio.create_task(self._cleanup(), name="result-cleanup")

    async def _cleanup(self) -> None:
        while True:
            await asyncio.sleep(self.cleanup_interval_sec)
            try:
                self.repository.delete_expired(time.time() - self.job_retention_sec)
            except Exception as exc:
                # An intermittent cleanup error must not kill processing or leak SQL/data.
                logger.error("Result cleanup failed (%s)", type(exc).__name__)

    async def stop(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
            self._cleanup_task = None
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Worker stopped unexpectedly (%s)", type(exc).__name__)
        self._worker_task = None
        for path in self.repository.recover_unfinished(time.time()):
            self.storage.delete(path)

    async def submit(
        self,
        *,
        job_id: str,
        video_path: Path,
        created_at: float,
        upload_duration_sec: float,
        upload_sha256: str | None = None,
    ) -> None:
        async with self._submit_lock:
            if not self.is_running:
                raise WorkerUnavailableError("Local worker is unavailable")
            if self._queue.full():
                raise QueueCapacityError("The local analysis queue is full")
            queued_at = time.time()
            self.repository.create(
                job_id=job_id,
                video_path=video_path,
                created_at=created_at,
                queued_at=queued_at,
                upload_duration_sec=upload_duration_sec,
                upload_sha256=upload_sha256,
            )
            self._queue.put_nowait(job_id)

    def get_public_response(self, job_id: str) -> JobResponse | None:
        record = self.repository.get(job_id)
        if record is None:
            return None
        if record.state in {"queued", "running"}:
            if not self.is_running:
                self.repository.mark_failed(job_id, "worker_unavailable", time.time())
                self.storage.delete(record.video_path)
                return FailedJobResponse(status="failed")
            return ProcessingJobResponse(status="processing")
        if record.state == "failed":
            return FailedJobResponse(status="failed")
        if record.state == "done" and record.result_json is not None:
            return DoneJobResponse.model_validate_json(record.result_json)
        raise RuntimeError("Stored job state is inconsistent")

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._process(job_id)
            finally:
                self._queue.task_done()

    async def _process(self, job_id: str) -> None:
        record = self.repository.get(job_id)
        if record is None:
            return

        if not self.repository.mark_running(job_id, time.time()):
            return  # A duplicate queue item must not re-run a completed/failed job.
        try:
            result, analyzer_duration, evidence_duration = await asyncio.wait_for(
                self._analyze_and_build(job_id, record.video_path),
                timeout=self.analysis_timeout_sec,
            )
            self.repository.mark_done(
                job_id,
                result,
                completed_at=time.time(),
                analyzer_duration_sec=analyzer_duration,
                evidence_duration_sec=evidence_duration,
            )
        except asyncio.CancelledError:
            self.repository.mark_failed(job_id, "worker_stopped", time.time())
            raise
        except Exception as exc:
            error_code = "analysis_timeout" if isinstance(exc, TimeoutError) else type(exc).__name__
            self.repository.mark_failed(job_id, error_code, time.time())
            logger.error("Analysis job %s failed (%s)", job_id, error_code)
        finally:
            self.storage.delete(record.video_path)

    async def _analyze_and_build(
        self,
        job_id: str,
        video_path: Path,
    ) -> tuple[DoneJobResponse, float, float]:
        def source_hash() -> str:
            with video_path.open("rb") as handle:
                return hashlib.file_digest(handle, "sha256").hexdigest()
        before = await asyncio.to_thread(source_hash)
        record = self.repository.get(job_id)
        if record is None or (record.upload_sha256 is not None and before != record.upload_sha256):
            raise ValueError("Uploaded source integrity mismatch")
        analyzer_started = time.perf_counter()
        analysis = await self.analyzer.analyze_video(video_path)
        if not isinstance(analysis, AnalysisResult) or not isinstance(analysis.events, tuple):
            raise InvalidAnalysisEventError("Malformed analyzer result")
        if analysis.diagnostics is not None:
            if analysis.diagnostics.get("source_sha256") != before:
                raise ValueError("Analyzed source integrity mismatch")
            self.repository.save_diagnostics(job_id, analysis.diagnostics)
        analyzer_duration = time.perf_counter() - analyzer_started
        evidence_started = time.perf_counter()
        result = await self._build_result(job_id, video_path, analysis)
        if await asyncio.to_thread(source_hash) != before:
            raise ValueError("Source changed during inference or evidence extraction")
        evidence_duration = time.perf_counter() - evidence_started
        return result, analyzer_duration, evidence_duration

    async def _build_result(
        self,
        job_id: str,
        video_path: Path,
        analysis: AnalysisResult,
    ) -> DoneJobResponse:
        if not analysis.events:
            return DoneJobResponse(
                job_id=job_id,
                status="done",
                verdict="no_violation",
                violations=[],
            )

        for event in analysis.events:
            self._validate_event(event)
        violations = [
            await self._build_violation(video_path, event)
            for event in analysis.events
        ]
        return DoneJobResponse(
            job_id=job_id,
            status="done",
            verdict="violation",
            violations=violations,
        )

    async def _build_violation(
        self,
        video_path: Path,
        event: AnalysisEvent,
    ) -> Violation:
        frames = await self.evidence_extractor.extract(
            video_path,
            event.evidence_timestamps_sec,
        )
        return Violation(
            type=event.class_id,
            timecode_sec=event.timecode_sec,
            description=event.observation,
            evidence_frames=frames,
        )

    @staticmethod
    def _validate_event(event: AnalysisEvent) -> None:
        if not isinstance(event.class_id, str) or not event.class_id.strip():
            raise InvalidAnalysisEventError("Event class_id must be non-blank")
        if not isinstance(event.observation, str) or not event.observation.strip():
            raise InvalidAnalysisEventError("Event observation must be non-blank")
        if event.rule_id is None and event.class_id == "possible_violation":
            pass  # Binary classifier has no verified category or legal rule.
        elif not isinstance(event.rule_id, str) or not event.rule_id.strip():
            raise InvalidAnalysisEventError("Event rule_id must be non-blank")

        numeric_values = (
            event.start_sec,
            event.end_sec,
            event.timecode_sec,
            event.confidence,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in numeric_values
        ):
            raise InvalidAnalysisEventError("Event numbers must be finite")
        if event.start_sec < 0 or event.end_sec < 0 or event.timecode_sec < 0:
            raise InvalidAnalysisEventError("Event times must be non-negative")
        if not event.start_sec <= event.timecode_sec <= event.end_sec or not 0 <= event.confidence <= 1:
            raise InvalidAnalysisEventError("Event bounds or score are invalid")
        if not 1 <= len(event.evidence_timestamps_sec) <= 3:
            raise InvalidAnalysisEventError(
                "An event requires one to three evidence timestamps"
            )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in event.evidence_timestamps_sec
        ):
            raise InvalidAnalysisEventError(
                "Evidence timestamps must be finite and non-negative"
            )
