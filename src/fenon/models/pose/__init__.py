"""Pose estimation models, exposed as ``client.pose`` (docs/features/pose.md).

``hand`` runs a hand-pose model on a whole local video. ``mode="recorded"``, the default, uploads it and
returns the result, or the job with ``wait=False``; ``video=True`` also asks for the video with the hands
drawn on it. ``mode="live"`` is refused while ``base.LIVE_MODE_AVAILABLE`` is off. ``wait`` and ``video`` must
be booleans.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal, overload

from ..._core import HttpClients, InvalidInputError, create_run
from . import base
from .base import LIVE, RECORDED, prepare_video
from .hamer import (
    CONTRACT,
    HandPoseJob,
    HandPoseLiveResult,
    HandPoseResult,
    requested_outputs,
    run_hand_pose_live,
)

_HAND_POSE_JOBS: Mapping[str, type[HandPoseJob]] = {CONTRACT.id: HandPoseJob}
_HAND_POSE_CONTRACTS = (CONTRACT,)


class Pose:
    def __init__(self, clients: HttpClients) -> None:
        self._clients = clients

    @overload
    def hand(
        self,
        *,
        model: str,
        input: str | os.PathLike[str],
        mode: Literal["recorded"] = "recorded",
        wait: Literal[True] = True,
        video: bool = False,
    ) -> HandPoseResult: ...

    @overload
    def hand(
        self,
        *,
        model: str,
        input: str | os.PathLike[str],
        mode: Literal["recorded"] = "recorded",
        wait: Literal[False],
        video: bool = False,
    ) -> HandPoseJob: ...

    @overload
    def hand(
        self,
        *,
        model: str,
        input: str | os.PathLike[str],
        mode: Literal["recorded"] = "recorded",
        wait: bool,
        video: bool = False,
    ) -> HandPoseResult | HandPoseJob: ...

    @overload
    def hand(
        self, *, model: str, input: str | os.PathLike[str], mode: Literal["live"]
    ) -> HandPoseLiveResult: ...

    def hand(
        self,
        *,
        model: str,
        input: str | os.PathLike[str],
        mode: str = RECORDED,
        wait: bool = True,
        video: bool = False,
    ) -> HandPoseResult | HandPoseJob | HandPoseLiveResult:
        model_id = _model_id(model)
        _check_flag("video", video)
        _check_flag("wait", wait)
        if mode not in (RECORDED, LIVE):
            raise InvalidInputError(
                f"mode must be {RECORDED!r} (or {LIVE!r}, coming soon).",
                code="mode_not_supported",
                fix='Pass mode="recorded", or leave mode out.',
            )
        if mode == LIVE:
            return self._live(model_id, input, wait=wait, video=video)
        run_input = prepare_video(self._clients, input)
        snapshot = create_run(
            self._clients,
            model=model_id,
            run_input=run_input,
            outputs=requested_outputs(video),
            supported=_HAND_POSE_CONTRACTS,
        )
        job = _HAND_POSE_JOBS[snapshot.run.contract.id](self._clients, snapshot, cancel_on_interrupt=wait)
        return job.result() if wait else job

    def _live(
        self, model: str, video_path: str | os.PathLike[str], *, wait: bool, video: bool
    ) -> HandPoseLiveResult:
        if not base.LIVE_MODE_AVAILABLE:
            raise InvalidInputError(
                "Live mode isn't available yet; it's coming soon.",
                code="mode_not_available",
                fix='Use mode="recorded" (the default).',
            )
        if wait is not True or video:
            raise InvalidInputError(
                "A live session runs the video to its end and returns no video; wait=False and video=True "
                "aren't supported with mode='live'.",
                code="invalid_argument",
                fix="Leave wait and video out with mode='live'.",
            )
        return run_hand_pose_live(self._clients, model, video_path)


def _check_flag(name: str, value: object) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be True or False.")


def _model_id(model: object) -> str:
    if not isinstance(model, str):
        raise TypeError("model must be a model ID string, for example 'fenon/hand-pose-hm'.")
    if not model.strip():
        raise InvalidInputError(
            "model is empty.", code="invalid_argument", fix="Pass a model ID such as fenon/hand-pose-hm."
        )
    return model
