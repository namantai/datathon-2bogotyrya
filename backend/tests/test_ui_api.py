"""UI bridge security and shared worker integration, exclusively synthetic media."""

import asyncio
import base64
import json
import time
from pathlib import Path

import pytest

from fortebank_api.analyzers import AnalysisResult, FakeAnalyzer
from fortebank_api.main import create_app
from tests.support import multipart_file, request
from tests.test_api import API_KEY, make_settings, synthetic_event
from tests.test_evidence import decoded_center_rgb

ORIGIN = "http://localhost:5173"


async def bootstrap(app, *, body=b"", headers=None):
    return await request(app, "POST", "/ui-api/session", body=body,
                         headers={"Origin": ORIGIN, **(headers or {})})


def session_headers(response):
    return {"Cookie": response.headers["set-cookie"].split(";")[0], "Origin": ORIGIN,
            "X-CSRF-Token": response.json()["csrf_token"]}


async def upload(app, headers, content, **kwargs):
    body, content_type = multipart_file(content, **kwargs)
    return await request(app, "POST", "/ui-api/analyze", body=body,
                         headers={**headers, "Content-Type": content_type})


async def terminal(app, headers, job_id):
    for _ in range(500):
        result = await request(app, "GET", f"/ui-api/jobs/{job_id}", headers=headers)
        if result.json().get("status") != "processing":
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("UI job did not finish")


def settings(tmp_path, **kwargs):
    return make_settings(tmp_path, **kwargs)


def test_anonymous_bootstrap_reuses_session_without_rotating_or_extending(tmp_path):
    async def scenario():
        app = create_app(settings(tmp_path))
        response = await bootstrap(app)
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert "Path=/ui-api" in response.headers["set-cookie"]
        assert response.headers["cache-control"] == "no-store"
        assert API_KEY not in response.content.decode()
        headers = session_headers(response)
        session = next(iter(app.state.ui_security.sessions.values()))
        expires = session.expires
        session.jobs.add("synthetic-owned-job")
        renewed = await bootstrap(app, headers=headers)
        assert renewed.json() == response.json()
        assert "set-cookie" not in renewed.headers
        assert len(app.state.ui_security.sessions) == 1
        assert session.expires == expires and "synthetic-owned-job" in session.jobs
        for method in ("GET", "DELETE"):
            assert (await request(app, method, "/ui-api/session", headers=headers)).status_code == 405
        old_login = await bootstrap(app, body=json.dumps({"access_code": "obsolete-synthetic"}).encode())
        assert old_login.status_code == 400
    asyncio.run(scenario())


def test_expiry_and_forged_cookie_cannot_recover_visitor_jobs(tmp_path):
    async def scenario():
        app = create_app(settings(tmp_path))
        headers = session_headers(await bootstrap(app))
        old = next(iter(app.state.ui_security.sessions.values()))
        old.jobs.add("old-job")
        old.expires = time.monotonic() - 1
        assert (await request(app, "GET", "/ui-api/jobs/old-job", headers=headers)).status_code == 401
        forged = {"Cookie": "forteguard_session=forged"}
        assert (await request(app, "GET", "/ui-api/jobs/old-job", headers=forged)).status_code == 401
        renewed = await bootstrap(app, headers=headers)
        assert renewed.status_code == 200
        assert session_headers(renewed)["Cookie"] != headers["Cookie"]
        assert (await request(app, "GET", "/ui-api/jobs/old-job", headers=session_headers(renewed))).status_code == 404
        assert next(iter(app.state.ui_security.sessions.values())).jobs == set()
    asyncio.run(scenario())


@pytest.mark.parametrize("override", [{"Origin": "https://evil.test"}, {"Origin": "null"},
                                      {"Origin": ""}, {"X-CSRF-Token": "wrong"},
                                      {"X-CSRF-Token": ""}, {"Sec-Fetch-Site": "cross-site"}])
def test_csrf_rejected_before_file_processing(tmp_path, override):
    async def scenario():
        app = create_app(settings(tmp_path))
        headers = {**session_headers(await bootstrap(app)), **override}
        response = await upload(app, headers, b"not-read")
        assert response.status_code == 403
        assert not (tmp_path / "storage").exists()
    asyncio.run(scenario())


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.test"}, {"Origin": "null"},
                                     {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}])
