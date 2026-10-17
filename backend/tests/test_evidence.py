"""Real-frame extraction tests using only synthetic MOV videos."""

from __future__ import annotations

import asyncio
import base64
import math
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from fortebank_api.evidence import EvidenceExtractionError, VideoEvidenceExtractor


def make_extractor(**overrides: object) -> VideoEvidenceExtractor:
    values: dict[str, object] = {
        "ffmpeg_path": shutil.which("ffmpeg") or "ffmpeg",
        "ffprobe_path": shutil.which("ffprobe") or "ffprobe",
        "timeout_sec": 5.0,
        "max_width": 128,
        "max_height": 96,
        "max_jpeg_bytes": 512 * 1024,
        "jpeg_quality": 3,
        "endpoint_scan_sec": 1.0,
    }
    values.update(overrides)
    return VideoEvidenceExtractor(**values)  # type: ignore[arg-type]


def decoded_center_rgb(image_base64: str) -> tuple[int, int, int]:
    """Decode a JPEG with FFmpeg and return its scaled one-pixel RGB value."""

    result = subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-v",
            "error",
            "-f",
            "image2pipe",
            "-c:v",
            "mjpeg",
            "-i",
            "pipe:0",
            "-vf",
            "scale=1:1",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        input=base64.b64decode(image_base64, validate=True),
        capture_output=True,
        check=True,
        timeout=5,
    )
    assert len(result.stdout) == 3
    return tuple(result.stdout)  # type: ignore[return-value]


def test_extracts_frames_from_correct_intervals_and_reports_actual_pts(
    timed_color_mov: Path,
) -> None:
    started = time.perf_counter()
    frames = asyncio.run(make_extractor().extract(timed_color_mov, (0.0, 1.2, 3.0)))
    elapsed = time.perf_counter() - started

    red, green, blue = (decoded_center_rgb(frame.image_base64) for frame in frames)
    assert red[0] > red[1] + 80 and red[0] > red[2] + 80
    assert green[1] > green[0] + 30 and green[1] > green[2] + 30
    assert blue[2] > blue[0] + 80 and blue[2] > blue[1] + 80
    assert frames[0].timestamp_sec == pytest.approx(0.0, abs=0.001)
    assert 1.2 <= frames[1].timestamp_sec <= 1.3
    assert frames[2].timestamp_sec == pytest.approx(2.9, abs=0.001)
    assert elapsed < 5.0


def test_multiple_timestamps_have_bounded_measured_error(timed_color_mov: Path) -> None:
    requested = (0.05, 0.55, 2.05)
    frames = asyncio.run(make_extractor().extract(timed_color_mov, requested))
    errors = [frame.timestamp_sec - target for frame, target in zip(frames, requested)]
    assert all(0 <= error <= 0.051 for error in errors)


def test_jpeg_dimensions_and_byte_limit_are_enforced(timed_color_mov: Path) -> None:
    frame = asyncio.run(
        make_extractor(max_width=64, max_height=64).extract(timed_color_mov, (0.0,))
    )[0]
    jpeg = base64.b64decode(frame.image_base64, validate=True)
    assert VideoEvidenceExtractor._jpeg_dimensions(jpeg) == (64, 48)

    with pytest.raises(EvidenceExtractionError, match="byte limit"):
        asyncio.run(
            make_extractor(max_jpeg_bytes=16).extract(timed_color_mov, (0.0,))
        )


def test_variable_frame_rate_uses_real_frame_pts(variable_rate_mov: Path) -> None:
    frame = asyncio.run(make_extractor().extract(variable_rate_mov, (1.15,)))[0]
    assert frame.timestamp_sec >= 1.15
    assert frame.timestamp_sec != pytest.approx(1.15, abs=1e-6)
    assert base64.b64decode(frame.image_base64, validate=True).startswith(b"\xff\xd8")


def test_very_short_video_can_supply_start_and_end_frames(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg is required")
    video = tmp_path / "short.mov"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=32x32:r=25:d=0.08",
            "-c:v",
            "mjpeg",
            "-an",
            "-y",
            str(video),
        ],
        check=True,
        timeout=5,
    )
    frames = asyncio.run(make_extractor().extract(video, (0.0, 0.08)))
    assert len(frames) == 2
    assert frames[0].timestamp_sec == pytest.approx(0.0, abs=0.001)
    assert 0 <= frames[1].timestamp_sec < 0.08


def test_rotation_metadata_is_applied_before_jpeg_scaling(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg is required")
    source = tmp_path / "landscape.mov"
    rotated = tmp_path / "rotated.mov"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=80x40:r=5:d=0.4",
            "-c:v",
            "mpeg4",
            "-y",
            str(source),
        ],
        check=True,
        timeout=5,
    )
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-display_rotation",
            "90",
            "-i",
            str(source),
            "-c",
            "copy",
            "-y",
            str(rotated),
        ],
        check=True,
        timeout=5,
    )

    frame = asyncio.run(make_extractor().extract(rotated, (0.0,)))[0]
    dimensions = VideoEvidenceExtractor._jpeg_dimensions(
        base64.b64decode(frame.image_base64, validate=True)
    )
    assert dimensions == (40, 80)


@pytest.mark.parametrize(
    "timestamps",
    [(), (0.0, 0.1, 0.2, 0.3), (-0.1,), (math.nan,), (math.inf,), (True,)],
)
def test_rejects_invalid_timestamps(
    timed_color_mov: Path,
    timestamps: tuple[object, ...],
) -> None:
    with pytest.raises(EvidenceExtractionError):
        asyncio.run(make_extractor().extract(timed_color_mov, timestamps))  # type: ignore[arg-type]


def test_rejects_timestamp_past_video_end(timed_color_mov: Path) -> None:
    with pytest.raises(EvidenceExtractionError, match="outside"):
        asyncio.run(make_extractor().extract(timed_color_mov, (3.01,)))


def test_corrupted_video_fails_without_leaving_frames(tmp_path: Path) -> None:
    video = tmp_path / "corrupted.mov"
    video.write_bytes(b"not-a-video")
    with pytest.raises(EvidenceExtractionError):
        asyncio.run(make_extractor().extract(video, (0.0,)))
    assert list(tmp_path.iterdir()) == [video]


def test_ffmpeg_failure_is_controlled(timed_color_mov: Path) -> None:
    extractor = make_extractor(ffmpeg_path="/missing/ffmpeg")
    with pytest.raises(EvidenceExtractionError, match="unavailable"):
        asyncio.run(extractor.extract(timed_color_mov, (0.0,)))


def test_subprocess_timeout_is_controlled(tmp_path: Path, timed_color_mov: Path) -> None:
    slow = tmp_path / "slow-ffmpeg"
    slow.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    slow.chmod(0o700)
    extractor = make_extractor(ffmpeg_path=str(slow), timeout_sec=0.02)
    with pytest.raises(EvidenceExtractionError, match="timed out"):
        asyncio.run(extractor.extract(timed_color_mov, (0.0,)))


@pytest.mark.parametrize("payload", [b"", b"not-jpeg", b"\xff\xd8broken\xff\xd9"])
def test_invalid_jpeg_is_rejected(payload: bytes) -> None:
    with pytest.raises(EvidenceExtractionError):
        VideoEvidenceExtractor._jpeg_dimensions(payload)
