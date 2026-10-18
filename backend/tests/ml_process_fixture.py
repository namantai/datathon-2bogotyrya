"""Only synthetic process fixtures. Not a model; excluded from Docker images."""

import os
from pathlib import Path
import time


def successful(path: Path):
    assert path.is_file()
    assert "APP_API_KEY" not in os.environ
    print("Synthetic diagnostic that must not become JSON")
    return {"events": [{"code": "CASH_UNSECURED", "timestamp_seconds": 0.2,
                        "description": "Синтетическое наблюдение"}]}


def empty(path: Path):
    assert path.is_file()
    return {"events": []}


def multiple(path: Path):
    assert path.is_file()
    return {"events": [
        {"code": "CASH_UNSECURED", "timestamp_seconds": .2, "description": "Synthetic one"},
        {"code": "UNKNOWN_SYNTHETIC_CODE", "timestamp_seconds": .7, "description": "Synthetic two"},
    ]}


def blocking(path: Path):
    # The parent test owns this isolated tmp directory.
    path.with_suffix(".pid").write_text(str(os.getpid()))
    time.sleep(30)
    return {"events": []}


def broken(path: Path):
    raise RuntimeError(f"synthetic private observation at {path}")


def missing_events(path: Path):
    return {}


def oversized(path: Path):
    return {"events": [], "oversized": "x" * (2 * 1024 * 1024)}
