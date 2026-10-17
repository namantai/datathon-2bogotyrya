"""Opt-in real HTTP smoke: python -m tests.http_e2e (only synthetic MOV).

Starts temporary single-process FastAPI and Vite, tests the real proxy, then
stops both and removes only its TemporaryDirectory. Not a browser E2E.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import ExitStack
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from fortebank_api.analyzers import AnalysisResult, FakeAnalyzer
from fortebank_api.config import Settings
from fortebank_api.main import create_app
from tests.support import multipart_file
from tests.test_api import synthetic_event


class HTTPTestAnalyzer(FakeAnalyzer):
    """Predefined empty, one/multiple-event and failure scenarios, test fixture only."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    async def analyze_video(self, local_video_path: Path) -> AnalysisResult:
        assert local_video_path.is_file()
        self.calls += 1
        await asyncio.sleep(.2)
        if self.calls == 1:
            return AnalysisResult()
        if self.calls == 2:
            return AnalysisResult((synthetic_event(.2),))
        if self.calls == 3:
            return AnalysisResult(tuple(synthetic_event(t, index=i) for i, t in enumerate((.2, 1.2))))
        raise RuntimeError("Synthetic private exception must not reach HTTP")


def create_test_app():
    if os.environ.get("FORTE_HTTP_TEST") != "synthetic-only":
        raise RuntimeError("This fixture is only for the opt-in local HTTP test")
    root = Path(os.environ["FORTE_HTTP_ROOT"])
    return create_app(Settings(_env_file=None, environment="test", debug=False,
                              name="ForteGuard synthetic HTTP test", analyzer_mode="fake",
                              storage_dir=root / "storage", database_path=root / "jobs.sqlite3"),
                      analyzer=HTTPTestAnalyzer())


