"""Analyzer boundary and development-only implementations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from fortebank_api.config import Settings


@dataclass(frozen=True, slots=True)
class AnalysisEvent:
    """Draft internal event aligned with ``docs/ml-contract.md``."""

    class_id: str
    start_sec: float
    end_sec: float
    timecode_sec: float
    confidence: float
    observation: str
    rule_id: str | None
    evidence_timestamps_sec: tuple[float, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Internal analyzer result; an empty tuple means no detected events."""

    events: tuple[AnalysisEvent, ...] = field(default_factory=tuple)
    diagnostics: dict | None = None


class Analyzer(Protocol):
    """Async service boundary around the future local ML adapter."""

    async def analyze_video(self, local_video_path: Path) -> AnalysisResult:
        """Analyze one local video or raise a controlled exception."""


class AnalyzerUnavailableError(RuntimeError):
    """Raised when no real analyzer is configured."""


class DisabledAnalyzer:
    """Fail closed instead of returning a fabricated production verdict."""

    async def analyze_video(self, local_video_path: Path) -> AnalysisResult:
        del local_video_path
        raise AnalyzerUnavailableError("A production analyzer is not configured")


def create_analyzer(settings: Settings) -> Analyzer:
    """Select explicitly; local mode never falls back to the development stub."""
    if settings.analyzer_mode == "local":
        from fortebank_api.ml_analyzer import LocalMLAnalyzer
        return LocalMLAnalyzer(settings)
    return FakeAnalyzer() if settings.analyzer_mode == "fake" else DisabledAnalyzer()


class FakeAnalyzer:
    """Development/test stub; never a real AI model or production analyzer."""

    def __init__(
        self,
        result: AnalysisResult | None = None,
        *,
        delay_sec: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self._result = result or AnalysisResult()
        self._delay_sec = delay_sec
        self._error = error

    async def analyze_video(self, local_video_path: Path) -> AnalysisResult:
        if not local_video_path.is_file():
            raise FileNotFoundError("Local video is unavailable")
        if self._delay_sec > 0:
            await asyncio.sleep(self._delay_sec)
        if self._error is not None:
            raise self._error
        return self._result
