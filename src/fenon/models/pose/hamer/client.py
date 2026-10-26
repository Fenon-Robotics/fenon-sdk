"""HaMeR hand pose (contract ``fenon.hand_pose.video``): the job, the result and its frames."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar, get_args

from pydantic import BaseModel, ValidationError

from ...._core._generated import Run
from ...._core.errors import ServerError
from ...._core.http import decode_json
from ...._core.runs import ContractSupport, Job
from ...._core.usage import Usage
from ..base import RunOutput
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

CONTRACT = ContractSupport(
    CONTRACT_ID, int(CONTRACT_VERSION.split(".")[0]), {CONTRACT_VERSION: SCHEMA_SHA256}
)
ANNOTATIONS = "annotations"
USAGE_UNIT = "frame"
_JOINT_ORDER_TEXT: str = get_args(HandPoseFrame.model_fields["joint_order"].annotation)[0]
JOINT_ORDER: tuple[str, ...] = tuple(_JOINT_ORDER_TEXT.split(","))

ModelT = TypeVar("ModelT", bound=BaseModel)


class Frames:
    """The run's frames, parsed from the annotations output each time they are iterated."""

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
    """A finished hand-pose run: the video it read, the camera it assumed, its usage and its frames."""

    id: str
    model: str
    model_version: str
    video: Video
    joint_order: tuple[str, ...]
    camera: Camera
    usage: Usage
    _annotations: RunOutput = field(repr=False)
    _range: ProcessedRange = field(repr=False)

    @property
    def frames(self) -> Frames:
        """Every processed frame, in order; the output is downloaded the first time it is needed."""
        return Frames(self._annotations, self._range)

    def save(self, path: str | os.PathLike[str]) -> Path:
        """Write the annotations output (JSON Lines) to ``path`` and return the path."""
        return self._annotations.save(path)


class HandPoseJob(Job[HandPoseResult]):
    """A hand-pose run in progress; ``result()`` waits for it."""

    _supported = (CONTRACT,)

    def _make_result(self, run: Run) -> HandPoseResult:
        result = _validated(HandPoseRunResult, run.result)
        outputs = None if run.outputs is None else {name: f.model_dump() for name, f in run.outputs.items()}
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
            _annotations=RunOutput(self._clients, run, ANNOTATIONS),
            _range=result.range,
        )


def _validated(model: type[ModelT], value: Any) -> ModelT:
    """Validate a part of a succeeded run against its contract model."""
    try:
        parsed = model.model_validate(value, strict=True)
    except ValidationError:
        parsed = None
    if parsed is None:
        raise _invalid_run()
    return parsed


def _parse_row(line: bytes, number: int) -> HandPoseFrame:
    """Parse one line of the annotations output."""
    payload = decode_json(line)
    try:
        frame = HandPoseFrame.model_validate(payload, strict=True)
    except ValidationError:
        frame = None
    if frame is None:
        raise _invalid_line(number)
    return frame


def _invalid_line(number: int) -> ServerError:
    """Return the error for a line of the annotations output that breaks the contract."""
    return ServerError(
        f"Line {number} of the annotations output doesn't match the contract.",
        code="output_invalid",
        fix="Report it with the run ID.",
        details={"line": number},
    )


def _invalid_run() -> ServerError:
    """Return the error for a succeeded run whose result, outputs or usage break the contract."""
    return ServerError(
        "The run's result doesn't match the contract.",
        code="invalid_response",
        fix="Report it with the run ID.",
    )
