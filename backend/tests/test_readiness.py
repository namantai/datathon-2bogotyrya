"""Regression checks for confirmed ingress and lifecycle readiness gaps."""

import asyncio
import contextlib
import sqlite3
import time
from pathlib import Path

import pytest
import starlette.formparsers

from fortebank_api.main import create_app
from tests.support import multipart_file, request
from tests.test_api import API_KEY, AcceptingVideoValidator, get_job, make_settings, post_file, wait_for_terminal
from fortebank_api.jobs import JobRepository
from fortebank_api.schemas import DoneJobResponse
from fortebank_api.video_validation import FFprobeVideoValidator


@pytest.mark.parametrize("key", [None, "wrong-synthetic-key", "\u00ff"])
def test_jury_auth_rejects_before_reading_multipart(tmp_path: Path, key: str | None) -> None:
    async def scenario():
        app = create_app(make_settings(tmp_path))
        reads = 0
        async def observed(scope, receive, send):
            async def counted_receive():
                nonlocal reads
                reads += 1
                return await receive()
            await app(scope, counted_receive, send)
        body, content_type = multipart_file(b"synthetic")
        headers = {"Content-Type": content_type}
        if key is not None:
            headers["X-API-Key"] = key
        response = await request(observed, "POST", "/api/v1/analyze", body=body,
                                 headers=headers)
        assert response.status_code == 401
        assert reads == 0, "Unauthorized requests must not spool multipart before authentication"
    asyncio.run(scenario())


@pytest.mark.parametrize("slow", [False, True])
def test_jury_chunked_rejection_closes_partially_parsed_file(tmp_path: Path, monkeypatch, slow: bool) -> None:
    original = starlette.formparsers.SpooledTemporaryFile
    opened = []
    def tracked(*args, **kwargs):
        file = original(*args, **kwargs)
        opened.append(file)
        return file
    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", tracked)

    async def scenario():
        app = create_app(make_settings(tmp_path, max_upload_bytes=100, jury_upload_timeout_sec=.02))
        body, media = multipart_file(b"x" * 70000)
        async def streamed(scope, receive, send):
            scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"content-length"]
            first = True
            async def chunks():
                nonlocal first
                if first:
                    first = False
                    return {"type": "http.request", "body": body[:1024], "more_body": True}
                if slow:
                    await asyncio.sleep(1)
                return {"type": "http.request", "body": body[1024:], "more_body": False}
            await app(scope, chunks, send)
        response = await request(streamed, "POST", "/api/v1/analyze",
                                 headers={"Content-Type": media, "X-API-Key": API_KEY})
        assert response.status_code == (408 if slow else 413)
        assert opened and all(file.closed for file in opened)
    asyncio.run(scenario())


