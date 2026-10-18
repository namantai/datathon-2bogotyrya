"""Short real deadlines exercise the same path as the 300 second budget."""

import asyncio
from datetime import datetime
import json
import os
import sqlite3
import time
from uuid import UUID

import pytest

from fortebank_api.analyzers import AnalysisResult, FakeAnalyzer
from fortebank_api.config import Settings
from fortebank_api.jobs import JobRepository
from fortebank_api.local_analyzer import LocalProcessAnalyzer
from fortebank_api.main import create_app
from tests.support import multipart_file, request
from tests.test_api import (AUTH_HEADERS, AcceptingVideoValidator, SyntheticEvidenceExtractor,
                            get_job, make_settings, post_file, wait_for_terminal)
from tests.test_ui_api import bootstrap, session_headers


@pytest.mark.parametrize("budget", [0, -1, 301, float("nan"), float("inf")])
def test_budget_is_positive_finite_and_never_exceeds_300(budget):
    with pytest.raises(ValueError):
        Settings(_env_file=None, job_timeout_sec=budget)


def test_upload_time_consumes_job_budget(tmp_path):
    async def scenario():
        app = create_app(make_settings(tmp_path, job_timeout_sec=.2),
                         analyzer=FakeAnalyzer(delay_sec=.14),
                         video_validator=AcceptingVideoValidator())
        async def delayed_upload(scope, receive, send):
            async def slow_receive():
                await asyncio.sleep(.1)
                return await receive()
            await app(scope, slow_receive, send)
        async with app.router.lifespan_context(app):
            accepted = await post_file(delayed_upload, b"synthetic")
            assert accepted.status_code == 202
            job_id = accepted.json()["job_id"]
            UUID(job_id)
            assert (await wait_for_terminal(app, job_id)).json() == {"status": "failed"}
            record = app.state.job_manager.repository.get(job_id)
            assert record.upload_duration_sec >= .1
            assert record.deadline_at - record.created_at == pytest.approx(.2, abs=.001)
            assert record.error_code == "analysis_timeout"
            assert record.total_duration_sec < .35
            for _ in range(10):
                assert (await get_job(app, job_id)).json() == {"status": "failed"}
        assert not list((tmp_path / "storage").glob("*.mov"))
    asyncio.run(scenario())


def test_validation_deadline_rejects_before_registration_and_cleans_file(tmp_path):
    class SlowValidator:
        async def validate(self, path):
            await asyncio.sleep(1)
    async def scenario():
        app = create_app(make_settings(tmp_path, job_timeout_sec=.03), video_validator=SlowValidator())
        async with app.router.lifespan_context(app):
            response = await post_file(app, b"synthetic")
            assert response.status_code == 408
            assert not list((tmp_path / "storage").glob("*.mov"))
            with sqlite3.connect(app.state.job_manager.repository.database_path) as db:
                assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    asyncio.run(scenario())


