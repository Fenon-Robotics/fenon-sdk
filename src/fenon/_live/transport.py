"""The aiortc peer of a live session: the SDK's H.264 out, the model's results in (docs/features/live.md)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

import av
from aiortc import (
    RTCConfiguration,
    RTCDataChannel,
    RTCIceServer,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from aiortc.rtcicetransport import parse_stun_turn_uri
from av.packet import Packet
from av.stream import Stream

from .._core import NetworkError, ServerError
from ._generated import IceServer
from .config import LIVE_LIMITS
from .encoder import EncodedFrame

RESULTS_CHANNEL = "results"
_PROFILE_LEVEL_ID = "42e01f"
_FAILED_STATES = frozenset({"failed", "closed"})


class _PacketTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self) -> None:
        super().__init__()
        self._queue: asyncio.Queue[EncodedFrame | None] = asyncio.Queue(LIVE_LIMITS.send_queue_frames)

    async def put(self, frame: EncodedFrame | None) -> None:
        await self._queue.put(frame)

    async def recv(self) -> Packet[Stream]:
        frame = await self._queue.get()
        if frame is None:
            self.stop()
            raise MediaStreamError
        packet: Packet[Stream] = av.Packet(frame.data)
        packet.pts = frame.pts
        packet.time_base = frame.time_base
        return packet


class LiveTransport:
    def __init__(self, ice_servers: Sequence[IceServer], on_message: Callable[[str], None]) -> None:
        self._peer = RTCPeerConnection(
            RTCConfiguration(iceServers=[_ice_server(server) for server in ice_servers])
        )
        self._track = _PacketTrack()
        transceiver = self._peer.addTransceiver(self._track, direction="sendonly")
        transceiver.setCodecPreferences(
            [
                codec
                for codec in RTCRtpSender.getCapabilities("video").codecs
                if codec.mimeType == "video/H264"
                and codec.parameters.get("profile-level-id") == _PROFILE_LEVEL_ID
            ]
        )
        self._channel: RTCDataChannel = self._peer.createDataChannel(
            RESULTS_CHANNEL, ordered=False, maxRetransmits=1
        )
        self._on_message = on_message
        self._opened = asyncio.Event()
        self._ended = asyncio.Event()
        self._closing = False
        self.lost = asyncio.Event()
        self._channel.on("open", self._opened.set)
        self._channel.on("message", self._on_channel_message)
        self._peer.on("connectionstatechange", self._on_state)

    async def create_offer(self) -> str:
        await self._peer.setLocalDescription(await self._peer.createOffer())
        return self._peer.localDescription.sdp

    async def accept_answer(self, sdp: str) -> None:
        await self._peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))

    async def wait_connected(self) -> None:
        opened = asyncio.ensure_future(self._opened.wait())
        ended = asyncio.ensure_future(self._ended.wait())
        try:
            done, _ = await asyncio.wait(
                {opened, ended}, timeout=LIVE_LIMITS.connect_timeout_s, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            opened.cancel()
            ended.cancel()
        if opened not in done:
            raise NetworkError(
                "The live connection to the gateway couldn't be set up.",
                code="connection_failed",
                fix="Check that the network allows WebRTC (UDP) traffic to the gateway, then try again.",
                retryable=True,
            )

    async def send(self, frame: EncodedFrame) -> None:
        await self._put(frame)

    async def end_media(self) -> None:
        await self._put(None)

    async def close(self) -> None:
        self._closing = True
        await self._peer.close()

    async def _put(self, frame: EncodedFrame | None) -> None:
        put = asyncio.ensure_future(self._track.put(frame))
        ended = asyncio.ensure_future(self._ended.wait())
        try:
            await asyncio.wait({put, ended}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            ended.cancel()
            if not put.done():
                put.cancel()
        if put.cancelled():
            raise NetworkError(
                "The live connection to the gateway was lost.",
                code="connection_lost",
                fix="Try again; the results that arrived are kept.",
                retryable=True,
            )

    def _on_channel_message(self, message: str | bytes) -> None:
        self._on_message(message if isinstance(message, str) else message.decode("utf-8", errors="replace"))

    def _on_state(self) -> None:
        if self._peer.connectionState in _FAILED_STATES:
            self._ended.set()
            if not self._closing:
                self.lost.set()


def _ice_server(server: IceServer) -> RTCIceServer:
    urls = [server.urls] if isinstance(server.urls, str) else list(server.urls)
    for url in urls:
        try:
            parse_stun_turn_uri(url)
        except ValueError:
            raise ServerError(
                "fenon-api gave an ICE server URL the SDK can't use.",
                code="unusable_ice_server",
                fix="Report this to Fenon.",
            ) from None
    return RTCIceServer(urls=urls, username=server.username, credential=server.credential)