def run():
    backend = Path(__file__).resolve().parents[1]
    frontend = backend.parent / "frontend"
    ui_port = int(os.environ.get("FORTE_HTTP_UI_PORT", "5173"))
    backend_port = int(os.environ.get("FORTE_HTTP_BACKEND_PORT", "8001"))
    backend_origin = f"http://127.0.0.1:{backend_port}"
    origin = f"http://localhost:{ui_port}"
    for host, port in (("127.0.0.1", backend_port), ("localhost", ui_port)):
        try:
            probe = socket.create_connection((host, port), timeout=.5)
        except ConnectionRefusedError:
            continue
        else:
            probe.close()
            raise RuntimeError(f"Port {port} is occupied; stop the local server before this test")
    processes: list[subprocess.Popen] = []
    with tempfile.TemporaryDirectory(prefix="forteguard-http-") as temporary, ExitStack() as resources:
        root = Path(temporary)
        code, jury_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        env = {**os.environ, "FORTE_HTTP_TEST": "synthetic-only", "FORTE_HTTP_ROOT": temporary,
               "APP_API_KEY": jury_key, "APP_UI_ACCESS_CODE": code,
               "APP_UI_ALLOWED_ORIGINS": json.dumps([origin]),
               "APP_UI_SESSION_TTL_SEC": "1800", "APP_MAX_UPLOAD_BYTES": "2097152",
               "PYTHONPATH": str(backend / "src"), "VITE_UI_MODE": "bridge",
               "UI_PROXY_TARGET": backend_origin}
        # Do not give server secrets to the Vite subprocess.
        vite_env = {k: v for k, v in env.items() if not k.startswith("APP_")}
        logs = [root / "backend.log", root / "vite.log"]
        outputs = [resources.enter_context(path.open("wb")) for path in logs]
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        def call(path, *, method="GET", data=None, headers=None, direct=False):
            url = (backend_origin if direct else origin) + path
            req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
            try:
                result = opener.open(req, timeout=10)
            except urllib.error.HTTPError as error:
                result = error
            with result:
                body = result.read()
                return result.status, body, result.headers
        try:
            processes.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "tests.http_e2e:create_test_app",
                "--factory", "--host", "127.0.0.1", "--port", str(backend_port), "--no-access-log", "--no-proxy-headers"],
                cwd=backend, env=env, stdout=outputs[0], stderr=subprocess.STDOUT))
            processes.append(subprocess.Popen([shutil.which("node") or "node", "node_modules/vite/bin/vite.js", "--port", str(ui_port)],
                cwd=frontend, env=vite_env, stdout=outputs[1], stderr=subprocess.STDOUT))
            deadline = time.monotonic() + 20
            while True:
                if any(p.poll() is not None for p in processes):
                    raise RuntimeError("An HTTP test server failed to start")
                try:
                    status, body, _ = call("/health", direct=True)
                    assert status == 200 and json.loads(body)["service"] == "ForteGuard synthetic HTTP test"
                    assert call("/")[0] == 200
                    break
                except (OSError, AssertionError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("HTTP test servers did not become ready") from None
                    time.sleep(.1)
            assert call("/ui-api/session")[0] == 401
            status, body, headers = call("/ui-api/session", method="POST",
                data=json.dumps({"access_code": code}).encode(),
                headers={"Origin": origin, "Content-Type": "application/json"})
            assert status == 200 and "HttpOnly" in headers["Set-Cookie"]
            assert code.encode() not in body and jury_key.encode() not in body
            csrf = json.loads(body)["csrf_token"]
            assert json.loads(body)["analyzer_mode"] == "fake"
            assert call("/ui-api/session")[0] == 200
            video = root / "synthetic.mov"
            subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-f", "lavfi",
                "-i", "testsrc2=size=64x64:rate=10", "-t", "2", "-an", "-c:v", "mjpeg", str(video)],
                check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            body, media_type = multipart_file(video.read_bytes())
            upload_headers = {"Origin": origin, "X-CSRF-Token": csrf, "Content-Type": media_type}
            assert call("/ui-api/analyze", method="POST", data=body,
                        headers={**upload_headers, "X-CSRF-Token": "wrong"})[0] == 403
            assert call("/api/v1/analyze", direct=True, method="POST", data=body,
                        headers={"Content-Type": media_type})[0] == 401
            for expected, event_count in (("no_violation", 0), ("violation", 1), ("violation", 2), ("failed", 0)):
                status, accepted, _ = call("/ui-api/analyze", method="POST", data=body, headers=upload_headers)
                assert status == 202
                job_id = json.loads(accepted)["job_id"]
                path = f"/ui-api/jobs/{job_id}"
                assert json.loads(call(path)[1]) == {"status": "processing"}
                deadline = time.monotonic() + 15
                while True:
                    status, payload, _ = call(path)
                    assert status == 200
                    result = json.loads(payload)
                    if result["status"] != "processing":
                        break
                    assert time.monotonic() < deadline, "HTTP job timeout"
                    time.sleep(.05)
                if expected == "failed":
                    assert result == {"status": "failed"}
                else:
                    assert result["status"] == "done" and result["verdict"] == expected
                    assert len(result["violations"]) == event_count
                    for event in result["violations"]:
                        for frame in event["evidence_frames"]:
                            jpeg = base64.b64decode(frame["image_base64"], validate=True)
                            assert jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")
                with sqlite3.connect(root / "jobs.sqlite3") as db:
                    assert db.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0] == result["status"]
                assert not list((root / "storage").glob("*.mov"))
            assert call("/ui-api/session", method="DELETE", headers={"Origin": origin, "X-CSRF-Token": csrf})[0] == 204
            assert call("/ui-api/session")[0] == 401
            print("PASS: real Vite proxy → FastAPI → SQLite/worker; login, CSRF, processing, no_violation, one/multiple events/JPEG, failed, repeat, logout. Synthetic data only; not a browser E2E.")
        except Exception:
            for log in logs:
                print(log.read_text().replace(code, "[redacted]").replace(jury_key, "[redacted]")[-2500:])
            raise
        finally:
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    run()