def test_waiting_job_expires_without_polling_or_running_analyzer(tmp_path):
    class CountingAnalyzer(FakeAnalyzer):
        calls = 0
        async def analyze_video(self, path):
            self.calls += 1
            return await super().analyze_video(path)
    async def scenario():
        analyzer = CountingAnalyzer(delay_sec=.2)
        app = create_app(make_settings(tmp_path, job_timeout_sec=.5), analyzer=analyzer,
                         video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            first = (await post_file(app, b"synthetic")).json()["job_id"]
            for _ in range(20):
                if analyzer.calls:
                    break
                await asyncio.sleep(.005)
            manager = app.state.job_manager
            path = tmp_path / "storage" / "synthetic-queued.mov"
            path.write_bytes(b"synthetic")
            await manager.submit(job_id="slow-upload", video_path=path,
                                 created_at=time.time() - .46, upload_duration_sec=.46,
                                 request_started_perf=time.perf_counter() - .46)
            await asyncio.sleep(.08)
            record = manager.repository.get("slow-upload")
            assert record.state == "failed" and record.started_at is None
            assert not path.exists()
            assert (await wait_for_terminal(app, first)).json()["status"] == "done"
            await manager._queue.join()
            assert analyzer.calls == 1
            assert not manager._deadlines and not manager._expiry_handles
    asyncio.run(scenario())


def test_queue_time_is_not_granted_again_to_analyzer(tmp_path):
    async def scenario():
        app = create_app(make_settings(tmp_path, job_timeout_sec=.2),
                         analyzer=FakeAnalyzer(delay_sec=.13), video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            first = (await post_file(app, b"one")).json()["job_id"]
            second = (await post_file(app, b"two")).json()["job_id"]
            assert (await wait_for_terminal(app, first)).json()["status"] == "done"
            assert (await wait_for_terminal(app, second)).json() == {"status": "failed"}
            assert app.state.job_manager.repository.get(second).queue_duration_sec >= .1
    asyncio.run(scenario())


def test_suppressed_cancellation_cannot_publish_late_success(tmp_path):
    class BadAsyncAnalyzer:
        async def analyze_video(self, path):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                await asyncio.sleep(.01)
                return AnalysisResult()
    async def scenario():
        app = create_app(make_settings(tmp_path, analysis_timeout_sec=.02),
                         analyzer=BadAsyncAnalyzer(), video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            job_id = (await post_file(app, b"synthetic")).json()["job_id"]
            assert (await wait_for_terminal(app, job_id)).json() == {"status": "failed"}
    asyncio.run(scenario())


def test_evidence_shares_deadline_and_is_cancelled(tmp_path):
    from tests.test_api import synthetic_event
    class SlowEvidence:
        cancelled = False
        async def extract(self, path, timestamps):
            try:
                await asyncio.sleep(1)
            finally:
                self.cancelled = True
    async def scenario():
        evidence = SlowEvidence()
        app = create_app(make_settings(tmp_path, job_timeout_sec=.07),
                         analyzer=FakeAnalyzer(AnalysisResult(events=(synthetic_event(.2),)), delay_sec=.03),
                         video_validator=AcceptingVideoValidator(), evidence_extractor=evidence)
        async with app.router.lifespan_context(app):
            job_id = (await post_file(app, b"synthetic")).json()["job_id"]
            assert (await wait_for_terminal(app, job_id)).json() == {"status": "failed"}
            await app.state.job_manager._queue.join()
            assert evidence.cancelled
    asyncio.run(scenario())


def test_ui_metadata_is_opt_in_private_and_persisted(tmp_path):
    async def scenario():
        app = create_app(make_settings(tmp_path),
                         analyzer=LocalProcessAnalyzer("tests.ml_process_fixture:successful"),
                         video_validator=AcceptingVideoValidator(), evidence_extractor=SyntheticEvidenceExtractor())
        async with app.router.lifespan_context(app):
            headers = session_headers(await bootstrap(app))
            other = session_headers(await bootstrap(app))
            body, content_type = multipart_file(b"synthetic")
            accepted = await request(app, "POST", "/ui-api/analyze",
                                     headers={**headers, "Content-Type": content_type}, body=body)
            job_id = accepted.json()["job_id"]
            original = (await wait_for_terminal(app, job_id)).json()
            assert original["status"] == "done" and original["verdict"] == "violation"
            assert set(original) == {"status", "job_id", "verdict", "violations"}
            # Helper normally has no query support; wrapper changes only ASGI query_string.
            async def details_request(scope, receive, send):
                scope["query_string"] = b"include_details=true"
                await app(scope, receive, send)
            route = f"/ui-api/jobs/{job_id}"
            detailed = await request(details_request, "GET", route, headers=headers)
            payload = detailed.json()
            assert detailed.headers["cache-control"] == "no-store"
            assert payload["event_details"][0]["event_code"] == "CASH_UNSECURED"
            assert payload["event_details"][0]["regulatory_references"][0]["status"] == "insufficient_data"
            timing = payload["timing"]
            assert datetime.fromisoformat(timing["deadline"]) > datetime.fromisoformat(timing["accepted_at"])
            assert timing["duration_sec"] >= 0
            assert payload["failure_reason"] is None
            assert (await request(app, "GET", route, headers=headers)).json() == original
            assert (await request(details_request, "GET", route, headers=other)).status_code == 404
            assert (await request(details_request, "GET", route)).status_code == 401
            assert (await request(details_request, "GET", f"/api/v1/jobs/{job_id}", headers=AUTH_HEADERS)).json() == original
            assert (await request(details_request, "GET", f"/api/v1/jobs/{job_id}")).status_code == 401
            repo = app.state.job_manager.repository
            saved = repo.get(job_id)
        restarted = create_app(make_settings(tmp_path))
        async with restarted.router.lifespan_context(restarted):
            restored = restarted.state.job_manager.repository.get(job_id)
            assert restored.deadline_at == saved.deadline_at
            assert json.loads(restored.ui_details_json) == payload["event_details"]
            assert (await get_job(restarted, job_id)).json() == original
    asyncio.run(scenario())


def test_old_sqlite_is_migrated_without_losing_results(tmp_path):
    db_path = tmp_path / "old.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("""CREATE TABLE jobs (job_id TEXT PRIMARY KEY, state TEXT, video_path TEXT,
            created_at REAL, queued_at REAL, started_at REAL, completed_at REAL,
            result_json TEXT, error_code TEXT, upload_duration_sec REAL,
            queue_duration_sec REAL, analyzer_duration_sec REAL, total_duration_sec REAL)""")
        db.execute("INSERT INTO jobs (job_id,state,video_path,created_at,queued_at,result_json,upload_duration_sec) "
                   "VALUES ('old','done','unused',10,11,'{}',1)")
    repository = JobRepository(db_path)
    repository.initialize()
    repository.initialize()
    record = repository.get("old")
    assert record.state == "done" and record.result_json == "{}"
    assert record.deadline_at == 310 and record.ui_details_json is None


@pytest.mark.parametrize("function,count", [("empty", 0), ("successful", 1), ("multiple", 2)])
def test_process_adapter_with_real_mov_and_real_jpegs(tmp_path, synthetic_mov, function, count):
    async def scenario():
        app = create_app(make_settings(tmp_path, analysis_timeout_sec=5),
                         analyzer=LocalProcessAnalyzer(f"tests.ml_process_fixture:{function}"))
        async with app.router.lifespan_context(app):
            job_id = (await post_file(app, synthetic_mov)).json()["job_id"]
            result = (await wait_for_terminal(app, job_id)).json()
            assert result["status"] == "done"
            assert result["verdict"] == ("violation" if count else "no_violation")
            assert len(result["violations"]) == count
            for event in result["violations"]:
                assert event["evidence_frames"][0]["image_base64"].startswith("/9j/")
            if count == 2:
                assert result["violations"][1]["type"] == "UNKNOWN_SYNTHETIC_CODE"
                details = json.loads(app.state.job_manager.repository.get(job_id).ui_details_json)
                assert details[1]["status"] == "unknown_event"
        assert not list((tmp_path / "storage").glob("*.mov"))
    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process lifecycle")
def test_worker_deadline_kills_ml_and_exposes_safe_timeout_details(tmp_path):
    async def scenario():
        app = create_app(make_settings(tmp_path, job_timeout_sec=.8),
                         analyzer=LocalProcessAnalyzer("tests.ml_process_fixture:blocking"),
                         video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            headers = session_headers(await bootstrap(app))
            body, content_type = multipart_file(b"synthetic")
            accepted = await request(app, "POST", "/ui-api/analyze",
                                     headers={**headers, "Content-Type": content_type}, body=body)
            job_id = accepted.json()["job_id"]
            path = app.state.job_manager.repository.get(job_id).video_path
            for _ in range(150):
                if path.with_suffix(".pid").exists():
                    break
                await asyncio.sleep(.005)
            assert path.with_suffix(".pid").exists()
            pid = int(path.with_suffix(".pid").read_text())
            assert (await request(app, "GET", "/health")).status_code == 200
            assert (await wait_for_terminal(app, job_id)).json() == {"status": "failed"}
            await app.state.job_manager._queue.join()
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            assert not path.exists()
            async def detailed(scope, receive, send):
                scope["query_string"] = b"include_details=true"
                await app(scope, receive, send)
            payload = (await request(detailed, "GET", f"/ui-api/jobs/{job_id}", headers=headers)).json()
            assert payload["failure_reason"] == "timeout"
            assert payload["event_details"] == [] and "verdict" not in payload
    asyncio.run(scenario())
