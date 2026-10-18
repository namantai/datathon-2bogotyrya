"""Optional actual DINOv2 -> trained head -> evidence on generated colors only."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from ml.backbone import DinoV2Encoder
from ml.extract_features import extract_video
from ml.feature_data import dataset_fingerprint, load_features, split_videos
from ml.inference import VideoAnalyzer
from ml.temporal import DetectionConfig, save_checkpoint
from ml.train import fit_head


@pytest.mark.skipif(not os.environ.get("FORTEVISION_TEST_MODEL_DIR"), reason="Public snapshot required")
def test_real_offline_pipeline_on_generated_color_videos(tmp_path, monkeypatch, capsys):
    def denied(*args, **kwargs):
        raise AssertionError("No network permitted during local ML pipeline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    model_dir = Path(os.environ["FORTEVISION_TEST_MODEL_DIR"])
    encoder = DinoV2Encoder(model_dir, device="cpu")
    cache = tmp_path / "features"
    cache.mkdir()
    index = []
    for i in range(12):
        label = i % 2
        video = tmp_path / f"generated-{i}.mov"
        color = f"0x{150+i:02x}1020" if label else f"0x1020{150+i:02x}"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            f"color=c={color}:size=64x48:rate=6:duration=2", "-an", "-c:v", "png", "-pix_fmt", "rgb24", str(video)],
            check=True, capture_output=True, timeout=30)
        extract_video(video, cache / f"video_{i}", encoder)
        index.append({"video_id": f"generated-{i}", "directory": f"video_{i}", "label": label,
                      "timestamps_sec": [1.] if label else [], "group_id": None})
    (cache / "index.json").write_text(json.dumps({"schema_version": 1, "videos": index}))
    videos = load_features(cache, tmp_path)
    split = split_videos(videos)
    train = [v for v in videos if v.video_id in split["train"]]
    validation = [v for v in videos if v.video_id in split["validation"]]
    config = DetectionConfig(smooth_windows=1)
    head, training = fit_head(train, validation, config, epochs=8, learning_rate=.01)
    training.update({"split": split, "dataset_fingerprint": dataset_fingerprint(videos), "synthetic_only": True})
    checkpoint = tmp_path / "head.pt"
    save_checkpoint(checkpoint, head, config, training)
    analyzer = VideoAnalyzer(checkpoint, model_dir, private_root=tmp_path, device="cpu")
    for video in [v for v in videos if v.video_id in split["test"]]:
        events = analyzer.analyze(video.metadata["video_path"])
        # Only a synthetic color-separation assertion, never banking-event accuracy.
        assert bool(events) == bool(video.label)
        for event in events:
            assert all(Path(path).is_file() for path in event["evidence_paths"])

    # Exercise actual CLI parsers/output paths with the same offline socket guard.
    from ml import evaluate, inference
    reports = {}
    common = ["evaluate", "--private-root", str(tmp_path), "--features", str(cache),
              "--checkpoint", str(checkpoint), "--split", "test", "--model-dir", str(model_dir), "--device", "cpu"]
    for mode in ("cached", "complete", "stress"):
        output = tmp_path / f"{mode}.json"
        args = common + ["--output", str(output)]
        if mode != "cached":
            args += ["--end-to-end"]
        if mode == "stress":
            args += ["--perturbation", "resolution"]
        monkeypatch.setattr(sys, "argv", args)
        evaluate.main()
        reports[mode] = json.loads(output.read_text())
    assert reports["cached"]["metrics"] == reports["complete"]["metrics"]
    assert [r["detections_sec"] for r in reports["cached"]["videos"]] == [r["detections_sec"] for r in reports["complete"]["videos"]]
    assert reports["complete"]["mode"] == "complete_local_inference"
    assert reports["complete"]["model_load_sec"] > 0
    assert reports["stress"]["perturbation"] == "resolution"

    monkeypatch.setattr(sys, "argv", common + ["--threshold", ".6", "--output", str(tmp_path / "forbidden.json")])
    with pytest.raises(ValueError, match="frozen"):
        evaluate.main()
    assert not (tmp_path / "forbidden.json").exists()

    video = next(v for v in videos if v.label and v.video_id in split["test"])
    output = tmp_path / "inference.json"
    monkeypatch.setattr(sys, "argv", ["inference", "--private-root", str(tmp_path),
        "--checkpoint", str(checkpoint), "--model-dir", str(model_dir), "--device", "cpu",
        "--video", video.metadata["video_path"], "--output", str(output)])
    inference.main()
    result = json.loads(output.read_text())
    assert result["events"] and result["cold_total_sec"] >= result["complete_inference_sec"]
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        inference.main()
    assert output.read_bytes() == original
    captured = capsys.readouterr()
    assert "generated-" not in captured.out + captured.err
