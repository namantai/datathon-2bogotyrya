"""Train a binary logistic baseline or a small temporal head on LOCAL frozen features."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import random
import time
import warnings

import numpy as np
import torch
from torch.nn import functional as F

from ml.feature_data import VideoFeatures, dataset_fingerprint, load_features, split_videos, validate_split
from ml.local_io import positive_float, private_path, run_cli, write_json
from ml.temporal import BinaryHead, DetectionConfig, save_checkpoint, window_labels, windows


def training_arrays(videos: list[VideoFeatures], config: DetectionConfig, *,
                    augment: bool = False) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    xs, ys, ws = [], [], []
    class_counts = {label: sum(v.label == label for v in videos) for label in (0, 1)}
    if not all(class_counts.values()):
        raise ValueError("Both labels are required")
    for video in videos:
        x, bounds = windows(video.embeddings, video.times, config)
        labels = window_labels(bounds, video.label, video.points)
        if augment and video.brightness_embeddings is not None:
            variant, _ = windows(video.brightness_embeddings, video.times, config)
            x = np.concatenate((x, variant))
            labels = np.tile(labels, 2)
        selected = labels >= 0
        count = int(selected.sum())
        if not count:
            raise ValueError("Video has no supervised windows")
        xs.append(x[selected])
        ys.append(labels[selected])
        # Equal total weight per class and per video within a class, not per duration.
        ws.append(np.full(count, 1 / (class_counts[video.label] * count), dtype=np.float32))
    weights = np.concatenate(ws)
    weights /= weights.mean()
    return np.concatenate(xs), np.concatenate(ys).astype(np.float32), weights


def fit_head(train: list[VideoFeatures], validation: list[VideoFeatures], config: DetectionConfig,
             *, kind: str = "logistic", epochs: int = 60, batch_size: int = 32,
             learning_rate: float = .001, patience: int = 10, seed: int = 2026,
             device: str = "cpu") -> tuple[BinaryHead, dict]:
    if (epochs < 1 or batch_size < 1 or patience < 1 or not np.isfinite(learning_rate)
            or learning_rate <= 0 or device not in {"cpu", "auto", "mps"}):
        raise ValueError("Invalid training parameters")
    if ({v.video_id for v in train} & {v.video_id for v in validation}
            or {v.digest for v in train} & {v.digest for v in validation}
            or {v.group for v in train} & {v.group for v in validation}):
        raise ValueError("Training/validation leakage")
    for video in train + validation:
        if video.metadata["sample_fps"] != config.sample_fps:
            raise ValueError("Sampling rate differs from training configuration")
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.set_num_threads(4)
    actual_device = "mps" if device != "cpu" and torch.backends.mps.is_available() else "cpu"
    if device == "mps" and actual_device == "cpu":
        warnings.warn("MPS unavailable; training on CPU", RuntimeWarning)
    x, y, weights = training_arrays(train, config, augment=True)
    vx, vy, vw = training_arrays(validation, config)
    model = BinaryHead(kind)
    # Fit normalization only on training videos, giving each video equal influence.
    mean = np.mean([v.embeddings.mean(axis=0) for v in train], axis=0)
    variance = np.mean([((v.embeddings - mean) ** 2).mean(axis=0) for v in train], axis=0)
    model.feature_mean.copy_(torch.from_numpy(mean))
    model.feature_std.copy_(torch.from_numpy(np.sqrt(variance).clip(min=1e-4)))
    model.to(actual_device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=.01)
    generator = np.random.default_rng(seed)
    initial = {name: p.detach().cpu().clone() for name, p in model.named_parameters()}
    best_loss, best_state, stale, best_epoch = float("inf"), None, 0, 0
    history, gradient_seen = [], False
    for epoch in range(epochs):
        model.train()
        order = generator.permutation(len(x))
        total = 0.0
        for begin in range(0, len(order), batch_size):
            idx = order[begin:begin+batch_size]
            inputs = torch.from_numpy(x[idx]).to(actual_device)
            targets = torch.from_numpy(y[idx]).to(actual_device)
            w = torch.from_numpy(weights[idx]).to(actual_device)
            optimizer.zero_grad(set_to_none=True)
            loss = (F.binary_cross_entropy_with_logits(model(inputs), targets, reduction="none") * w).mean()
            if not torch.isfinite(loss):
                raise ValueError("Nonfinite training loss")
            loss.backward()
            norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0, error_if_nonfinite=True)
            gradient_seen |= bool(norm.item() > 0)
            optimizer.step()
            total += float(loss.item()) * len(idx)
        model.eval()
        val_total = 0.0
        with torch.inference_mode():
            for begin in range(0, len(vx), batch_size):
                logits = model(torch.from_numpy(vx[begin:begin+batch_size]).to(actual_device))
                loss = F.binary_cross_entropy_with_logits(logits,
                    torch.from_numpy(vy[begin:begin+batch_size]).to(actual_device), reduction="none")
                val_total += float((loss * torch.from_numpy(vw[begin:begin+batch_size]).to(actual_device)).sum().item())
        val_loss = val_total / len(vx)
        if not np.isfinite(val_loss):
            raise ValueError("Nonfinite validation loss")
        history.append({"epoch": epoch+1, "train_loss": total/len(x), "validation_loss": val_loss})
        if val_loss < best_loss - 1e-6:
            best_loss, stale, best_epoch = val_loss, 0, epoch+1
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        else:
            stale += 1
            if stale >= patience:
                break
    if best_state is None or not gradient_seen:
        raise ValueError("Head did not receive gradients")
    model.to("cpu").load_state_dict(best_state)
    changed = any(not torch.equal(initial[name], p.detach()) for name, p in model.named_parameters())
    if not changed:
        raise ValueError("Head weights did not change")
    return model.eval(), {"completed": True, "device": actual_device, "gradient_seen": gradient_seen,
        "weights_changed": changed, "best_epoch": best_epoch, "history": history,
        "train_windows": len(x), "validation_windows": len(vx), "seed": seed,
        "epochs_requested": epochs, "batch_size": batch_size, "learning_rate": learning_rate,
        "patience": patience, "torch_version": str(torch.__version__),
        "brightness_augmentation_train_videos": sum(v.brightness_embeddings is not None for v in train),
        "supervision": "positive_point_windows_only; negative_video_windows; other_positive_windows_unknown"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", default="private_data")
    parser.add_argument("--features", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--split", help="Reuse the baseline split.json for fair temporal comparison")
    parser.add_argument("--kind", choices=["logistic", "temporal"], default="logistic")
    parser.add_argument("--device", choices=["auto", "cpu", "mps"], default="cpu",
                        help="Small head defaults to CPU; backbone extraction uses auto/MPS")
    parser.add_argument("--epochs", type=int, default=60)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--learning-rate", type=positive_float, default=.001)
    parser.add_argument("--patience", type=int, default=10)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--val-fraction", type=float, default=.2)
    parser.add_argument("--test-fraction", type=float, default=.2)
    parser.add_argument("--sample-fps", type=positive_float, default=2)
    parser.add_argument("--window-sec", type=positive_float, default=4)
    parser.add_argument("--stride-sec", type=positive_float, default=1)
    parser.add_argument("--threshold", type=float, default=.5)
    parser.add_argument("--smooth-windows", type=int, default=3)
    parser.add_argument("--merge-gap-sec", type=float, default=0)
    args = parser.parse_args()
    output = private_path(args.output, args.private_root)
    if output.exists():
        raise FileExistsError("Output exists")
    videos = load_features(Path(args.features), Path(args.private_root))
    fingerprint = dataset_fingerprint(videos)
    if args.split:
        saved = json.loads(private_path(args.split, args.private_root).read_text())
        if saved["dataset_fingerprint"] != fingerprint:
            raise ValueError("Dataset changed since split creation")
        split = saved["split"]
    else:
        split = split_videos(videos, args.seed, args.val_fraction, args.test_fraction)
    validate_split(videos, split)
    config = DetectionConfig(args.sample_fps, args.window_sec, args.stride_sec,
                             args.threshold, args.smooth_windows, args.merge_gap_sec)
    started = time.perf_counter()
    fit_args = dict(kind=args.kind, epochs=args.epochs, batch_size=args.batch_size,
                    learning_rate=args.learning_rate, patience=args.patience, seed=args.seed, device=args.device)
    train = [v for v in videos if v.video_id in split["train"]]
    validation = [v for v in videos if v.video_id in split["validation"]]
    try:
        model, training = fit_head(train, validation, config, **fit_args)
    except (RuntimeError, NotImplementedError) as error:
        if args.device == "cpu" or "mps" not in str(error).lower():
            raise
        warnings.warn("MPS training failed; restarting head training on CPU with same seed", RuntimeWarning)
        fit_args["device"] = "cpu"
        model, training = fit_head(train, validation, config, **fit_args)
        training["mps_fallback"] = True
    training.update({"split": split, "dataset_fingerprint": fingerprint,
                     "identities": [v.identity() for v in videos],
                     "training_sec": time.perf_counter()-started, "detection_config": asdict(config)})
    output.mkdir(parents=True)
    save_checkpoint(output / "head.pt", model, config, training)
    write_json(output / "split.json", {"dataset_fingerprint": fingerprint, "split": split})
    write_json(output / "training.json", training)
    print("Local classifier trained and saved. Held-out test was not used for optimization.")


if __name__ == "__main__":
    run_cli(main)
