"""Synthetic embeddings only. These tests are not real-data quality measurements."""
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest
import torch

from ml.backbone import MODEL_ID, MODEL_REVISION, PREPROCESS_VERSION, WEIGHT_SHA256
from ml.evaluate import binary_metrics, localization
from ml.feature_data import VideoFeatures, dataset_fingerprint, load_features, split_videos, validate_split
from ml.temporal import BinaryHead, DetectionConfig, detect_events, load_checkpoint, save_checkpoint, score_windows, window_labels, windows
from ml.train import fit_head, training_arrays


@pytest.fixture
def synthetic_videos():
    rng = np.random.default_rng(9)
    result = []
    for i in range(20):
        label = i % 2
        times = np.arange(0, 10, .5)
        x = rng.normal(0, .05, (len(times), 384)).astype(np.float32)
        if label:
            x[(times >= 3) & (times <= 6), :12] += 2
        result.append(VideoFeatures(f"synthetic-{i}", label, [4.5] if label else [], f"group-{i}",
                                   f"synthetic-hash-{i}", x, times, {"sample_fps": 2.0}))
    return result


def test_windows_cover_tail_and_unknown_is_not_negative():
    config = DetectionConfig()
    times = np.arange(0, 14, .5)
    x, bounds = windows(np.zeros((len(times), 384), np.float32), times, config)
    assert x.shape[1:] == (8, 384)
    assert bounds[0, 0] == 0 and bounds[-1, 1] == times[-1]
    labels = window_labels(bounds, 1, [1, 12])
    assert set(labels) == {-1, 1}
    assert (window_labels(bounds, 0, []) == 0).all()
    assert (window_labels(bounds, 1, [13.9]) == 1).sum() == 1
    assert all(any(start <= t <= end for start, end in bounds) for t in times)


def test_short_and_invalid_features():
    x, bounds = windows(np.ones((1, 384)), np.array([0.]), DetectionConfig())
    assert x.shape == (1, 8, 384)
    assert bounds.tolist() == [[0, 0]]
    for times in (np.array([]), np.array([1., 2.]), np.array([0., 0.]), np.array([0., np.nan])):
        with pytest.raises(ValueError):
            windows(np.zeros((len(times), 384)), times, DetectionConfig())


@pytest.mark.parametrize("settings", [{"threshold": 0}, {"threshold": float("nan")},
    {"window_sec": -1}, {"stride_sec": 5}, {"smooth_windows": 2}, {"merge_gap_sec": -1}])
def test_invalid_config(settings):
    with pytest.raises(ValueError):
        DetectionConfig(**settings)


def test_multiple_events_merging_and_no_events():
    config = DetectionConfig(window_sec=1, stride_sec=1, smooth_windows=1)
    bounds = np.array([[i, i+1] for i in range(8)])
    events = detect_events(np.array([.1, .8, .9, .1, .1, .95, .8, .1]), bounds, config)
    assert len(events) == 2
    assert [e["timecode_sec"] for e in events] == [2.5, 5.5]
    assert events[0]["start_sec"] == 1 and events[0]["end_sec"] == 3
    assert detect_events(np.zeros(8), bounds, config) == []
    with pytest.raises(ValueError):
        detect_events(np.full(8, np.nan), bounds, config)


def test_disjoint_repeatable_grouped_split(synthetic_videos):
    for i, video in enumerate(synthetic_videos):
        video.group = f"scenario-{i//2}"
    split = split_videos(synthetic_videos)
    assert split == split_videos(synthetic_videos)
    validate_split(synthetic_videos, split)
    broken = {key: list(ids) for key, ids in split.items()}
    broken["train"].append(broken["test"][0])
    with pytest.raises(ValueError):
        validate_split(synthetic_videos, broken)


