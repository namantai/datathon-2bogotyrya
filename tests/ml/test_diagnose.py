"""Privacy and diagnostic correctness using generated embeddings only."""
import json
import socket
import subprocess
import sys

import numpy as np
import pytest
import torch

from ml import diagnose as diagnostics
from ml.evaluate import binary_metrics
from ml.feature_data import dataset_fingerprint, load_features, split_videos
from ml.temporal import BinaryHead, DetectionConfig, detect_events, save_checkpoint, score_windows, smooth_scores, window_labels, windows
from ml.train import fit_head, training_arrays
from test_training import synthetic_videos, write_cache


@pytest.fixture
def diagnostic_inputs(tmp_path, synthetic_videos):
    root = tmp_path / "features"
    write_cache(root, synthetic_videos)
    videos = load_features(root, tmp_path)
    split = split_videos(videos, test_fraction=0)
    train = [v for v in videos if v.video_id in split["train"]]
    validation = [v for v in videos if v.video_id in split["validation"]]
    paths = {}
    for kind in ("logistic", "temporal"):
        model, training = fit_head(train, validation, DetectionConfig(), kind=kind, epochs=2)
        training.update({"split": split, "dataset_fingerprint": dataset_fingerprint(videos)})
        paths[kind] = tmp_path / f"{kind}.pt"
        save_checkpoint(paths[kind], model, DetectionConfig(), training)
    return root, videos, split, paths


