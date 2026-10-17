"""Strict public JSON schema and analyzer-boundary tests."""

import asyncio
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from fortebank_api.analyzers import AnalysisEvent, AnalysisResult, FakeAnalyzer
from fortebank_api.config import Settings
from fortebank_api.schemas import DoneJobResponse, EvidenceFrame, Violation
from tests.support import SYNTHETIC_JPEG_BASE64


def synthetic_violation(index: int) -> Violation:
    return Violation(
        type=f"synthetic-type-{index}",
        timecode_sec=float(index),
        description="Синтетический наблюдаемый факт только для теста схемы",
        evidence_frames=[
            EvidenceFrame(
                timestamp_sec=float(index),
                image_base64=SYNTHETIC_JPEG_BASE64,
            )
        ],
    )


def test_multiple_violations_match_done_schema() -> None:
    result = DoneJobResponse(
        job_id="synthetic-job",
        status="done",
        verdict="violation",
        violations=[synthetic_violation(1), synthetic_violation(2)],
    )
    assert len(result.violations) == 2
    assert set(result.model_dump()) == {"job_id", "status", "verdict", "violations"}


@pytest.mark.parametrize(
    "verdict,violations",
    [
        ("no_violation", [synthetic_violation(1)]),
        ("violation", []),
    ],
)
def test_verdict_and_violations_must_agree(
    verdict: str,
    violations: list[Violation],
) -> None:
    with pytest.raises(ValidationError):
        DoneJobResponse(
            job_id="synthetic-job",
            status="done",
            verdict=verdict,
            violations=violations,
        )


@pytest.mark.parametrize("value", ["not-base64", "bm90LWEtanBlZw=="])
def test_evidence_must_be_base64_jpeg(value: str) -> None:
    with pytest.raises(ValidationError):
        EvidenceFrame(timestamp_sec=0, image_base64=value)


@pytest.mark.parametrize("value", [-1.0, math.inf, -math.inf, math.nan, "1.0", True])
def test_timestamps_must_be_json_numbers_nonnegative_and_finite(value: object) -> None:
    with pytest.raises(ValidationError):
        EvidenceFrame(timestamp_sec=value, image_base64=SYNTHETIC_JPEG_BASE64)


def test_evidence_count_is_limited_to_three() -> None:
    frame = EvidenceFrame(timestamp_sec=0, image_base64=SYNTHETIC_JPEG_BASE64)
    with pytest.raises(ValidationError):
        Violation(
            type="synthetic",
            timecode_sec=0,
            description="synthetic",
            evidence_frames=[frame, frame, frame, frame],
        )


def test_unknown_public_enum_values_are_rejected() -> None:
    with pytest.raises(ValidationError):
        DoneJobResponse(
            job_id="synthetic-job",
            status="done",
            verdict="unknown",
            violations=[],
        )


def test_fake_analyzer_accepts_predefined_internal_events(tmp_path: Path) -> None:
    video = tmp_path / "synthetic.mov"
    video.write_bytes(b"synthetic")
    expected = AnalysisResult(
        events=(
            AnalysisEvent(
                class_id="synthetic-class",
                start_sec=1.0,
                end_sec=2.0,
                timecode_sec=1.5,
                confidence=0.8,
                observation="Синтетическое наблюдение",
                rule_id="synthetic-rule",
                evidence_timestamps_sec=(1.5,),
            ),
        )
    )
    assert asyncio.run(FakeAnalyzer(expected).analyze_video(video)) == expected


def test_production_rejects_placeholder_key() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="production",
            analyzer_mode="disabled",
            api_key="replace-with-a-long-random-local-secret",
        )


def test_production_rejects_fake_analyzer() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="production",
            analyzer_mode="fake",
            api_key="strong-production-key-that-is-not-placeholder",
        )


@pytest.mark.parametrize("value", ["", "                ", " surrounded-by-spaces ", "too-short"])
def test_explicit_unsafe_api_keys_are_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="test", api_key=value)
