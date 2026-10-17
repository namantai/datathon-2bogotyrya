import json
import subprocess

import numpy as np
from PIL import Image
import pytest

from ml.preprocess import preprocess_video, probe_video, sample_video


def test_cfr_sampling_keeps_original_pts_and_last_frame(make_video):
    path = make_video()
    frames = list(sample_video(path, sample_fps=2))
    assert [f.index for f in frames] == [0, 5, 10, 15, 19]
    assert [f.timestamp_sec for f in frames] == pytest.approx([0, 0.5, 1, 1.5, 1.9])
    for frame in frames:
        assert frame.timestamp_sec == pytest.approx((frame.pts - frame.origin_pts) * frame.time_base_num / frame.time_base_den)
    assert probe_video(path)["duration_sec"] == pytest.approx(2)


def test_vfr_matches_original_frame_pts(make_video):
    path = make_video(variable_fps=True)
    frames = list(sample_video(path))
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames",
        "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(path)],
        capture_output=True, check=True, timeout=30)
    actual = [float(f["best_effort_timestamp_time"]) for f in json.loads(result.stdout)["frames"]]
    assert len(set(round(d, 4) for d in np.diff(actual))) > 1
    assert [f.timestamp_sec for f in frames] == pytest.approx([actual[f.index] - actual[0] for f in frames])
    assert frames[-1].index == len(actual) - 1


def test_rotation_preserved(tmp_path, make_video):
    original = make_video()
    rotated = tmp_path / "rotated.mov"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-display_rotation:v:0", "90",
        "-i", str(original), "-c", "copy", str(rotated)], check=True, capture_output=True, timeout=30)
    assert probe_video(rotated)["rotation_deg_ccw"] == 90
    first = next(sample_video(original)).rgb
    second = next(sample_video(rotated)).rgb
    assert second.shape == (96, 64, 3)
    assert np.array_equal(second, np.rot90(first))


def test_jpeg_export_and_refuse_overwrite(tmp_path, make_video):
    path = make_video()
    output = tmp_path / "frames"
    manifest = preprocess_video(path, output)
    assert len(manifest["frames"]) == 5
    for record in manifest["frames"]:
        with Image.open(output / record["jpeg"]) as image:
            assert image.format == "JPEG"
            assert image.size == (96, 64)
    assert (output / "frames.json").is_file()
    with pytest.raises(FileExistsError):
        preprocess_video(path, output)


@pytest.mark.parametrize("rate", [0, -1, float("nan"), float("inf")])
def test_invalid_sampling_rate(make_video, rate):
    with pytest.raises(ValueError):
        list(sample_video(make_video(), rate))


def test_high_sampling_rate_does_not_duplicate_frames(make_video):
    frames = list(sample_video(make_video(), sample_fps=50))
    assert len(frames) == len({f.pts for f in frames}) == 20


def test_deadline_fails_without_complete_manifest(make_video, tmp_path):
    with pytest.raises(RuntimeError):
        preprocess_video(make_video(), tmp_path / "timeout", timeout_sec=1e-12)
    assert not (tmp_path / "timeout" / "frames.json").exists()
