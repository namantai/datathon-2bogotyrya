"""Stream original video frames, preserving PTS and rotation; never decode audio."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import subprocess
import time
from typing import Iterator

import numpy as np
from PIL import Image

from ml.local_io import positive_float, private_path, run_cli, write_json


@dataclass(frozen=True)
class SampledFrame:
    index: int
    pts: int
    time_base_num: int
    time_base_den: int
    origin_pts: int
    timestamp_sec: float
    rgb: np.ndarray

    def metadata(self) -> dict:
        return {name: getattr(self, name) for name in (
            "index", "pts", "time_base_num", "time_base_den", "origin_pts", "timestamp_sec")}


def probe_video(path: Path, timeout_sec: float = 30) -> dict:
    if not path.is_file() or path.suffix.lower() != ".mov":
        raise ValueError("Expected local MOV file")
    try:
        result = subprocess.run(
            ["ffprobe", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov",
             "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", str(path.resolve())],
            capture_output=True, timeout=timeout_sec, check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Video metadata timeout") from None
    if result.returncode:
        raise ValueError("Cannot read video metadata")
    streams = json.loads(result.stdout).get("streams", [])
    if len(streams) != 1:
        raise ValueError("Video stream unavailable")
    stream = streams[0]
    duration = float(stream.get("duration", "nan"))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Video duration unavailable")
    rotation = next((float(s["rotation"]) for s in stream.get("side_data_list", []) if "rotation" in s),
                    float(stream.get("tags", {}).get("rotate", 0)))
    if not math.isfinite(rotation) or rotation % 90 != 0:
        raise ValueError("Only right-angle display rotations are supported")
    width, height = int(stream["width"]), int(stream["height"])
    if width <= 0 or height <= 0:
        raise ValueError("Invalid video dimensions")
    return {"duration_sec": duration, "width": width, "height": height,
            "rotation_deg_ccw": rotation, "avg_frame_rate": stream.get("avg_frame_rate"),
            "time_base": stream.get("time_base"), "start_time_sec": float(stream.get("start_time", 0)),
            "declared_frames": int(stream.get("nb_frames", "0")) if str(stream.get("nb_frames", "0")).isdigit() else 0}


def sample_video(path: Path, sample_fps: float = 2, timeout_sec: float = 240) -> Iterator[SampledFrame]:
    """First real frame at/after each sample grid point, plus the last frame.

    Times are relative to the first displayed frame, not frame_index / avg_fps.
    At most one RGB frame is retained internally. No seeking or audio decoding.
    Timeout is a cooperative wall deadline (including consumer work), not an OS watchdog.
    """
    import av

    if not all(math.isfinite(v) and v > 0 for v in (sample_fps, timeout_sec)):
        raise ValueError("Sampling rate and deadline must be positive and finite")
    metadata = probe_video(path)
    began = time.monotonic()
    container = av.open(str(path), format="mov", options={
        "protocol_whitelist": "file,pipe", "enable_drefs": "0", "use_absolute_path": "0",
    })
    try:
        stream = container.streams.video[0]
        stream.thread_count = 1
        origin = None
        previous = -math.inf
        next_sample = 0.0
        last_selected = -1
        last_frame = None
        index = -1
        time_base = None
        for index, frame in enumerate(container.decode(stream)):
            if time.monotonic() - began > timeout_sec:
                raise RuntimeError("Video processing deadline exceeded")
            if frame.is_corrupt or frame.pts is None or frame.time_base is None:
                raise ValueError("Corrupt frame or absent PTS")
            if time_base is None:
                time_base = frame.time_base
            if frame.time_base != time_base:
                raise ValueError("Changing time base is unsupported")
            if origin is None:
                origin = frame.pts
            timestamp = float((frame.pts - origin) * frame.time_base)
            if timestamp <= previous:
                raise ValueError("Non-monotonic PTS")
            previous = timestamp
            last_frame = frame
            if timestamp + 1e-9 >= next_sample:
                rgb = frame.to_ndarray(format="rgb24")
                rgb = np.ascontiguousarray(np.rot90(rgb, round(metadata["rotation_deg_ccw"] / 90)))
                yield SampledFrame(index, frame.pts, time_base.numerator, time_base.denominator, origin, timestamp, rgb)
                last_selected = index
                next_sample = (math.floor((timestamp + 1e-9) * sample_fps) + 1) / sample_fps
        if last_frame is None or origin is None or time_base is None:
            raise ValueError("Video has no decodable frames")
        if metadata["declared_frames"] and index + 1 != metadata["declared_frames"]:
            raise ValueError("Decoded frame count differs from MOV metadata")
        if last_selected != index:
            rgb = last_frame.to_ndarray(format="rgb24")
            rgb = np.ascontiguousarray(np.rot90(rgb, round(metadata["rotation_deg_ccw"] / 90)))
            yield SampledFrame(index, last_frame.pts, time_base.numerator, time_base.denominator, origin, previous, rgb)
    except av.error.FFmpegError:
        raise ValueError("Video decoding failed") from None
    finally:
        container.close()


def preprocess_video(path: Path, output: Path, *, sample_fps: float = 2, timeout_sec: float = 240) -> dict:
    """Export source-resolution JPEG samples; manifest is written only on success."""
    output.mkdir(parents=True, exist_ok=False)
    samples = []
    for frame in sample_video(path, sample_fps, timeout_sec):
        name = f"frame_{frame.index:09d}.jpg"
        Image.fromarray(frame.rgb).save(output / name, format="JPEG", quality=95)
        samples.append({**frame.metadata(), "jpeg": name})
    manifest = {"schema_version": 1, "video_path": str(path.resolve()), "sample_fps": sample_fps,
                "timestamp_origin": "first_decoded_video_frame", "frames": samples}
    write_json(output / "frames.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", default="private_data")
    parser.add_argument("--video", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sample-fps", type=positive_float, default=2)
    parser.add_argument("--timeout-sec", type=positive_float, default=240)
    args = parser.parse_args()
    preprocess_video(private_path(args.video, args.private_root), private_path(args.output, args.private_root),
                     sample_fps=args.sample_fps, timeout_sec=args.timeout_sec)
    print("Local frame samples and PTS manifest saved.")


if __name__ == "__main__":
    run_cli(main)
