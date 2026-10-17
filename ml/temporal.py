"""Shared binary windows, trainable heads and event postprocessing (no video IO)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from pathlib import Path

import numpy as np
import torch
from torch import nn

from ml.backbone import MODEL_ID, MODEL_REVISION, PREPROCESS_VERSION, WEIGHT_SHA256


@dataclass(frozen=True)
class DetectionConfig:
    sample_fps: float = 2.0
    window_sec: float = 4.0
    stride_sec: float = 1.0
    threshold: float = 0.5
    smooth_windows: int = 3
    merge_gap_sec: float = 0.0

    def __post_init__(self) -> None:
        if not all(math.isfinite(v) and v > 0 for v in (self.sample_fps, self.window_sec, self.stride_sec)):
            raise ValueError("Invalid window configuration")
        if self.stride_sec > self.window_sec or not 0 < self.threshold < 1:
            raise ValueError("Windows must cover the video and threshold must be in (0, 1)")
        if (not isinstance(self.smooth_windows, int) or self.smooth_windows < 1
                or self.smooth_windows % 2 != 1 or not math.isfinite(self.merge_gap_sec) or self.merge_gap_sec < 0):
            raise ValueError("Invalid smoothing or merge gap")
        if not 2 <= self.steps <= 512:
            raise ValueError("Window must contain 2..512 temporal samples")

    @property
    def steps(self) -> int:
        return round(self.sample_fps * self.window_sec)


def windows(embeddings: np.ndarray, times: np.ndarray, config: DetectionConfig) -> tuple[np.ndarray, np.ndarray]:
    """Fixed-length nearest-PTS sequences; bounds use original seconds, including tail."""
    if (embeddings.ndim != 2 or embeddings.shape[1] != 384 or len(embeddings) != len(times)
            or times.ndim != 1 or not len(times) or not np.isfinite(embeddings).all()
            or not np.isfinite(times).all() or times[0] != 0 or np.any(np.diff(times) <= 0)):
        raise ValueError("Invalid complete-video features or timestamps")
    tail = max(0.0, float(times[-1]) - config.window_sec)
    starts = np.unique(np.append(np.arange(0, tail, config.stride_sec), tail))
    bounds = np.array([(s, min(s + config.window_sec, float(times[-1]))) for s in starts])
    sequences = []
    for start, end in bounds:
        available = np.flatnonzero((times >= start) & (times <= end))
        if not len(available):
            raise ValueError("Window contains no observed frames")
        grid = np.linspace(start, end, config.steps)
        nearest = np.abs(times[available, None] - grid[None, :]).argmin(axis=0)
        sequences.append(embeddings[available[nearest]])
    return np.stack(sequences).astype(np.float32), bounds


def window_labels(bounds: np.ndarray, label: int, points: list[float]) -> np.ndarray:
    """Only point-containing positive windows are supervised; others remain unknown (-1)."""
    if label == 0 and not points:
        return np.zeros(len(bounds), dtype=np.int64)
    if label != 1 or not points or not np.isfinite(points).all():
        raise ValueError("Binary label and reference points are inconsistent")
    result = np.full(len(bounds), -1, dtype=np.int64)
    for point in points:
        selected = (bounds[:, 0] <= point) & (bounds[:, 1] >= point)
        # A reference can be after the last frame's PTS but before the video ends.
        if not selected.any() and point > bounds[-1, 1]:
            selected[-1] = True
        result[selected] = 1
    return result


class BinaryHead(nn.Module):
    def __init__(self, kind: str = "logistic", hidden: int = 64):
        super().__init__()
        if kind not in {"logistic", "temporal"} or not 1 <= hidden <= 512:
            raise ValueError("Unsupported classifier")
        self.kind, self.hidden = kind, hidden
        self.register_buffer("feature_mean", torch.zeros(384))
        self.register_buffer("feature_std", torch.ones(384))
        if kind == "logistic":
            self.net = nn.Linear(384, 1)
        else:
            self.conv = nn.Sequential(nn.Conv1d(384, hidden, 3, padding=1), nn.GELU(),
                                      nn.Conv1d(hidden, hidden, 3, padding=1), nn.GELU())
            self.net = nn.Linear(hidden * 2, 1)

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        x = (inputs - self.feature_mean) / self.feature_std
        if self.kind == "logistic":
            pooled = x.mean(dim=1)
        else:
            x = self.conv(x.transpose(1, 2))
            pooled = torch.cat((x.mean(dim=2), x.amax(dim=2)), dim=1)
        return self.net(pooled).squeeze(-1)


def score_windows(model: BinaryHead, x: np.ndarray, batch_size: int = 64) -> np.ndarray:
    if batch_size < 1 or not len(x):
        raise ValueError("Invalid scoring batch")
    device = next(model.parameters()).device
    model.eval()
    with torch.inference_mode():
        scores = np.concatenate([model(torch.from_numpy(x[i:i + batch_size]).to(device)).sigmoid().cpu().numpy()
                                 for i in range(0, len(x), batch_size)])
    if not np.isfinite(scores).all():
        raise ValueError("Nonfinite classifier scores")
    return scores


def smooth_scores(scores: np.ndarray, config: DetectionConfig) -> np.ndarray:
    """The same centered smoothing for inference and local diagnostics."""
    if (scores.ndim != 1 or not len(scores) or not np.isfinite(scores).all()
            or np.any((scores < 0) | (scores > 1))):
        raise ValueError("Invalid scores")
    radius = config.smooth_windows // 2
    return np.array([scores[max(0, i-radius):min(len(scores), i+radius+1)].mean()
                     for i in range(len(scores))])


def detect_events(scores: np.ndarray, bounds: np.ndarray, config: DetectionConfig) -> list[dict]:
    if scores.shape != (len(bounds),):
        raise ValueError("Invalid scores")
    smoothed = smooth_scores(scores, config)
    events: list[dict] = []
    for i in np.flatnonzero(smoothed >= config.threshold):
        start, end = map(float, bounds[i])
        candidate = {"start_sec": start, "end_sec": end, "timecode_sec": (start + end) / 2,
                     "confidence": float(smoothed[i]), "window_index": int(i)}
        if events and start <= events[-1]["end_sec"] + config.merge_gap_sec:
            previous = events[-1]
            previous["end_sec"] = max(previous["end_sec"], end)
            if candidate["confidence"] > previous["confidence"]:
                previous.update({k: candidate[k] for k in ("timecode_sec", "confidence", "window_index")})
        else:
            events.append(candidate)
    return events


def save_checkpoint(path: Path, model: BinaryHead, config: DetectionConfig, training: dict) -> None:
    payload = {"schema_version": 1, "kind": model.kind, "hidden": model.hidden,
               "config": asdict(config), "training": training,
               "model_id": MODEL_ID, "model_revision": MODEL_REVISION,
               "preprocess": PREPROCESS_VERSION, "weight_sha256": WEIGHT_SHA256,
               "state_dict": {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}}
    with path.open("xb") as handle:
        torch.save(payload, handle)


def load_checkpoint(path: Path) -> tuple[BinaryHead, DetectionConfig, dict]:
    payload = torch.load(path, map_location="cpu", weights_only=True)
    if (payload["schema_version"] != 1 or payload["model_id"] != MODEL_ID
            or payload["model_revision"] != MODEL_REVISION or payload["preprocess"] != PREPROCESS_VERSION
            or payload["weight_sha256"] != WEIGHT_SHA256 or not payload["training"].get("completed")):
        raise ValueError("Untrained or incompatible checkpoint")
    config = DetectionConfig(**payload["config"])
    model = BinaryHead(payload["kind"], payload["hidden"])
    model.load_state_dict(payload["state_dict"], strict=True)
    if any(not torch.isfinite(t).all() for t in model.state_dict().values()) or (model.feature_std <= 0).any():
        raise ValueError("Invalid checkpoint tensors")
    return model.eval(), config, payload["training"]
