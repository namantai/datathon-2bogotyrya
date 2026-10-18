"""Opt-in LOCAL Docker HTTP smoke. Synthetic data only; never prints the API key.

Run from a trusted checkout: sudo python3 scripts/docker_smoke.py [--restart].
Requires the already running compose.local-fake.yaml stand. --restart interrupts
its worker and invalidates its UI sessions; do not run against real workloads.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
from pathlib import Path
import secrets
import subprocess
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://localhost:8080"
COMPOSE = ["docker", "compose", "-f", "compose.yaml", "-f", "compose.local-fake.yaml"]


def docker(*args: str) -> bytes:
    result = subprocess.run([*COMPOSE, *args], cwd=ROOT, capture_output=True, timeout=90)
    if result.returncode:
        # Do not echo arbitrary container output or runtime environment/secrets.
        raise RuntimeError("Docker command failed; check daemon access and service health")
    return result.stdout


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def visitor():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )


def call(client, path: str, *, method="GET", data=None, headers=None):
    request = urllib.request.Request(ORIGIN + path, data=data, method=method, headers=headers or {})
    try:
        response = client.open(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.read(), response.headers


def poll(client, path, headers=None):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status, body, _ = call(client, path, headers=headers)
        require(status == 200, "Polling failed")
        result = json.loads(body)
        if result["status"] != "processing":
            require(result["status"] == "done" and result["verdict"] == "no_violation"
                    and result["violations"] == [], "Unexpected local FakeAnalyzer result")
            return result
        time.sleep(.1)
    raise RuntimeError("Synthetic job did not finish in 30 seconds")


def run(restart: bool) -> None:
    settings = json.loads(docker("exec", "-T", "backend", "python", "-c",
        "import json,os; print(json.dumps({k:os.environ.get(k) for k in "
        "('APP_ENVIRONMENT','APP_ANALYZER_MODE','APP_API_KEY')}))"))
    require(settings["APP_ENVIRONMENT"] == "development" and settings["APP_ANALYZER_MODE"] == "fake",
            "This smoke test requires the explicit LOCAL fake overlay")
    guard = docker("exec", "-T", "backend", "python", "-c",
        "from fortebank_api.config import Settings\n"
        "try:\n"
        " Settings(_env_file=None, environment='production', analyzer_mode='fake', debug=False, "
        "api_key='synthetic-production-guard-only', ui_allowed_origins=['https://example.invalid'])\n"
        "except ValueError as e:\n"
        " assert 'FakeAnalyzer cannot run in production' in str(e)\n"
        " print('production-fake-rejected')\n"
        "else:\n"
        " raise SystemExit(1)\n")
    require(guard.strip() == b"production-fake-rejected", "Production FakeAnalyzer guard failed")
    key = settings["APP_API_KEY"]
    require(bool(key) and len(key) >= 16, "Configure a valid jury API key first")
    jury_headers = {"X-API-Key": key}
    owner, other = visitor(), visitor()
    for path in ("/", "/dashboard", "/health"):
        require(call(owner, path)[0] == 200, "Site, SPA or health failed")
    require(call(owner, "/ui-api/session")[0] == 405, "Obsolete session GET should not be enabled")
    require(call(owner, "/ui-api/session", method="POST", data=b"")[0] == 403, "Origin check failed")
    status, session, headers = call(owner, "/ui-api/session", method="POST", data=b"", headers={"Origin": ORIGIN})
    require(status == 200 and "HttpOnly" in headers.get("Set-Cookie", "")
            and "SameSite=strict" in headers.get("Set-Cookie", ""), "Anonymous cookie is not protected")
    require(key.encode() not in session, "API key leaked into UI response")
    csrf = json.loads(session)["csrf_token"]
    status, reused, _ = call(owner, "/ui-api/session", method="POST", data=b"", headers={"Origin": ORIGIN})
    require(status == 200 and json.loads(reused)["csrf_token"] == csrf, "Existing session was not reused")
    require(call(other, "/ui-api/session", method="POST", data=b"", headers={"Origin": ORIGIN})[0] == 200,
            "Second visitor bootstrap failed")
    # Generate a seekable MOV in a private container temporary directory; no real data.
    video = docker("exec", "-T", "backend", "python", "-c",
        "import pathlib,subprocess,sys,tempfile\n"
        "with tempfile.TemporaryDirectory(prefix='synthetic-smoke-') as d:\n"
        " p=pathlib.Path(d)/'synthetic.mov'\n"
        " subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=64x64:rate=10',"
        "'-t','2','-an','-c:v','mjpeg',str(p)],check=True,timeout=15)\n"
        " sys.stdout.buffer.write(p.read_bytes())\n")
    boundary = "smoke-" + secrets.token_hex(16)
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="synthetic.mov"\r\n'
            'Content-Type: video/quicktime\r\n\r\n').encode() + video + f'\r\n--{boundary}--\r\n'.encode()
    media = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    upload = {**media, "Origin": ORIGIN, "X-CSRF-Token": csrf}
    for auth in ({}, {"X-API-Key": "intentionally-invalid"}):
        require(call(owner, "/api/v1/analyze", method="POST", data=body, headers={**media, **auth})[0] == 401,
                "Jury upload did not enforce API key")
    require(call(owner, "/ui-api/analyze", method="POST", data=body,
                 headers={**upload, "X-CSRF-Token": "wrong"})[0] == 403, "CSRF check failed")
    status, accepted, _ = call(owner, "/ui-api/analyze", method="POST", data=body, headers=upload)
    require(status == 202, "UI upload failed")
    ui_job = json.loads(accepted)["job_id"]
    ui_result = poll(owner, f"/ui-api/jobs/{ui_job}")
    require(call(other, f"/ui-api/jobs/{ui_job}")[0] == 404, "Visitors are not isolated")
    for auth in ({}, {"X-API-Key": "intentionally-invalid"}):
        require(call(owner, f"/api/v1/jobs/{ui_job}", headers=auth)[0] == 401,
                "Jury result did not enforce API key")
    require(poll(owner, f"/api/v1/jobs/{ui_job}", jury_headers) == ui_result, "UI/jury results differ")
    status, accepted, _ = call(owner, "/api/v1/analyze", method="POST", data=body, headers={**media, **jury_headers})
    require(status == 202, "Jury upload with valid key failed")
    jury_job = json.loads(accepted)["job_id"]
    jury_result = poll(owner, f"/api/v1/jobs/{jury_job}", jury_headers)
    print("OK: production rejects FakeAnalyzer; NGINX/SPA/health, anonymous cookie/reuse,")
    print("    Origin/CSRF, visitor isolation, jury key,")
    print("    synthetic MOV → real Backend/worker → done (FakeAnalyzer only; NOT real AI).")
    if restart:
        docker("restart", "backend")
        deadline = time.monotonic() + 40
        while True:
            try:
                if call(owner, "/health")[0] == 200:
                    break
            except OSError:
                pass
            require(time.monotonic() < deadline, "Backend did not recover after restart")
            time.sleep(.5)
        require(poll(owner, f"/api/v1/jobs/{jury_job}", jury_headers) == jury_result,
                "SQLite result did not survive restart")
        require(call(owner, f"/ui-api/jobs/{ui_job}")[0] == 401, "Old in-memory session should expire on restart")
        require(call(owner, "/ui-api/session", method="POST", data=b"", headers={"Origin": ORIGIN})[0] == 200,
                "New anonymous session failed after restart")
        require(call(owner, f"/ui-api/jobs/{ui_job}")[0] == 404, "New session must not inherit old jobs")
        print("OK: SQLite survives backend restart; old UI session is invalidated safely.")
    print("HTTP smoke complete. Browser request-loop and image-content checks are separate.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart", action="store_true", help="also restart the local backend (interrupts its work)")
    try:
        run(parser.parse_args().restart)
    except RuntimeError as error:
        # Our checks contain static diagnostic text, never response payloads/keys.
        raise SystemExit(f"Smoke FAILED: {error}") from None
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        # Exceptions from third-party tools may contain secrets: print only type.
        raise SystemExit(f"Smoke FAILED ({type(error).__name__}); inspect configuration/service health privately.") from None
