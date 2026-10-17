"""Bounded local extraction of real JPEG evidence frames with FFmpeg."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

from fortebank_api.schemas import EvidenceFrame


logger = logging.getLogger(__name__)

_PTS_TIME_PATTERN = re.compile(
    rb"\bn:\s*0\b.*?\bpts_time:\s*"
    rb"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
)
_START_OF_FRAME_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


class EvidenceExtractionError(RuntimeError):
    """Raised when required evidence cannot be extracted safely."""


@dataclass(frozen=True, slots=True)
class _VideoTimeline:
    duration_sec: float
    start_time_sec: float


class VideoEvidenceExtractor:
    """Extract original video frames into the jury-facing evidence schema.

    FFmpeg performs accurate input seeking and its ``showinfo`` filter reports
    the PTS of the frame that was actually encoded. The reported public
    timestamp is normalized to the start of the input video timeline.
    """

    def __init__(
        self,
        *,
        ffmpeg_path: str,
        ffprobe_path: str,
        timeout_sec: float,
        max_width: int,
        max_height: int,
        max_jpeg_bytes: int,
        jpeg_quality: int,
        endpoint_scan_sec: float,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.timeout_sec = timeout_sec
        self.max_width = max_width
        self.max_height = max_height
        self.max_jpeg_bytes = max_jpeg_bytes
        self.jpeg_quality = jpeg_quality
        self.endpoint_scan_sec = endpoint_scan_sec

    async def extract(
        self,
        video_path: Path,
        timestamps_sec: tuple[float, ...],
    ) -> list[EvidenceFrame]:
        """Return one real JPEG for each of one to three requested timestamps."""

        started = time.perf_counter()
        path = video_path.resolve()
        if not path.is_file():
            raise EvidenceExtractionError("Local video is unavailable")
        if not 1 <= len(timestamps_sec) <= 3:
            raise EvidenceExtractionError("An event requires one to three evidence timestamps")

        requested = tuple(self._validate_timestamp(value) for value in timestamps_sec)
        timeline = await self._probe_timeline(path)
        for timestamp in requested:
            if timestamp > timeline.duration_sec:
                raise EvidenceExtractionError("Evidence timestamp is outside the video")

        frames = [
            await self._extract_at(path, timestamp, timeline)
            for timestamp in requested
        ]
        logger.info(
            "Evidence extraction completed (frames=%d, duration_sec=%.3f)",
            len(frames),
            time.perf_counter() - started,
        )
        return frames

    @staticmethod
    def _validate_timestamp(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EvidenceExtractionError("Evidence timestamp must be a number")
        timestamp = float(value)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise EvidenceExtractionError(
                "Evidence timestamp must be finite and non-negative"
            )
        return timestamp

    async def _probe_timeline(self, path: Path) -> _VideoTimeline:
        command = [
            self.ffprobe_path,
            "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "mov",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=start_time,duration:format=start_time,duration",
            "-of",
            "json",
            str(path),
        ]
        stdout, _ = await self._run(command, tool_name="FFprobe")
        try:
            payload = json.loads(stdout)
            stream = payload["streams"][0]
            format_data = payload.get("format", {})
            duration = self._first_finite_number(
                format_data.get("duration"),
                stream.get("duration"),
            )
            start_time = self._first_finite_number(
                stream.get("start_time"),
                format_data.get("start_time"),
                default=0.0,
            )
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EvidenceExtractionError("Video timeline metadata is invalid") from exc

        if duration <= 0:
            raise EvidenceExtractionError("Video duration must be positive")
        return _VideoTimeline(duration_sec=duration, start_time_sec=start_time)

    @staticmethod
    def _first_finite_number(*values: object, default: float | None = None) -> float:
        for value in values:
            if value is None or value == "N/A":
                continue
            number = float(value)
            if math.isfinite(number):
                return number
        if default is not None:
            return default
        raise ValueError("No finite number is available")

    async def _extract_at(
        self,
        path: Path,
        requested_sec: float,
        timeline: _VideoTimeline,
    ) -> EvidenceFrame:
        result = await self._run_ffmpeg(path, requested_sec, timeline)
        if result is None:
            fallback_sec = await self._last_frame_at_or_before(
                path,
                requested_sec,
                timeline,
            )
            if fallback_sec is None:
                raise EvidenceExtractionError("FFmpeg returned no evidence frame")
            # Seek a tiny amount before the known PTS so that floating-point
            # rounding cannot skip the final frame.
            result = await self._run_ffmpeg(
                path,
                max(0.0, fallback_sec - 1e-6),
                timeline,
            )
            if result is None:
                raise EvidenceExtractionError("FFmpeg returned no evidence frame")

        jpeg, actual_timestamp_sec = result
        width, height = self._jpeg_dimensions(jpeg)
        if width > self.max_width or height > self.max_height:
            raise EvidenceExtractionError("Extracted JPEG exceeds configured dimensions")
        if len(jpeg) > self.max_jpeg_bytes:
            raise EvidenceExtractionError("Extracted JPEG exceeds configured byte limit")

        image_base64 = base64.b64encode(jpeg).decode("ascii")
        try:
            if base64.b64decode(image_base64, validate=True) != jpeg:
                raise EvidenceExtractionError("JPEG base64 round-trip failed")
            return EvidenceFrame(
                timestamp_sec=actual_timestamp_sec,
                image_base64=image_base64,
            )
        except ValueError as exc:
            raise EvidenceExtractionError("Extracted evidence is invalid") from exc

    async def _run_ffmpeg(
        self,
        path: Path,
        seek_sec: float,
        timeline: _VideoTimeline,
    ) -> tuple[bytes, float] | None:
        scale_filter = (
            f"scale=w='min(iw,{self.max_width})':h='min(ih,{self.max_height})':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2,showinfo"
        )
        command = [
            self.ffmpeg_path,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "info",
            "-threads",
            "1",
            "-copyts",
            "-ss",
            f"{seek_sec:.9f}",
            "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "mov",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-frames:v",
            "1",
            "-vf",
            scale_filter,
            "-fps_mode",
            "vfr",
            "-c:v",
            "mjpeg",
            "-q:v",
            str(self.jpeg_quality),
            "-f",
            "image2pipe",
            "pipe:1",
        ]
        jpeg, stderr = await self._run(command, tool_name="FFmpeg", allow_empty=True)
        if not jpeg:
            return None

        match = _PTS_TIME_PATTERN.search(stderr)
        if match is None:
            raise EvidenceExtractionError("FFmpeg did not report the extracted frame timestamp")
        absolute_pts = float(match.group(1))
        actual_timestamp = absolute_pts - timeline.start_time_sec
        if not math.isfinite(actual_timestamp) or actual_timestamp < -1e-6:
            raise EvidenceExtractionError("FFmpeg reported an invalid frame timestamp")
        actual_timestamp = max(0.0, actual_timestamp)
        if actual_timestamp > timeline.duration_sec + 1e-3:
            raise EvidenceExtractionError("FFmpeg frame timestamp is outside the video")
        return jpeg, actual_timestamp

    async def _last_frame_at_or_before(
        self,
        path: Path,
        requested_sec: float,
        timeline: _VideoTimeline,
    ) -> float | None:
        scan_start = max(0.0, requested_sec - self.endpoint_scan_sec)
        scan_end = min(timeline.duration_sec + 1e-3, requested_sec + 1e-3)
        command = [
            self.ffprobe_path,
            "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "mov",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-read_intervals",
            f"{scan_start:.9f}%{scan_end:.9f}",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "json",
            str(path),
        ]
        stdout, _ = await self._run(command, tool_name="FFprobe")
        try:
            payload = json.loads(stdout)
            candidates = []
            for frame in payload.get("frames", []):
                raw_timestamp = frame.get("best_effort_timestamp_time")
                if raw_timestamp in {None, "N/A"}:
                    continue
                relative = float(raw_timestamp) - timeline.start_time_sec
                if math.isfinite(relative) and -1e-6 <= relative <= requested_sec + 1e-6:
                    candidates.append(max(0.0, relative))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EvidenceExtractionError("FFprobe frame timestamps are invalid") from exc
        return max(candidates) if candidates else None

    async def _run(
        self,
        command: list[str],
        *,
        tool_name: str,
        allow_empty: bool = False,
    ) -> tuple[bytes, bytes]:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            raise EvidenceExtractionError(f"{tool_name} is unavailable") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout_sec,
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise EvidenceExtractionError(f"{tool_name} timed out") from exc
        except asyncio.CancelledError:
            process.kill()
            await process.communicate()
            raise

        empty_frame_output = (
            allow_empty
            and not stdout
            and (
                b"No filtered frames for output stream" in stderr
                or b"Nothing was written into output file" in stderr
            )
        )
        if process.returncode != 0 and not empty_frame_output:
            raise EvidenceExtractionError(f"{tool_name} failed to extract evidence")
        if not allow_empty and not stdout:
            raise EvidenceExtractionError(f"{tool_name} returned no data")
        return stdout, stderr

    @staticmethod
    def _jpeg_dimensions(jpeg: bytes) -> tuple[int, int]:
        """Validate a complete JPEG stream and return dimensions from its SOF."""

        if len(jpeg) < 4 or not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
            raise EvidenceExtractionError("FFmpeg output is not a complete JPEG")

        position = 2
        while position < len(jpeg):
            if jpeg[position] != 0xFF:
                position += 1
                continue
            while position < len(jpeg) and jpeg[position] == 0xFF:
                position += 1
            if position >= len(jpeg):
                break
            marker = jpeg[position]
            position += 1
            if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if position + 2 > len(jpeg):
                break
            segment_length = int.from_bytes(jpeg[position : position + 2], "big")
            if segment_length < 2 or position + segment_length > len(jpeg):
                break
            if marker in _START_OF_FRAME_MARKERS:
                if segment_length < 7:
                    break
                height = int.from_bytes(jpeg[position + 3 : position + 5], "big")
                width = int.from_bytes(jpeg[position + 5 : position + 7], "big")
                if width <= 0 or height <= 0:
                    break
                return width, height
            position += segment_length
        raise EvidenceExtractionError("JPEG dimensions could not be validated")
