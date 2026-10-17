"""Generated MOVs/labels only. Never opens repository private_data or real heads."""
import json
from pathlib import Path
import socket
import subprocess
import sys

import numpy as np
import pytest
import torch
from torch.nn import functional as F

from ml import audit_annotations as audit
from ml.backbone import WEIGHT_SHA256, sha256_file
from ml.diagnose import duration_score_audit, score_video
from ml.extract_features import extract_video
from ml.feature_data import VideoFeatures, dataset_fingerprint, load_features
from ml.preprocess import sample_video
from ml.temporal import BinaryHead, DetectionConfig, save_checkpoint
from ml.train import fit_head, training_arrays
from test_training import synthetic_videos


class SyntheticEncoder:
    device = "cpu"
    fallback_used = False
    weight_sha256 = WEIGHT_SHA256

    def encode(self, images):
        return np.ones((len(images), 384), dtype=np.float32)


@pytest.fixture
def audit_inputs(tmp_path, make_video):
    base = make_video("base.mov")
    root = tmp_path / "features"
    root.mkdir()
    entries, sources = [], []
    for i in range(6):
        source = tmp_path / f"source_{i}.mov"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(base), "-map", "0:v:0",
                        "-c", "copy", "-metadata", f"comment=synthetic-{i}", str(source)],
                       capture_output=True, check=True, timeout=30)
        sources.append(source)
        extract_video(source, root / f"v{i}", SyntheticEncoder())
        entries.append({"video_id": f"synthetic-{i}", "directory": f"v{i}", "label": i % 2,
                        "timestamps_sec": [0.5] if i % 2 else [], "group_id": None})
    (root / "index.json").write_text(json.dumps({"schema_version": 1, "videos": entries}))
    videos = load_features(root, tmp_path)
    split = {"train": ["synthetic-0", "synthetic-1"], "validation": ["synthetic-2", "synthetic-3"],
             "test": ["synthetic-4", "synthetic-5"]}
    checkpoint = tmp_path / "synthetic.pt"
    save_checkpoint(checkpoint, BinaryHead("temporal"), DetectionConfig(),
                    {"completed": True, "synthetic_only": True, "split": split,
                     "dataset_fingerprint": dataset_fingerprint(videos)})
    return root, checkpoint, sources, videos


def test_offline_review_verifies_sources_without_forward_train_or_network(tmp_path, audit_inputs, monkeypatch):
    root, checkpoint, sources, videos = audit_inputs
    before = {path: path.read_bytes() for path in sources + [checkpoint]}
    original_sample = audit.sample_video
    decoded = []
    def checked_sample(path, *args):
        decoded.append(path)
        assert path not in sources[4:]
        yield from original_sample(path, *args)
    def forbidden(*args, **kwargs):
        pytest.fail("Review must not run a model or contact a network")
    monkeypatch.setattr(audit, "sample_video", checked_sample)
    monkeypatch.setattr(BinaryHead, "forward", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    output = tmp_path / "review"
    report = audit.build_audit(root, checkpoint, output, tmp_path)
    assert set(decoded) == set(sources[:4])
    assert report["status"] == "complete" and not report["labels_modified"]
    assert not report["training_performed"] and not report["test_videos_reviewed"]
    assert report["duration_by_split_and_label"]["validation"]["1"]["videos"] == 1
    assert (output.stat().st_mode & 0o777) == 0o700
    assert len(list(output.glob("video_*/*.jpg"))) == 20
    page = (output / "index.html").read_text()
    assert "connect-src 'none'" in page and "https://" not in page
    assert "proposals_only:true" in page and "full_video_checked" in page
    assert "synthetic-4" not in page
    assert "timestamp_sec" in (output / "report.json").read_text()
    assert all(path.read_bytes() == data for path, data in before.items())
    with pytest.raises(FileExistsError):
        audit.build_audit(root, checkpoint, output, tmp_path)


def test_changed_source_fails_without_success_marker(tmp_path, audit_inputs):
    root, checkpoint, sources, _ = audit_inputs
    sources[0].write_bytes(b"synthetic changed source")
    output = tmp_path / "review"
    with pytest.raises(ValueError, match="hash differs"):
        audit.build_audit(root, checkpoint, output, tmp_path)
    assert not (output / "report.json").exists() and not (output / "index.html").exists()


def test_cache_origin_mismatch_is_not_hidden(tmp_path, audit_inputs):
    root, checkpoint, _, videos = audit_inputs
    metadata_path = root / "v0/metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["frames"][0]["origin_pts"] += 1
    metadata_path.write_text(json.dumps(metadata))
    # Fingerprint verifies embeddings/labels but not every frame field: audit must verify source PTS too.
    assert dataset_fingerprint(load_features(root, tmp_path)) == dataset_fingerprint(videos)
    with pytest.raises(ValueError, match="PTS/origin"):
        audit.build_audit(root, checkpoint, tmp_path / "review", tmp_path)


def test_manifest_disagreement_is_reported_without_changing_labels(tmp_path, audit_inputs):
    root, checkpoint, _, _ = audit_inputs
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"numeric_timestamp_unit": "excel_days", "videos": [
        {"video_id": "synthetic-2", "label": 1, "timestamps_sec": [1]},
        {"video_id": "synthetic-3", "label": 1, "timestamps_sec": [0.5]}]}))
    report = audit.build_audit(root, checkpoint, tmp_path / "review", tmp_path,
                               split="validation", manifest=manifest)
    assert [v["manifest_label_and_points_match"] for v in report["videos"]] == [False, True]
    assert report["videos"][0]["label"] == 0 and report["videos"][0]["timestamps_sec"] == []
    assert report["annotation_unit_from_manifest"] == "excel_days"


