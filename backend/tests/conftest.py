"""Shared synthetic fixtures for Backend tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _require_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg is required for synthetic video integration tests")
    return ffmpeg


@pytest.fixture(scope="session")
def synthetic_mov(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """Generate a tiny synthetic MOV without real people or banking data."""

    ffmpeg = _require_ffmpeg()

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


@pytest.fixture(scope="session")
def timed_color_mov(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create three one-second red/green/blue intervals at 10 FPS."""

    ffmpeg = _require_ffmpeg()
    output = tmp_path_factory.mktemp("timed-color-video") / "colors.mov"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x120:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=160x120:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x120:r=10:d=1",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map",
            "[v]",
            "-an",
            "-c:v",
            "mpeg4",
            "-q:v",
            "2",
            "-f",
            "mov",
            "-y",
            str(output),
        ],
        check=True,
        timeout=15,
    )
    return output


@pytest.fixture(scope="session")
def variable_rate_mov(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create a small MOV with deliberately non-uniform frame timestamps."""

    ffmpeg = _require_ffmpeg()
    output = tmp_path_factory.mktemp("vfr-video") / "variable.mov"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=96x64:r=10:d=2",
            "-vf",
            "setpts='if(lt(N,10),N/(10*TB),(1+(N-10)/(5*TB)))'",
            "-fps_mode",
            "vfr",
            "-an",
            "-c:v",
            "mpeg4",
            "-q:v",
            "2",
            "-f",
            "mov",
            "-y",
            str(output),
        ],
        check=True,
        timeout=15,
    )
    return output
