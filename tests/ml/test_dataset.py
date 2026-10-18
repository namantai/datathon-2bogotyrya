import csv
from datetime import time, timedelta
import math

import pytest

from ml.annotations import create_template
from ml.dataset import normalize_video_id, parse_timestamp, parse_timestamps, validate_dataset
from ml.local_io import private_path


def validate(excel, videos):
    return validate_dataset(excel, videos, id_column="video", label_column="violation",
                            timestamp_column="timestamp", group_column="group")


@pytest.mark.parametrize("value", [" x 001 .mov ", "Х001.MOV", "X001", "ｘ００１"])
def test_id_normalization(value):
    assert normalize_video_id(value) == "X001"


@pytest.mark.parametrize("value,unit,expected", [
    (1.5, "seconds", 1.5), ("01:02.5", "seconds", 62.5),
    ("1:02:03", "seconds", 3723), ("1,5", "seconds", 1.5),
    (time(0, 1, 2, 500000), "seconds", 62.5), (timedelta(seconds=5), "seconds", 5),
    (0.5, "excel_days", 43200), (0.5, "seconds", 0.5),
])
def test_timestamps(value, unit, expected):
    assert parse_timestamp(value, unit) == expected


@pytest.mark.parametrize("value", [-1, math.nan, math.inf, "00:60", "-1:20", "1.2:00", True, "invalid"])
def test_reject_invalid_timestamps(value):
    with pytest.raises(ValueError):
        parse_timestamp(value)


def test_multiple_points():
    assert parse_timestamps("1.2;00:02;1.2", "seconds") == [1.2, 2.0]


def test_valid_inventory_and_annotation_template(tmp_path, make_video, make_excel):
    make_video("videos/Х1.MOV")
    make_video("videos/X2.mov")
    excel = make_excel([["X1", 1, "0.5;1.5", "scenario-a"], ["X2.mov", 0, None, "scenario-b"]])
    report, manifest = validate(excel, tmp_path / "videos")
    assert report["issues"] == []
    assert report["summary"]["positive_rows"] == 1
    assert report["summary"]["negative_rows"] == 1
    assert report["summary"]["eligible_videos"] == 2
    output = tmp_path / "annotations.csv"
    create_template(manifest, output)
    with output.open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert all(row["event_type"] == row["start_sec"] == row["end_sec"] == "" for row in rows)
    assert rows[0]["group_id"] == "scenario-a"
    with pytest.raises(FileExistsError):
        create_template(manifest, output)


def test_missing_duplicate_unlabeled_and_invalid_times(tmp_path, make_video, make_excel):
    for name in ["X1", "Х1", "X2", "X3", "X4", "X5", "X6"]:
        make_video(f"videos/{name}.mov")
    excel = make_excel([["X1", 0, None], ["X2", 1, 2.0], ["X3", None, None],
                        ["X4", 0, None], ["Х4", 1, 0.5], ["X6", "maybe", None], ["X9", 0, None]])
    report, manifest = validate(excel, tmp_path / "videos")
    codes = {issue["code"] for issue in report["issues"]}
    assert {"duplicate_video_id", "duplicate_label_id", "missing_video", "missing_label",
            "timestamp_out_of_range", "invalid_label_or_timestamp"} <= codes
    assert not manifest["validation_ok"]
    assert report["summary"]["unique_label_ids"] == 6
    assert report["summary"]["video_files"] == 7
    assert not any(record["eligible"] for record in manifest["videos"])
    assert next(r for r in manifest["videos"] if r["video_id"] == "X5")["label"] is None


@pytest.mark.parametrize("label,timestamp", [(1, None), (0, 0.2), (1, "=1+1")])
def test_inconsistent_annotation_is_ineligible(tmp_path, make_video, make_excel, label, timestamp):
    make_video("videos/X1.mov")
    report, manifest = validate(make_excel([["X1", label, timestamp]]), tmp_path / "videos")
    assert report["issues"]
    assert not manifest["videos"][0]["eligible"]


def test_corrupt_video(tmp_path, make_excel):
    directory = tmp_path / "videos"
    directory.mkdir()
    (directory / "X1.mov").write_bytes(b"synthetic invalid video")
    report, _ = validate(make_excel([["X1", 0, None]]), directory)
    assert any(issue["code"] == "unreadable_video" for issue in report["issues"])


def test_path_boundary_blocks_escape_and_symlinks(tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        private_path(root / "escape" / "data.mov", root)
    with pytest.raises(ValueError):
        private_path(root / ".." / "data.mov", root)
    assert private_path(root / "safe.mov", root) == root / "safe.mov"


def test_explicit_label_mapping(tmp_path, make_video, make_excel):
    make_video("videos/X1.mov")
    excel = make_excel([["X1", "Да", 0.5]])
    report, manifest = validate_dataset(excel, tmp_path / "videos", id_column="video",
        label_column="violation", timestamp_column="timestamp", positive_values=("да",), negative_values=("нет",))
    assert report["issues"] == []
    assert manifest["videos"][0]["label"] == 1