def test_stopped_worker_does_not_report_processing_or_accept_jobs(tmp_path: Path) -> None:
    async def scenario():
        app = create_app(make_settings(tmp_path), video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            accepted = await post_file(app, b"synthetic")
            worker = app.state.job_manager._worker_task
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            assert (await get_job(app, accepted.json()["job_id"])).json() == {"status": "failed"}
            assert (await post_file(app, b"another-synthetic")).status_code == 503
            assert (await request(app, "GET", "/health")).status_code == 503
    asyncio.run(scenario())


def test_restart_cleans_unregistered_generated_uploads_only(tmp_path: Path) -> None:
    async def scenario():
        settings = make_settings(tmp_path)
        settings.storage_dir.mkdir()
        orphan = settings.storage_dir / ("a" * 32 + ".mov")
        orphan.write_bytes(b"synthetic-crash-before-registration")
        unrelated = settings.storage_dir / "user-owned.mov"
        unrelated.write_bytes(b"synthetic-unrelated-file")
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            assert not orphan.exists()
            assert unrelated.exists()
    asyncio.run(scenario())


def test_terminal_retention_keeps_active_recent_and_reuses_sqlite_pages(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "test.sqlite3")
    repo.initialize()
    now = time.time()
    for job_id in ("old-done", "old-failed", "recent", "queued", "running"):
        repo.create(job_id=job_id, video_path=tmp_path / f"{job_id}.mov", created_at=now - 1000,
                    queued_at=now - 1000, upload_duration_sec=.1)
    for job_id in ("old-done", "recent", "running"):
        repo.mark_running(job_id, now - 500)
    for job_id, completed in (("old-done", now - 200), ("recent", now)):
        repo.mark_done(job_id, DoneJobResponse(job_id=job_id, status="done", verdict="no_violation", violations=[]),
                       completed_at=completed, analyzer_duration_sec=.1, evidence_duration_sec=.1)
    repo.mark_failed("old-failed", "synthetic", now - 200)
    with sqlite3.connect(repo.database_path) as db:
        # Synthetic payload solely to exercise page reclamation, never a real frame.
        db.execute("UPDATE jobs SET result_json=? WHERE job_id='old-done'", ("synthetic" * 100000,))
    assert repo.delete_expired(now - 100, batch_size=1) == 1
    assert repo.delete_expired(now - 100) == 1
    assert repo.get("old-done") is None and repo.get("old-failed") is None
    assert all(repo.get(job_id) is not None for job_id in ("recent", "queued", "running"))
    assert repo.delete_expired(now - 100) == 0
    with sqlite3.connect(repo.database_path) as db:
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        free = db.execute("PRAGMA freelist_count").fetchone()[0]
        assert free > 100
        db.execute("UPDATE jobs SET result_json=? WHERE job_id='recent'", ("synthetic" * 100000,))
        assert db.execute("PRAGMA page_count").fetchone()[0] <= pages


def test_periodic_retention_returns_404_and_restart_preserves_recent_result(tmp_path: Path) -> None:
    async def scenario():
        settings = make_settings(tmp_path, job_retention_sec=60, cleanup_interval_sec=.01)
        app = create_app(settings, video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            old = (await post_file(app, b"synthetic-old")).json()["job_id"]
            await wait_for_terminal(app, old)
            recent = (await post_file(app, b"synthetic-recent")).json()["job_id"]
            result = (await wait_for_terminal(app, recent)).json()
            with sqlite3.connect(settings.database_path) as db:
                db.execute("UPDATE jobs SET completed_at=? WHERE job_id=?", (time.time() - 120, old))
            for _ in range(100):
                if (await get_job(app, old)).status_code == 404:
                    break
                await asyncio.sleep(.01)
            else:
                raise AssertionError("Expired result was not purged")
            assert (await get_job(app, recent)).json() == result
        restarted = create_app(settings)
        async with restarted.router.lifespan_context(restarted):
            assert (await get_job(restarted, recent)).json() == result
    asyncio.run(scenario())


@pytest.mark.parametrize("content_length", ["0", "70000"])
def test_jury_body_cap_with_and_without_honest_length(tmp_path: Path, content_length: str) -> None:
    async def scenario():
        app = create_app(make_settings(tmp_path, max_upload_bytes=100))
        body, media = multipart_file(b"x" * 70000)
        response = await request(app, "POST", "/api/v1/analyze", body=body,
            headers={"Content-Type": media, "X-API-Key": API_KEY, "Content-Length": content_length})
        assert response.status_code == 413
        assert not (tmp_path / "storage").exists()
    asyncio.run(scenario())


def test_jury_slow_body_timeout_and_concurrency_capacity(tmp_path: Path) -> None:
    async def scenario():
        app = create_app(make_settings(tmp_path, jury_upload_timeout_sec=.02, max_jury_uploads=1))
        entered = asyncio.Event()
        async def slow(scope, receive, send):
            async def delayed():
                entered.set()
                await asyncio.sleep(1)
                return await receive()
            await app(scope, delayed, send)
        body, media = multipart_file(b"synthetic")
        headers = {"Content-Type": media, "X-API-Key": API_KEY}
        first = asyncio.create_task(request(slow, "POST", "/api/v1/analyze", body=body, headers=headers))
        await entered.wait()
        assert (await request(app, "POST", "/api/v1/analyze", body=body, headers=headers)).status_code == 503
        assert (await first).status_code == 408
        # Capacity is released even after parser timeout.
        assert (await request(app, "POST", "/api/v1/analyze", headers={"X-API-Key": API_KEY})).status_code == 422
    asyncio.run(scenario())


def test_ffprobe_limits_inputs_and_is_reaped_on_cancellation(tmp_path: Path, monkeypatch) -> None:
    class Process:
        returncode = None
        killed = False
        calls = 0
        async def communicate(self):
            self.calls += 1
            if self.calls == 1:
                raise asyncio.CancelledError
            return b"", b""
        def kill(self):
            self.killed = True
            self.returncode = -9
    process = Process()
    async def spawn(*command, **kwargs):
        assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
        assert command[command.index("-format_whitelist") + 1] == "mov"
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(FFprobeVideoValidator("ffprobe", 1).validate(tmp_path / "synthetic.mov"))
    assert process.killed and process.calls == 2


def test_worker_exception_is_fail_closed_and_shutdown_still_cleans(tmp_path: Path, monkeypatch) -> None:
    async def scenario():
        app = create_app(make_settings(tmp_path), video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            def fail(*args):
                raise RuntimeError("synthetic private failure")
            monkeypatch.setattr(app.state.job_manager.repository, "mark_running", fail)
            job_id = (await post_file(app, b"synthetic")).json()["job_id"]
            for _ in range(20):
                if not app.state.job_manager.is_running:
                    break
                await asyncio.sleep(.01)
            assert (await get_job(app, job_id)).json() == {"status": "failed"}
            assert (await request(app, "GET", "/health")).status_code == 503
        assert not list((tmp_path / "storage").glob("*.mov"))
    asyncio.run(scenario())