@pytest.mark.parametrize("kind", ["logistic", "temporal"])
def test_real_gradients_learning_and_checkpoint(tmp_path, synthetic_videos, kind):
    split = split_videos(synthetic_videos)
    train = [v for v in synthetic_videos if v.video_id in split["train"]]
    val = [v for v in synthetic_videos if v.video_id in split["validation"]]
    config = DetectionConfig(smooth_windows=1)
    model, report = fit_head(train, val, config, kind=kind, epochs=12, learning_rate=.01, patience=5)
    assert report["gradient_seen"] and report["weights_changed"]
    assert report["history"][-1]["train_loss"] < report["history"][0]["train_loss"]
    assert not model.training
    x, _, weights = training_arrays(train, config)
    assert weights.mean() == pytest.approx(1)
    path = tmp_path / "head.pt"
    save_checkpoint(path, model, config, report)
    loaded, restored_config, metadata = load_checkpoint(path)
    np.testing.assert_allclose(score_windows(model, x), score_windows(loaded, x))
    assert restored_config == config and metadata["completed"]
    with pytest.raises(FileExistsError):
        save_checkpoint(path, model, config, report)


def test_train_refuses_leakage_and_load_refuses_untrained(tmp_path, synthetic_videos):
    with pytest.raises(ValueError, match="leakage"):
        fit_head(synthetic_videos, synthetic_videos[:4], DetectionConfig())
    path = tmp_path / "head.pt"
    save_checkpoint(path, BinaryHead(), DetectionConfig(), {"completed": False})
    with pytest.raises(ValueError, match="Untrained"):
        load_checkpoint(path)


def test_metrics_include_errors_and_unmatched_events():
    report = binary_metrics([0, 0, 1, 1], [0, 1, 0, 1])
    assert report["confusion_matrix"] == [[1, 1], [1, 1]]
    assert report["accuracy"] == report["f1"] == .5
    assert binary_metrics([0, 1], [0, 0])["recall"] == 0
    assert localization([1, 9], [2])["unmatched_references"] == 1
    assert localization([1, 9], [2])["absolute_errors_sec"] == [1]


def write_cache(root, videos):
    root.mkdir()
    index = []
    for i, video in enumerate(videos):
        directory = root / f"video_{i}"
        directory.mkdir()
        frames = [{"index": j, "pts": j, "timestamp_sec": float(t)} for j, t in enumerate(video.times)]
        np.savez_compressed(directory / "features.npz", embeddings=video.embeddings,
                            timestamps_sec=video.times, frame_indices=np.arange(len(frames)), pts=np.arange(len(frames)))
        meta = {"model_id": MODEL_ID, "model_revision": MODEL_REVISION, "weight_sha256": WEIGHT_SHA256,
                "preprocess": PREPROCESS_VERSION, "embedding_kind": "last_hidden_state_cls", "embedding_dim": 384,
                "timestamp_origin": "first_decoded_video_frame", "sample_fps": 2., "video_sha256": video.digest,
                "video_path": str(root / "not-a-real-video.mov"), "frames": frames}
        (directory / "metadata.json").write_text(json.dumps(meta))
        index.append({"directory": directory.name, "video_id": video.video_id, "label": video.label,
                      "timestamps_sec": video.points, "group_id": video.group})
    (root / "index.json").write_text(json.dumps({"schema_version": 1, "videos": index}))


@pytest.mark.parametrize("test_fraction", [0, .2])
def test_train_and_evaluate_cli(tmp_path, synthetic_videos, test_fraction, monkeypatch):
    root = tmp_path / "features"
    write_cache(root, synthetic_videos)
    loaded = load_features(root, tmp_path)
    assert len(loaded) == 20
    assert dataset_fingerprint(loaded) == dataset_fingerprint(loaded[::-1])
    command = [sys.executable, "-m", "ml.train", "--private-root", str(tmp_path), "--features", str(root),
               "--output", str(tmp_path / "run"), "--epochs", "4", "--learning-rate", ".01",
               "--val-fraction", ".20", "--test-fraction", str(test_fraction)]
    result = subprocess.run(command, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    checkpoint = tmp_path / "run/head.pt"
    split = json.loads((tmp_path / "run/split.json").read_text())["split"]
    assert bool(split["test"]) == bool(test_fraction)
    for name in ("validation", "test"):
        cmd = [sys.executable, "-m", "ml.evaluate", "--private-root", str(tmp_path), "--features", str(root),
               "--checkpoint", str(checkpoint), "--split", name, "--output", str(tmp_path / f"{name}.json")]
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=60)
        if name == "test" and test_fraction == 0:
            assert result.returncode == 2
            assert not (tmp_path / "test.json").exists()
            from ml import evaluate
            monkeypatch.setattr(sys, "argv", ["evaluate", *cmd[3:], "--end-to-end"])
            with pytest.raises(ValueError, match="split is empty"):
                evaluate.main()
            continue
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads((tmp_path / f"{name}.json").read_text())
        assert {r["video_id"] for r in report["videos"]} == set(split[name])
        assert report["mode"] == "cached_features_head_only"
        assert not set(split["train"]) & {r["video_id"] for r in report["videos"]}
    # A changed cache must not silently claim the old split remains a valid held-out test.
    index = json.loads((root / "index.json").read_text())
    index["videos"][0]["group_id"] = "changed"
    (root / "index.json").write_text(json.dumps(index))
    cmd[-1] = str(tmp_path / "changed.json")
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=60)
    assert result.returncode != 0


