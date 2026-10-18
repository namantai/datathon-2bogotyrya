"""Synthetic ML integration checks; these do not measure model accuracy.

Run from the repository root:
PYTHONPATH=backend/src backend/.venv/bin/python -m pytest tests/ml -q
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from fortebank_api.evidence import EvidenceExtractionError, VideoEvidenceExtractor
from fortebank_api.local_analyzer import MLResult
from fortebank_api.rules import RuleEngine


def run_media(tool: str, *arguments: str, payload: bytes | None = None) -> bytes:
    executable = shutil.which(tool)
    if executable is None:
        pytest.skip(f"{tool} is required for synthetic media checks")
    return subprocess.run(
        [executable, *arguments], input=payload, capture_output=True,
        check=True, timeout=15,
    ).stdout


def extractor() -> VideoEvidenceExtractor:
    return VideoEvidenceExtractor(
        ffmpeg_path=shutil.which("ffmpeg") or "ffmpeg",
        ffprobe_path=shutil.which("ffprobe") or "ffprobe",
        timeout_sec=10, max_width=96, max_height=64,
        max_jpeg_bytes=512 * 1024, jpeg_quality=3, endpoint_scan_sec=1,
    )


@pytest.mark.parametrize("start_offset", [0, 5])
@pytest.mark.parametrize("variable_rate", [False, True])
def test_evidence_matches_source_pts_and_pixels(
    tmp_path: Path, start_offset: int, variable_rate: bool,
) -> None:
    """Independently decode source frames; check VFR and nonzero media origins."""
    source = tmp_path / "synthetic.mov"
    seconds = "if(lt(N,10),N/10,1+(N-10)/5)" if variable_rate else "N/10"
    run_media(
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=s=96x64:r=10:d=2", "-vf",
        f"setpts='({seconds}+{start_offset})/TB'", "-fps_mode", "vfr",
        "-an", "-c:v", "mpeg4", "-q:v", "2", str(source),
    )
    metadata = json.loads(run_media(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=start_time:frame=best_effort_timestamp_time",
        "-of", "json", str(source),
    ))
    origin = float(metadata["streams"][0]["start_time"])
    assert origin == pytest.approx(start_offset, abs=1e-6)
    timestamps = [float(frame["best_effort_timestamp_time"]) - origin
                  for frame in metadata["frames"]]
    if variable_rate:
        intervals = {round(b - a, 4) for a, b in zip(timestamps, timestamps[1:])}
        assert len(intervals) > 1, "Fixture must actually contain variable frame intervals"

    requested = (0.05, 0.75, 1.15)
    evidence = asyncio.run(extractor().extract(source, requested))
    assert len(evidence) == 3
    for target, frame in zip(requested, evidence):
        index = next(i for i, timestamp in enumerate(timestamps) if timestamp >= target)
        assert frame.timestamp_sec == pytest.approx(timestamps[index], abs=1e-5)
        jpeg = base64.b64decode(frame.image_base64, validate=True)
        actual_pixels = run_media(
            "ffmpeg", "-v", "error", "-i", "pipe:0", "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1", payload=jpeg,
        )
        source_pixels = run_media(
            "ffmpeg", "-v", "error", "-i", str(source), "-vf",
            f"select=eq(n\\,{index})", "-frames:v", "1", "-f", "rawvideo",
            "-pix_fmt", "rgb24", "pipe:1",
        )
        assert len(actual_pixels) == len(source_pixels) == 96 * 64 * 3
        # JPEG is lossy: compare decoded pixels, not encoded byte equality.
        mean_error = sum(abs(a - b) for a, b in zip(actual_pixels, source_pixels)) / len(source_pixels)
        assert mean_error < 15


def test_missing_video_cannot_produce_evidence(tmp_path: Path) -> None:
    with pytest.raises(EvidenceExtractionError, match="unavailable"):
        asyncio.run(extractor().extract(tmp_path / "missing.mov", (0.0,)))


@pytest.mark.parametrize("confidence", [None, 0.01, 0.99, 1.0])
def test_unclassified_candidate_does_not_gain_law_or_severity(confidence: float | None) -> None:
    """A model score alone must not create a supported category or legal rule."""
    event = MLResult.model_validate({"events": [{
        "class_id": "SYNTHETIC_UNCLASSIFIED_CANDIDATE",
        "timecode_sec": 0.75,
        "observation": "Synthetic candidate only; action type is not established.",
        "confidence": confidence,
        "rule_id": "rk-nbrk-231-storage-candidate",
    }]}).events[0].to_event()
    assessment = RuleEngine().assess(event.class_id, event.conditions)
    assert assessment.status == "unknown_event"
    assert assessment.severity == "undetermined"
    assert assessment.regulatory_references == []
    assert event.confidence == confidence
