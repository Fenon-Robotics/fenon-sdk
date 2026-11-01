"""Opt-in: the live transport against fenon-app's local stack with the echo engine (docs/features/live.md)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from aiortc.rtcdtlstransport import RTCDtlsTransport
from aiortc.rtp import is_rtcp
from av.video.frame import VideoFrame

from fenon._core.http import ClientConfig, open_http_clients
from fenon._live._generated import IceServer
from fenon._live.encoder import EncodedFrame, H264Encoder
from fenon._live.gateway import GatewayClient, HangUp
from fenon._live.sessions import LiveSessions, gateway_access
from fenon._live.source import Pacer, SourceFrame, VideoFile, VideoInfo
from fenon._live.transport import LiveTransport

API_KEY = os.environ.get("FENON_LIVE_STACK_API_KEY")
BASE_URL = os.environ.get("FENON_LIVE_STACK_BASE_URL", "http://127.0.0.1:8000/v1")
FIXTURES = Path(__file__).parents[1] / "live" / "fixtures"
FPS = 30
SECONDS = 7
WIDTH, HEIGHT = 640, 360
SYNTHETIC = VideoInfo(
    width=WIDTH,
    height=HEIGHT,
    frame_rate=Fraction(FPS),
    time_base=Fraction(1, 90000),
    duration_s=SECONDS,
    rotation=0,
)

pytestmark = pytest.mark.skipif(
    not API_KEY, reason="needs fenon-app's local live stack (docs/features/live.md)"
)


def media_time_90k(frame: EncodedFrame) -> int:
    return int(frame.pts * frame.time_base / Fraction(1, 90000))


@dataclass
class Run:
    sent: dict[int, EncodedFrame] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    hang_up: HangUp | None = None
    lost: bool = False
    end_reason: str | None = None

    def results(self) -> list[dict[str, Any]]:
        return sorted((m for m in self.messages if m["type"] == "result"), key=lambda m: m["frame_id"])

    def kind(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.messages if m["type"] == kind]

    def frame_of(self, result: dict[str, Any]) -> EncodedFrame:
        return self.sent[result["media_time_90k"]]

    def assert_every_frame_arrived_byte_for_byte(self, width: int, height: int) -> None:
        results = self.results()
        assert sorted(set(self.sent) - {result["media_time_90k"] for result in results}) == []
        assert len(results) == len(self.sent)
        for result in results:
            sent = self.frame_of(result)
            assert result["data"]["sha256"] == hashlib.sha256(sent.data).hexdigest()
            assert result["data"]["byte_length"] == len(sent.data)
            assert result["data"]["keyframe"] == sent.keyframe
            assert (result["frame_width"], result["frame_height"]) == (width, height)
        assert {result["data"]["codec_string"][:9] for result in results} == {"avc1.42C0"}


@dataclass
class RtpBlocker:
    from_s: float | None = None
    started: float | None = None
    blocked: set[int] = field(default_factory=set)

    def blocks(self, packet: bytes) -> bool:
        if self.from_s is None or self.started is None or is_rtcp(packet):
            return False
        sequence_number = int.from_bytes(packet[2:4], "big")
        if self.from_s <= time.monotonic() - self.started < self.from_s + 0.2:
            self.blocked.add(sequence_number)
        return sequence_number in self.blocked


def synthetic_frames() -> Iterator[SourceFrame]:
    for index in range(FPS * SECONDS):
        frame = VideoFrame(WIDTH, HEIGHT, "yuv420p")
        for plane_index, plane in enumerate(frame.planes):
            row = bytearray([(16 + plane_index * 40) % 256]) * plane.line_size
            bar = (index * 8) % max(1, plane.width - 16)
            row[bar : bar + 16] = b"\xeb" * 16
            plane.update(bytes(row) * (plane.buffer_size // plane.line_size))
        frame.pts = index * 90000 // FPS
        frame.time_base = SYNTHETIC.time_base
        yield SourceFrame(picture=frame, index=index, time_s=index / FPS)


def stream(
    monkeypatch: pytest.MonkeyPatch,
    info: VideoInfo,
    frames: Iterable[SourceFrame],
    *,
    block_from_s: float | None = None,
) -> Run:
    blocker = RtpBlocker(from_s=block_from_s)
    send_rtp = RTCDtlsTransport._send_rtp

    async def blocking_send_rtp(transport: RTCDtlsTransport, data: bytes) -> None:
        if not blocker.blocks(data):
            await send_rtp(transport, data)

    monkeypatch.setattr(RTCDtlsTransport, "_send_rtp", blocking_send_rtp)
    config = ClientConfig.resolve(api_key=API_KEY, base_url=BASE_URL, timeout=60, max_retries=2)
    clients = open_http_clients(config)
    sessions = LiveSessions(clients.api)
    created = sessions.create(model="fenon/hand-pose-hm", input_duration_s=info.duration_s)
    access = gateway_access(created, datetime.now(UTC))
    assert access is not None
    gateway = GatewayClient(access, clients.runtime)
    run = Run()

    async def media() -> None:
        transport = LiveTransport(access.ice_servers, lambda text: run.messages.append(json.loads(text)))
        try:
            answer = await asyncio.to_thread(gateway.offer, await transport.create_offer())
            await transport.accept_answer(answer)
            await transport.wait_connected()
            encoder = H264Encoder(
                width=info.width, height=info.height, frame_rate=info.frame_rate, time_base=info.time_base
            )
            pacer = Pacer(asyncio.get_running_loop().time, asyncio.sleep)
            source = iter(frames)
            blocker.started = time.monotonic()
            while (frame := await asyncio.to_thread(next, source, None)) is not None:
                encoded = await asyncio.to_thread(encoder.encode, frame.picture, frame.index)
                await pacer.wait_for(frame.time_s)
                for packet in encoded:
                    run.sent[media_time_90k(packet)] = packet
                    await transport.send(packet)
            await transport.end_media()
            await asyncio.sleep(1.0)
            run.lost = transport.lost.is_set()
        finally:
            run.hang_up = await asyncio.to_thread(gateway.hang_up)
            await transport.close()

    try:
        asyncio.run(media())
    finally:
        run.end_reason = sessions.end(created.id).end_reason
        gateway.close()
        clients.close()
    return run


def test_every_frame_reaches_the_engine_byte_for_byte_with_its_frame_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = stream(monkeypatch, SYNTHETIC, synthetic_frames())
    run.assert_every_frame_arrived_byte_for_byte(WIDTH, HEIGHT)
    assert len(run.sent) == FPS * SECONDS
    keyframes = [run.frame_of(r).frame_number for r in run.results() if r["data"]["keyframe"]]
    assert keyframes == list(range(0, FPS * SECONDS, 2 * FPS))
    assert run.kind("gap") == []
    assert [m["reason"] for m in run.kind("end")] == ["client_closed"]
    assert (run.hang_up, run.end_reason, run.lost) == (HangUp.ENDED, "client_closed", False)


def test_after_an_unrepairable_loss_results_resume_at_the_next_periodic_keyframe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = stream(monkeypatch, SYNTHETIC, synthetic_frames(), block_from_s=3.0)
    assert [gap["reason"] for gap in run.kind("gap")] == ["packet_loss"]
    numbers = [run.frame_of(r).frame_number for r in run.results()]
    resumed = next(number for number in numbers if number > 90)
    assert resumed == 120
    resumed_result = next(r for r in run.results() if run.frame_of(r).frame_number == resumed)
    assert resumed_result["data"]["keyframe"]
    assert resumed_result["data"]["discontinuity"]
    for result in run.results():
        assert result["data"]["sha256"] == hashlib.sha256(run.frame_of(result).data).hexdigest()
    assert (run.hang_up, run.end_reason) == (HangUp.ENDED, "client_closed")


@pytest.mark.parametrize("name", ["rotated-90.mp4", "odd-size.mp4"])
def test_a_video_file_reaches_the_engine_upright_and_whole(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    with VideoFile(FIXTURES / name) as video:
        info = video.info
        run = stream(monkeypatch, info, video.frames())
    run.assert_every_frame_arrived_byte_for_byte(info.width, info.height)
    assert [packet.frame_number for _, packet in sorted(run.sent.items())] == list(range(len(run.sent)))
    assert (run.hang_up, run.end_reason, run.lost) == (HangUp.ENDED, "client_closed", False)


def connect_once(ice_servers: list[IceServer] | None) -> tuple[dict[str, int], float]:
    config = ClientConfig.resolve(api_key=API_KEY, base_url=BASE_URL, timeout=60, max_retries=2)
    clients = open_http_clients(config)
    sessions = LiveSessions(clients.api)
    created = sessions.create(model="fenon/hand-pose-hm", input_duration_s=5)
    access = gateway_access(created, datetime.now(UTC))
    assert access is not None
    gateway = GatewayClient(access, clients.runtime)

    async def media() -> tuple[dict[str, int], float]:
        transport = LiveTransport(
            access.ice_servers if ice_servers is None else ice_servers, lambda text: None
        )
        try:
            started = time.monotonic()
            offer = await transport.create_offer()
            offer_s = time.monotonic() - started
            await transport.accept_answer(await asyncio.to_thread(gateway.offer, offer))
            await transport.wait_connected()
            types: dict[str, int] = {}
            for kind in re.findall(r"^a=candidate:.* typ (\w+)", offer, flags=re.MULTILINE):
                types[kind] = types.get(kind, 0) + 1
            return types, offer_s
        finally:
            await asyncio.to_thread(gateway.hang_up)
            await transport.close()

    try:
        return asyncio.run(media())
    finally:
        sessions.end(created.id)
        gateway.close()
        clients.close()


def test_the_sessions_stun_server_gives_a_public_candidate_and_connects() -> None:
    types, _ = connect_once(None)
    assert types.get("srflx", 0) >= 1
    assert types.get("host", 0) >= 1


def test_an_unreachable_stun_server_only_delays_the_offer() -> None:
    types, offer_s = connect_once([IceServer.model_validate({"urls": "stun:192.0.2.1:3478"})])
    assert "srflx" not in types
    assert offer_s < 6
