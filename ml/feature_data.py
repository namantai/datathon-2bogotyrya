"""Validated local feature cache and reproducible video/group splits."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import numpy as np

from ml.backbone import MODEL_ID, MODEL_REVISION, PREPROCESS_VERSION, WEIGHT_SHA256
from ml.local_io import private_path


@dataclass
class VideoFeatures:
    video_id: str
    label: int
    points: list[float]
    group: str
    digest: str
    embeddings: np.ndarray
    times: np.ndarray
    metadata: dict
    brightness_embeddings: np.ndarray | None = None

    def identity(self) -> dict:
        return {"video_id": self.video_id, "video_sha256": self.digest, "group": self.group,
                "label": self.label, "timestamps_sec": self.points}


def load_features(root: Path, private_root: Path) -> list[VideoFeatures]:
    root = private_path(root, private_root)
    index = json.loads(private_path(root / "index.json", root).read_text())
    if index["schema_version"] != 1:
        raise ValueError("Unsupported feature index")
    result, seen_ids, seen_hashes = [], set(), set()
    for entry in index["videos"]:
        directory = private_path(root / entry["directory"], root)
        meta = json.loads(private_path(directory / "metadata.json", root).read_text())
        if (meta["model_id"] != MODEL_ID or meta["model_revision"] != MODEL_REVISION
                or meta["preprocess"] != PREPROCESS_VERSION or meta["weight_sha256"] != WEIGHT_SHA256
                or meta["embedding_kind"] != "last_hidden_state_cls" or meta["embedding_dim"] != 384
                or meta["timestamp_origin"] != "first_decoded_video_frame"):
            raise ValueError("Incompatible feature provenance")
        identifier, digest = entry["video_id"], meta["video_sha256"]
        if not identifier or identifier in seen_ids or digest in seen_hashes:
            raise ValueError("Duplicate video ID or identical source video in cache")
        if entry["label"] not in (0, 1):
            raise ValueError("Missing binary label")
        points = entry["timestamps_sec"]
        if (not isinstance(points, list) or not np.isfinite(points).all() or any(p < 0 for p in points)
                or bool(points) != bool(entry["label"])):
            raise ValueError("Invalid reference timestamps")
        with np.load(private_path(directory / "features.npz", root), allow_pickle=False) as archive:
            x = archive["embeddings"].astype(np.float32)
            times = archive["timestamps_sec"].astype(np.float64)
            indices, pts = archive["frame_indices"], archive["pts"]
            augmented = archive["brightness_embeddings"].astype(np.float32) if "brightness_embeddings" in archive else None
        frames = meta["frames"]
        if (x.ndim != 2 or x.shape != (len(times), 384) or not len(times)
                or times.ndim != 1 or times[0] != 0 or not np.isfinite(x).all()
                or not np.isfinite(times).all() or np.any(np.diff(times) <= 0)
                or len(frames) != len(times) or not np.array_equal(indices, [f["index"] for f in frames])
                or not np.array_equal(pts, [f["pts"] for f in frames])
                or not np.allclose(times, [f["timestamp_sec"] for f in frames], atol=1e-9, rtol=0)):
            raise ValueError("Invalid or incomplete feature cache")
        if (augmented is not None and (augmented.shape != x.shape or not np.isfinite(augmented).all())):
            raise ValueError("Invalid brightness augmentation")
        if meta.get("augmentations", []) != (["brightness"] if augmented is not None else []):
            raise ValueError("Unsupported or inconsistent augmentation metadata")
        # Last PTS may precede duration by one frame; validate points against stored duration when available.
        rate = meta["sample_fps"]
        if not isinstance(rate, (int, float)) or not np.isfinite(rate) or rate <= 0:
            raise ValueError("Invalid cache timing metadata: sample_fps")
        duration = meta.get("duration_sec", float(times[-1]) + 1 / rate)
        if (not isinstance(duration, (int, float)) or not np.isfinite(duration)
                or duration <= times[-1]):
            raise ValueError("Invalid cache timing metadata: duration_sec")
        if any(p >= duration for p in points):
            raise ValueError("Reference point outside video duration")
        seen_ids.add(identifier)
        seen_hashes.add(digest)
        group = "group:" + str(entry["group_id"]) if entry.get("group_id") else "video:" + identifier
        result.append(VideoFeatures(identifier, entry["label"], points, group, digest, x, times, meta, augmented))
    if not result:
        raise ValueError("Empty feature index")
    return result


def dataset_fingerprint(videos: list[VideoFeatures]) -> str:
    payload = [{**v.identity(), "sample_fps": v.metadata["sample_fps"],
               "duration_sec": v.metadata.get("duration_sec"),
               "features_sha256": hashlib.sha256(v.embeddings.tobytes() + v.times.tobytes()
               + (v.brightness_embeddings.tobytes() if v.brightness_embeddings is not None else b"")).hexdigest()}
               for v in sorted(videos, key=lambda v: v.video_id)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def split_videos(videos: list[VideoFeatures], seed: int = 2026,
                 val_fraction: float = .2, test_fraction: float = .2) -> dict[str, list[str]]:
    if not (0 < val_fraction < 1 and 0 <= test_fraction < 1 and val_fraction + test_fraction < 1):
        raise ValueError("Invalid split fractions")
    groups = sorted({v.group for v in videos})
    nval = max(1, round(len(groups) * val_fraction))
    ntest = max(1, round(len(groups) * test_fraction)) if test_fraction > 0 else 0
    # Without a test set, two mixed-class scenario groups can suffice.
    minimum_groups = 6 if test_fraction > 0 else 2
    if len(groups) < minimum_groups or nval + ntest >= len(groups):
        raise ValueError("Not enough independent groups for the requested splits with both labels")
    rng = np.random.default_rng(seed)
    best, best_error = None, float("inf")
    for _ in range(2000):
        ordered = rng.permutation(groups).tolist()
        partitions = {"validation": set(ordered[:nval]), "test": set(ordered[nval:nval+ntest]),
                      "train": set(ordered[nval+ntest:])}
        selected = {name: [v for v in videos if v.group in subset] for name, subset in partitions.items()}
        if any({v.label for v in subset} != {0, 1}
               for name, subset in selected.items() if name != "test" or ntest > 0):
            continue
        fractions = {"train": 1-val_fraction-test_fraction, "validation": val_fraction, "test": test_fraction}
        error = sum(abs(sum(v.label == label for v in selected[name]) / sum(v.label == label for v in videos)
                        - fraction) for name, fraction in fractions.items() for label in (0, 1))
        if error < best_error:
            best = {name: sorted(v.video_id for v in subset) for name, subset in selected.items()}
            best_error = error
    if best is None:
        raise ValueError("Cannot form group-disjoint requested splits with both labels; review groups locally")
    return best


def validate_split(videos: list[VideoFeatures], split: dict) -> None:
    if set(split) != {"train", "validation", "test"}:
        raise ValueError("Expected train/validation/test")
    all_ids = [i for subset in split.values() for i in subset]
    if len(set(all_ids)) != len(all_ids) or set(all_ids) != {v.video_id for v in videos}:
        raise ValueError("Split contains overlap or different videos")
    group_owner = {}
    for name, ids in split.items():
        if name == "test" and not ids:
            continue  # Explicitly disabled holdout; train and validation remain mandatory.
        selected = [v for v in videos if v.video_id in ids]
        if {v.label for v in selected} != {0, 1}:
            raise ValueError("Each split needs both labels")
        for v in selected:
            if v.group in group_owner and group_owner[v.group] != name:
                raise ValueError("Related groups cross split boundaries")
            group_owner[v.group] = name
