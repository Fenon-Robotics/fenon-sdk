"""A live session's results: messages parsed, ordered and matched to frames sent (docs/features/live.md)."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from pydantic import TypeAdapter

from .._core import parse_contract
from ._generated import GapMessage, ResultMessage, ResultsChannelMessage, Usage

_MESSAGE = TypeAdapter[ResultsChannelMessage](ResultsChannelMessage)
_RTP_CLOCK = Fraction(1, 90000)


@dataclass(frozen=True)
class LiveFrame:
    frame_index: int
    time_s: float
    width: int
    height: int
    data: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class LiveGap:
    reason: str
    after_frame_index: int | None


@dataclass(frozen=True)
class LiveRun:
    session_id: str
    model: str
    frames: tuple[LiveFrame, ...]
    gaps: tuple[LiveGap, ...]
    end_reason: str | None
    usage: Usage
    frames_sent: int
    unmatched_results: int


def media_time_90k(pts: int, time_base: Fraction) -> int:
    return int(pts * time_base / _RTP_CLOCK)


class ResultLog:
    def __init__(self) -> None:
        self._messages: list[str] = []
        self._sent: dict[int, tuple[int, float]] = {}

    @property
    def frames_sent(self) -> int:
        return len(self._sent)

    def record_sent(self, pts: int, time_base: Fraction, frame_index: int, time_s: float) -> None:
        self._sent[media_time_90k(pts, time_base)] = (frame_index, time_s)

    def record_message(self, text: str) -> None:
        self._messages.append(text)

    def collect(self) -> tuple[tuple[LiveFrame, ...], tuple[LiveGap, ...], int]:
        messages = [parse_contract(_MESSAGE, text, what="results-channel message") for text in self._messages]
        results = sorted((m for m in messages if isinstance(m, ResultMessage)), key=lambda m: m.frame_id)
        frames: list[LiveFrame] = []
        index_of_frame_id: dict[int, int] = {}
        unmatched = 0
        for result in results:
            sent = self._sent.get(result.media_time_90k)
            if sent is None:
                unmatched += 1
                continue
            index_of_frame_id[result.frame_id] = sent[0]
            frames.append(
                LiveFrame(
                    frame_index=sent[0],
                    time_s=sent[1],
                    width=result.frame_width,
                    height=result.frame_height,
                    data=result.data,
                )
            )
        gaps = tuple(
            LiveGap(
                reason=gap.reason,
                after_frame_index=None
                if gap.after_frame_id is None
                else index_of_frame_id.get(gap.after_frame_id),
            )
            for gap in messages
            if isinstance(gap, GapMessage)
        )
        return tuple(sorted(frames, key=lambda frame: frame.frame_index)), gaps, unmatched
