"""Build an OFFLINE human annotation review pack. Run manually on the data owner’s Mac.

Reads cached labels/PTS, a checkpoint's config/split, and original MOVs locally.
No model inference, training, network, label edits, or checkpoint/report overwrites.
The output contains private source frames and must remain local.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import html
import json
from pathlib import Path

import numpy as np
from PIL import Image

from ml.backbone import sha256_file
from ml.feature_data import VideoFeatures, dataset_fingerprint, load_features, validate_split
from ml.local_io import positive_float, private_path, run_cli, write_json
from ml.preprocess import probe_video, sample_video
from ml.temporal import DetectionConfig, load_checkpoint, window_labels, windows


def union_length(intervals: list[list[float]]) -> float:
    end, total = -float("inf"), 0.0
    for start, stop in sorted(intervals):
        total += max(0.0, stop - max(start, end))
        end = max(end, stop)
    return total


def supervision_details(video: VideoFeatures, config: DetectionConfig) -> dict:
    """Expose exactly the sequences/targets used by training, including overlap and tail."""
    sequences, bounds = windows(video.embeddings, video.times, config)
    targets = window_labels(bounds, video.label, video.points)
    rows = []
    for index, ((start, end), target) in enumerate(zip(bounds, targets)):
        available = np.flatnonzero((video.times >= start) & (video.times <= end))
        grid = np.linspace(start, end, config.steps)
        indices = available[np.abs(video.times[available, None] - grid[None, :]).argmin(axis=0)]
        if not np.array_equal(sequences[index], video.embeddings[indices]):
            raise ValueError("Audit sampling differs from training sampling")
        rows.append({"index": index, "start_sec": float(start), "end_sec": float(end),
                     "target": int(target), "sample_indices": indices.tolist(),
                     "sample_times_sec": video.times[indices].tolist(),
                     "containing_points_sec": [p for p in video.points if start <= p <= end],
                     "tail_point_assigned": bool(target == 1 and any(p > bounds[-1, 1] for p in video.points)
                                                  and index == len(bounds)-1)})
    supervised = [bounds[i].tolist() for i, target in enumerate(targets) if target >= 0]
    covered = union_length(supervised)
    point_details = []
    for point in video.points:
        nearest = int(np.abs(video.times-point).argmin())
        positive = [row for row in rows if point in row["containing_points_sec"]
                    or (row["tail_point_assigned"] and point > bounds[-1, 1])]
        point_details.append({"timestamp_sec": point, "nearest_sample_sec": float(video.times[nearest]),
                              "sample_distance_sec": float(abs(video.times[nearest]-point)),
                              "after_last_sample": bool(point > video.times[-1]),
                              "positive_window_indices": [row["index"] for row in positive],
                              "positive_window_sequence_checks": [
                                  {"window_index": row["index"],
                                   "nearest_cached_sample_retained": nearest in row["sample_indices"],
                                   "closest_sequence_sample_distance_sec": float(min(abs(t-point) for t in row["sample_times_sec"]))}
                                  for row in positive]})
    return {"window_counts": {name: int((targets == target).sum()) for name, target in
                             (("positive", 1), ("negative", 0), ("ignored", -1))},
            "supervised_union_sec": covered,
            "outside_supervised_union_sec": max(0.0, float(video.times[-1])-covered),
            "note": "Window counts overlap in time; ignored-window count is not an ignored-duration estimate.",
            "points": point_details, "windows": rows}


def export_source_samples(video: VideoFeatures, source: Path, output: Path, config: DetectionConfig,
                          timeout_sec: float) -> tuple[list[dict], dict]:
    """Verify bytes + exact cached PTS/indices before calling a review pack complete."""
    if sha256_file(source) != video.digest:
        raise ValueError("Source hash differs from feature cache")
    source_stat = source.stat()
    metadata = probe_video(source)
    frames = []
    output.mkdir()
    expected = video.metadata["frames"]
    for number, frame in enumerate(sample_video(source, config.sample_fps, timeout_sec)):
        if number >= len(expected):
            raise ValueError("Source sampling differs from cached frame count")
        actual, cached = frame.metadata(), expected[number]
        if any(actual[key] != cached[key] for key in actual if key != "timestamp_sec") or not np.isclose(
                actual["timestamp_sec"], cached["timestamp_sec"], atol=1e-9, rtol=0):
            raise ValueError("Source PTS/origin/indices differ from cached samples")
        image = Image.fromarray(frame.rgb)
        image.thumbnail((640, 640))
        filename = f"sample_{number:05d}.jpg"
        image.save(output / filename, "JPEG", quality=85)
        frames.append({**actual, "jpeg": f"{output.name}/{filename}"})
    after = source.stat()
    if len(frames) != len(expected):
        raise ValueError("Source sampling differs from cached frame count")
    if (source_stat.st_size, source_stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Source changed during audit")
    return frames, metadata


def duration_summary(rows: list[dict]) -> dict:
    result = {}
    for partition in ("train", "validation"):
        result[partition] = {}
        for label in (0, 1):
            selected = [r for r in rows if r["split"] == partition and r["label"] == label]
            values = [r["source_metadata"]["duration_sec"] for r in selected]
            result[partition][str(label)] = {"videos": len(values), "duration_sec": values,
                "median_duration_sec": float(np.median(values)) if values else None,
                "window_counts": [sum(r["supervision"]["window_counts"].values()) for r in selected]}
    return result


def render_review(report: dict) -> str:
    escape = lambda value: html.escape(str(value), quote=True)
    sections = []
    for number, row in enumerate(report["videos"]):
        details, samples = row["supervision"], row["samples"]
        points = ", ".join(f"{p:.6g}" for p in row["timestamps_sec"]) or "—"
        windows_html = []
        for window in details["windows"]:
            target = {1: "positive (1)", 0: "negative (0)", -1: "ignored (-1)"}[window["target"]]
            images = "".join(f'<figure><img loading="lazy" src="{escape(samples[i]["jpeg"])}" alt="Source sample">'
                             f'<figcaption>PTS {samples[i]["timestamp_sec"]:.6f} s; frame {samples[i]["index"]}</figcaption></figure>'
                             for i in dict.fromkeys(window["sample_indices"]))
            windows_html.append(f'<details class="window"><summary>#{window["index"]}: '
                f'{window["start_sec"]:.6f}–{window["end_sec"]:.6f} s — {target}</summary>'
                f'<button type="button" data-seek="{window["start_sec"]}">Seek source</button>'
                f'<p>Model sample PTS: {escape(window["sample_times_sec"])}; '
                f'tail assignment: {window["tail_point_assigned"]}</p><div class="samples">{images}</div></details>')
        point_html = "".join(f'<li>Point {p["timestamp_sec"]:.6f} s → nearest source sample '
                             f'{p["nearest_sample_sec"]:.6f} s (gap {p["sample_distance_sec"]:.6f} s); '
                             f'positive windows {escape(p["positive_window_indices"])}; '
                             f'after last sample: {p["after_last_sample"]}; '
                             f'nearest point sample retained in head sequences: '
                             f'{escape(p["positive_window_sequence_checks"])}</li>' for p in details["points"])
        sections.append(f'''<section data-number="{number}"><h2>{escape(row["video_id"])} — {escape(row["split"])}</h2>
<p>Cached label: {row["label"]} (1 = potential event, 0 = negative). Points (seconds): {points}.
Group: {escape(row["group"])}. Duration: {row["source_metadata"]["duration_sec"]:.6f} s.</p>
<p>Counts: {escape(details["window_counts"])}. Supervised union: {details["supervised_union_sec"]:.6f} s;
outside supervised union through last sampled frame: {details["outside_supervised_union_sec"]:.6f} s.</p>
<p>Optional manifest label/points agreement: {escape(row["manifest_label_and_points_match"])}
(None = not supplied; False requires review). No workbook semantics have been inferred.</p>
<p>First decoded PTS: {samples[0]["origin_pts"]}; time base {samples[0]["time_base_num"]}/{samples[0]["time_base_den"]}.
ffprobe stream start: {row["source_metadata"]["start_time_sec"]:.6f} s. Cached zero = first decoded frame.</p>
<video controls preload="none" muted src="{escape(row["source_uri"])}"></video>
<p><a href="{escape(row["source_uri"])}">Open original MOV in a local player</a> if this codec is unsupported.
Browser seeking is approximate; JPEG PTS below are verified against the cache. Watch the full original,
including ignored regions, and compare the original workbook manually.</p><ul>{point_html}</ul>
<fieldset><legend>Human review — proposals only; never applied to training inputs</legend>
<label>Status <select name="review_status"><option>pending</option><option>checked</option><option>needs_correction</option><option>uncertain</option></select></label>
<label>Proposed label <select name="proposed_label"><option value="">No proposal</option><option>0</option><option>1</option></select></label>
<label>Proposed points (seconds; separated by ;) <input name="proposed_points_sec"></label>
<label>Proposed observed interval(s) and required context <textarea name="proposed_intervals"></textarea></label>
<label>Proposed scenario group (no identity recognition) <input name="proposed_group_id"></label>
<label>Workbook label mapping, timestamp units/origin, ambiguity and notes <textarea name="notes"></textarea></label>
<label><input name="original_workbook_checked" type="checkbox">Original workbook semantics checked</label>
<label><input name="full_video_checked" type="checkbox">Full source video, including ignored regions, reviewed</label>
<label><input name="origin_checked" type="checkbox">Seconds from first displayed frame verified</label>
</fieldset>{''.join(windows_html)}</section>''')
    identities = [{k: row[k] for k in ("video_id", "label", "timestamps_sec", "group", "split", "source_sha256")}
                  for row in report["videos"]]
    encoded = json.dumps(identities, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c")
    return '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' file:; media-src 'self' file:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; base-uri 'none'; object-src 'none'; form-action 'none'">
<title>LOCAL annotation audit</title><style>body{font:16px system-ui;max-width:1200px;margin:auto;padding:20px}section{border-top:2px solid #555;margin-top:30px}video{width:100%;max-height:500px}label{display:block;margin:8px}input:not([type=checkbox]),textarea{width:85%}.samples{display:flex;flex-wrap:wrap}figure{width:240px;margin:8px}img{width:100%}.window{margin:10px}fieldset{margin:20px 0}button{padding:8px}</style>
<h1>Private offline annotation review</h1><p>1 = potential event, 0 = negative, -1 = ignored.
Points are weak supervision, not event boundaries. Overlapping window counts are not time coverage.
No labels are changed. No network requests or external assets. Keep this pack and downloaded decisions local.</p>
<p>Record corrections and scenario groups only after human review. An absent visible action does not prove it never occurred.
Do not optimize labels/thresholds to repair these validation predictions. Review training annotations with the same criteria.</p>
<button id="export" type="button">Download review-decisions.json locally</button>
''' + "".join(sections) + f'<script id="identities" type="application/json">{encoded}</script>' + '''<script>
"use strict";
document.querySelectorAll('[data-seek]').forEach(button => button.addEventListener('click', () => {
  const video = button.closest('section').querySelector('video');
  const seek = () => { video.currentTime = Number(button.dataset.seek); };
  if (video.readyState) seek(); else {video.addEventListener('loadedmetadata', seek, {once:true}); video.load();}
}));
document.getElementById('export').addEventListener('click', () => {
  const identities = JSON.parse(document.getElementById('identities').textContent);
  const reviews = Array.from(document.querySelectorAll('section')).map(section => {
    const fields = {};
    section.querySelectorAll('[name]').forEach(input => {fields[input.name] = input.type === 'checkbox' ? input.checked : input.value;});
    return {original:identities[Number(section.dataset.number)], ...fields};
  });
  const blob = new Blob([JSON.stringify({proposals_only:true, reviews}, null, 2)], {type:'application/json'});
  const url = URL.createObjectURL(blob), link = document.createElement('a');
  link.href = url; link.download = 'review-decisions.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
</script></html>'''


def build_audit(features: Path, checkpoint: Path, output: Path, private_root: Path, *,
                split: str = "both", timeout_sec: float = 240, manifest: Path | None = None) -> dict:
    features, checkpoint, output = [private_path(p, private_root) for p in (features, checkpoint, output)]
    if output.exists():
        raise FileExistsError("Output exists")
    if split not in {"train", "validation", "both"}:
        raise ValueError("Annotation audit excludes test holdouts")
    videos = load_features(features, private_root)
    _, config, training = load_checkpoint(checkpoint)
    if dataset_fingerprint(videos) != training["dataset_fingerprint"]:
        raise ValueError("Checkpoint/cache fingerprint mismatch")
    validate_split(videos, training["split"])
    if any(v.metadata["sample_fps"] != config.sample_fps for v in videos):
        raise ValueError("Sampling rate differs from checkpoint")
    source_manifest = json.loads(private_path(manifest, private_root).read_text()) if manifest else None
    selected = [(v, name) for name in ("train", "validation") if split in {name, "both"}
                for v in videos if v.video_id in training["split"][name]]
    # Resolve every source boundary before processing any source video.
    paths = [private_path(v.metadata["video_path"], private_root) for v, _ in selected]
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    rows = []
    for number, ((video, partition), source) in enumerate(zip(selected, paths)):
        samples, metadata = export_source_samples(video, source, output / f"video_{number:04d}", config, timeout_sec)
        matches = [r for r in source_manifest["videos"] if r["video_id"] == video.video_id] if source_manifest else []
        agreement = None if not source_manifest else (len(matches) == 1 and matches[0]["label"] == video.label
                    and matches[0]["timestamps_sec"] == video.points)
        rows.append({"video_id": video.video_id, "split": partition, "label": video.label,
                     "timestamps_sec": video.points, "group": video.group, "source_sha256": video.digest,
                     "source_uri": source.as_uri(), "source_metadata": metadata,
                     "manifest_label_and_points_match": agreement,
                     "samples": samples, "supervision": supervision_details(video, config)})
    report = {"schema_version": 1, "status": "complete", "private_local_report": True,
              "labels_modified": False, "training_performed": False, "config": asdict(config),
              "test_videos_reviewed": False, "source_workbook_read": False,
              "annotation_unit_from_manifest": source_manifest.get("numeric_timestamp_unit") if source_manifest else None,
              "origin_assumption_requires_human_review": "timestamps are seconds from first decoded video frame",
              "duration_by_split_and_label": duration_summary(rows), "videos": rows}
    # Completion markers are absent if source/cache verification or decoding fails.
    with (output / "index.html").open("x", encoding="utf-8") as handle:
        handle.write(render_review(report))
    write_json(output / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", default="private_data")
    parser.add_argument("--features", required=True)
    parser.add_argument("--checkpoint", required=True, help="Use checkpoint config/split; no forward pass")
    parser.add_argument("--manifest", help="Optional original validation manifest to compare interpreted labels/points")
    parser.add_argument("--output", required=True, help="NEW private directory; never overwritten")
    parser.add_argument("--split", choices=["train", "validation", "both"], default="both")
    parser.add_argument("--timeout-sec", type=positive_float, default=240)
    args = parser.parse_args()
    build_audit(Path(args.features), Path(args.checkpoint), Path(args.output), Path(args.private_root),
                split=args.split, timeout_sec=args.timeout_sec, manifest=Path(args.manifest) if args.manifest else None)
    print("Offline annotation review pack saved. Open index.html locally; keep all artifacts private.")


if __name__ == "__main__":
    run_cli(main)
