"""Shared synthetic fixtures for Backend tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def synthetic_mov(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """Generate a tiny synthetic MOV without real people or banking data."""

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg is required for the synthetic MOV integration test")

    output: Path = tmp_path_factory.mktemp("synthetic-video") / "sample.mov"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:r=2",
            "-t",
            "1",
            "-an",
            "-c:v",
            "mjpeg",
            "-f",
            "mov",
            "-y",
            str(output),
        ],
        check=True,
        timeout=15,
    )
    return output.read_bytes()
