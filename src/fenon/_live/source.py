"""Video file input for live sessions: every frame, upright, even-sized and timed (docs/features/live.md)."""

from __future__ import annotations

import math
import os
import struct
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from fractions import Fraction
from types import TracebackType
from typing import Self, cast

import av
from av.container import InputContainer
from av.error import FFmpegError
from av.filter import Graph
from av.sidedata.sidedata import SideDataContainer, Type
from av.video.frame import VideoFrame
from av.video.stream import VideoStream

from .._core import InvalidInputError

_UPRIGHT_FILTERS: dict[int, tuple[tuple[str, str | None], ...]] = {
    0: (),
    90: (("transpose", "cclock"),),
    180: (("hflip", None), ("vflip", None)),
    270: (("transpose", "clock"),),
}
_EVEN_SIZE = "trunc(iw/2)*2:trunc(ih/2)*2"
_FIXED_POINT = 65536


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    frame_rate: Fraction
    time_base: Fraction
    duration_s: float
    rotation: int


@dataclass(frozen=True)
class SourceFrame:
    picture: VideoFrame
    index: int
    time_s: float


class VideoFile:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._container = _open(path)
        try:
            stream = _video_stream(self._container)
            self._decoded = self._container.decode(stream)
            self._next_index = 0
            first = self._next_decoded()
            if first is None or first.pts is None:
                raise _invalid("The video has no decodable frames with timestamps.", "invalid_input")
            rotation = _rotation(first)
            self._time_base = _time_base(stream)
            self._graph = _upright_graph(first, self._time_base, rotation)
            self._first_pts = first.pts
            self._input_shape = _shape(first)
            self._last_pts: int | None = None
            self._first: SourceFrame | None = self._convert(first)
            picture = self._first.picture
            self.info = VideoInfo(
                width=picture.width,
                height=picture.height,
                frame_rate=_frame_rate(stream),
                time_base=self._time_base,
                duration_s=_duration_s(self._container, stream),
                rotation=rotation,
            )
        except BaseException:
            self._container.close()
            raise

    def frames(self) -> Iterator[SourceFrame]:
        if self._first is not None:
            first, self._first = self._first, None
            yield first
        while (decoded := self._next_decoded()) is not None:
            yield self._convert(decoded)

    def close(self) -> None:
        self._container.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()

    def _next_decoded(self) -> VideoFrame | None:
        try:
            return next(self._decoded, None)
        except FFmpegError:
            where = "at its first frame" if self._next_index == 0 else f"after frame {self._next_index - 1}"
            raise _invalid(
                f"The video can't be decoded {where}; the file is damaged or cut short.", "file_damaged"
            ) from None

    def _convert(self, decoded: VideoFrame) -> SourceFrame:
        if _shape(decoded) != self._input_shape:
            raise _invalid("The video changes size or pixel format partway through.", "video_size_changes")
        if decoded.pts is None or (self._last_pts is not None and decoded.pts <= self._last_pts):
            raise _invalid("The video's frame timestamps are missing or out of order.", "invalid_input")
        self._last_pts = decoded.pts
        self._graph.push(decoded)
        upright = self._graph.pull()
        if not isinstance(upright, VideoFrame) or upright.pts is None:
            raise _invalid("A frame of the video couldn't be converted.", "file_unreadable")
        pts = upright.pts - self._first_pts
        upright.pts = pts
        upright.time_base = self._time_base
        index = self._next_index
        self._next_index += 1
        return SourceFrame(picture=upright, index=index, time_s=float(pts * self._time_base))


class Pacer:
    def __init__(self, now: Callable[[], float], sleep: Callable[[float], Awaitable[None]]) -> None:
        self._now = now
        self._sleep = sleep
        self._origin: float | None = None

    async def wait_for(self, time_s: float) -> None:
        if self._origin is None:
            self._origin = self._now() - time_s
        delay_s = self._origin + time_s - self._now()
        if delay_s > 0:
            await self._sleep(delay_s)


def _open(path: str | os.PathLike[str]) -> InputContainer:
    if not os.path.isfile(path):
        raise _invalid("The video file doesn't exist or isn't a file.", "file_not_found")
    try:
        return av.open(os.fspath(path))
    except (OSError, FFmpegError):
        raise _invalid("The video file can't be read as a video.", "file_unreadable") from None


def _video_stream(container: InputContainer) -> VideoStream:
    if not container.streams.video:
        raise _invalid("The file has no video stream.", "no_video_stream")
    return container.streams.video[0]


def _shape(frame: VideoFrame) -> tuple[int, int, str]:
    return frame.width, frame.height, frame.format.name


def _rotation(frame: VideoFrame) -> int:
    side_data = cast(SideDataContainer, frame.side_data)
    if Type.DISPLAYMATRIX not in side_data:
        return 0
    m = struct.unpack("<9i", bytes(side_data[Type.DISPLAYMATRIX]))
    a, b, c, d = (value / _FIXED_POINT for value in (m[0], m[1], m[3], m[4]))
    degrees = -math.degrees(math.atan2(b / math.hypot(b, d), a / math.hypot(a, c))) % 360
    rotation = round(degrees / 90) * 90 % 360
    if a * d - b * c < 0 or abs(degrees - round(degrees / 90) * 90) > 1:
        raise _invalid(
            "The video's display matrix mirrors or turns the picture by an angle the SDK can't apply.",
            "unsupported_rotation",
        )
    return rotation


def _upright_graph(first: VideoFrame, time_base: Fraction, rotation: int) -> Graph:
    graph = Graph()
    node = graph.add_buffer(width=first.width, height=first.height, format=first.format, time_base=time_base)
    for name, argument in _UPRIGHT_FILTERS[rotation]:
        following = graph.add(name, argument) if argument else graph.add(name)
        node.link_to(following)
        node = following
    for name, argument in (("crop", _EVEN_SIZE), ("format", "yuv420p")):
        following = graph.add(name, argument)
        node.link_to(following)
        node = following
    sink = graph.add("buffersink")
    node.link_to(sink)
    graph.configure()
    return graph


def _time_base(stream: VideoStream) -> Fraction:
    if stream.time_base is None:
        raise _invalid("The video stream has no time base.", "invalid_input")
    return stream.time_base


def _frame_rate(stream: VideoStream) -> Fraction:
    rate = stream.average_rate or stream.guessed_rate
    if not rate:
        raise _invalid("The video's frame rate is unknown.", "invalid_input")
    return Fraction(rate)


def _duration_s(container: InputContainer, stream: VideoStream) -> float:
    if stream.duration and stream.time_base:
        return float(stream.duration * stream.time_base)
    if container.duration:
        return container.duration / av.time_base
    raise _invalid("The video's duration is unknown.", "invalid_input")


def _invalid(message: str, code: str) -> InvalidInputError:
    return InvalidInputError(
        message, code=code, fix="Pass a readable video file, such as an .mp4 from a camera."
    )
