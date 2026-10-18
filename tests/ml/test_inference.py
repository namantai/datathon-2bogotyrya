"""Generated MOVs and synthetic heads; no private footage or claimed model accuracy."""
from pathlib import Path

import numpy as np
from PIL import Image
import pytest
import torch

from ml import inference
from ml.backbone import WEIGHT_SHA256
from ml.temporal import BinaryHead, DetectionConfig, save_checkpoint


class SyntheticEncoder:
    device = "cpu"
    weight_sha256 = WEIGHT_SHA256
    fallback_used = False

    def __init__(self, *args, **kwargs):
        pass

    def encode(self, images):
        return np.ones((len(images), 384), dtype=np.float32)


def analyzer(tmp_path, monkeypatch, positive=True):
    monkeypatch.setattr(inference, "DinoV2Encoder", SyntheticEncoder)
    model = BinaryHead()
    with torch.no_grad():
        model.net.weight.zero_()
        model.net.bias.fill_(10 if positive else -10)
    path = tmp_path / "head.pt"
    save_checkpoint(path, model, DetectionConfig(), {"completed": True, "synthetic_only": True})
    return inference.VideoAnalyzer(path, tmp_path / "unused-public-model", private_root=tmp_path)


@pytest.mark.parametrize("variable_fps", [False, True])
def test_complete_inference_evidence_matches_source(tmp_path, monkeypatch, make_video, variable_fps):
    from ml.preprocess import sample_video
    video = make_video(variable_fps=variable_fps)
    runner = analyzer(tmp_path, monkeypatch)
    events = runner.analyze(str(video))
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "possible_violation" and "rule_id" not in event
    assert isinstance(event["timecode_sec"], float)
    assert len(event["evidence_paths"]) == len(event["evidence_timestamps_sec"]) == 3
    assert event["timecode_sec"] in event["evidence_timestamps_sec"]
    originals = {f.timestamp_sec: f.rgb for f in sample_video(video)}
    for path, timestamp in zip(event["evidence_paths"], event["evidence_timestamps_sec"]):
        with Image.open(path) as image:
            assert image.format == "JPEG"
            assert image.size == (96, 64)
            assert np.abs(np.array(image).astype(float)-originals[timestamp]).mean() < 15
    assert runner.last_timing["complete_inference_sec"] > 0
    assert not list(runner.evidence_root.glob(".features-*"))


def test_negative_only_after_success_and_errors_propagate(tmp_path, monkeypatch, make_video):
    runner = analyzer(tmp_path, monkeypatch, positive=False)
    assert runner.analyze(str(make_video())) == []
    corrupt = tmp_path / "corrupt.mov"
    corrupt.write_bytes(b"synthetic corrupt MOV")
    with pytest.raises(ValueError):
        runner.analyze(str(corrupt))
    empty = tmp_path / "empty.mov"
    empty.touch()
    with pytest.raises(ValueError):
        runner.analyze(str(empty))
    with pytest.raises(ValueError):
        runner.analyze(str(tmp_path.parent / "outside.mov"))
    assert not list(runner.evidence_root.glob(".features-*"))


def test_callable_and_deadline(tmp_path, monkeypatch, make_video):
    runner = analyzer(tmp_path, monkeypatch)
    monkeypatch.setattr(inference, "_configured_analyzer", lambda: runner)
    video = make_video()
    assert inference.analyze_video(str(video))
    runner.timeout_sec = 1e-12
    with pytest.raises(RuntimeError, match="deadline"):
        inference.analyze_video(str(video))


def test_missing_checkpoint_fails_before_encoder(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Must check trained head before loading backbone")
    monkeypatch.setattr(inference, "DinoV2Encoder", forbidden)
    with pytest.raises(FileNotFoundError):
        inference.VideoAnalyzer(tmp_path / "missing.pt", tmp_path / "model", private_root=tmp_path)


def test_backend_predictions_match_standalone_and_write_no_jpegs(tmp_path, monkeypatch, make_video):
    runner = analyzer(tmp_path, monkeypatch)
    video = make_video()
    predicted = runner.predict(str(video))
    assert predicted and not list(runner.evidence_root.iterdir())
    assert 0 <= predicted[0]["start_sec"] <= predicted[0]["timecode_sec"] <= predicted[0]["end_sec"]
    assert "evidence_paths" not in predicted[0]
    full = runner.analyze(str(video))
    for key in ("type", "timecode_sec", "description", "confidence", "evidence_timestamps_sec"):
        assert predicted[0][key] == full[0][key]


def test_backend_input_root_separate_from_readonly_model_root(tmp_path, monkeypatch, make_video):
    runner = analyzer(tmp_path, monkeypatch)
    storage = tmp_path / "storage"
    storage.mkdir()
    runner.input_root = storage
    video = make_video("storage/source.mov")
    assert runner.predict(str(video))
    with pytest.raises(ValueError):
        runner.predict(str(tmp_path / "outside-storage.mov"))


def test_required_classifier_kind_checked_before_backbone_load(tmp_path, monkeypatch):
    checkpoint = tmp_path / "temporal.pt"
    save_checkpoint(checkpoint, BinaryHead("temporal"), DetectionConfig(), {"completed": True, "synthetic_only": True})
    def forbidden(*args, **kwargs):
        pytest.fail("Must reject wrong head before loading backbone")
    monkeypatch.setattr(inference, "DinoV2Encoder", forbidden)
    with pytest.raises(ValueError, match="kind"):
        inference.VideoAnalyzer(checkpoint, tmp_path / "model", private_root=tmp_path, required_kind="logistic")