def test_diagnostic_is_offline_read_only_and_does_not_train(tmp_path, diagnostic_inputs, monkeypatch):
    root, videos, split, paths = diagnostic_inputs
    before = {name: path.read_bytes() for name, path in paths.items()}
    def forbidden(*args, **kwargs):
        pytest.fail("Default diagnostics must not train, decode video, or access network")
    monkeypatch.setattr(diagnostics, "fit_head", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    import av
    monkeypatch.setattr(av, "open", forbidden)
    report = diagnostics.diagnose(root, paths, tmp_path)
    assert report["status"] == "ok" and not report["source_videos_opened"]
    assert report["cross_validation"] == {"status": "not_requested", "training_performed": False}
    assert report["comparison"]["same_validation_set"]
    assert report["inventory"]["videos"] == 20
    for name, model in report["models"].items():
        assert not model["test_scored"] and model["normalization"]["matches_train_only"]
        assert set(model["validation_baselines"]) == {"always_negative", "always_positive", "training_majority",
                                                     "mean_embedding_nearest_centroid", "duration_nearest_centroid"}
        train = model["partitions"]["train"]
        assert train["window_counts"]["positive"] > 0 and train["window_counts"]["negative"] > 0
        assert train["window_counts"]["ignored"] > 0
        assert train["loss"]["weight_mass_by_label"] == pytest.approx({"0": .5, "1": .5})
        assert paths[name].read_bytes() == before[name]
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("include_temporal", [False, True])
def test_diagnostic_cli_keeps_sensitive_results_local(tmp_path, diagnostic_inputs, include_temporal):
    root, _, _, paths = diagnostic_inputs
    output = tmp_path / "local_report"
    command = [sys.executable, "-m", "ml.diagnose", "--private-root", str(tmp_path),
        "--features", str(root), "--logistic-checkpoint", str(paths["logistic"]), "--output", str(output)]
    if include_temporal:
        command.extend(["--temporal-checkpoint", str(paths["temporal"])])
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "synthetic-" not in result.stdout + result.stderr
    report_path = output / "report.json"
    report_bytes = report_path.read_bytes()
    report = json.loads(report_bytes)
    assert report["status"] == "ok"
    assert set(report["models"]) == ({"logistic", "temporal"} if include_temporal else {"logistic"})
    assert report["cross_validation"]["training_performed"] is False
    for partition in report["models"]["logistic"]["partitions"].values():
        assert set(("tp", "tn", "fp", "fn", "false_positive_rate", "false_negative_rate")) <= set(partition["video_metrics"])
        assert partition["event_metrics_at_3sec"]["tolerance_sec"] == 3
    assert "synthetic-" in report_bytes.decode()
    repeated = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert repeated.returncode == 2 and report_path.read_bytes() == report_bytes


def test_mismatched_cache_does_not_claim_validation_metrics(tmp_path, diagnostic_inputs):
    root, _, _, paths = diagnostic_inputs
    index_path = root / "index.json"
    data = json.loads(index_path.read_text())
    data["videos"][0]["group_id"] = "changed-synthetic-group"
    index_path.write_text(json.dumps(data))
    report = diagnostics.diagnose(root, paths, tmp_path, cv_folds=3)
    assert report["status"] == "incompatible_checkpoint_or_cache"
    assert all(r["status"] == "incompatible_cache" for r in report["models"].values())
    assert not report["cross_validation"]["training_performed"]
    assert report["comparison"]["status"] == "not_comparable"


def test_group_cv_never_uses_original_validation(tmp_path, diagnostic_inputs, monkeypatch):
    root, videos, split, paths = diagnostic_inputs
    excluded = set(split["validation"] + split["test"])
    actual_fit = diagnostics.fit_head
    calls = []
    def checked_fit(train, early, *args, **kwargs):
        calls.append(1)
        assert not excluded.intersection(v.video_id for v in train + early)
        assert not {v.group for v in train} & {v.group for v in early}
        return actual_fit(train, early, *args, **kwargs)
    monkeypatch.setattr(diagnostics, "fit_head", checked_fit)
    report = diagnostics.diagnose(root, paths, tmp_path, cv_folds=3, cv_epochs=2)
    cv = report["cross_validation"]
    json.dumps(report, allow_nan=False)
    assert cv["status"] == "ok" and cv["effective_folds"] == 3
    assert not cv["checkpoints_written"] and len(calls) == 6
    assert set(cv["eligible_video_ids"]) == set(split["train"])
    seen = []
    for fold in cv["folds"]:
        partition = fold["partition"]
        seen.extend(partition["outer_holdout"])
        sets = [set(ids) for ids in partition.values()]
        assert not sets[0] & sets[1] and not sets[0] & sets[2] and not sets[1] & sets[2]
        assert not excluded.intersection(set.union(*sets))
    assert len(seen) == len(set(seen)) == len(split["train"])
    assert cv["out_of_fold_metrics"]["logistic"]["videos"] == len(split["train"])


def test_group_folds_keep_related_recordings_together(synthetic_videos):
    for i, video in enumerate(synthetic_videos):
        video.group = f"related-{i//2}"
    plan = diagnostics.group_folds(synthetic_videos, 3, 2026)
    assert plan == diagnostics.group_folds(synthetic_videos, 3, 2026)
    owners = {}
    for i, ids in enumerate(plan):
        for video in synthetic_videos:
            if video.video_id in ids:
                assert owners.setdefault(video.group, i) == i
    assert diagnostics.group_folds(synthetic_videos[:2], 3, 2026) is None


def test_cv_pool_excludes_holdouts_of_both_models(synthetic_videos):
    ids = [v.video_id for v in synthetic_videos]
    reports = {"a": {"status": "ok", "split": {"train": ids[:16], "validation": ids[16:], "test": []}},
               "b": {"status": "ok", "split": {"train": ids[4:], "validation": ids[:2], "test": ids[2:4]}}}
    assert {v.video_id for v in diagnostics.cv_pool(synthetic_videos, reports)} == set(ids[4:16])


def test_equal_bad_metrics_do_not_imply_equal_predictions():
    labels, a, b = [1, 1, 0, 0, 0], [1, 0, 1, 1, 1], [0, 1, 1, 1, 1]
    assert binary_metrics(labels, a) == binary_metrics(labels, b)
    assert binary_metrics(labels, a)["accuracy"] == .2
    assert binary_metrics(labels, a)["f1"] == pytest.approx(1/3)
    def report(pred):
        rows = [{"video_id": str(i), "label": y, "prediction": p, "correct": y == p,
                 "video_score_max_smoothed": .9 if p else .1} for i, (y, p) in enumerate(zip(labels, pred))]
        return {"status": "ok", "partitions": {"validation": {"videos": rows}}}
    comparison = diagnostics.compare_models({"logistic": report(a), "temporal": report(b)})
    assert comparison["same_validation_set"]
    assert not comparison["identical_predictions_on_common_validation"]


def test_window_balancing_and_ignored_regions(synthetic_videos):
    config = DetectionConfig()
    _, y, weights = training_arrays(synthetic_videos, config)
    assert set(y) == {0, 1}
    assert weights[y == 0].sum() == pytest.approx(weights[y == 1].sum())
    v = synthetic_videos[1]
    _, bounds = windows(v.embeddings, v.times, config)
    labels = window_labels(bounds, v.label, v.points)
    assert (labels == 1).sum() == 4 and (labels == -1).sum() == 3
    assert (labels == 0).sum() == 0


def test_any_window_decision_can_disagree_with_average_window_accuracy():
    config = DetectionConfig(window_sec=1, stride_sec=1)
    raw = np.full(100, .1)
    raw[48:51] = .9
    bounds = np.array([[i, i+1] for i in range(100)])
    assert (raw < .5).mean() == .97  # 97% window accuracy on a negative video.
    assert bool(detect_events(raw, bounds, config))  # Still a video-level false positive.
    assert int(smooth_scores(raw, config).max() >= .5) == 1


def test_temporal_head_can_use_order_but_logistic_cannot():
    x = np.zeros((1, 8, 384), dtype=np.float32)
    x[0, :, 0] = [0, 0, 1, 3, 0, 0, 0, 0]
    reverse = np.ascontiguousarray(x[:, ::-1])
    logistic = BinaryHead("logistic")
    np.testing.assert_allclose(score_windows(logistic, x), score_windows(logistic, reverse), atol=1e-6)
    temporal = BinaryHead("temporal", hidden=1)
    with torch.no_grad():
        for parameter in temporal.parameters():
            parameter.zero_()
        temporal.conv[0].weight[0, 0, 0] = 1
        temporal.conv[0].weight[0, 0, 1] = -2
        temporal.conv[2].weight[0, 0, 1] = 1
        temporal.net.weight[0, 0] = 1
    assert abs(float(score_windows(temporal, x)[0]-score_windows(temporal, reverse)[0])) > 1e-4


def test_group_cv_reports_infeasibility_without_training(synthetic_videos, monkeypatch):
    videos = synthetic_videos[:4]
    ids = [v.video_id for v in videos]
    reports = {"logistic": {"status": "ok", "split": {"train": ids, "validation": [], "test": []}}}
    monkeypatch.setattr(diagnostics, "fit_head", lambda *a, **kw: pytest.fail("Infeasible CV must not train"))
    report = diagnostics.grouped_cv(videos, reports, folds=2)
    assert report["status"] == "infeasible" and not report["training_performed"]


def test_baseline_predictions_do_not_use_validation_labels(synthetic_videos):
    train, validation = synthetic_videos[:16], synthetic_videos[16:]
    before = diagnostics.simple_baselines(train, validation)
    for video in validation:
        video.label = 1-video.label
    after = diagnostics.simple_baselines(train, validation)
    assert all(before[name]["predictions"] == after[name]["predictions"] for name in before)


def test_diagnostic_flags_normalization_mismatch(tmp_path, diagnostic_inputs):
    root, videos, _, paths = diagnostic_inputs
    from ml.temporal import load_checkpoint
    model, config, training = load_checkpoint(paths["logistic"])
    model.feature_mean.add_(1.)
    report = diagnostics.model_audit(videos, model, config, training, seed=2026)
    assert report["status"] == "ok"
    assert not report["normalization"]["matches_train_only"]
    assert report["normalization"]["max_mean_error_vs_train_only"] == pytest.approx(1.)


def test_identical_feature_arrays_are_review_cues(synthetic_videos):
    videos = synthetic_videos[:3]
    videos[1].embeddings = videos[0].embeddings.copy()
    split = {"train": [v.video_id for v in videos[:1]], "validation": [v.video_id for v in videos[1:]], "test": []}
    result = diagnostics.similarity_audit(videos, {"model": {"status": "ok", "split": split}})
    pair = result["identical_feature_pairs"][0]
    assert pair["video_ids"] == [videos[0].video_id, videos[1].video_id]
    assert pair["crosses_train_validation"]["model"]
    assert not pair["same_declared_group"]
