"""Offline binary full-video detector. Exceptions must become failed jobs, never clean verdicts."""
from __future__ import annotations

import argparse
from dataclasses import replace
from functools import lru_cache
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import uuid

import numpy as np
from PIL import Image

from ml.backbone import DinoV2Encoder, sha256_file, MODEL_ID, MODEL_REVISION, PREPROCESS_VERSION
from ml.extract_features import extract_video
from ml.local_io import positive_float, private_path, run_cli, write_json
from ml.preprocess import sample_video
from ml.temporal import detect_events, load_checkpoint, score_windows, windows, score_diagnostics

# Proposed internal binary type; not an organizer class or verified legal finding.
BINARY_EVENT_TYPE = "possible_violation"
_INITIALIZE_LOCK = threading.Lock()
DESCRIPTION = ("Обнаружены признаки возможного нарушения кассовой дисциплины. "
               "Тип события и нормативное основание требуют проверки.")


class VideoAnalyzer:
    def __init__(self, checkpoint: Path, model_dir: Path, *, private_root: Path = Path("private_data"),
                 evidence_root: Path | None = None, device: str = "auto", batch_size: int = 4,
                 timeout_sec: float = 240, evidence_count: int = 3, threshold: float | None = None,
                 input_root: Path | None = None, runtime_root: Path | None = None,
                 required_kind: str | None = None):
        self.private_root = private_root.expanduser().resolve()
        self.input_root = (input_root or self.private_root).expanduser().resolve()
        checkpoint = private_path(checkpoint, self.private_root)
        self.evidence_root = private_path(evidence_root or self.private_root / "evidence", runtime_root or self.private_root)
        if batch_size < 1 or evidence_count not in (1, 2, 3) or not np.isfinite(timeout_sec) or timeout_sec <= 0:
            raise ValueError("Invalid inference settings")
        checkpoint_hash = sha256_file(checkpoint)
        self.model, self.config, self.training = load_checkpoint(checkpoint)
        if sha256_file(checkpoint) != checkpoint_hash:
            raise ValueError("Checkpoint changed while loading")
        if required_kind is not None and self.model.kind != required_kind:
            raise ValueError("Checkpoint classifier kind differs from required runtime kind")
        if threshold is not None:
            self.config = replace(self.config, threshold=threshold)
        self.encoder = DinoV2Encoder(model_dir, device=device)
        state_hash = hashlib.sha256()
        for name, value in sorted(self.model.state_dict().items()):
            state_hash.update(name.encode())
            state_hash.update(value.detach().cpu().numpy().tobytes())
        self.identity = {"checkpoint_sha256": checkpoint_hash, "head_state_sha256": state_hash.hexdigest(),
            "classifier_kind": self.model.kind, "model_id": MODEL_ID, "model_revision": MODEL_REVISION,
            "preprocess": PREPROCESS_VERSION, "backbone_sha256": self.encoder.weight_sha256,
            "backbone_snapshot_sha256": getattr(self.encoder, "snapshot_sha256", None),
            "encoder_class": type(self.encoder).__module__ + "." + type(self.encoder).__qualname__,
            "detection_config": vars(self.config).copy(), "timestamp_origin": "first_decoded_video_frame"}
        self.batch_size, self.timeout_sec, self.evidence_count = batch_size, timeout_sec, evidence_count
        self._lock = threading.Lock()
        self.last_timing: dict = {}
        self.last_diagnostics: dict = {}

    def analyze(self, video_path: str) -> list[dict]:
        return self._run(video_path, extract_evidence=True)

    def predict(self, video_path: str) -> list[dict]:
        """Same trained detector; return source PTS candidates for backend-owned JPEG extraction."""
        return self._run(video_path, extract_evidence=False)

    def _run(self, video_path: str, *, extract_evidence: bool) -> list[dict]:
        # Serial inference bounds memory on unified-memory laptops. Queue wait is timed.
        started = time.perf_counter()
        if not self._lock.acquire(timeout=self.timeout_sec):
            raise RuntimeError("Inference queue deadline exceeded")
        try:
            return self._analyze(video_path, started, extract_evidence=extract_evidence)
        finally:
            self._lock.release()

    def _analyze(self, video_path: str, started: float, *, extract_evidence: bool = True) -> list[dict]:
        def remaining() -> float:
            value = self.timeout_sec - (time.perf_counter() - started)
            if value <= 0:
                raise RuntimeError("Complete inference deadline exceeded")
            return value

        self.last_timing = {}
        self.last_diagnostics = {}
        video = private_path(video_path, self.input_root)
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".features-", dir=self.evidence_root) as temporary:
            cache = Path(temporary) / "cache"
            meta = extract_video(video, cache, self.encoder, sample_fps=self.config.sample_fps,
                                 batch_size=self.batch_size, timeout_sec=remaining())
            with np.load(cache / "features.npz", allow_pickle=False) as archive:
                features, times = archive["embeddings"], archive["timestamps_sec"]
            x, bounds = windows(features, times, self.config)
            scores = score_windows(self.model, x)
            candidates = detect_events(scores, bounds, self.config)
            normalized = (features.astype(np.float64) - self.model.feature_mean.numpy()) / self.model.feature_std.numpy()
            self.last_diagnostics = {"model_identity": self.identity, "source_sha256": meta["video_sha256"],
                "duration_sec": meta["duration_sec"], "sampled_timestamps_sec": times.tolist(),
                "sampled_frames": len(times), "first_pts": meta["frames"][0]["pts"],
                "last_pts": meta["frames"][-1]["pts"], "device": self.encoder.device,
                "decoded_frame_count": meta["frames"][-1]["index"] + 1,
                "time_base": [meta["frames"][0]["time_base_num"], meta["frames"][0]["time_base_den"]],
                "max_sample_gap_sec": float(np.diff(times).max()) if len(times) > 1 else 0,
                "normalization": {"std_min": float(self.model.feature_std.min()),
                    "std_max": float(self.model.feature_std.max()),
                    "normalized_rms": float(np.sqrt(np.mean(normalized**2))),
                    "fraction_abs_gt_5": float((np.abs(normalized) > 5).mean())},
                "cpu_fallback_used": self.encoder.fallback_used,
                **score_diagnostics(scores, bounds, self.config), "candidate_events": candidates}
        remaining()
        events = []
        if candidates:
            # One extra sequential decode, retaining only chosen real source frames.
            selections = []
            for event in candidates:
                start, end = bounds[event["window_index"]]
                eligible = np.flatnonzero((times >= start) & (times <= end))
                nearest = eligible[np.argsort(np.abs(times[eligible] - event["timecode_sec"]), kind="stable")]
                selected = sorted(nearest[:self.evidence_count].tolist())
                peak = int(nearest[0])
                selections.append((selected, peak))
            if not extract_evidence:
                for candidate, (selected, peak) in zip(candidates, selections):
                    events.append({"type": BINARY_EVENT_TYPE, "start_sec": candidate["start_sec"],
                        "end_sec": candidate["end_sec"], "timecode_sec": float(times[peak]),
                        "description": DESCRIPTION, "confidence": candidate["confidence"],
                        "evidence_timestamps_sec": [float(times[i]) for i in selected]})
                remaining()
                self.last_timing = {"complete_inference_sec": time.perf_counter()-started,
                    "feature_extraction_sec": meta["processing_sec"], "duration_sec": meta["duration_sec"],
                    "device": self.encoder.device, "cpu_fallback_used": self.encoder.fallback_used}
                return events
            wanted = {int(meta["frames"][i]["index"]): i for selected, _ in selections for i in selected}
            # TemporaryDirectory cleans partial evidence on any failure. Rename only after validation.
            with TemporaryDirectory(prefix=".evidence-", dir=self.evidence_root) as temporary:
                stage = Path(temporary) / "frames"
                stage.mkdir()
                found = {}
                for frame in sample_video(video, self.config.sample_fps, remaining()):
                    if frame.index in wanted:
                        i = wanted[frame.index]
                        expected = meta["frames"][i]
                        if frame.pts != expected["pts"] or abs(frame.timestamp_sec-times[i]) > 1e-9:
                            raise ValueError("Source timestamps changed during inference")
                        name = f"frame_{frame.index:09d}.jpg"
                        Image.fromarray(frame.rgb).save(stage / name, format="JPEG", quality=95)
                        found[i] = name
                if len(found) != len(wanted) or sha256_file(video) != meta["video_sha256"]:
                    raise ValueError("Incomplete evidence or changed source video")
                remaining()
                destination = self.evidence_root / uuid.uuid4().hex
                for candidate, (selected, peak) in zip(candidates, selections):
                    events.append({"type": BINARY_EVENT_TYPE, "timecode_sec": float(times[peak]),
                        "description": DESCRIPTION, "evidence_paths": [str(destination / found[i]) for i in selected],
                        "evidence_timestamps_sec": [float(times[i]) for i in selected],
                        "confidence": candidate["confidence"]})
                write_json(stage / "evidence.json", {"video_sha256": meta["video_sha256"], "events": events,
                    "frames": [meta["frames"][i] for i in sorted(found)],
                    "timestamp_origin": "first_decoded_video_frame"})
                stage.rename(destination)
        remaining()
        self.last_timing = {"complete_inference_sec": time.perf_counter()-started,
                            "feature_extraction_sec": meta["processing_sec"],
                            "duration_sec": meta["duration_sec"],
                            "device": self.encoder.device, "cpu_fallback_used": self.encoder.fallback_used}
        return events


