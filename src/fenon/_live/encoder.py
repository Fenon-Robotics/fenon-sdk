"""H.264 Constrained Baseline with the frame-number SEI in every frame (docs/features/live.md)."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Protocol

import av
from av.packet import Packet
from av.stream import Stream
from av.video.frame import VideoFrame

from .config import LIVE_LIMITS

FRAME_NUMBER_UUID = bytes.fromhex("7f4667936dbf48da9cb254c9d8d9e52e")
_START_CODE = b"\x00\x00\x00\x01"
_SEI_NAL_HEADER = 0x06
_USER_DATA_UNREGISTERED = 5
_RBSP_STOP_BIT = 0x80
_VCL_NAL_TYPES = frozenset({1, 5})
_MAX_FRAME_NUMBER = 2**32 - 1


class _VideoEncoder(Protocol):
    def encode(self, frame: VideoFrame | None = None) -> list[Packet[Stream]]: ...


@dataclass(frozen=True)
class EncodedFrame:
    data: bytes
    pts: int
    time_base: Fraction
    keyframe: bool
    frame_number: int


class H264Encoder:
    def __init__(self, *, width: int, height: int, frame_rate: Fraction, time_base: Fraction) -> None:
        self.keyframe_interval = max(1, round(frame_rate * Fraction(LIVE_LIMITS.keyframe_interval_s)))
        codec = av.CodecContext.create("libx264", "w")
        codec.width = width
        codec.height = height
        codec.pix_fmt = "yuv420p"
        codec.time_base = time_base
        codec.framerate = frame_rate
        codec.gop_size = self.keyframe_interval
        codec.max_b_frames = 0
        codec.profile = "Baseline"
        codec.options = {
            "tune": "zerolatency",
            "x264-params": (
                f"keyint={self.keyframe_interval}:min-keyint={self.keyframe_interval}:scenecut=0"
                ":bframes=0:repeat-headers=1"
            ),
        }
        self._codec: _VideoEncoder = codec
        self._size = (width, height)
        self._time_base = time_base
        self._frame_numbers: dict[int, int] = {}

    def encode(self, frame: VideoFrame, frame_number: int) -> list[EncodedFrame]:
        if not 0 <= frame_number <= _MAX_FRAME_NUMBER:
            raise ValueError("frame_number must fit in an unsigned 32-bit integer")
        pts = frame.pts
        if pts is None:
            raise ValueError("frame needs a pts in the encoder's time base")
        picture = frame
        if frame.format.name != "yuv420p" or (frame.width, frame.height) != self._size:
            picture = frame.reformat(width=self._size[0], height=self._size[1], format="yuv420p")
            picture.pts = pts
            picture.time_base = self._time_base
        self._frame_numbers[pts] = frame_number
        return [self._with_frame_number(packet) for packet in self._codec.encode(picture)]

    def flush(self) -> list[EncodedFrame]:
        return [self._with_frame_number(packet) for packet in self._codec.encode(None)]

    def _with_frame_number(self, packet: Packet[Stream]) -> EncodedFrame:
        if packet.pts is None:
            raise ValueError("the encoder produced a packet without a pts")
        frame_number = self._frame_numbers.pop(packet.pts)
        units = split_annex_b(bytes(packet))
        first_slice = next(index for index, unit in enumerate(units) if unit[0] & 0x1F in _VCL_NAL_TYPES)
        units.insert(first_slice, frame_number_sei(frame_number))
        return EncodedFrame(
            data=b"".join(_START_CODE + unit for unit in units),
            pts=packet.pts,
            time_base=self._time_base,
            keyframe=packet.is_keyframe,
            frame_number=frame_number,
        )


def frame_number_sei(frame_number: int) -> bytes:
    payload = FRAME_NUMBER_UUID + frame_number.to_bytes(4, "big")
    rbsp = bytes([_USER_DATA_UNREGISTERED, len(payload)]) + payload + bytes([_RBSP_STOP_BIT])
    return bytes([_SEI_NAL_HEADER]) + add_emulation_prevention(rbsp)


def add_emulation_prevention(rbsp: bytes) -> bytes:
    out = bytearray()
    zeros = 0
    for byte in rbsp:
        if zeros >= 2 and byte <= 3:
            out.append(3)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def split_annex_b(stream: bytes) -> list[bytes]:
    units: list[bytes] = []
    start = stream.find(b"\x00\x00\x01")
    while start != -1:
        begin = start + 3
        following = stream.find(b"\x00\x00\x01", begin)
        end = len(stream) if following == -1 else following
        unit = stream[begin:end].rstrip(b"\x00")
        if unit:
            units.append(unit)
        start = following
    return units
