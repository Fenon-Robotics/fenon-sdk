"""Tests for the live video source: every frame, upright like ffmpeg autorotate, even-sized, timed, paced."""

from __future__ import annotations

import asyncio
from fractions import Fraction
from pathlib import Path

import av
import pytest
from av.video.frame import VideoFrame

from fenon._core.errors import InvalidInputError
from fenon._live.encoder import H264Encoder
from fenon._live.source import Pacer, VideoFile

FIXTURES = Path(__file__).parent / "fixtures"


def raw_yuv420p(picture: VideoFrame) -> bytes:
    return b"".join(
        bytes(plane)[row * plane.line_size : row * plane.line_size + plane.width]
        for plane in picture.planes
        for row in range(plane.height)
    )


def decoded_count(path: Path) -> int:
    with av.open(str(path)) as container:
        return sum(1 for _ in container.decode(container.streams.video[0]))


def test_every_frame_is_read_once_in_order_with_its_time() -> None:
    path = FIXTURES / "rotated-90.mp4"
    with VideoFile(path) as video:
        frames = list(video.frames())
    assert len(frames) == decoded_count(path) == 9
    assert [frame.index for frame in frames] == list(range(9))
    assert [round(frame.time_s * 30) for frame in frames] == list(range(9))
    assert frames[0].picture.pts == 0


@pytest.mark.parametrize(("rotation", "size"), [(90, (48, 64)), (180, (64, 48)), (270, (48, 64))])
def test_rotated_videos_come_out_upright_as_ffmpeg_turns_them(rotation: int, size: tuple[int, int]) -> None:
    with VideoFile(FIXTURES / f"rotated-{rotation}.mp4") as video:
        first = next(video.frames()).picture
        assert (video.info.rotation, (video.info.width, video.info.height)) == (rotation, size)
    assert raw_yuv420p(first) == (FIXTURES / f"rotated-{rotation}.first-frame.yuv").read_bytes()


def test_odd_sizes_are_cropped_to_even_yuv420p() -> None:
    with VideoFile(FIXTURES / "odd-size.mp4") as video:
        frames = list(video.frames())
        info = video.info
    assert (info.width, info.height, info.frame_rate) == (62, 46, Fraction(25))
    assert {(f.picture.width, f.picture.height, f.picture.format.name) for f in frames} == {
        (62, 46, "yuv420p")
    }
    assert len(frames) == 5


def test_the_info_describes_the_video() -> None:
    with VideoFile(FIXTURES / "rotated-180.mp4") as video:
        info = video.info
    assert info.frame_rate == Fraction(30)
    assert info.duration_s == pytest.approx(0.3, abs=0.02)
    assert info.time_base > 0


def test_every_source_frame_goes_through_the_encoder() -> None:
    with VideoFile(FIXTURES / "rotated-90.mp4") as video:
        info = video.info
        encoder = H264Encoder(
            width=info.width, height=info.height, frame_rate=info.frame_rate, time_base=info.time_base
        )
        encoded = [e for frame in video.frames() for e in encoder.encode(frame.picture, frame.index)]
    assert [e.frame_number for e in encoded] == list(range(9))
    assert encoded[0].keyframe


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("mirrored.mp4", "unsupported_rotation"),
        ("audio-only.m4a", "no_video_stream"),
        ("missing.mp4", "file_not_found"),
        (".", "file_not_found"),
        ("damaged-at-start.mp4", "file_damaged"),
        ("rotated-90.first-frame.yuv", "file_unreadable"),
    ],
)
def test_unusable_files_are_refused_with_a_clear_code(name: str, code: str) -> None:
    with pytest.raises(InvalidInputError) as raised:
        VideoFile(FIXTURES / name)
    assert raised.value.code == code


def test_a_video_that_changes_size_partway_is_refused_when_the_change_comes() -> None:
    with VideoFile(FIXTURES / "size-change.ts") as video:
        frames = video.frames()
        assert [next(frames).picture.width for _ in range(6)] == [64] * 6
        with pytest.raises(InvalidInputError) as raised:
            next(frames)
    assert raised.value.code == "video_size_changes"


def test_a_file_damaged_partway_yields_its_good_frames_then_file_damaged() -> None:
    frames: list[int] = []
    with VideoFile(FIXTURES / "truncated.mp4") as video, pytest.raises(InvalidInputError) as raised:
        frames.extend(frame.index for frame in video.frames())
    assert frames == list(range(21))
    assert raised.value.code == "file_damaged"
    assert "after frame 20" in raised.value.message


class FakeClock:
    def __init__(self) -> None:
        self.time = 100.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.time

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.time += seconds


def test_the_pacer_sends_each_frame_at_its_own_time() -> None:
    clock = FakeClock()
    pacer = Pacer(clock.now, clock.sleep)

    async def run() -> None:
        for time_s in (0.0, 0.5, 1.0):
            await pacer.wait_for(time_s)

    asyncio.run(run())
    assert clock.sleeps == [0.5, 0.5]


def test_the_pacer_never_waits_for_a_late_frame_and_never_skips_one() -> None:
    clock = FakeClock()
    pacer = Pacer(clock.now, clock.sleep)

    async def run() -> None:
        await pacer.wait_for(0.0)
        clock.time += 2.0
        await pacer.wait_for(1.0)
        await pacer.wait_for(1.5)
        await pacer.wait_for(2.5)

    asyncio.run(run())
    assert clock.sleeps == [0.5]
