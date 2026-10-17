"""Explicit online setup for PUBLIC weights only. Not called by processing code."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from ml.backbone import MODEL_FILES, MODEL_ID, MODEL_REVISION, WEIGHT_SHA256, sha256_file
from ml.local_io import run_cli, write_json


def download_weights(output: Path) -> None:
    if output.exists():
        raise FileExistsError("Model directory already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".dinov2-download-", dir=output.parent) as temporary_dir:
        staging = Path(temporary_dir) / "snapshot"
        staging.mkdir()
        checksums = {}
        for name in MODEL_FILES:
            url = f"https://huggingface.co/{MODEL_ID}/resolve/{MODEL_REVISION}/{name}"
            with urlopen(url, timeout=60) as response, (staging / name).open("xb") as handle:
                shutil.copyfileobj(response, handle, length=1024 * 1024)
            checksums[name] = sha256_file(staging / name)
            if name == "model.safetensors" and checksums[name] != WEIGHT_SHA256:
                raise ValueError("Downloaded weights differ from published SHA-256")
        write_json(staging / "download_manifest.json", {"model_id": MODEL_ID, "revision": MODEL_REVISION,
            "sha256": checksums, "license": "Apache-2.0",
            "license_source": "https://github.com/facebookresearch/dinov2/blob/main/LICENSE"})
        if output.exists():
            raise FileExistsError("Model directory already exists")
        staging.rename(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="private_data/weights/dinov2-small")
    args = parser.parse_args()
    download_weights(Path(args.output).expanduser().resolve())
    print("Public DINOv2 snapshot saved. Subsequent encoding is offline-only.")


if __name__ == "__main__":
    run_cli(main)
