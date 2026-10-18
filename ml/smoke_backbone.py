"""Verify the real pretrained backbone on generated pixels only; never reads videos."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import numpy as np

from ml.backbone import DinoV2Encoder
from ml.local_io import run_cli


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", default="private_data/weights/dinov2-small")
    parser.add_argument("--device", choices=["auto", "cpu", "mps"], default="auto")
    args = parser.parse_args()
    started = time.perf_counter()
    encoder = DinoV2Encoder(Path(args.model_dir), device=args.device)
    rng = np.random.default_rng(2026)
    pixels = [rng.integers(0, 256, (180, 320, 3), dtype=np.uint8) for _ in range(2)]
    first, second = encoder.encode(pixels), encoder.encode(pixels)
    if not np.allclose(first, second, atol=1e-5):
        raise ValueError("Nonrepeatable eval-mode features")
    if any(p.requires_grad for p in encoder.model.parameters()):
        raise ValueError("Backbone must be frozen")
    print(json.dumps({"synthetic_only": True, "device": encoder.device,
        "fallback_used": encoder.fallback_used, "shape": list(first.shape), "finite": bool(np.isfinite(first).all()),
        "frozen": True, "repeatable": True, "load_and_forward_sec": round(time.perf_counter() - started, 3)}))


if __name__ == "__main__":
    run_cli(main)
