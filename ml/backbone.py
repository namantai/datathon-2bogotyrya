"""Frozen, offline-only DINOv2 ViT-S/14 feature encoder."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import warnings

import numpy as np
from PIL import Image

MODEL_ID = "facebook/dinov2-small"
MODEL_REVISION = "ed25f3a31f01632728cabb09d1542f84ab7b0056"
# Published LFS SHA-256 at the pinned HF revision, verified 2026-10-17.
WEIGHT_SHA256 = "ae1e99fcefd534ed978cdeb8326f08030c96e28b7a81ffcbc98a857c84d14be1"
MODEL_FILES = ("config.json", "preprocessor_config.json", "model.safetensors", "README.md")
PREPROCESS_VERSION = "full_frame_letterbox_224_bicubic_rgb_imagenet_v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_rgb(rgb: np.ndarray, mean: list[float], std: list[float]) -> np.ndarray:
    """Preserve the whole field of view, padding with normalization mean."""
    if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3 or min(rgb.shape[:2]) < 1:
        raise ValueError("Expected HWC uint8 RGB")
    image = Image.fromarray(rgb)
    width, height = image.size
    scale = min(224 / width, 224 / height)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    resized = image.resize(size, Image.Resampling.BICUBIC)
    canvas = Image.new("RGB", (224, 224), tuple(round(m * 255) for m in mean))
    canvas.paste(resized, ((224 - size[0]) // 2, (224 - size[1]) // 2))
    array = np.asarray(canvas, dtype=np.float32) / 255
    array = (array - np.asarray(mean, dtype=np.float32)) / np.asarray(std, dtype=np.float32)
    return np.ascontiguousarray(array.transpose(2, 0, 1))


class DinoV2Encoder:
    def __init__(self, model_dir: Path, device: str = "auto", cpu_threads: int = 4):
        # Never accept a Hub identifier, download weights, or execute remote code here.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        import torch
        from transformers import Dinov2Model

        model_dir = Path(model_dir).resolve(strict=True)
        if device not in {"auto", "cpu", "mps"} or cpu_threads < 1:
            raise ValueError("Invalid device or thread count")
        provenance = json.loads((model_dir / "download_manifest.json").read_text())
        if provenance["model_id"] != MODEL_ID or provenance["revision"] != MODEL_REVISION:
            raise ValueError("Unexpected backbone revision")
        for name in MODEL_FILES:
            if sha256_file(model_dir / name) != provenance["sha256"][name]:
                raise ValueError("Backbone snapshot checksum mismatch")
        if provenance["sha256"]["model.safetensors"] != WEIGHT_SHA256:
            raise ValueError("Backbone does not match published weight checksum")
        processor = json.loads((model_dir / "preprocessor_config.json").read_text())
        self.mean, self.std = processor["image_mean"], processor["image_std"]
        if (len(self.mean) != 3 or len(self.std) != 3
                or not np.isfinite(self.mean + self.std).all() or any(s <= 0 for s in self.std)):
            raise ValueError("Invalid normalization")
        if (not np.allclose(self.mean, [.485, .456, .406])
                or not np.allclose(self.std, [.229, .224, .225])):
            raise ValueError("Unexpected ImageNet normalization")
        torch.set_num_threads(cpu_threads)
        model, info = Dinov2Model.from_pretrained(
            str(model_dir), local_files_only=True, use_safetensors=True,
            attn_implementation="eager", output_loading_info=True,
        )
        if any(info.get(key) for key in ("missing_keys", "unexpected_keys", "mismatched_keys", "error_msgs")):
            raise ValueError("Backbone weights do not exactly match architecture")
        cfg = model.config
        if (cfg.hidden_size, cfg.patch_size, cfg.num_hidden_layers, cfg.num_attention_heads) != (384, 14, 12, 6):
            raise ValueError("Expected standard DINOv2 ViT-S/14")
        mps_available = torch.backends.mps.is_available()
        self.device = "mps" if device != "cpu" and mps_available else "cpu"
        if device == "mps" and not mps_available:
            warnings.warn("MPS unavailable; using CPU.", RuntimeWarning)
        self.model = model.eval().requires_grad_(False)
        self.fallback_used = device == "mps" and not mps_available
        try:
            self.model.to(self.device)
        except (RuntimeError, NotImplementedError):
            if self.device != "mps":
                raise
            self._fallback()
        self.weight_sha256 = provenance["sha256"]["model.safetensors"]

    def _fallback(self) -> None:
        import torch
        self.model.to("cpu")
        self.device = "cpu"
        self.fallback_used = True
        torch.mps.empty_cache()
        warnings.warn("MPS operation failed; retrying this batch on CPU.", RuntimeWarning)

    def encode(self, images: list[np.ndarray]) -> np.ndarray:
        import torch
        if not images:
            raise ValueError("Empty image batch")
        batch = torch.from_numpy(np.stack([prepare_rgb(image, self.mean, self.std) for image in images]))
        with torch.inference_mode():
            try:
                features = self.model(pixel_values=batch.to(self.device)).last_hidden_state[:, 0]
            except (RuntimeError, NotImplementedError):
                if self.device != "mps":
                    raise
                self._fallback()
                features = self.model(pixel_values=batch).last_hidden_state[:, 0]
        result = features.float().cpu().numpy()
        if not np.isfinite(result).all() and self.device == "mps":
            self._fallback()
            return self.encode(images)
        if result.shape != (len(images), 384) or not np.isfinite(result).all():
            raise ValueError("Invalid backbone features")
        return result
