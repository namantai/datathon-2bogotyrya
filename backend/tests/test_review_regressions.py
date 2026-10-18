"""Independent review regressions; no real data or five-minute waits."""

import asyncio
import time

import pytest

from fortebank_api.jobs import JobRepository
from fortebank_api.main import create_app
from fortebank_api.schemas import DoneJobResponse
from tests.support import request
from tests.test_api import AcceptingVideoValidator, get_job, make_settings, post_file, wait_for_terminal


def test_serialization_cannot_publish_success_after_deadline(tmp_path, monkeypatch):
    clock = time.perf_counter
    offset = 0.0
    serialize = DoneJobResponse.model_dump_json

    def slow_serialization(self, *args, **kwargs):
        nonlocal offset
        payload = serialize(self, *args, **kwargs)
        offset += 301  # Advance an injected monotonic clock, not real time.
        return payload

    monkeypatch.setattr(time, "perf_counter", lambda: clock() + offset)
    monkeypatch.setattr(DoneJobResponse, "model_dump_json", slow_serialization)

    async def scenario():
        app = create_app(make_settings(tmp_path), video_validator=AcceptingVideoValidator())
        async with app.router.lifespan_context(app):
            job_id = (await post_file(app, b"synthetic")).json()["job_id"]
            assert (await wait_for_terminal(app, job_id)).json() == {"status": "failed"}
            assert (await get_job(app, job_id)).json() == {"status": "failed"}
            assert app.state.job_manager.repository.get(job_id).error_code == "analysis_timeout"
    asyncio.run(scenario())


def test_expired_upload_is_rejected_before_even_a_non_yielding_validator(tmp_path):
    async def scenario():
        app = create_app(make_settings(tmp_path, job_timeout_sec=.01),
                         video_validator=AcceptingVideoValidator())
        async def delayed(scope, receive, send):
            async def slow_receive():
                await asyncio.sleep(.03)
                return await receive()
            await app(scope, slow_receive, send)
        async with app.router.lifespan_context(app):
            assert (await post_file(delayed, b"synthetic")).status_code == 408
            assert not list((tmp_path / "storage").glob("*.mov"))
    asyncio.run(scenario())


def test_swagger_and_openapi_keep_jury_contract_and_hide_ui(tmp_path):
    async def scenario():
        app = create_app(make_settings(tmp_path))
        schema = (await request(app, "GET", "/openapi.json")).json()
        assert set(schema["paths"]) == {"/health", "/api/v1/analyze", "/api/v1/jobs/{job_id}"}
        for path, method in (("/api/v1/analyze", "post"), ("/api/v1/jobs/{job_id}", "get")):
            assert any(p["name"] == "X-API-Key" and p["in"] == "header"
                       for p in schema["paths"][path][method]["parameters"])
        assert set(schema["components"]["schemas"]["DoneJobResponse"]["properties"]) == {
            "job_id", "status", "verdict", "violations"}
        assert (await request(app, "GET", "/docs")).status_code == 200
        assert (await request(app, "GET", "/redoc")).status_code == 200
        assert (await request(app, "GET", "/api/v1/jobs/missing")).status_code == 401
    asyncio.run(scenario())


def test_late_sqlite_write_is_rolled_back_before_success_is_visible(tmp_path, monkeypatch):
    repository = JobRepository(tmp_path / "synthetic.sqlite3")
    repository.initialize()
    repository.create(job_id="synthetic", video_path=tmp_path / "unused.mov",
                      created_at=0, queued_at=0, upload_duration_sec=0)
    repository.mark_running("synthetic", 0)
    clock = iter((0, 0, 301))  # after serialization, before write, after write
    monkeypatch.setattr(time, "perf_counter", lambda: next(clock))
    with pytest.raises(TimeoutError):
        repository.mark_done("synthetic", DoneJobResponse(job_id="synthetic", status="done",
            verdict="no_violation", violations=[]), completed_at=0,
            analyzer_duration_sec=0, evidence_duration_sec=0, deadline_perf=300)
    record = repository.get("synthetic")
    assert record.state == "running" and record.result_json is None