def test_source_symlink_outside_private_root_rejected_before_decode(tmp_path, audit_inputs, monkeypatch):
    root, checkpoint, _, _ = audit_inputs
    escaped = tmp_path / "escape.mov"
    escaped.symlink_to(tmp_path.parent / "outside-synthetic.mov")
    metadata_path = root / "v0/metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["video_path"] = str(escaped)
    metadata_path.write_text(json.dumps(metadata))
    monkeypatch.setattr(audit, "sample_video", lambda *a, **kw: pytest.fail("Must fail before decoding"))
    with pytest.raises(ValueError, match="inside"):
        audit.build_audit(root, checkpoint, tmp_path / "review", tmp_path)
    assert not (tmp_path / "review").exists()


def test_audit_cli_keeps_private_details_out_of_terminal(tmp_path, audit_inputs):
    root, checkpoint, sources, _ = audit_inputs
    command = [sys.executable, "-m", "ml.audit_annotations", "--private-root", str(tmp_path),
               "--features", str(root), "--checkpoint", str(checkpoint), "--split", "validation",
               "--output", str(tmp_path / "review")]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "synthetic-" not in result.stdout + result.stderr
    report = json.loads((tmp_path / "review/report.json").read_text())
    assert {r["split"] for r in report["videos"]} == {"validation"}
    sources[2].write_bytes(b"synthetic broken source")
    command[-1] = str(tmp_path / "failed")
    failed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert failed.returncode == 2 and "source_2" not in failed.stdout + failed.stderr
    assert "synthetic-" not in failed.stdout + failed.stderr


def test_review_html_escapes_identifiers_and_blocks_script_injection(tmp_path, audit_inputs):
    root, checkpoint, _, _ = audit_inputs
    report = audit.build_audit(root, checkpoint, tmp_path / "review", tmp_path, split="validation")
    report["videos"][0]["video_id"] = '</script><script src="https://example.invalid">'
    page = audit.render_review(report)
    assert '<script src="https://' not in page
    assert '\\u003c/script>' in page
    assert '&lt;/script&gt;' in page


def test_nonzero_pts_and_vfr_keep_annotation_seconds_relative_to_first_frame(tmp_path, make_video):
    original = make_video(variable_fps=True)
    shifted = tmp_path / "shifted.mov"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(original), "-c", "copy",
                    "-output_ts_offset", "7", str(shifted)], check=True, capture_output=True, timeout=30)
    base, frames = list(sample_video(original)), list(sample_video(shifted))
    assert frames[0].origin_pts * frames[0].time_base_num / frames[0].time_base_den == pytest.approx(7)
    assert [f.timestamp_sec for f in frames] == pytest.approx([f.timestamp_sec for f in base])
    assert frames[0].timestamp_sec == 0
    video = VideoFeatures("synthetic", 1, [0.5], "g", sha256_file(shifted),
                          np.ones((len(frames), 384), np.float32), np.array([f.timestamp_sec for f in frames]),
                          {"sample_fps": 2, "frames": [f.metadata() for f in frames]})
    exported, _ = audit.export_source_samples(video, shifted, tmp_path / "samples", DetectionConfig(), 30)
    assert exported[1]["timestamp_sec"] == pytest.approx(0.5)
    details = audit.supervision_details(video, DetectionConfig())
    assert details["points"][0]["positive_window_indices"] == [0]