def test_cache_rejects_missing_labels_and_duplicate_content(tmp_path, synthetic_videos):
    root = tmp_path / "features"
    write_cache(root, synthetic_videos)
    index = json.loads((root / "index.json").read_text())
    index["videos"][0]["label"] = None
    (root / "index.json").write_text(json.dumps(index))
    with pytest.raises(ValueError, match="Missing"):
        load_features(root, tmp_path)


def test_augmented_windows_only_enter_training(synthetic_videos):
    config = DetectionConfig()
    original, labels, weights = training_arrays(synthetic_videos, config)
    for video in synthetic_videos:
        video.brightness_embeddings = video.embeddings + .1
    train, train_labels, train_weights = training_arrays(synthetic_videos, config, augment=True)
    validation, validation_labels, _ = training_arrays(synthetic_videos, config)
    assert len(train) == 2*len(original)
    np.testing.assert_array_equal(validation, original)
    np.testing.assert_array_equal(validation_labels, labels)
    assert train_weights.mean() == pytest.approx(weights.mean())
    assert set(train_labels) == {0, 1}


@pytest.mark.parametrize("profile", ["brightness", "scale", "perspective", "occlusion", "resolution"])
def test_spatial_stress_profiles(profile):
    from ml.augment import perturb_rgb
    pixels = np.random.default_rng(1).integers(0, 256, (40, 60, 3), dtype=np.uint8)
    changed = perturb_rgb(pixels, profile)
    assert changed.shape == pixels.shape and changed.dtype == np.uint8
    assert not np.array_equal(pixels, changed)
    np.testing.assert_array_equal(changed, perturb_rgb(pixels, profile))


def test_feature_cache_symlink_escape_and_duplicate_sources(tmp_path, synthetic_videos):
    root = tmp_path / "features"
    write_cache(root, synthetic_videos)
    first = root / "video_0/metadata.json"
    meta = json.loads(first.read_text())
    second = root / "video_1/metadata.json"
    changed = json.loads(second.read_text())
    changed["video_sha256"] = meta["video_sha256"]
    second.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="identical source"):
        load_features(root, tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(first.read_text())
    first.unlink()
    first.symlink_to(outside)
    with pytest.raises(ValueError, match="inside"):
        load_features(root, tmp_path)


def test_seed_reproducibility(synthetic_videos):
    split = split_videos(synthetic_videos)
    train = [v for v in synthetic_videos if v.video_id in split["train"]]
    val = [v for v in synthetic_videos if v.video_id in split["validation"]]
    one, report_one = fit_head(train, val, DetectionConfig(), epochs=3, seed=17)
    two, report_two = fit_head(train, val, DetectionConfig(), epochs=3, seed=17)
    assert report_one["history"] == report_two["history"]
    assert all(torch.equal(value, two.state_dict()[name]) for name, value in one.state_dict().items())


@pytest.mark.parametrize("key,value", [
    ("duration_sec", float("nan")), ("duration_sec", float("inf")),
    ("duration_sec", 0), ("duration_sec", 9.5),
    ("sample_fps", 0), ("sample_fps", float("nan")), ("sample_fps", float("inf")),
])
def test_cache_rejects_invalid_timing_metadata(tmp_path, synthetic_videos, key, value):
    root = tmp_path / "features"
    write_cache(root, synthetic_videos)
    path = root / "video_0/metadata.json"
    metadata = json.loads(path.read_text())
    metadata[key] = value
    path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="timing"):
        load_features(root, tmp_path)