def test_bootstrap_rejects_cross_site_or_missing_origin_before_body(tmp_path, headers):
    async def scenario():
        app = create_app(settings(tmp_path))
        reads = 0
        async def observed(scope, receive, send):
            async def counted():
                nonlocal reads
                reads += 1
                return await receive()
            await app(scope, counted, send)
        assert (await request(observed, "POST", "/ui-api/session", body=b"unread", headers=headers)).status_code == 403
        assert reads == 0 and not app.state.ui_security.sessions
    asyncio.run(scenario())


def test_bootstrap_rate_body_and_session_capacity_limits(tmp_path):
    async def scenario():
        app = create_app(settings(tmp_path))
        assert (await bootstrap(app, body=b"x" * 5000)).status_code == 413
        for _ in range(9):
            assert (await bootstrap(app)).status_code == 200
        assert (await bootstrap(app)).status_code == 429
        assert len(app.state.ui_security.sessions) == 9
        security = app.state.ui_security
        security.bootstraps.clear()
        session = next(iter(security.sessions.values()))
        security.sessions = {str(i): session for i in range(64)}
        assert (await bootstrap(app)).status_code == 429
        assert len(security.sessions) == 64
    asyncio.run(scenario())


@pytest.mark.parametrize("count", [0, 1, 2])
def test_bridge_real_mov_worker_evidence_and_ownership(tmp_path, timed_color_mov, count):
    async def scenario():
        events = tuple(synthetic_event(t, index=i, evidence_timestamps_sec=(t, t + .1))
                       for i, t in enumerate((.2, 2.2)[:count]))
        app = create_app(settings(tmp_path, analysis_timeout_sec=10),
                         analyzer=FakeAnalyzer(AnalysisResult(events), delay_sec=.05))
        async with app.router.lifespan_context(app):
            headers = session_headers(await bootstrap(app))
            foreign = session_headers(await bootstrap(app))
            for _ in range(2):
                accepted = await upload(app, headers, timed_color_mov.read_bytes())
                assert accepted.status_code == 202
                job_id = accepted.json()["job_id"]
                path = f"/ui-api/jobs/{job_id}"
                assert (await request(app, "GET", path, headers=headers)).json() == {"status": "processing"}
                assert (await request(app, "GET", path, headers=foreign)).status_code == 404
                result = (await terminal(app, headers, job_id)).json()
                assert result["verdict"] == ("violation" if count else "no_violation")
                assert len(result["violations"]) == count
                for i, event in enumerate(result["violations"]):
                    assert len(event["evidence_frames"]) == 2
                    for frame in event["evidence_frames"]:
                        assert base64.b64decode(frame["image_base64"], validate=True).startswith(b"\xff\xd8")
                        rgb = decoded_center_rgb(frame["image_base64"])
                        assert rgb[0 if i == 0 else 2] > 200
                record = app.state.job_manager.repository.get(job_id)
                assert record.state == "done" and not record.video_path.exists()
                assert (await request(app, "GET", f"/api/v1/jobs/{job_id}", headers=headers)).status_code == 401
                assert (await request(app, "GET", f"/api/v1/jobs/{job_id}", headers={"X-API-Key": API_KEY})).json() == result
            assert not list((tmp_path / "storage").glob("*.mov"))
    asyncio.run(scenario())


def test_bridge_failed_and_no_internal_error_leak(tmp_path, synthetic_mov):
    async def scenario():
        app = create_app(settings(tmp_path), analyzer=FakeAnalyzer(error=RuntimeError("private-internal-path-and-secret")))
        async with app.router.lifespan_context(app):
            headers = session_headers(await bootstrap(app))
            accepted = await upload(app, headers, synthetic_mov)
            response = await terminal(app, headers, accepted.json()["job_id"])
            assert response.json() == {"status": "failed"}
    asyncio.run(scenario())


@pytest.mark.parametrize("content,filename,field,status", [(b"bad", "x.mp4", "file", 415),
    (b"bad", "x.mov", "file", 400), (b"", "x.mov", "file", 400),
    (b"bad", "x.mov", "video", 422), (b"x" * 101, "x.mov", "file", 413)])
def test_bridge_upload_validation(tmp_path, content, filename, field, status):
    async def scenario():
        app = create_app(settings(tmp_path, max_upload_bytes=100))
        async with app.router.lifespan_context(app):
            headers = session_headers(await bootstrap(app))
            response = await upload(app, headers, content, filename=filename, field_name=field)
            assert response.status_code == status
            assert not list((tmp_path / "storage").glob("*.mov"))
            assert app.state.ui_security.active_uploads == 0
    asyncio.run(scenario())


