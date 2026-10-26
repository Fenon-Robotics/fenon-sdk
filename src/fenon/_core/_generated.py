"""Shared Fenon API shapes, generated from docs/openapi.yaml by scripts/generate_types.py. Do not edit."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from typing_extensions import TypeAliasType

UploadId = TypeAliasType("UploadId", Annotated[str, Field(..., pattern="^upl_[0-9a-z]{12}$")])


RunId = TypeAliasType("RunId", Annotated[str, Field(..., pattern="^run_[0-9a-z]{12}$")])


Timestamp = TypeAliasType("Timestamp", AwareDatetime)


class ContractRef(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
    )
    id: str = Field(..., examples=["fenon.hand_pose.video"])
    version: str = Field(..., pattern="^[0-9]+\\.[0-9]+\\.[0-9]+$")
    schema_sha256: str = Field(
        ...,
        description="SHA-256 of the contract's input, result, outputs and row schema files, concatenated in that order. A published version's files never change.",
        pattern="^[0-9a-f]{64}$",
    )


class Limits(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
    )
    content_types: list[str] = Field(..., min_length=1)
    max_size_bytes: int = Field(..., ge=1)
    max_duration_s: float = Field(..., gt=0.0)
    max_run_s: int = Field(
        ...,
        description="A run still not final after this many seconds fails with deadline_exceeded.",
        ge=1,
    )


class Model(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
    )
    id: str = Field(..., examples=["fenon/hand-pose-hm"])
    model_version: str = Field(..., description="Immutable.")
    modes: list[Literal["recorded", "live"]] = Field(..., min_length=1)
    contract: ContractRef
    limits: Limits
    usage_unit: str = Field(..., examples=["frame"])


class ModelPage(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
    )
    data: list[Model]
    next_cursor: str | None


class UploadCreate(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
    )
    filename: str = Field(
        ...,
        description="Display only; never used as the storage key.",
        max_length=255,
        min_length=1,
    )
    content_type: str
    size_bytes: int = Field(..., ge=1)


class UploadReservation(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
    )
    id: UploadId
    status: Literal["pending"]
    upload_url: str = Field(..., description="Presigned R2 URL. Send no Authorization header.", repr=False)
    method: Literal["PUT"]
    headers: dict[str, str] = Field(
        ...,
        description="Send exactly these headers with the PUT (Content-Type, Content-Length).",
    )
    expires_at: Timestamp


class Media(BaseModel):
    """
    Filled when status is ready.
    """

    model_config = ConfigDict(
        extra="ignore",
    )
    duration_s: float = Field(..., gt=0.0)
    fps: float = Field(..., gt=0.0)
    frame_count: int = Field(..., ge=1)
    width: int = Field(..., ge=1)
    height: int = Field(..., ge=1)
    constant_frame_rate: bool


class RunCreate(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
    )
    model: str
    model_version: str | None = Field(
        None,
        description="Optional pin to an exact version; null uses the version the model ID points to. The SDK doesn't send it in v1.",
    )
    input: dict[str, Any] = Field(
        ...,
        description="Must match the model contract's input schema (for fenon.hand_pose.video, contracts/fenon.hand_pose.video/1.0.0/input.schema.json).",
    )


RunStatus = TypeAliasType(
    "RunStatus",
    Annotated[
        Literal[
            "preparing_input",
            "queued",
            "processing_frames",
            "writing_results",
            "succeeded",
            "failed",
            "canceled",
        ],
        Field(
            ...,
            description="Fixed for all of /v1. Final statuses are succeeded, failed and canceled.",
        ),
    ],
)


class Progress(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
    )
    completed: int = Field(..., ge=0)
    total: int = Field(..., ge=0)
    unit: str


class Usage(BaseModel):
    """
    How much of the model's usage unit a run consumed.
    """

    model_config = ConfigDict(
        extra="ignore",
    )
    unit: str
    quantity: int = Field(..., ge=0)


class OutputFile(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
    )
    content_type: str
    url: str = Field(
        ...,
        description="Presigned R2 URL. Send no Authorization header. Fetch the run again for a fresh URL.",
        repr=False,
    )
    url_expires_at: Timestamp
    size_bytes: int = Field(..., ge=0)


class InputFiles(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
    )
    id: UploadId
    filename: str
    size_bytes: int = Field(..., ge=1)


class ErrorObject(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
    )
    code: str = Field(
        ...,
        description="May grow; clients keep unknown codes as they are. Run failures share input_unreadable, processing_failed, deadline_exceeded, internal_error, canceled; contracts add their own (fenon.hand_pose.video: video_too_long, variable_frame_rate).",
    )
    message: str = Field(..., description="Never contains keys or presigned URLs.")
    fix: str | None
    retryable: bool
    retry_after_s: float | None = Field(..., ge=0.0)
    details: dict[str, Any] = Field(
        ...,
        description="Always present. For input_invalid: { field } as a JSON Pointer (RFC 6901) into the request body; for not_implemented: { port }.",
    )


class RunErrorObject(ErrorObject):
    model_config = ConfigDict(
        extra="ignore",
    )
    stage: Literal["preparing_input", "queued", "processing_frames", "writing_results"] = Field(
        ..., description="The status the run was in when it stopped."
    )
    frame: int | None = Field(None, description="Present when the failure is tied to one frame.", ge=0)
    timestamp_s: float | None = Field(None, ge=0.0)


class Error(ErrorObject):
    model_config = ConfigDict(
        extra="ignore",
    )
    request_id: str = Field(..., pattern="^req_[0-9a-z]{12}$")


class ErrorResponse(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
    )
    error: Error


class Upload(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
    )
    id: UploadId
    filename: str = Field(..., description="Display name sent when the upload was reserved.")
    status: Literal["pending", "probing", "ready", "invalid"]
    content_type: str
    size_bytes: int = Field(..., ge=1)
    media: Media | None = Field(..., description="Filled when status is ready.")
    error: ErrorObject | None = Field(
        ...,
        description="Filled when status is invalid (input_unreadable). Model-specific checks such as duration happen in the run, not here.",
    )
    created_at: Timestamp


class Run(BaseModel):
    """
    Non-null by status: preparing_input: base fields only; queued: + usage_estimate; processing_frames and writing_results: + started_at, progress; succeeded: + outputs, result, usage, finished_at; failed: + error, usage (0), finished_at; canceled: + error (code canceled), usage (0), finished_at. The run deadline counts from created_at.

    """

    model_config = ConfigDict(
        extra="ignore",
    )
    id: RunId
    object: Literal["run"]
    model: str
    model_version: str
    contract: ContractRef
    status: RunStatus
    input: dict[str, Any] = Field(..., description="As sent, per the contract's input schema.")
    input_files: dict[str, InputFiles] = Field(
        ...,
        description="Each upload the input refers to, keyed by input field, for display in run history.",
    )
    progress: Progress | None
    usage_estimate: Usage | None
    outputs: dict[str, OutputFile] | None
    result: dict[str, Any] | None = Field(..., description="Defined by the contract's result schema.")
    usage: Usage | None
    error: RunErrorObject | None
    created_at: Timestamp
    started_at: Timestamp | None
    finished_at: Timestamp | None


class RunPage(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
    )
    data: list[Run]
    next_cursor: str | None
