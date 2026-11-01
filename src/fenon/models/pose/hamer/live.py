"""HaMeR hand pose over a live session: typed frames from the draft live data (docs/features/pose.md)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from ...._core import HttpClients, LiveSessionError, parse_contract
from ...._live import LiveFrame, LiveGap, LiveRun, LiveUsage, run_file
from ._generated import Hand
from ._generated_live import Camera, HandPoseLiveData
from .client import JOINT_ORDER

_DATA = TypeAdapter(HandPoseLiveData)
_HAND = TypeAdapter(Hand)


@dataclass(frozen=True)
class HandPoseLiveFrame:
    frame_index: int
    time_s: float
    image_size: tuple[int, int]
    camera: Camera
    raw_hand_count: int
    suppressed_duplicate_count: int
    hands: tuple[Hand, ...]


@dataclass(frozen=True, eq=False)
class HandPoseLiveResult:
    session_id: str
    model: str
    joint_order: tuple[str, ...]
    frames: tuple[HandPoseLiveFrame, ...]
    gaps: tuple[LiveGap, ...]
    end_reason: str | None
    usage: LiveUsage
    frames_sent: int
    unmatched_results: int

    def save(self, path: str | os.PathLike[str]) -> Path:
        target = Path(path)
        with target.open("w", encoding="utf-8") as out:
            for frame in self.frames:
                out.write(json.dumps(_row(frame)) + "\n")
        return target


def run_hand_pose_live(clients: HttpClients, model: str, video: str | os.PathLike[str]) -> HandPoseLiveResult:
    try:
        run = run_file(clients.api, clients.runtime, model=model, path=video)
    except LiveSessionError as error:
        if isinstance(error.result, LiveRun):
            error.result = hand_pose_live_result(error.result)
        raise
    return hand_pose_live_result(run)


def hand_pose_live_result(run: LiveRun) -> HandPoseLiveResult:
    return HandPoseLiveResult(
        session_id=run.session_id,
        model=run.model,
        joint_order=JOINT_ORDER,
        frames=tuple(_frame(frame) for frame in run.frames),
        gaps=run.gaps,
        end_reason=run.end_reason,
        usage=run.usage,
        frames_sent=run.frames_sent,
        unmatched_results=run.unmatched_results,
    )


def _frame(frame: LiveFrame) -> HandPoseLiveFrame:
    what = f"hand-pose data of frame {frame.frame_index}"
    data = parse_contract(_DATA, frame.data, what=what)
    return HandPoseLiveFrame(
        frame_index=frame.frame_index,
        time_s=frame.time_s,
        image_size=(frame.width, frame.height),
        camera=data.camera,
        raw_hand_count=data.raw_hand_count,
        suppressed_duplicate_count=data.suppressed_duplicate_count,
        hands=tuple(parse_contract(_HAND, hand.model_dump(mode="json"), what=what) for hand in data.hands),
    )


def _row(frame: HandPoseLiveFrame) -> dict[str, Any]:
    return {
        "frame_index": frame.frame_index,
        "time_s": frame.time_s,
        "image_size": {"width": frame.image_size[0], "height": frame.image_size[1]},
        "camera": frame.camera.model_dump(mode="json"),
        "raw_hand_count": frame.raw_hand_count,
        "suppressed_duplicate_count": frame.suppressed_duplicate_count,
        "hands": [hand.model_dump(mode="json") for hand in frame.hands],
    }
