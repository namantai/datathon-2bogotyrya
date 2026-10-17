"""Local MOV container validation using FFprobe."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Protocol


class InvalidVideoError(ValueError):
    """Raised when FFprobe cannot confirm a supported MOV video."""


class VideoValidationUnavailableError(RuntimeError):
    """Raised when FFprobe is missing or cannot be executed."""


class VideoValidator(Protocol):
    async def validate(self, path: Path) -> None:
        """Validate a locally stored file or raise a controlled exception."""


class FFprobeVideoValidator:
    """Validate container metadata without shell interpolation or network access."""

    def __init__(self, executable: str, timeout_sec: float) -> None:
        self.executable = executable
        self.timeout_sec = timeout_sec

    async def validate(self, path: Path) -> None:
        command = [
            self.executable,
            "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "mov",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type:format=format_name,duration",
            "-of",
            "json",
            str(path),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise VideoValidationUnavailableError("FFprobe is not installed") from exc
        except OSError as exc:
            raise VideoValidationUnavailableError("FFprobe validation is unavailable") from exc

        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout_sec,
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise VideoValidationUnavailableError("FFprobe validation timed out") from exc
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise

        if process.returncode != 0:
            raise InvalidVideoError("The uploaded file is not a valid video")

        try:
            payload = json.loads(stdout)
            format_data = payload["format"]
            formats = set(str(format_data["format_name"]).split(","))
            duration = float(format_data["duration"])
            streams = payload["streams"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InvalidVideoError("FFprobe returned incomplete video metadata") from exc

        has_video = any(stream.get("codec_type") == "video" for stream in streams)
        if "mov" not in formats or not has_video:
            raise InvalidVideoError("The file must contain a MOV video stream")
        if not math.isfinite(duration) or duration <= 0:
            raise InvalidVideoError("The video duration must be positive and finite")
