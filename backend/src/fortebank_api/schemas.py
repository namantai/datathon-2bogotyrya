"""Strict Pydantic schemas for the public HTTP API."""

import base64
import binascii
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def require_json_number(value: object) -> object:
    """Reject bools and strings instead of coercing them into timestamps."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("value must be a JSON number")
    return value


NonNegativeFiniteFloat = Annotated[
    float,
    BeforeValidator(require_json_number),
    Field(ge=0, allow_inf_nan=False),
]


class PublicModel(BaseModel):
    """Forbid accidental additions to the jury-facing schema."""

    model_config = ConfigDict(extra="forbid", revalidate_instances="always")


class HealthResponse(PublicModel):
    """Response returned by the technical health endpoint."""

    status: Literal["ok"]
    service: str
    environment: str


class AnalyzeAccepted(PublicModel):
    """Fast acknowledgement returned after a job has been registered."""

    job_id: Annotated[str, Field(min_length=1)]


class EvidenceFrame(PublicModel):
    """A JPEG evidence frame encoded for JSON transport."""

    timestamp_sec: NonNegativeFiniteFloat
    image_base64: Annotated[str, Field(min_length=1)]

    @field_validator("image_base64")
    @classmethod
    def validate_jpeg_base64(cls, value: str) -> str:
        try:
            image = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("image_base64 must be valid base64") from exc

        if len(image) < 4 or not image.startswith(b"\xff\xd8\xff") or not image.endswith(b"\xff\xd9"):
            raise ValueError("image_base64 must contain JPEG bytes")
        return value


class Violation(PublicModel):
    """One possible violation reported by the finished analysis."""

    type: Annotated[str, Field(min_length=1)]
    timecode_sec: NonNegativeFiniteFloat
    description: Annotated[str, Field(min_length=1)]
    evidence_frames: Annotated[list[EvidenceFrame], Field(min_length=1, max_length=3)]

    @field_validator("type", "description")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text fields cannot be blank")
        return value


class ProcessingJobResponse(PublicModel):
    """Minimal public representation of a queued or running job."""

    status: Literal["processing"]


class FailedJobResponse(PublicModel):
    """Minimal public representation of a failed job."""

    status: Literal["failed"]


class DoneJobResponse(PublicModel):
    """Completed result that follows the immutable jury contract."""

    job_id: Annotated[str, Field(min_length=1)]
    status: Literal["done"]
    verdict: Literal["no_violation", "violation"]
    violations: list[Violation]

    @model_validator(mode="after")
    def validate_verdict_matches_violations(self) -> "DoneJobResponse":
        if self.verdict == "no_violation" and self.violations:
            raise ValueError("no_violation requires an empty violations array")
        if self.verdict == "violation" and not self.violations:
            raise ValueError("violation requires at least one event")
        return self


JobResponse = ProcessingJobResponse | DoneJobResponse | FailedJobResponse
