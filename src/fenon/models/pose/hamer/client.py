"""HaMeR hand pose (contract ``fenon.hand_pose.video``): the job, the result and its frames.

A succeeded run's result, outputs and usage are validated against the contract as JSON text in strict mode,
because the contract's timestamps are strings. Frames are parsed from the annotations output each time they
are iterated; the output is downloaded the first time it is needed (docs/features/pose.md).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar, get_args

from pydantic import BaseModel, ValidationError

from ...._core import (
    ContractSupport,
    InvalidInputError,
    Job,
    Run,
    RunOutput,
    ServerError,
    Usage,
    decode_json,
)
from ._generated import (
    CONTRACT_ID,
    CONTRACT_VERSION,
    SCHEMA_SHA256,
    Camera,
    HandPoseFrame,
    HandPoseOutputs,
    HandPoseRunResult,
    ProcessedRange,
    Video,
)

CONTRACT = ContractSupport(CONTRACT_ID, {CONTRACT_VERSION: SCHEMA_SHA256})
ANNOTATIONS = "annotations"
VIDEO = "video"
USAGE_UNIT = "frame"
_JOINT_ORDER_TEXT: str = get_args(HandPoseFrame.model_fields["joint_order"].annotation)[0]
JOINT_ORDER: tuple[str, ...] = tuple(_JOINT_ORDER_TEXT.split(","))

ModelT = TypeVar("ModelT", bound=BaseModel)


class Frames:
    def __init__(self, output: RunOutput, processed: ProcessedRange) -> None:
        self._output = output
        self._range = processed

    def __iter__(self) -> Iterator[HandPoseFrame]:
        expected = self._range.end_frame - self._range.start_frame
        lines = self._output.content().splitlines()
        for number, line in enumerate(lines, start=1):
            frame = _parse_row(line, number)
            if number > expected or frame.frame_index != self._range.start_frame + number - 1:
                raise _invalid_line(number)
            yield frame
        if len(lines) != expected:
            raise ServerError(
                "The annotations output has fewer frames than the run processed.",
                code="output_invalid",
                fix="Report it with the run ID.",
                details={"expected_frames": expected, "received_frames": len(lines)},
            )


@dataclass(frozen=True, eq=False)
class HandPoseResult:
    id: str
    model: str
    model_version: str | None
    video: Video
    joint_order: tuple[str, ...]
    camera: Camera
    usage: Usage
    outputs_expire_at: datetime | None
    _annotations: RunOutput = field(repr=False)
    _video: RunOutput | None = field(repr=False)
    _range: ProcessedRange = field(repr=False)

    @property
    def frames(self) -> Frames:
        return Frames(self._annotations, self._range)

    def save(self, path: str | os.PathLike[str]) -> Path:
        return self._annotations.save(path)

    def save_video(self, path: str | os.PathLike[str]) -> Path:
        if self._video is None:
            raise InvalidInputError(
                "This run has no video: it was run without video=True.",
                code="video_not_requested",
                fix="Run the video again with video=True.",
            )
        return self._video.save(path)


def requested_outputs(video: bool) -> tuple[str, ...]:
    return (ANNOTATIONS, VIDEO) if video else (ANNOTATIONS,)


class HandPoseJob(Job[HandPoseResult]):
    _supported = (CONTRACT,)

    def _make_result(self, run: Run) -> HandPoseResult:
        result = _validated(HandPoseRunResult, run.result)
        if not run.outputs_expired:
            outputs = (
                None
                if run.outputs is None
                else {name: f.model_dump(mode="json") for name, f in run.outputs.items()}
            )
            _validated(HandPoseOutputs, outputs)
        frames = result.range.end_frame - result.range.start_frame
        usage = run.usage
        if (
            tuple(result.joint_order) != JOINT_ORDER
            or usage is None
            or usage.unit != USAGE_UNIT
            or usage.quantity != frames
        ):
            raise _invalid_run()
        return HandPoseResult(
            id=run.id,
            model=run.model,
            model_version=run.model_version,
            video=result.video,
            joint_order=JOINT_ORDER,
            camera=result.camera,
            usage=usage,
            outputs_expire_at=_timestamp(run.outputs_expire_at),
            _annotations=RunOutput(self._clients, run, ANNOTATIONS),
            _video=RunOutput(self._clients, run, VIDEO) if VIDEO in run.outputs_requested else None,
            _range=result.range,
        )


def _timestamp(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _validated(model: type[ModelT], value: Any) -> ModelT:
    try:
        parsed = model.model_validate_json(json.dumps(value), strict=True)
    except ValidationError:
        parsed = None
    if parsed is None:
        raise _invalid_run()
    return parsed


def _parse_row(line: bytes, number: int) -> HandPoseFrame:
    payload = decode_json(line)
    try:
        frame = HandPoseFrame.model_validate(payload, strict=True)
    except ValidationError:
        frame = None
    if frame is None:
        raise _invalid_line(number)
    return frame


def _invalid_line(number: int) -> ServerError:
    return ServerError(
        f"Line {number} of the annotations output doesn't match the contract.",
        code="output_invalid",
        fix="Report it with the run ID.",
        details={"line": number},
    )


def _invalid_run() -> ServerError:
    return ServerError(
        "The run's result doesn't match the contract.",
        code="invalid_response",
        fix="Report it with the run ID.",
    )
