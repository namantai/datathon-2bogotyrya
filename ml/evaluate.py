"""Video-level held-out evaluation. No threshold selection on the test split."""
from __future__ import annotations

import argparse
from dataclasses import replace
import time
from pathlib import Path

import numpy as np

from ml.augment import PROFILES, PerturbedEncoder
from ml.feature_data import dataset_fingerprint, load_features, validate_split
from ml.local_io import private_path, run_cli, write_json
from ml.temporal import detect_events, load_checkpoint, score_windows, windows


def binary_metrics(labels: list[int], predictions: list[int]) -> dict:
    if not labels or len(labels) != len(predictions) or not set(labels + predictions) <= {0, 1}:
        raise ValueError("Invalid metric inputs")
    y, p = np.array(labels), np.array(predictions)
    tp, tn = int(((y == 1) & (p == 1)).sum()), int(((y == 0) & (p == 0)).sum())
    fp, fn = int(((y == 0) & (p == 1)).sum()), int(((y == 1) & (p == 0)).sum())
    precision = tp/(tp+fp) if tp+fp else 0.0
    recall = tp/(tp+fn) if tp+fn else 0.0
    return {"accuracy": (tp+tn)/len(y), "precision": precision, "recall": recall,
            "f1": 2*precision*recall/(precision+recall) if precision+recall else 0.0,
            "false_positives": fp, "false_negatives": fn,
            "confusion_matrix": [[tn, fp], [fn, tp]], "confusion_order": "rows=true, columns=predicted; labels=[0,1]",
            "videos": len(y)}


def localization(points: list[float], detections: list[float]) -> dict:
    # One-to-one greedy matching, report unmatched points/events; never hide missed events in MAE.
    pairs = sorted((abs(p-d), i, j) for i, p in enumerate(points) for j, d in enumerate(detections))
    used_points, used_detections, errors = set(), set(), []
    for error, i, j in pairs:
        if i not in used_points and j not in used_detections:
            used_points.add(i)
            used_detections.add(j)
            errors.append(error)
    return {"absolute_errors_sec": errors, "matched_events": len(errors),
            "unmatched_references": len(points)-len(errors), "unmatched_detections": len(detections)-len(errors)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", default="private_data")
    parser.add_argument("--features", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--split", choices=["validation", "test"], default="validation")
    parser.add_argument("--threshold", type=float, help="Validation only; freeze before reporting test results")
    parser.add_argument("--end-to-end", action="store_true", help="Decode held-out originals, encode and save evidence locally")
    parser.add_argument("--model-dir", default="private_data/weights/dinov2-small")
    parser.add_argument("--device", choices=["auto", "cpu", "mps"], default="auto")
    parser.add_argument("--perturbation", choices=PROFILES, default="none",
                        help="Requires end-to-end; original frames are retained as evidence")
    args = parser.parse_args()
    output = private_path(args.output, args.private_root)
    if output.exists():
        raise FileExistsError("Output exists")
    if args.perturbation != "none" and not args.end_to_end:
        raise ValueError("Spatial stress tests require --end-to-end")
    checkpoint = private_path(args.checkpoint, args.private_root)
    model, config, training = load_checkpoint(checkpoint)
    if args.threshold is not None:
        if args.split == "test":
            raise ValueError("Test threshold must be frozen in checkpoint")
        config = replace(config, threshold=args.threshold)
    videos = load_features(Path(args.features), Path(args.private_root))
    if dataset_fingerprint(videos) != training["dataset_fingerprint"]:
        raise ValueError("Dataset differs from checkpoint; cannot certify held-out evaluation")
    validate_split(videos, training["split"])
    selected = [v for v in videos if v.video_id in training["split"][args.split]]
    if not selected:
        raise ValueError("Requested evaluation split is empty; no test holdout was reserved")
    analyzer, load_sec = None, None
    if args.end_to_end:
        from ml.inference import VideoAnalyzer
        started = time.perf_counter()
        analyzer = VideoAnalyzer(checkpoint, Path(args.model_dir), private_root=Path(args.private_root),
                                 device=args.device, threshold=config.threshold)
        if args.perturbation != "none":
            analyzer.encoder = PerturbedEncoder(analyzer.encoder, args.perturbation)
        load_sec = time.perf_counter()-started
    rows, errors, unmatched_ref, unmatched_det = [], [], 0, 0
    for video in selected:
        started = time.perf_counter()
        if analyzer:
            from ml.backbone import sha256_file
            path = private_path(video.metadata["video_path"], args.private_root)
            if sha256_file(path) != video.digest:
                raise ValueError("Held-out source video changed")
            events = analyzer.analyze(str(path))
        else:
            x, bounds = windows(video.embeddings, video.times, config)
            events = detect_events(score_windows(model, x), bounds, config)
            for event in events:
                start, end = bounds[event["window_index"]]
                candidates = np.flatnonzero((video.times >= start) & (video.times <= end))
                nearest = candidates[np.abs(video.times[candidates]-event["timecode_sec"]).argmin()]
                event["timecode_sec"] = float(video.times[nearest])
        elapsed = time.perf_counter()-started
        match = localization(video.points, [e["timecode_sec"] for e in events])
        errors.extend(match["absolute_errors_sec"])
        unmatched_ref += match["unmatched_references"]
        unmatched_det += match["unmatched_detections"]
        rows.append({"video_id": video.video_id, "label": video.label, "prediction": int(bool(events)),
                     "detections_sec": [e["timecode_sec"] for e in events], "processing_sec": elapsed,
                     "localization": match})
    report = {"split": args.split, "used_for_early_stopping": args.split == "validation",
              "mode": "complete_local_inference" if analyzer else "cached_features_head_only",
              "includes_http_and_backend_queue": False, "model_load_sec": load_sec,
              "config": vars(config), "perturbation": args.perturbation,
              "metrics": binary_metrics([r["label"] for r in rows], [r["prediction"] for r in rows]),
              "localization": {"matching": "greedy_one_to_one_no_tolerance", "matched_events": len(errors),
                  "mean_absolute_error_sec": float(np.mean(errors)) if errors else None,
                  "median_absolute_error_sec": float(np.median(errors)) if errors else None,
                  "unmatched_references": unmatched_ref, "unmatched_detections": unmatched_det},
              "total_processing_sec": sum(r["processing_sec"] for r in rows), "videos": rows}
    write_json(output, report)
    print("Held-out local evaluation saved. Cached-feature timing is not complete inference latency.")


if __name__ == "__main__":
    run_cli(main)
