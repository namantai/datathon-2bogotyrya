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

UI_CODE = "synthetic-ui-only-access-code-32chars"
ORIGIN = "http://localhost:5173"


async def login(app, *, code=UI_CODE, headers=None):
    return await request(app, "POST", "/ui-api/session", body=json.dumps({"access_code": code}).encode(),
                         headers={"Origin": ORIGIN, "Content-Type": "application/json", **(headers or {})})


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
    return make_settings(tmp_path, ui_access_code=UI_CODE, **kwargs)


def test_ui_login_security_and_rotation(tmp_path):
    async def scenario():
        app = create_app(settings(tmp_path))
        async with app.router.lifespan_context(app):
            assert (await request(app, "GET", "/ui-api/session")).status_code == 401
            wrong = await login(app, code="wrong")
            assert wrong.status_code == 401
            assert b"wrong" not in wrong.content
            response = await login(app)
            assert response.status_code == 200
            assert "HttpOnly" in response.headers["set-cookie"]
            assert "SameSite=strict" in response.headers["set-cookie"]
            assert "Path=/ui-api" in response.headers["set-cookie"]
            assert response.headers["cache-control"] == "no-store"
            assert UI_CODE not in response.content.decode() and API_KEY not in response.content.decode()
            headers = session_headers(response)
            assert (await request(app, "GET", "/ui-api/session", headers=headers)).status_code == 200
            renewed = await login(app, headers=headers)
            assert renewed.headers["set-cookie"] != response.headers["set-cookie"]
            assert (await request(app, "GET", "/ui-api/session", headers=headers)).status_code == 401
            new_headers = session_headers(renewed)
            assert (await request(app, "DELETE", "/ui-api/session", headers=new_headers)).status_code == 204
            assert (await request(app, "GET", "/ui-api/session", headers=new_headers)).status_code == 401
    asyncio.run(scenario())


def test_expiry_and_forged_cookie(tmp_path):
    async def scenario():
        app = create_app(settings(tmp_path))
        headers = session_headers(await login(app))
        next(iter(app.state.ui_security.sessions.values())).expires = time.monotonic() - 1
        assert (await request(app, "GET", "/ui-api/session", headers=headers)).status_code == 401
        assert (await request(app, "GET", "/ui-api/session", headers={"Cookie": "forteguard_session=forged"})).status_code == 401
    asyncio.run(scenario())


@pytest.mark.parametrize("override", [{"Origin": "https://evil.test"}, {"Origin": "null"},
                                      {"Origin": ""}, {"X-CSRF-Token": "wrong"},
                                      {"X-CSRF-Token": ""}, {"Sec-Fetch-Site": "cross-site"}])
def test_csrf_rejected_before_file_processing(tmp_path, override):
    async def scenario():
        app = create_app(settings(tmp_path))
        headers = {**session_headers(await login(app)), **override}
        response = await upload(app, headers, b"not-read")
        assert response.status_code == 403
        assert not (tmp_path / "storage").exists()
    asyncio.run(scenario())


def test_login_origin_rate_and_body_limits(tmp_path):
    async def scenario():
        app = create_app(settings(tmp_path))
        assert (await login(app, headers={"Origin": "https://evil.test"})).status_code == 403
        assert (await login(app, code="x" * 5000)).status_code == 413
        for _ in range(9):
            assert (await login(app, code="wrong")).status_code == 401
        assert (await login(app)).status_code == 429
        assert not app.state.ui_security.sessions
    asyncio.run(scenario())


@pytest.mark.parametrize("count", [0, 2])
def test_bridge_real_mov_worker_evidence_and_ownership(tmp_path, timed_color_mov, count):
    async def scenario():
        events = tuple(synthetic_event(t, index=i, evidence_timestamps_sec=(t, t + .1))
                       for i, t in enumerate((.2, 2.2)[:count]))
        app = create_app(settings(tmp_path, analysis_timeout_sec=10),
                         analyzer=FakeAnalyzer(AnalysisResult(events), delay_sec=.05))
        async with app.router.lifespan_context(app):
            headers = session_headers(await login(app))
            foreign = session_headers(await login(app))
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
            headers = session_headers(await login(app))
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
            headers = session_headers(await login(app))
            response = await upload(app, headers, content, filename=filename, field_name=field)
            assert response.status_code == status
            assert not list((tmp_path / "storage").glob("*.mov"))
            assert app.state.ui_security.active_uploads == 0
    asyncio.run(scenario())


def test_bridge_busy_job_and_global_upload_limit(tmp_path, synthetic_mov):
    async def scenario():
        app = create_app(settings(tmp_path), analyzer=FakeAnalyzer(delay_sec=.2))
        async with app.router.lifespan_context(app):
            headers = session_headers(await login(app))
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
            response = await login(app, headers={"Origin": "https://ui.example.test"})
            assert "Secure" in response.headers["set-cookie"]
            headers = {**session_headers(response), "Origin": "https://ui.example.test"}
            accepted = await upload(app, headers, synthetic_mov)
            assert (await terminal(app, headers, accepted.json()["job_id"])).json() == {"status": "failed"}
    asyncio.run(scenario())


@pytest.mark.parametrize("overrides", [{"ui_access_code": API_KEY}, {"ui_access_code": "short"},
    {"ui_allowed_origins": ["*"]}, {"ui_allowed_origins": ["https://host/path"]},
    {"environment": "production", "analyzer_mode": "disabled"}])
def test_invalid_ui_config_rejected(tmp_path, overrides):
    with pytest.raises(ValueError) as error:
        make_settings(tmp_path, **{"ui_access_code": UI_CODE, **overrides})
    assert API_KEY not in str(error.value)
    assert UI_CODE not in str(error.value)


def test_bridge_disabled_without_code_and_openapi_unchanged(tmp_path):
    app = create_app(make_settings(tmp_path))
    assert set(app.openapi()["paths"]) == {"/health", "/api/v1/analyze", "/api/v1/jobs/{job_id}"}
    assert asyncio.run(request(app, "GET", "/ui-api/session")).status_code == 503


def test_streamed_body_limit_cannot_be_bypassed_by_content_length(tmp_path):
    async def scenario():
        app = create_app(settings(tmp_path, max_upload_bytes=100))
        async with app.router.lifespan_context(app):
            headers = session_headers(await login(app))
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
        headers = session_headers(await login(app))
        assert (await request(app, "POST", "/ui-api/analyze", headers=headers)).status_code == 422
        assert (await request(app, "GET", "/ui-api/jobs/missing", headers=headers)).status_code == 404
        assert (await request(app, "DELETE", "/ui-api/session", headers={**headers, "X-CSRF-Token": ""})).status_code == 403
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
            response = await upload(app, session_headers(await login(app)), b"synthetic")
            assert response.status_code == 500
            assert response.json() == {"detail": "UI request failed"}
            assert not list((tmp_path / "storage").glob("*.mov"))
            assert app.state.ui_security.active_uploads == 0
    asyncio.run(scenario())
