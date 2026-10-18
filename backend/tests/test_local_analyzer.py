import asyncio
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from fortebank_api.local_analyzer import LocalAnalyzerError, LocalProcessAnalyzer, MLResult


def observation(**overrides):
    return {"code": "SYNTHETIC", "timestamp_seconds": 0.25,
            "description": "Synthetic only", **overrides}


def test_aliases_preserve_original_timestamps_and_optional_confidence():
    result = MLResult.model_validate({"events": [observation()]})
    event = result.events[0].to_event()
    assert event.class_id == "SYNTHETIC"
    assert event.timecode_sec == 0.25
    assert event.evidence_timestamps_sec == (0.25,)
    assert event.confidence is None and event.rule_id is None
    canonical = MLResult.model_validate({"events": [{
        "class_id": "SYNTHETIC", "timecode_sec": 1.2, "observation": "Synthetic",
        "start_sec": 1, "end_sec": 2, "confidence": 0.4,
        "evidence_timestamps_sec": [1.1, 1.2, 1.3],
    }]})
    assert canonical.events[0].to_event().evidence_timestamps_sec == (1.1, 1.2, 1.3)


@pytest.mark.parametrize("override", [
    {"timestamp_seconds": -1}, {"timestamp_seconds": float("nan")},
    {"timestamp_seconds": True}, {"confidence": 1.1}, {"confidence": "0.5"},
    {"start_sec": 1}, {"end_sec": 0}, {"description": " "},
    {"code": " "}, {"conditions": {"fact": "true"}},
    {"evidence_timestamps_sec": []}, {"evidence": "fabricated-image"},
])
def test_invalid_observations_rejected(override):
    with pytest.raises(ValidationError):
        MLResult.model_validate({"events": [observation(**override)]})


@pytest.mark.parametrize("payload", [{}, {"events": None}, {"events": [observation()] * 129}])
def test_missing_or_unbounded_output_rejected(payload):
    with pytest.raises(ValidationError):
        MLResult.model_validate(payload)


@pytest.mark.parametrize("function,count", [("successful", 1), ("empty", 0)])
def test_subprocess_success_without_inheriting_secrets(tmp_path, monkeypatch, function, count):
    monkeypatch.setenv("APP_API_KEY", "synthetic-never-pass-to-child")
    path = tmp_path / "synthetic.mov"
    path.write_bytes(b"synthetic")
    async def scenario():
        result = await asyncio.wait_for(LocalProcessAnalyzer(
            f"tests.ml_process_fixture:{function}").analyze_video(path), 5)
        assert len(result.events) == count
    asyncio.run(scenario())


@pytest.mark.parametrize("function", ["broken", "missing_events", "oversized", "does_not_exist"])
def test_subprocess_errors_never_return_negative_or_private_text(tmp_path, function):
    async def scenario():
        with pytest.raises(LocalAnalyzerError) as caught:
            await asyncio.wait_for(LocalProcessAnalyzer(
                f"tests.ml_process_fixture:{function}").analyze_video(tmp_path / "synthetic.mov"), 5)
        assert str(tmp_path) not in str(caught.value)
        assert "private observation" not in str(caught.value)
    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process lifecycle assertion")
def test_cancellation_reaps_blocking_process_and_does_not_block_loop(tmp_path):
    path = tmp_path / "synthetic.mov"
    path.write_bytes(b"synthetic")
    async def scenario():
        task = asyncio.create_task(LocalProcessAnalyzer(
            "tests.ml_process_fixture:blocking").analyze_video(path))
        try:
            for _ in range(200):
                if path.with_suffix(".pid").exists():
                    break
                await asyncio.sleep(.01)
            else:
                pytest.fail("Local child did not start")
            pid = int(path.with_suffix(".pid").read_text())
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    asyncio.run(scenario())


def test_no_shell_or_untrusted_entrypoint():
    with pytest.raises(ValueError):
        LocalProcessAnalyzer("module:func; echo secret")
