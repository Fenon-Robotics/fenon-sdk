"""Tests for the live WebRTC transport: what the offer carries, ICE server checks, a connection that fails."""

from __future__ import annotations

import asyncio

import pytest

from fenon._core.errors import NetworkError, ServerError
from fenon._live._generated import IceServer
from fenon._live.transport import LiveTransport, _ice_server


def offer_lines(ice_servers: list[IceServer] | None = None) -> list[str]:
    async def build() -> str:
        transport = LiveTransport(ice_servers or [], on_message=lambda message: None)
        try:
            return await transport.create_offer()
        finally:
            await transport.close()

    return asyncio.run(build()).splitlines()


def section(lines: list[str], media: str) -> list[str]:
    starts = [index for index, line in enumerate(lines) if line.startswith("m=")]
    for begin, end in zip(starts, [*starts[1:], len(lines)], strict=True):
        if lines[begin].startswith(f"m={media} "):
            return lines[begin:end]
    raise AssertionError(f"no {media} section")


def test_the_offer_sends_only_h264_constrained_baseline_video() -> None:
    video = section(offer_lines(), "video")
    rtpmaps = [line for line in video if line.startswith("a=rtpmap:")]
    assert [line.split(" ", 1)[1] for line in rtpmaps] == ["H264/90000"]
    fmtp = [line for line in video if line.startswith("a=fmtp:")]
    assert len(fmtp) == 1
    assert "profile-level-id=42e01f" in fmtp[0]
    assert "packetization-mode=1" in fmtp[0]
    assert "a=sendonly" in video
    assert any(line.endswith(" nack pli") for line in video)


def test_the_offer_carries_the_data_channel_and_every_candidate() -> None:
    lines = offer_lines()
    application = section(lines, "application")
    assert application[0].split()[2] == "UDP/DTLS/SCTP"
    assert application[0].endswith("webrtc-datachannel")
    assert any(line.startswith("a=candidate:") for line in lines)
    assert not any(line.startswith("m=audio") for line in lines)


def test_ice_servers_are_taken_as_given_whether_one_url_or_many() -> None:
    stun = _ice_server(IceServer.model_validate({"urls": "stun:stun.cloudflare.com:3478"}))
    turn = _ice_server(
        IceServer.model_validate(
            {
                "urls": ["turn:turn.test:3478?transport=udp", "turns:turn.test:5349?transport=tcp"],
                "username": "u",
                "credential": "c",
            }
        )
    )
    assert (stun.urls, stun.username, stun.credential) == (["stun:stun.cloudflare.com:3478"], None, None)
    assert turn.urls == ["turn:turn.test:3478?transport=udp", "turns:turn.test:5349?transport=tcp"]
    assert (turn.username, turn.credential, turn.credentialType) == ("u", "c", "password")


def test_an_ice_server_url_aiortc_cannot_parse_is_refused() -> None:
    with pytest.raises(ServerError) as raised:
        offer_lines([IceServer.model_validate({"urls": "https://not-an-ice-server.test"})])
    assert raised.value.code == "unusable_ice_server"


def test_a_connection_that_ends_before_the_channel_opens_fails_at_once() -> None:
    async def run() -> None:
        transport = LiveTransport([], on_message=lambda message: None)
        await transport.create_offer()
        await transport.close()
        await transport.wait_connected()

    with pytest.raises(NetworkError) as raised:
        asyncio.run(run())
    assert raised.value.code == "connection_failed"