@lru_cache(maxsize=1)
def _configured_analyzer() -> VideoAnalyzer:
    root = Path(os.environ.get("FORTEVISION_PRIVATE_ROOT", "private_data"))
    return VideoAnalyzer(
        Path(os.environ.get("FORTEVISION_HEAD", str(root / "checkpoints/logistic-v4/head.pt"))),
        Path(os.environ.get("FORTEVISION_MODEL_DIR", str(root / "weights/dinov2-small"))),
        private_root=root,
        evidence_root=Path(os.environ.get("FORTEVISION_EVIDENCE_ROOT", str(root / "evidence"))),
        device=os.environ.get("FORTEVISION_DEVICE", "auto"))


def analyze_video(video_path: str) -> list[dict]:
    """Return [] only after successful complete-video analysis. Failures propagate.

    evidence_timestamps_sec aligns 1:1 with evidence_paths (internal, not jury JSON).
    Backend must read JPEGs, apply API-key/queue limits and clean artifacts after use.
    Restart the worker after replacing model/config environment; the model is cached.
    """
    # lru_cache alone may construct two encoders during concurrent first calls.
    with _INITIALIZE_LOCK:
        analyzer = _configured_analyzer()
    return analyzer.analyze(video_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", default="private_data")
    parser.add_argument("--video", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--model-dir", default="private_data/weights/dinov2-small")
    parser.add_argument("--output", required=True, help="Local JSON with events and benchmark timings")
    parser.add_argument("--evidence-root")
    parser.add_argument("--device", choices=["auto", "cpu", "mps"], default="auto")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--timeout-sec", type=positive_float, default=240)
    parser.add_argument("--evidence-count", type=int, choices=[1, 2, 3], default=3)
    parser.add_argument("--threshold", type=float)
    args = parser.parse_args()
    output = private_path(args.output, args.private_root)
    if output.exists():
        raise FileExistsError("Output exists")
    started = time.perf_counter()
    analyzer = VideoAnalyzer(Path(args.checkpoint), Path(args.model_dir), private_root=Path(args.private_root),
        evidence_root=Path(args.evidence_root) if args.evidence_root else None,
        device=args.device, batch_size=args.batch_size, timeout_sec=args.timeout_sec,
        evidence_count=args.evidence_count, threshold=args.threshold)
    load_sec = time.perf_counter()-started
    events = analyzer.analyze(args.video)
    write_json(output, {"events": events, **analyzer.last_timing, "model_load_sec": load_sec,
        "cold_total_sec": time.perf_counter()-started, "includes_http_and_backend_queue": False,
        "detection_config": vars(analyzer.config)})
    print("Local inference result and measured timings saved. API end-to-end latency requires backend measurement.")


if __name__ == "__main__":
    run_cli(main)
