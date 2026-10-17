"""Local-only checkpoint diagnostics on cached features; never opens source video.

No training by default. --cv-folds explicitly opts into CPU head-only nested/grouped
cross-validation on the intersection of original training sets. No checkpoints are
written or updated. Reports contain private IDs/scores: retain them locally.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
from itertools import combinations
from pathlib import Path
import time

import numpy as np
import torch
from torch.nn import functional as F

from ml.evaluate import binary_metrics
from ml.feature_data import VideoFeatures, dataset_fingerprint, load_features, split_videos, validate_split
from ml.local_io import private_path, run_cli, write_json
from ml.temporal import BinaryHead, DetectionConfig, detect_events, load_checkpoint, score_windows, smooth_scores, window_labels, windows
from ml.train import fit_head, training_arrays


def distribution(values) -> dict | None:
    values = np.asarray(values, dtype=float)
    if not values.size:
        return None
    return {"count": int(values.size), "min": float(values.min()), "p10": float(np.quantile(values, .1)),
            "median": float(np.median(values)), "p90": float(np.quantile(values, .9)),
            "max": float(values.max()), "mean": float(values.mean()), "std": float(values.std())}


def score_video(model: BinaryHead, video: VideoFeatures, config: DetectionConfig,
                *, seed: int = 2026, order_checks: bool = True) -> dict:
    x, bounds = windows(video.embeddings, video.times, config)
    targets = window_labels(bounds, video.label, video.points)
    raw = score_windows(model, x)
    smooth = smooth_scores(raw, config)
    video_score = float(smooth.max())
    prediction = int(video_score >= config.threshold)
    # This equivalence is critical: merging/localization must not change the verdict.
    if prediction != int(bool(detect_events(raw, bounds, config))):
        raise ValueError("Diagnostic decision differs from production detector")
    counts = {name: int((targets == value).sum()) for name, value in
              (("positive", 1), ("negative", 0), ("ignored", -1))}
    mean, std = model.feature_mean.detach().cpu().numpy(), model.feature_std.detach().cpu().numpy()
    normalized = (video.embeddings - mean) / std
    row = {"video_id": video.video_id, "group": video.group, "label": video.label,
           "prediction": prediction, "correct": prediction == video.label,
           "video_score_max_smoothed": video_score, "window_counts": counts,
           "decision_margin": video_score - config.threshold,
           "peak_window_index": int(smooth.argmax()),
           "sample_count": len(video.times), "duration_sec": video.metadata.get("duration_sec"),
           "last_sample_sec": float(video.times[-1]),
           "raw_score_distribution": distribution(raw), "smoothed_score_distribution": distribution(smooth),
           "positive_window_scores": distribution(raw[targets == 1]),
           "negative_window_scores": distribution(raw[targets == 0]),
           "ignored_window_scores": distribution(raw[targets == -1]),
           "fraction_smoothed_windows_above_threshold": float((smooth >= config.threshold).mean()),
           "fraction_normalized_values_abs_gt_5": float((np.abs(normalized) > 5).mean()),
           "normalized_rms": float(np.sqrt(np.mean(normalized.astype(float)**2))),
           "point_to_nearest_sample_sec": [float(np.abs(video.times-p).min()) for p in video.points],
           "points_after_last_sample": int(sum(p > video.times[-1] for p in video.points)),
           "bounds_sec": bounds.tolist(), "window_targets": targets.tolist(),
           "window_scores": raw.tolist(), "smoothed_window_scores": smooth.tolist(),
           "order_sensitivity": {}}
    if order_checks:
        rng = np.random.default_rng(seed)
        for name, order in (("reverse", np.arange(config.steps)[::-1]),
                            ("shuffle", rng.permutation(config.steps))):
            changed = score_windows(model, np.ascontiguousarray(x[:, order, :]))
            changed_video_score = float(smooth_scores(changed, config).max())
            row["order_sensitivity"][name] = {
                "mean_absolute_window_score_change": float(np.abs(changed-raw).mean()),
                "max_absolute_window_score_change": float(np.abs(changed-raw).max()),
                "video_score": changed_video_score, "prediction": int(changed_video_score >= config.threshold),
                "video_score_change": changed_video_score-video_score,
                "decision_margin": changed_video_score-config.threshold,
                "peak_window_index": int(smooth_scores(changed, config).argmax()),
                "decision_changed": bool((changed_video_score >= config.threshold) != bool(prediction))}
    return row


def partition_metrics(rows: list[dict], threshold: float) -> dict:
    labels = [r["label"] for r in rows]
    predictions = [r["prediction"] for r in rows]
    supervised_labels, supervised_predictions = [], []
    for row in rows:
        for label, score in zip(row["window_targets"], row["window_scores"]):
            if label >= 0:
                supervised_labels.append(label)
                supervised_predictions.append(int(score >= threshold))
    return {"video_metrics": binary_metrics(labels, predictions),
            "supervised_window_metrics": binary_metrics(supervised_labels, supervised_predictions),
            "window_counts": {name: sum(r["window_counts"][name] for r in rows)
                              for name in ("positive", "negative", "ignored")},
            "video_scores_by_label": {str(label): distribution([r["video_score_max_smoothed"] for r in rows if r["label"] == label])
                                      for label in (0, 1)},
            "order_ablation_metrics": {name: binary_metrics(labels, [r["order_sensitivity"][name]["prediction"] for r in rows])
                                       for name in ("reverse", "shuffle") if all(name in r["order_sensitivity"] for r in rows)}}


def loss_audit(model: BinaryHead, videos: list[VideoFeatures], config: DetectionConfig, *, augment: bool) -> dict:
    x, y, weights = training_arrays(videos, config, augment=augment)
    total = 0.0
    with torch.inference_mode():
        for begin in range(0, len(x), 64):
            logits = model(torch.from_numpy(x[begin:begin+64]))
            losses = F.binary_cross_entropy_with_logits(logits, torch.from_numpy(y[begin:begin+64]), reduction="none")
            total += float((losses * torch.from_numpy(weights[begin:begin+64])).double().sum())
    return {"weighted_bce_at_saved_checkpoint": total/len(x), "augmentation_included": augment,
            "effective_window_counts": {str(label): int((y == label).sum()) for label in (0, 1)},
            "weight_mass_by_label": {str(label): float(weights[y == label].sum()/weights.sum()) for label in (0, 1)},
            "weight_distribution": distribution(weights),
            "note": "Fixed saved head loss; training history loss was accumulated while weights changed."}


def simple_baselines(train: list[VideoFeatures], evaluation: list[VideoFeatures]) -> dict:
    """All learned statistics use training videos only; these are video/context baselines."""
    train_y = np.array([v.label for v in train])
    if set(train_y.tolist()) != {0, 1} or not evaluation:
        raise ValueError("Baselines require both training classes and nonempty evaluation")
    pooled_train = np.stack([v.embeddings.mean(axis=0) for v in train])
    pooled_eval = np.stack([v.embeddings.mean(axis=0) for v in evaluation])
    mean, std = pooled_train.mean(axis=0), pooled_train.std(axis=0).clip(min=1e-4)
    ztrain, zeval = (pooled_train-mean)/std, (pooled_eval-mean)/std
    centers = np.stack([ztrain[train_y == label].mean(axis=0) for label in (0, 1)])
    nearest = ((zeval[:, None]-centers[None])**2).mean(axis=2).argmin(axis=1).tolist()
    durations = np.array([v.metadata.get("duration_sec", float(v.times[-1])) for v in train])
    duration_centers = np.array([durations[train_y == label].mean() for label in (0, 1)])
    eval_durations = np.array([v.metadata.get("duration_sec", float(v.times[-1])) for v in evaluation])
    predictions = {"always_negative": [0]*len(evaluation), "always_positive": [1]*len(evaluation),
        "training_majority": [int(train_y.mean() > .5)]*len(evaluation),
        "mean_embedding_nearest_centroid": nearest,
        "duration_nearest_centroid": np.abs(eval_durations[:, None]-duration_centers).argmin(axis=1).tolist()}
    return {name: {"metrics": binary_metrics([v.label for v in evaluation], values),
                   "predictions": dict(zip([v.video_id for v in evaluation], values))}
            for name, values in predictions.items()}


def threshold_audit(rows: list[dict]) -> dict:
    """Exploratory validation curve, never a selected/deployed threshold or test tuning."""
    labels = [r["label"] for r in rows]
    scores = np.array([r["video_score_max_smoothed"] for r in rows])
    bins = []
    for i in range(5):
        lo, hi = i/5, (i+1)/5
        selected = (scores >= lo) & ((scores <= hi) if i == 4 else (scores < hi))
        bins.append({"lower": lo, "upper": hi, "count": int(selected.sum()),
                     "mean_score": float(scores[selected].mean()) if selected.any() else None,
                     "positive_fraction": float(np.array(labels)[selected].mean()) if selected.any() else None})
    return {"scope": "exploratory_validation_only; no threshold chosen or checkpoint updated",
            "brier_score_uncalibrated_max_score": float(np.mean((scores-np.array(labels))**2)),
            "reliability_bins": bins,
            "curve": [{"threshold": t, "metrics": binary_metrics(labels, (scores >= t).astype(int).tolist())}
                      for t in (.05, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95)]}


def duration_score_audit(rows: list[dict]) -> dict:
    """Descriptive correlations only: fit no thresholds and make no causal claim."""
    def summarize(selected):
        durations = [r["duration_sec"] if r["duration_sec"] is not None else r["last_sample_sec"] for r in selected]
        scores = [r["video_score_max_smoothed"] for r in selected]
        counts = [sum(r["window_counts"].values()) for r in selected]
        def correlation(values):
            if len(values) < 3 or np.std(values) < 1e-12 or np.std(scores) < 1e-12:
                return None
            return float(np.corrcoef(values, scores)[0, 1])
        return {"videos": len(selected), "duration_distribution_sec": distribution(durations),
                "window_count_distribution": distribution(counts),
                "pearson_duration_vs_max_score": correlation(durations),
                "pearson_window_count_vs_max_score": correlation(counts)}
    return {"note": "Descriptive only; duration and number of max-pooling opportunities can correlate with scores. Small samples and scene confounding prevent causal conclusions.",
            "all_labels": summarize(rows), "by_label": {str(label): summarize([r for r in rows if r["label"] == label])
                                                         for label in (0, 1)}}


def model_audit(videos: list[VideoFeatures], model: BinaryHead, config: DetectionConfig, training: dict, seed: int) -> dict:
    if dataset_fingerprint(videos) != training.get("dataset_fingerprint"):
        return {"status": "incompatible_cache", "reason": "checkpoint_dataset_fingerprint_mismatch"}
    validate_split(videos, training["split"])
    if any(v.metadata["sample_fps"] != config.sample_fps for v in videos):
        return {"status": "incompatible_cache", "reason": "sampling_rate_mismatch"}
    selected = {name: [v for v in videos if v.video_id in training["split"][name]] for name in ("train", "validation")}
    # Test is intentionally not scored by this development diagnostic.
    rows = {name: [score_video(model, v, config, seed=seed) for v in subset] for name, subset in selected.items()}
    mean = np.mean([v.embeddings.mean(axis=0) for v in selected["train"]], axis=0)
    var = np.mean([((v.embeddings-mean)**2).mean(axis=0) for v in selected["train"]], axis=0)
    std = np.sqrt(var).clip(min=1e-4)
    mean_error = float(np.abs(model.feature_mean.numpy()-mean).max())
    std_error = float(np.abs(model.feature_std.numpy()-std).max())
    return {"status": "ok", "kind": model.kind, "config": asdict(config), "split": training["split"],
            "test_scored": False, "validation_used_for_early_stopping": True,
            "training_evidence": {k: training.get(k) for k in ("completed", "gradient_seen", "weights_changed", "best_epoch", "history", "seed")},
            "normalization": {"max_mean_error_vs_train_only": mean_error, "max_std_error_vs_train_only": std_error,
                "matches_train_only": bool(np.allclose(model.feature_mean.numpy(), mean, atol=1e-5)
                                            and np.allclose(model.feature_std.numpy(), std, atol=1e-5)),
                "std_at_floor_dimensions": int((model.feature_std.numpy() <= 1.0001e-4).sum())},
            "partitions": {name: {**partition_metrics(rows[name], config.threshold), "videos": rows[name],
                                  "loss": loss_audit(model, subset, config, augment=name == "train")}
                           for name, subset in selected.items()},
            "validation_baselines": simple_baselines(selected["train"], selected["validation"]),
            "duration_score_audit": {name: duration_score_audit(partition) for name, partition in rows.items()},
            "validation_threshold_audit": threshold_audit(rows["validation"])}


def compare_models(reports: dict[str, dict]) -> dict:
    if any(report.get("status") != "ok" for report in reports.values()) or len(reports) != 2:
        return {"status": "not_comparable"}
    first, second = list(reports.values())
    a = {r["video_id"]: r for r in first["partitions"]["validation"]["videos"]}
    b = {r["video_id"]: r for r in second["partitions"]["validation"]["videos"]}
    common = sorted(a.keys() & b.keys())
    return {"status": "ok" if common else "no_common_validation_videos",
            "same_validation_set": a.keys() == b.keys(), "common_validation_videos": len(common),
            "identical_predictions_on_common_validation": all(a[i]["prediction"] == b[i]["prediction"] for i in common) if common else None,
            "mean_absolute_video_score_difference": float(np.mean([abs(a[i]["video_score_max_smoothed"]-b[i]["video_score_max_smoothed"]) for i in common])) if common else None,
            "per_video": [{"video_id": i, "label": a[i]["label"], "predictions": [a[i]["prediction"], b[i]["prediction"]],
                           "both_wrong": not a[i]["correct"] and not b[i]["correct"]} for i in common]}


def similarity_audit(videos: list[VideoFeatures], reports: dict[str, dict]) -> dict:
    # No test-video similarity inspection during development diagnostics.
    test_ids = {i for report in reports.values() if report.get("status") == "ok" for i in report["split"]["test"]}
    allowed = [v for v in videos if v.video_id not in test_ids]
    vectors = {v.video_id: v.embeddings.mean(axis=0).astype(float) for v in allowed}
    signatures = {v.video_id: hashlib.sha256(v.embeddings.tobytes()+v.times.tobytes()).hexdigest() for v in allowed}
    pairs = []
    for a, b in combinations(allowed, 2):
        denominator = np.linalg.norm(vectors[a.video_id])*np.linalg.norm(vectors[b.video_id])
        cosine = float(np.dot(vectors[a.video_id], vectors[b.video_id])/denominator) if denominator else None
        crosses = {name: ((a.video_id in r["split"]["train"] and b.video_id in r["split"]["validation"])
                          or (b.video_id in r["split"]["train"] and a.video_id in r["split"]["validation"]))
                   for name, r in reports.items() if r.get("status") == "ok"}
        pairs.append({"video_ids": [a.video_id, b.video_id], "cosine_mean_features": cosine,
                      "identical_feature_arrays": signatures[a.video_id] == signatures[b.video_id],
                      "same_declared_group": a.group == b.group, "crosses_train_validation": crosses})
    pairs.sort(key=lambda p: p["cosine_mean_features"] if p["cosine_mean_features"] is not None else -2, reverse=True)
    return {"note": "Similarity is a review cue, not proof of duplicate scenarios or identities; no faces/video decoded.",
            "fallback_video_groups": sum(v.group.startswith("video:") for v in allowed),
            "explicit_scenario_groups": len({v.group for v in allowed if v.group.startswith("group:")}),
            "closest_pairs": pairs[:20], "identical_feature_pairs": [p for p in pairs if p["identical_feature_arrays"]]}


def group_folds(videos: list[VideoFeatures], requested: int, seed: int) -> list[list[str]] | None:
    """Disjoint outer folds with both classes; reduce k if needed, never split a group."""
    if requested < 2:
        return None
    groups = sorted({v.group for v in videos})
    rng = np.random.default_rng(seed)
    for k in range(min(requested, len(groups)), 1, -1):
        for _ in range(1000):
            chunks = np.array_split(rng.permutation(groups), k)
            folds = [[v.video_id for v in videos if v.group in set(chunk.tolist())] for chunk in chunks]
            if all({v.label for v in videos if v.video_id in fold} == {0, 1} for fold in folds):
                return folds
    return None


def cv_pool(videos: list[VideoFeatures], reports: dict[str, dict]) -> list[VideoFeatures]:
    if not reports or any(r.get("status") != "ok" for r in reports.values()):
        return []
    common = set.intersection(*(set(r["split"]["train"]) for r in reports.values()))
    excluded_groups = {v.group for v in videos if v.video_id not in common}
    return [v for v in videos if v.video_id in common and v.group not in excluded_groups]


def grouped_cv(videos: list[VideoFeatures], reports: dict[str, dict], *, folds: int,
               epochs: int = 20, seed: int = 2026) -> dict:
    if folds == 0:
        return {"status": "not_requested", "training_performed": False}
    pool = cv_pool(videos, reports)
    plan = group_folds(pool, folds, seed)
    if not plan:
        return {"status": "infeasible", "training_performed": False,
                "reason": "insufficient_independent_training_groups_with_both_classes",
                "eligible_videos": len(pool)}
    # Preflight all inner splits before training any head.
    nested = []
    for index, holdout in enumerate(plan):
        outer_train = [v for v in pool if v.video_id not in holdout]
        inner = None
        for fraction in (.2, .33, .5):
            try:
                inner = split_videos(outer_train, seed=seed+index, val_fraction=fraction, test_fraction=0)
                validate_split(outer_train, inner)
                break
            except ValueError:
                continue
        if inner is None:
            return {"status": "infeasible", "training_performed": False,
                    "reason": "cannot_reserve_group_disjoint_inner_validation_for_early_stopping", "eligible_videos": len(pool)}
        nested.append({"train": inner["train"], "early_stopping": inner["validation"], "outer_holdout": holdout})
    results, out_of_fold = [], {}
    for index, partition in enumerate(nested):
        train = [v for v in pool if v.video_id in partition["train"]]
        early = [v for v in pool if v.video_id in partition["early_stopping"]]
        heldout = [v for v in pool if v.video_id in partition["outer_holdout"]]
        fold_result = {"fold": index, "partition": partition, "models": {}, "baselines": simple_baselines(train, heldout)}
        for name, report in reports.items():
            config = DetectionConfig(**report["config"])
            model, training = fit_head(train, early, config, kind=report["kind"], epochs=epochs,
                                      patience=min(5, epochs), seed=seed+index, device="cpu")
            rows = [score_video(model, v, config, seed=seed, order_checks=False) for v in heldout]
            fold_result["models"][name] = {"metrics": binary_metrics([r["label"] for r in rows], [r["prediction"] for r in rows]),
                                          "best_epoch": training["best_epoch"], "videos": rows}
            out_of_fold.setdefault(name, []).extend(rows)
        results.append(fold_result)
    return {"status": "ok", "training_performed": True, "checkpoints_written": False,
            "scope": "exploratory_CV_on_common_original_training_groups_only; not an independent final test",
            "requested_folds": folds, "effective_folds": len(plan), "epochs": epochs, "seed": seed,
            "eligible_video_ids": [v.video_id for v in pool], "folds": results,
            "out_of_fold_metrics": {name: binary_metrics([r["label"] for r in rows], [r["prediction"] for r in rows])
                                    for name, rows in out_of_fold.items()},
            "out_of_fold_baselines": {name: binary_metrics([v.label for v in pool],
                [next(f["baselines"][name]["predictions"][v.video_id] for f in results
                      if v.video_id in f["baselines"][name]["predictions"]) for v in pool])
                for name in results[0]["baselines"]}}


def diagnose(features: Path, checkpoints: dict[str, Path], private_root: Path,
             *, cv_folds: int = 0, cv_epochs: int = 20, seed: int = 2026) -> dict:
    if cv_folds < 0 or cv_folds == 1 or cv_epochs < 1:
        raise ValueError("CV folds must be zero or at least two; epochs must be positive")
    started = time.perf_counter()
    videos = load_features(features, private_root)
    torch.set_num_threads(4)
    reports = {}
    for name, path in checkpoints.items():
        model, config, training = load_checkpoint(private_path(path, private_root))
        if name not in {"logistic", "temporal"} or model.kind != name:
            raise ValueError("Checkpoint kind does not match the diagnostic argument")
        reports[name] = model_audit(videos, model, config, training, seed)
    compatible = all(r.get("status") == "ok" for r in reports.values())
    return {"schema_version": 1, "status": "ok" if compatible else "incompatible_checkpoint_or_cache",
            "private_local_report": True, "source_videos_opened": False,
            "notes": ["No real-data quality claims until this command is run locally.",
                      "Validation participates in early stopping; five videos give 20 percentage-point accuracy steps.",
                      "Equal accuracy/F1 does not imply identical per-video predictions.",
                      "Window BCE, balanced class weights, and max-over-windows video scoring have different objectives.",
                      "Max-smoothed scores are not calibrated probabilities. Threshold curves are exploratory only.",
                      "Reversal/shuffle sensitivity is not proof of learning the correct banking procedure."],
            "inventory": {"videos": len(videos), "positive": sum(v.label == 1 for v in videos),
                          "negative": sum(v.label == 0 for v in videos), "groups": len({v.group for v in videos}),
                          "exact_source_duplicates": "rejected_by_feature_loader"},
            "models": reports, "comparison": compare_models(reports),
            "scenario_similarity": similarity_audit(videos, reports) if compatible else {"status": "skipped_incompatible_inputs"},
            "cross_validation": grouped_cv(videos, reports, folds=cv_folds, epochs=cv_epochs, seed=seed),
            "diagnostic_sec": time.perf_counter()-started}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", default="private_data")
    parser.add_argument("--features", required=True)
    parser.add_argument("--logistic-checkpoint", required=True)
    parser.add_argument("--temporal-checkpoint", required=True)
    parser.add_argument("--output", required=True, help="New PRIVATE directory containing report.json")
    parser.add_argument("--cv-folds", type=int, default=0, help="0 disables training; >=2 explicitly enables nested group CV")
    parser.add_argument("--cv-epochs", type=int, default=20)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    output = private_path(args.output, args.private_root)
    features = private_path(args.features, args.private_root)
    checkpoints = {name: private_path(getattr(args, name+"_checkpoint"), args.private_root) for name in ("logistic", "temporal")}
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    try:
        report = diagnose(features, checkpoints, Path(args.private_root),
                          cv_folds=args.cv_folds, cv_epochs=args.cv_epochs, seed=args.seed)
    except Exception as error:
        # Full detail is allowed only in the explicitly requested local report, never stdout/stderr.
        write_json(output / "report.json", {"status": "diagnostic_failed", "private_local_report": True,
                   "error_type": type(error).__name__, "local_error_detail": str(error)})
        print("Local diagnostic failed; inspect the local report. Do not share private reports.")
        return 2
    write_json(output / "report.json", report)
    print("Local diagnostic report saved. No source videos opened; do not share private reports.")
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    run_cli(main)