def test_overlapping_point_windows_ignored_regions_and_inclusive_boundaries(synthetic_videos):
    video = synthetic_videos[1]
    details = audit.supervision_details(video, DetectionConfig())
    assert details["window_counts"] == {"positive": 4, "negative": 0, "ignored": 3}
    assert details["supervised_union_sec"] == 7
    assert details["outside_supervised_union_sec"] == 2.5
    video.points = [0, 4, 9.8]
    details = audit.supervision_details(video, DetectionConfig())
    assert details["points"][0]["positive_window_indices"] == [0]
    assert details["points"][1]["positive_window_indices"] == [0, 1, 2, 3, 4]
    assert details["points"][2]["after_last_sample"]
    assert details["windows"][-1]["tail_point_assigned"]
    for window in details["windows"]:
        assert np.diff(window["sample_times_sec"]).min() >= 0
    video.label, video.points = 0, []
    assert audit.supervision_details(video, DetectionConfig())["window_counts"] == {"positive": 0, "negative": 7, "ignored": 0}


def test_best_checkpoint_matches_weighted_supervised_validation_loss(synthetic_videos):
    model, training = fit_head(synthetic_videos[:16], synthetic_videos[16:], DetectionConfig(), epochs=3)
    x, targets, weights = training_arrays(synthetic_videos[16:], DetectionConfig())
    with torch.inference_mode():
        loss = (F.binary_cross_entropy_with_logits(model(torch.from_numpy(x)), torch.from_numpy(targets),
                                                   reduction="none") * torch.from_numpy(weights)).mean().item()
    best = training["history"][training["best_epoch"]-1]["validation_loss"]
    assert loss == pytest.approx(best, abs=1e-6)
    assert best == pytest.approx(min(epoch["validation_loss"] for epoch in training["history"]), abs=1e-6)


def test_audit_exposes_point_frame_omitted_by_window_resampling(synthetic_videos):
    video = synthetic_videos[1]
    video.points = [2]
    details = audit.supervision_details(video, DetectionConfig())
    point = details["points"][0]
    assert point["nearest_sample_sec"] == 2 and point["sample_distance_sec"] == 0
    first = point["positive_window_sequence_checks"][0]
    assert first["window_index"] == 0
    assert not first["nearest_cached_sample_retained"]
    assert first["closest_sequence_sample_distance_sec"] == .5
    assert details["windows"][0]["target"] == 1


def test_temporal_scores_can_change_without_crossing_video_threshold():
    model = BinaryHead("temporal", hidden=1)
    with torch.no_grad():
        for parameter in model.parameters():
            parameter.zero_()
        model.conv[0].weight[0, 0, 0] = 1
        model.conv[0].weight[0, 0, 1] = -2
        model.conv[2].weight[0, 0, 1] = 1
        model.net.weight[0, 0] = 1
        model.net.bias.fill_(2)
    x = np.zeros((8, 384), np.float32)
    x[:, 0] = [0, 0, 1, 3, 0, 0, 0, 0]
    video = VideoFeatures("synthetic", 1, [1], "g", "hash", x, np.arange(8)*.5, {"duration_sec": 4})
    row = score_video(model, video, DetectionConfig())
    reverse = row["order_sensitivity"]["reverse"]
    assert reverse["max_absolute_window_score_change"] > 1e-4
    assert not reverse["decision_changed"]
    assert row["decision_margin"] > 0 and reverse["decision_margin"] > 0
    assert reverse["video_score_change"] != 0


def test_duration_correlations_are_descriptive_and_handle_constant_scores():
    rows = [{"label": i % 2, "duration_sec": float(i), "video_score_max_smoothed": .1*i,
             "window_counts": {"positive": 0, "negative": i, "ignored": 0}} for i in range(1, 7)]
    report = duration_score_audit(rows)
    assert report["all_labels"]["pearson_duration_vs_max_score"] == pytest.approx(1)
    assert report["by_label"]["0"]["videos"] == 3
    for row in rows:
        row["video_score_max_smoothed"] = .9
    assert duration_score_audit(rows)["all_labels"]["pearson_duration_vs_max_score"] is None
    json.dumps(duration_score_audit([]), allow_nan=False)
