"""Extract complete-video frozen DINOv2 embeddings locally, in bounded batches."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import numpy as np

from ml.backbone import DinoV2Encoder, MODEL_ID, MODEL_REVISION, PREPROCESS_VERSION, sha256_file
from ml.local_io import positive_float, private_path, run_cli, write_json
from ml.preprocess import probe_video, sample_video


def extract_video(video: Path, output: Path, encoder: DinoV2Encoder, *, sample_fps: float = 2,
                  batch_size: int = 4, timeout_sec: float = 240, augment_brightness: bool = False) -> dict:
    if batch_size < 1:
        raise ValueError("Batch size must be positive")
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    before = video.stat()
    duration = probe_video(video)["duration_sec"]
    features, metadata, batch, augmented = [], [], [], []

    def encode_batch() -> None:
        features.append(encoder.encode(batch))
        if augment_brightness:
            from ml.augment import perturb_rgb
            augmented.append(encoder.encode([perturb_rgb(image, "brightness") for image in batch]))
        batch.clear()

    for frame in sample_video(video, sample_fps, timeout_sec):
        metadata.append(frame.metadata())
        batch.append(frame.rgb)
        if len(batch) == batch_size:
            encode_batch()
    if batch:
        encode_batch()
    if not features:
        raise ValueError("No sampled frames")
    if time.perf_counter() - started > timeout_sec:
        raise RuntimeError("Feature extraction deadline exceeded")
    video_sha256 = sha256_file(video)
    after = video.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Input changed during extraction")
    embeddings = np.concatenate(features, axis=0).astype(np.float32)
    if embeddings.shape != (len(metadata), 384) or not np.isfinite(embeddings).all():
        raise ValueError("Unexpected embedding shape")
    extra = {"brightness_embeddings": np.concatenate(augmented).astype(np.float32)} if augmented else {}
    if extra and (extra["brightness_embeddings"].shape != embeddings.shape
                  or not np.isfinite(extra["brightness_embeddings"]).all()):
        raise ValueError("Invalid augmented features")
    np.savez_compressed(output / "features.npz", embeddings=embeddings, **extra,
        timestamps_sec=np.array([m["timestamp_sec"] for m in metadata], dtype=np.float64),
        frame_indices=np.array([m["index"] for m in metadata], dtype=np.int64),
        pts=np.array([m["pts"] for m in metadata], dtype=np.int64))
    result = {"schema_version": 1, "video_sha256": video_sha256, "video_path": str(video.resolve()),
              "model_id": MODEL_ID, "model_revision": MODEL_REVISION, "weight_sha256": encoder.weight_sha256,
              "preprocess": PREPROCESS_VERSION, "sample_fps": sample_fps, "batch_size": batch_size,
              "embedding_kind": "last_hidden_state_cls", "embedding_dim": 384,
              "device": encoder.device, "cpu_fallback_used": encoder.fallback_used,
              "processing_sec": time.perf_counter() - started,
              "duration_sec": duration, "augmentations": ["brightness"] if augment_brightness else [],
              "timestamp_origin": "first_decoded_video_frame", "frames": metadata}
    if time.perf_counter() - started > timeout_sec:
        raise RuntimeError("Feature extraction deadline exceeded")
    write_json(output / "metadata.json", result)  # Completion marker, absent for interrupted jobs.
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", default="private_data")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--video")
    source.add_argument("--manifest")
    parser.add_argument("--output", required=True)
    parser.add_argument("--model-dir", default="private_data/weights/dinov2-small")
    parser.add_argument("--device", choices=["auto", "cpu", "mps"], default="auto")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--sample-fps", type=positive_float, default=2)
    parser.add_argument("--timeout-sec", type=positive_float, default=240)
    parser.add_argument("--augment-brightness", action="store_true",
                        help="Cache deterministic mild brightness variant; used only in training split")
    parser.add_argument("--allow-unlabeled", action="store_true",
                        help="Explicitly exclude missing labels; all other validation issues still fail")
    args = parser.parse_args()
    output = private_path(args.output, args.private_root)
    if output.exists():
        raise FileExistsError("Output exists")
    if args.batch_size < 1:
        raise ValueError("Invalid batch size")
    if args.video:
        videos = [{"video_path": str(private_path(args.video, args.private_root)), "video_id": None}]
    else:
        manifest = json.loads(private_path(args.manifest, args.private_root).read_text())
        allowed_missing = (args.allow_unlabeled and manifest.get("issue_codes") == ["missing_label"])
        if not manifest["validation_ok"] and not allowed_missing:
            raise ValueError("Resolve validation issues before batch extraction")
        videos = [record for record in manifest["videos"] if record["eligible"]]
    if not videos:
        raise ValueError("No eligible videos")
    # Validate every path before loading the backbone or touching any video.
    paths = [private_path(record["video_path"], args.private_root) for record in videos]
    encoder = DinoV2Encoder(Path(args.model_dir), device=args.device, cpu_threads=args.cpu_threads)
    if args.video:
        extract_video(paths[0], output, encoder, sample_fps=args.sample_fps,
                      batch_size=args.batch_size, timeout_sec=args.timeout_sec, augment_brightness=args.augment_brightness)
    else:
        output.mkdir(parents=True)
        index = []
        for number, (record, path) in enumerate(zip(videos, paths)):
            directory = f"video_{number:05d}"
            extract_video(path, output / directory, encoder, sample_fps=args.sample_fps,
                          batch_size=args.batch_size, timeout_sec=args.timeout_sec, augment_brightness=args.augment_brightness)
            index.append({"video_id": record["video_id"], "directory": directory,
                          "label": record["label"], "timestamps_sec": record["timestamps_sec"],
                          "group_id": record["group_id"]})
        write_json(output / "index.json", {"schema_version": 1, "videos": index})
    print("Local frozen-backbone embeddings saved. No classifier has been trained.")


if __name__ == "__main__":
    run_cli(main)
