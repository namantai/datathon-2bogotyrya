"""Opt-in loopback HTTP benchmark: python -m tests.benchmark.

Only generated 10/60/145-second MOVs. No real videos, model, public listener,
new runtime dependencies or retained media. Prints aggregate timing/size only.
"""

from __future__ import annotations

import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request

from fortebank_api.analyzers import AnalysisResult, FakeAnalyzer
from fortebank_api.config import Settings
from fortebank_api.main import create_app
from fortebank_api.video_validation import FFprobeVideoValidator
from tests.support import multipart_file
from tests.test_api import synthetic_event


def create_benchmark_app():
    if os.environ.get("FORTE_BENCHMARK") != "synthetic-only":
        raise RuntimeError("Synthetic local benchmark fixture only")
    root = Path(os.environ["FORTE_BENCH_ROOT"])
    duration = float(os.environ["FORTE_BENCH_DURATION"])

    class MeasuredValidator(FFprobeVideoValidator):
        async def validate(self, path: Path) -> None:
            started = time.perf_counter()
            await super().validate(path)
            # Test-only, private temporary metrics; no media, paths or secrets.
            (root / "validation.json").write_text(json.dumps(time.perf_counter() - started))

    events = tuple(
        replace(synthetic_event(t, index=i), evidence_timestamps_sec=(t - .1, t, t + .1))
        for i, t in enumerate((duration * .25, duration * .75))
    )
    return create_app(
        Settings(_env_file=None, environment="test", name="Synthetic benchmark",
                 analyzer_mode="fake", storage_dir=root / "uploads", database_path=root / "jobs.sqlite3"),
        analyzer=FakeAnalyzer(AnalysisResult(events)),  # No artificial delay, no ML.
        video_validator=MeasuredValidator("ffprobe", 10),
    )


def run() -> None:
    backend = Path(__file__).resolve().parents[1]
    # OS-assigned loopback port; no interference with running user servers.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="forteguard-benchmark-") as temporary:
        root = Path(temporary)
        for duration in (10, 60, 145):
            sample_root = root / str(duration)
            sample_root.mkdir()
            video = sample_root / "synthetic.mov"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                "-t", str(duration), "-an", "-c:v", "mpeg4", "-q:v", "5", "-threads", "1", str(video),
            ], check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            body, media_type = multipart_file(video.read_bytes())
            key = secrets.token_urlsafe(32)
            # Isolate from local .env/APP_* settings, including UI secrets.
            env = {k: v for k, v in os.environ.items() if not k.startswith("APP_")}
            env.update(FORTE_BENCHMARK="synthetic-only", FORTE_BENCH_ROOT=str(sample_root),
                       FORTE_BENCH_DURATION=str(duration), APP_API_KEY=key,
                       PYTHONPATH=str(backend / "src"))
            def call(path: str, data: bytes | None = None):
                req = urllib.request.Request(origin + path, data=data,
                    headers={"X-API-Key": key, "Content-Type": media_type})
                with urllib.request.urlopen(req, timeout=30) as response:
                    return json.load(response)

            with (sample_root / "server.log").open("wb") as log:
                process = subprocess.Popen([
                    sys.executable, "-m", "uvicorn", "tests.benchmark:create_benchmark_app", "--factory",
                    "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
                    "--no-access-log", "--no-proxy-headers",
                ], cwd=backend, env=env, stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 15
                    while True:
                        assert process.poll() is None, "Benchmark server exited"
                        try:
                            assert call("/health")["service"] == "Synthetic benchmark"
                            break
                        except OSError:
                            assert time.monotonic() < deadline, "Server startup timeout"
                            time.sleep(.05)
                    samples = []
                    for _ in range(3):
                        started = time.perf_counter()
                        job_id = call("/api/v1/analyze", body)["job_id"]
                        post_sec = time.perf_counter() - started
                        deadline = time.monotonic() + 60
                        while True:
                            result = call(f"/api/v1/jobs/{job_id}")
                            if result["status"] != "processing":
                                break
                            assert time.monotonic() < deadline, "Benchmark job timeout"
                            time.sleep(.02)
                        end_to_end = time.perf_counter() - started
                        assert result["status"] == "done" and result["verdict"] == "violation"
                        assert len(result["violations"]) == 2
                        for event in result["violations"]:
                            assert len(event["evidence_frames"]) == 3
                            for frame in event["evidence_frames"]:
                                jpeg = base64.b64decode(frame["image_base64"], validate=True)
                                assert jpeg[:2] == b"\xff\xd8" and jpeg[-2:] == b"\xff\xd9"
                        with sqlite3.connect(sample_root / "jobs.sqlite3") as db:
                            db.row_factory = sqlite3.Row
                            metrics = dict(db.execute(
                                "SELECT upload_duration_sec, queue_duration_sec, analyzer_duration_sec, "
                                "evidence_duration_sec, total_duration_sec, length(result_json) AS json_bytes "
                                "FROM jobs WHERE job_id=?", (job_id,)).fetchone())
                            db_bytes = db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
                        metrics.update(http_post_sec=post_sec, http_done_sec=end_to_end,
                                       ffprobe_sec=json.loads((sample_root / "validation.json").read_text()))
                        samples.append(metrics)
                        assert not list((sample_root / "uploads").glob("*.mov"))
                    row = {"duration_sec": duration, "video_bytes": video.stat().st_size, "runs": len(samples),
                           "median": {k: round(statistics.median(s[k] for s in samples), 6) for k in samples[0]},
                           "max_http_done_sec": round(max(s["http_done_sec"] for s in samples), 6),
                           "sqlite_bytes_after_3_results": db_bytes}
                    print(json.dumps(row), flush=True)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
    print("PASS: 9 real HTTP jury cycles; two synthetic events / six original JPEGs per job. No ML/SLA claim.")


if __name__ == "__main__":
    run()
