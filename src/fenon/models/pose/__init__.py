"""Pose estimation models, exposed as ``client.pose``."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal, overload

from ..._core.errors import InvalidInputError
from ..._core.http import HttpClients
from ..._core.runs import create_run
from .base import RECORDED, prepare_video
from .hamer import CONTRACT, HandPoseJob, HandPoseResult

_HAND_POSE_JOBS: Mapping[str, type[HandPoseJob]] = {CONTRACT.id: HandPoseJob}
_HAND_POSE_CONTRACTS = (CONTRACT,)


class Pose:
    """Pose estimation; ``hand`` runs a hand-pose model on a whole recorded video."""

    def __init__(self, clients: HttpClients) -> None:
        self._clients = clients

    @overload
    def hand(
        self,
        *,
        model: str,
        input: str | os.PathLike[str],
        mode: str = RECORDED,
        wait: Literal[True] = True,
    ) -> HandPoseResult: ...

    @overload
    def hand(
        self, *, model: str, input: str | os.PathLike[str], mode: str = RECORDED, wait: Literal[False]
    ) -> HandPoseJob: ...

    @overload
    def hand(
        self, *, model: str, input: str | os.PathLike[str], mode: str = RECORDED, wait: bool
    ) -> HandPoseResult | HandPoseJob: ...

    def hand(
        self, *, model: str, input: str | os.PathLike[str], mode: str = RECORDED, wait: bool = True
    ) -> HandPoseResult | HandPoseJob:
        """Run a hand-pose model on a local video; return its result, or its job when ``wait=False``."""
        model_id = _model_id(model)
        run_input = prepare_video(self._clients, input, mode)
        snapshot = create_run(
            self._clients, model=model_id, run_input=run_input, supported=_HAND_POSE_CONTRACTS
        )
        job = _HAND_POSE_JOBS[snapshot.run.contract.id](self._clients, snapshot, cancel_on_interrupt=wait)
        return job.result() if wait else job


def _model_id(model: object) -> str:
    """Return ``model`` when it is a non-empty model ID string."""
    if not isinstance(model, str):
        raise TypeError("model must be a model ID string, for example 'fenon/hand-pose-hm'.")
    if not model.strip():
        raise InvalidInputError(
            "model is empty.", code="invalid_argument", fix="Pass a model ID such as fenon/hand-pose-hm."
        )
    return model