def test_cache_rejects_mislabeled_augmentation(tmp_path, synthetic_videos):
    root = tmp_path / "features"
    write_cache(root, synthetic_videos)
    path = root / "video_0/features.npz"
    with np.load(path) as archive:
        arrays = dict(archive)
    np.savez_compressed(path, **arrays, brightness_embeddings=arrays["embeddings"])
    path = root / "video_0/metadata.json"
    metadata = json.loads(path.read_text())
    metadata["augmentations"] = ["unrecognized_transform"]
    path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="augmentation"):
        load_features(root, tmp_path)


def test_early_stopping_keeps_best_epoch(synthetic_videos, monkeypatch):
    split = split_videos(synthetic_videos)
    train = [v for v in synthetic_videos if v.video_id in split["train"]]
    val = [v for v in synthetic_videos if v.video_id in split["validation"]]
    step = torch.optim.AdamW.step
    calls, first_state = [], {}

    def plateau_after_first_update(optimizer, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            result = step(optimizer, *args, **kwargs)
            first_state["parameters"] = [p.detach().clone() for group in optimizer.param_groups for p in group["params"]]
            return result

    monkeypatch.setattr(torch.optim.AdamW, "step", plateau_after_first_update)
    model, report = fit_head(train, val, DetectionConfig(), epochs=20, batch_size=1024, patience=2)
    assert len(report["history"]) == 3 and report["best_epoch"] == 1
    assert all(torch.equal(p, expected) for p, expected in zip(model.parameters(), first_state["parameters"]))


@pytest.mark.parametrize("test_fraction", [0, .2])
@pytest.mark.parametrize("grouped", [False, True])
def test_optional_test_split_is_disjoint_and_stratified(synthetic_videos, test_fraction, grouped):
    if grouped:
        for i, video in enumerate(synthetic_videos):
            video.group = f"scenario-{i//2}"
    split = split_videos(synthetic_videos, val_fraction=.2, test_fraction=test_fraction)
    assert split == split_videos(synthetic_videos, val_fraction=.2, test_fraction=test_fraction)
    assert bool(split["test"]) == bool(test_fraction)
    assert len(split["validation"]) == 4
    assert len(split["train"]) == (16 if test_fraction == 0 else 12)
    validate_split(synthetic_videos, split)
    seen_ids, seen_groups = set(), set()
    for name, ids in split.items():
        selected = [v for v in synthetic_videos if v.video_id in ids]
        groups = {v.group for v in selected}
        assert not seen_ids.intersection(ids) and not seen_groups.intersection(groups)
        seen_ids.update(ids)
        seen_groups.update(groups)
        if name != "test" or test_fraction > 0:
            assert {v.label for v in selected} == {0, 1}
    assert seen_ids == {v.video_id for v in synthetic_videos}


def test_zero_test_split_allows_two_independent_mixed_class_groups(synthetic_videos):
    videos = synthetic_videos[:4]
    for i, video in enumerate(videos):
        video.group = f"scenario-{i//2}"
    split = split_videos(videos, val_fraction=.2, test_fraction=0)
    assert split["test"] == []
    assert len(split["train"]) == len(split["validation"]) == 2
    validate_split(videos, split)


@pytest.mark.parametrize("empty_name", ["train", "validation"])
def test_validate_split_still_rejects_empty_required_partitions(synthetic_videos, empty_name):
    split = {"train": [], "validation": [], "test": []}
    split["validation" if empty_name == "train" else "train"] = [v.video_id for v in synthetic_videos]
    with pytest.raises(ValueError, match="both labels"):
        validate_split(synthetic_videos, split)


def test_empty_test_does_not_disable_class_or_group_checks(synthetic_videos):
    split = {"train": [v.video_id for v in synthetic_videos[:10]],
             "validation": [v.video_id for v in synthetic_videos[10:]], "test": []}
    validate_split(synthetic_videos, split)
    synthetic_videos[10].group = synthetic_videos[0].group
    with pytest.raises(ValueError, match="groups cross"):
        validate_split(synthetic_videos, split)
    split = {"train": [v.video_id for v in synthetic_videos if v.label == 0],
             "validation": [v.video_id for v in synthetic_videos if v.label == 1], "test": []}
    with pytest.raises(ValueError, match="both labels"):
        validate_split(synthetic_videos, split)