def test_bridge_busy_job_and_global_upload_limit(tmp_path, synthetic_mov):
    async def scenario():
        app = create_app(settings(tmp_path), analyzer=FakeAnalyzer(delay_sec=.2))
        async with app.router.lifespan_context(app):
            headers = session_headers(await bootstrap(app))
            accepted = await upload(app, headers, synthetic_mov)
            assert (await upload(app, headers, synthetic_mov)).status_code == 429
            await terminal(app, headers, accepted.json()["job_id"])
            app.state.ui_security.uploads.extend([time.monotonic()] * 6)
            assert (await upload(app, headers, synthetic_mov)).status_code == 429
    asyncio.run(scenario())


def test_production_secure_cookie_disabled_analyzer_and_fake_rejected(tmp_path, synthetic_mov):
    production = settings(tmp_path, environment="production", analyzer_mode="disabled",
                          ui_allowed_origins=["https://ui.example.test"])
    with pytest.raises(ValueError, match="FakeAnalyzer"):
        create_app(production, analyzer=FakeAnalyzer())
    async def scenario():
        app = create_app(production)
        async with app.router.lifespan_context(app):
            response = await bootstrap(app, headers={"Origin": "https://ui.example.test"})
            assert "Secure" in response.headers["set-cookie"]
            headers = {**session_headers(response), "Origin": "https://ui.example.test"}
            accepted = await upload(app, headers, synthetic_mov)
            assert (await terminal(app, headers, accepted.json()["job_id"])).json() == {"status": "failed"}
    asyncio.run(scenario())


@pytest.mark.parametrize("overrides", [
    {"ui_allowed_origins": []}, {"ui_allowed_origins": ["*"]},
    {"ui_allowed_origins": ["https://host/path"]}, {"ui_session_ttl_sec": 0},
    {"environment": "production", "analyzer_mode": "disabled"}])
def test_invalid_ui_config_rejected(tmp_path, overrides):
    with pytest.raises(ValueError) as error:
        make_settings(tmp_path, **overrides)
    assert API_KEY not in str(error.value)


def test_bridge_needs_no_code_and_openapi_unchanged(tmp_path):
    app = create_app(make_settings(tmp_path))
    assert set(app.openapi()["paths"]) == {"/health", "/api/v1/analyze", "/api/v1/jobs/{job_id}"}
    assert asyncio.run(bootstrap(app)).status_code == 200


def test_streamed_body_limit_cannot_be_bypassed_by_content_length(tmp_path):
    async def scenario():
        app = create_app(settings(tmp_path, max_upload_bytes=100))
        async with app.router.lifespan_context(app):
            headers = session_headers(await bootstrap(app))
            response = await upload(app, {**headers, "Content-Length": "0"}, b"x" * 70000)
            assert response.status_code == 413
            assert app.state.ui_security.active_uploads == 0
            assert not list((tmp_path / "storage").glob("*.mov"))
            response = await request(app, "POST", "/ui-api/session", body=b"x" * 5000,
                headers={"Content-Type": "application/json", "Content-Length": "0", "Origin": ORIGIN})
            assert response.status_code == 413
    asyncio.run(scenario())


def test_upload_missing_file_and_unauthenticated_requests(tmp_path):
    async def scenario():
        app = create_app(settings(tmp_path))
        assert (await upload(app, {"Origin": ORIGIN}, b"unread")).status_code == 401
        headers = session_headers(await bootstrap(app))
        assert (await request(app, "POST", "/ui-api/analyze", headers=headers)).status_code == 422
        assert (await request(app, "GET", "/ui-api/jobs/missing", headers=headers)).status_code == 404
        assert (await request(app, "DELETE", "/ui-api/session", headers=headers)).status_code == 405
        body, media = multipart_file(b"unread")
        assert (await request(app, "POST", "/api/v1/analyze", body=body,
            headers={**headers, "Content-Type": media})).status_code == 401
    asyncio.run(scenario())


def test_unexpected_bridge_error_is_safe_and_uploaded_file_is_removed(tmp_path):
    class BrokenValidator:
        async def validate(self, path):
            raise RuntimeError("PRIVATE_DATABASE_PATH_AND_SECRET")
    async def scenario():
        app = create_app(settings(tmp_path), video_validator=BrokenValidator())
        async with app.router.lifespan_context(app):
            response = await upload(app, session_headers(await bootstrap(app)), b"synthetic")
            assert response.status_code == 500
            assert response.json() == {"detail": "UI request failed"}
            assert not list((tmp_path / "storage").glob("*.mov"))
            assert app.state.ui_security.active_uploads == 0
    asyncio.run(scenario())
