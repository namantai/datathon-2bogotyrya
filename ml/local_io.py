"""Local artifact handling. CLI errors never echo private paths or table values."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Callable


def private_path(path: str | Path, root: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    base = Path(root).expanduser().resolve()
    if resolved == base or not resolved.is_relative_to(base):
        raise ValueError("Path must be inside --private-root (including symlink targets).")
    return resolved


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def positive_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("Must be a positive finite number.")
    return result


def run_cli(action: Callable[[], int | None]) -> None:
    try:
        code = action()
    except Exception as error:
        # Details belong in local reports, never shared terminals or telemetry.
        print(f"Local operation failed ({type(error).__name__}). Check arguments, dependencies and local inputs; existing outputs are never overwritten.")
        raise SystemExit(2) from None
    raise SystemExit(code or 0)
