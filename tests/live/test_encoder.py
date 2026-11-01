"""Tests for the live H.264 encoder: profile, keyframes, parameter sets, latency and the frame-number SEI."""

from __future__ import annotations

from fractions import Fraction

import pytest
from av.video.frame import VideoFrame

from fenon._live.encoder import (
    FRAME_NUMBER_UUID,
    EncodedFrame,
    H264Encoder,
    add_emulation_prevention,
    frame_number_sei,
    split_annex_b,
)

TIME_BASE = Fraction(1, 90000)
FPS = Fraction(30)
SPS, PPS, SEI, IDR, SLICE = 7, 8, 6, 5, 1


def picture(index: int, width: int = 320, height: int = 240, pixel_format: str = "yuv420p") -> VideoFrame:
    frame = VideoFrame(width, height, pixel_format)
    for plane_index, plane in enumerate(frame.planes):
        plane.update(bytes([(index * 7 + plane_index * 50) % 256]) * plane.buffer_size)
    frame.pts = index * 3000
    frame.time_base = TIME_BASE
    return frame


def encoded(count: int) -> list[EncodedFrame]:
    encoder = H264Encoder(width=320, height=240, frame_rate=FPS, time_base=TIME_BASE)
    packets = [packet for index in range(count) for packet in encoder.encode(picture(index), index)]
    return packets + encoder.flush()


def nal_types(frame: EncodedFrame) -> list[int]:
    return [unit[0] & 0x1F for unit in split_annex_b(frame.data)]


def remove_emulation_prevention(data: bytes) -> bytes:
    return data.replace(b"\x00\x00\x03", b"\x00\x00")


def frame_number_of(frame: EncodedFrame) -> int:
    for unit in split_annex_b(frame.data):
        rbsp = remove_emulation_prevention(unit[1:])
        if unit[0] & 0x1F == SEI and rbsp[0] == 5 and rbsp[2:18] == FRAME_NUMBER_UUID:
            return int.from_bytes(rbsp[18:22], "big")
    raise AssertionError("no frame-number SEI")


def test_each_frame_comes_out_at_once_in_order() -> None:
    encoder = H264Encoder(width=320, height=240, frame_rate=FPS, time_base=TIME_BASE)
    for index in range(10):
        frames = encoder.encode(picture(index), index)
        assert len(frames) == 1
        assert (frames[0].pts, frames[0].frame_number) == (index * 3000, index)
    assert encoder.flush() == []


def test_every_frame_carries_its_number_in_an_sei_before_its_first_slice() -> None:
    frames = encoded(65)
    assert [frame_number_of(frame) for frame in frames] == list(range(65))
    for frame in frames:
        types = nal_types(frame)
        first_slice = next(index for index, kind in enumerate(types) if kind in (IDR, SLICE))
        assert SEI in types[:first_slice]


def test_keyframes_come_every_two_seconds_with_their_parameter_sets() -> None:
    frames = encoded(125)
    keyframes = [index for index, frame in enumerate(frames) if IDR in nal_types(frame)]
    assert keyframes == [0, 60, 120]
    assert [index for index, frame in enumerate(frames) if frame.keyframe] == keyframes
    for index in keyframes:
        types = nal_types(frames[index])
        assert types.index(SPS) < types.index(PPS) < types.index(IDR)


def test_the_stream_is_constrained_baseline() -> None:
    sps = next(unit for unit in split_annex_b(encoded(1)[0].data) if unit[0] & 0x1F == SPS)
    assert sps[1] == 66
    assert sps[2] & 0x40


def test_every_unit_has_a_four_byte_start_code() -> None:
    for frame in encoded(3):
        units = split_annex_b(frame.data)
        assert frame.data == b"".join(b"\x00\x00\x00\x01" + unit for unit in units)


@pytest.mark.parametrize("frame_number", [0, 1, 3, 0x0100, 0x030000, 2**32 - 1])
def test_the_sei_survives_emulation_prevention(frame_number: int) -> None:
    unit = frame_number_sei(frame_number)
    escaped = unit[1:]
    for position in range(len(escaped) - 2):
        assert not (escaped[position] == 0 and escaped[position + 1] == 0 and escaped[position + 2] <= 3) or (
            escaped[position + 2] == 3
        )
    rbsp = remove_emulation_prevention(escaped)
    assert rbsp[2:18] == FRAME_NUMBER_UUID
    assert int.from_bytes(rbsp[18:22], "big") == frame_number
    assert rbsp[-1] == 0x80


def test_emulation_prevention_escapes_two_zeros_before_a_low_byte() -> None:
    assert add_emulation_prevention(b"\x00\x00\x01\x00\x00\x00") == b"\x00\x00\x03\x01\x00\x00\x03\x00"


def test_frames_of_another_size_or_format_are_converted() -> None:
    encoder = H264Encoder(width=320, height=240, frame_rate=FPS, time_base=TIME_BASE)
    frames = encoder.encode(picture(0, width=640, height=480, pixel_format="rgb24"), 0)
    assert frame_number_of(frames[0]) == 0
    assert frames[0].pts == 0


def test_a_frame_number_outside_32_bits_is_refused() -> None:
    encoder = H264Encoder(width=320, height=240, frame_rate=FPS, time_base=TIME_BASE)
    with pytest.raises(ValueError, match="32-bit"):
        encoder.encode(picture(0), 2**32)
