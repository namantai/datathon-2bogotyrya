import json
from pathlib import Path
import subprocess
import sys

import pytest

from ml import extract_features


def command(module, *args):
    return subprocess.run([sys.executable, "-m", module, *map(str, args)],
                          text=True, capture_output=True, timeout=60)


def test_phase_a_local_cli_roundtrip(tmp_path, make_video, make_excel):
    video = make_video("videos/X1.mov")
    excel = make_excel([["Х1", 1, 0.5]])
    output = tmp_path / "validation"
    result = command("ml.dataset", "--private-root", tmp_path, "--excel", excel,
        "--videos", video.parent, "--id-column", "video", "--label-column", "violation",
        "--timestamp-column", "timestamp", "--output", output)
    assert result.returncode == 0, result.stderr
    assert "X1" not in result.stdout
    assert json.loads((output / "report.json").read_text())["summary"]["eligible_videos"] == 1
    annotation = command("ml.annotations", "--private-root", tmp_path,
        "--manifest", output / "manifest.json", "--output", tmp_path / "annotations.csv")
    assert annotation.returncode == 0, annotation.stderr
    frames = command("ml.preprocess", "--private-root", tmp_path, "--video", video,
        "--output", tmp_path / "frames")
    assert frames.returncode == 0, frames.stderr
    assert (tmp_path / "frames" / "frames.json").is_file()


def test_failed_cli_reports_no_private_values(tmp_path, make_video, make_excel):
    video = make_video("videos/X1.mov")
    excel = make_excel([["PRIVATE-ID-SYNTHETIC", "sensitive-label", None]])
    result = command("ml.dataset", "--private-root", tmp_path, "--excel", excel,
        "--videos", video.parent, "--id-column", "video", "--label-column", "violation",
        "--timestamp-column", "timestamp", "--output", tmp_path / "invalid")
    assert result.returncode == 2
    assert "PRIVATE-ID-SYNTHETIC" not in result.stdout + result.stderr
    assert "sensitive-label" not in result.stdout + result.stderr


def test_invalid_manifest_fails_before_model_loading(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"validation_ok": False, "videos": []}))
    monkeypatch.setattr(sys, "argv", ["extract", "--private-root", str(tmp_path),
        "--manifest", str(manifest), "--output", str(tmp_path / "out")])
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid dataset must not load model")
    monkeypatch.setattr(extract_features, "DinoV2Encoder", unexpected)
    with pytest.raises(ValueError, match="validation"):
        extract_features.main()


def test_explicit_skip_unlabeled_preserves_only_labeled(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"validation_ok": False, "issue_codes": ["missing_label"], "videos": [
        {"video_id": "synthetic-known", "video_path": str(tmp_path / "known.mov"), "eligible": True,
         "label": 0, "timestamps_sec": [], "group_id": None},
        {"video_id": "synthetic-unknown", "video_path": str(tmp_path / "unknown.mov"), "eligible": False,
         "label": None, "timestamps_sec": [], "group_id": None}]}))
    monkeypatch.setattr(sys, "argv", ["extract", "--private-root", str(tmp_path), "--manifest", str(manifest),
                                     "--output", str(tmp_path / "out"), "--allow-unlabeled"])
    monkeypatch.setattr(extract_features, "DinoV2Encoder", lambda *a, **kw: object())
    paths = []
    monkeypatch.setattr(extract_features, "extract_video", lambda path, *a, **kw: paths.append(path))
    extract_features.main()
    assert paths == [tmp_path / "known.mov"]
    index = json.loads((tmp_path / "out/index.json").read_text())
    assert len(index["videos"]) == 1 and index["videos"][0]["label"] == 0


def test_all_clis_have_help():
    for module in ("dataset", "annotations", "preprocess", "download_weights", "extract_features",
                   "smoke_backbone", "train", "evaluate", "inference"):
        result = command(f"ml.{module}", "--help")
        assert result.returncode == 0, module + result.stderr
        assert "usage:" in result.stdout
