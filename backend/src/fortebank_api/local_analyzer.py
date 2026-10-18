"""Opt-in local process adapter. No model, download or inference endpoint is bundled."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import re
import signal
import sys
from typing import Annotated

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StrictBool, model_validator

from fortebank_api.analyzers import AnalysisEvent, AnalysisResult
from fortebank_api.schemas import NonNegativeFiniteFloat


class LocalAnalyzerError(RuntimeError):
    """Safe error code; never contains child output or paths."""


class MLObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    class_id: Annotated[str, Field(min_length=1, max_length=128,
        validation_alias=AliasChoices("class_id", "code"))]
    timecode_sec: Annotated[NonNegativeFiniteFloat, Field(
        validation_alias=AliasChoices("timecode_sec", "timestamp_seconds"))]
    observation: Annotated[str, Field(min_length=1, max_length=4096,
        validation_alias=AliasChoices("observation", "description"))]
    start_sec: NonNegativeFiniteFloat | None = None
    end_sec: NonNegativeFiniteFloat | None = None
    confidence: Annotated[NonNegativeFiniteFloat, Field(le=1)] | None = None
    # Legacy hint retained for adapters, NEVER used as legal authority.
    rule_id: Annotated[str, Field(max_length=128)] | None = None
    evidence_timestamps_sec: Annotated[list[NonNegativeFiniteFloat], Field(min_length=1, max_length=3)] | None = None
    conditions: Annotated[dict[str, StrictBool], Field(max_length=32)] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_interval(self) -> MLObservation:
        start = self.timecode_sec if self.start_sec is None else self.start_sec
        end = self.timecode_sec if self.end_sec is None else self.end_sec
        if not start <= self.timecode_sec <= end or not self.class_id.strip() or not self.observation.strip():
            raise ValueError("Invalid observation or interval")
        return self

    def to_event(self) -> AnalysisEvent:
        return AnalysisEvent(
            class_id=self.class_id, timecode_sec=self.timecode_sec,
            start_sec=self.timecode_sec if self.start_sec is None else self.start_sec,
            end_sec=self.timecode_sec if self.end_sec is None else self.end_sec,
            confidence=self.confidence, observation=self.observation, rule_id=self.rule_id,
            evidence_timestamps_sec=tuple(self.evidence_timestamps_sec or [self.timecode_sec]),
            conditions=self.conditions,
        )


class MLResult(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    # Explicit [] is required: missing output must not become no_violation.
    events: Annotated[list[MLObservation], Field(max_length=128)]


class LocalProcessAnalyzer:
    """Run a trusted module:function in a fresh process, reaped before returning.

    Fresh-process model load time is part of the deadline. This is not a sandbox
    for untrusted Python; licensing, offline execution and model audit are prerequisites.
    """

    def __init__(self, entrypoint: str) -> None:
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", entrypoint):
            raise ValueError("Expected a trusted module:function entrypoint")
        self.entrypoint = entrypoint

    async def analyze_video(self, local_video_path: Path) -> AnalysisResult:
        # Do not inherit API keys, tokens or cloud credentials into model code.
        environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        environment.update(
            PYTHONPATH=os.pathsep.join(str(Path(p or ".").resolve()) for p in sys.path),
            PYTHONDONTWRITEBYTECODE="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "fortebank_api.ml_runner", self.entrypoint,
            str(local_video_path.resolve()),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL, env=environment,
            start_new_session=os.name == "posix",
        )
        try:
            assert process.stdout is not None
            payload = bytearray()
            while chunk := await process.stdout.read(65536):
                payload.extend(chunk)
                if len(payload) > 1024 * 1024:
                    raise LocalAnalyzerError("Local analyzer output exceeds limit")
            await process.wait()
            if process.returncode != 0:
                raise LocalAnalyzerError("Local analyzer failed")
            parsed = MLResult.model_validate_json(bytes(payload))
            return AnalysisResult(events=tuple(event.to_event() for event in parsed.events))
        except asyncio.CancelledError:
            raise
        except Exception:
            raise LocalAnalyzerError("Local analyzer returned no valid result") from None
        finally:
            # POSIX process group also covers ordinary decoder subprocesses.
            with contextlib.suppress(ProcessLookupError):
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                elif process.returncode is None:
                    process.kill()
            await process.communicate()
