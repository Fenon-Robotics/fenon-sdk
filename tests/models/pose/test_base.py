"""Tests for the shared pose pieces: the video prepare step."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx2
import pytest
from _support import UPLOAD_ID, Router, json_response, reservation_body

from fenon._core.errors import InvalidInputError
from fenon._core.http import HttpClients
from fenon.models.pose.base import prepare_video

MakeClients = Callable[..., HttpClients]


class BytesPath:
    def __fspath__(self) -> bytes:
        return b"clip.mp4"


def upload_routes(video: Path) -> tuple[Router, Router]:
    api = Router(
        {("POST", "/v1/uploads"): [json_response(201, reservation_body(size_bytes=video.stat().st_size))]}
    )
    storage = Router({("PUT", f"/bucket/{UPLOAD_ID}"): [httpx2.Response(200)]})
    return api, storage


@pytest.mark.parametrize(
    ("name", "content_type"),
    [("clip.mp4", "video/mp4"), ("clip.MOV", "video/quicktime")],
)
def test_uploads_with_the_extensions_content_type(
    make_clients: MakeClients, tmp_path: Path, name: str, content_type: str
) -> None:
    video = tmp_path / name
    video.write_bytes(b"video-bytes")
    api, storage = upload_routes(video)
    assert prepare_video(make_clients(api=api, storage=storage), video) == {"video": UPLOAD_ID}
    assert json.loads(api.calls("POST", "/v1/uploads")[0].content)["content_type"] == content_type


def test_a_string_path_is_accepted(make_clients: MakeClients, tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video-bytes")
    api, storage = upload_routes(video)
    assert prepare_video(make_clients(api=api, storage=storage), str(video)) == {"video": UPLOAD_ID}


@pytest.mark.parametrize("name", ["clip.avi", "clip.webm", "clip.mkv", "clip"])
def test_an_unsupported_extension_fails_before_any_request(
    make_clients: MakeClients, tmp_path: Path, name: str
) -> None:
    video = tmp_path / name
    video.write_bytes(b"video-bytes")
    with pytest.raises(InvalidInputError) as caught:
        prepare_video(make_clients(), video)
    assert caught.value.code == "unsupported_media_type"
    assert caught.value.message == "The video must be an .mp4 or .mov file."
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("value", [123, b"clip.mp4", None, BytesPath()])
def test_input_must_be_a_text_path(make_clients: MakeClients, value: object) -> None:
    with pytest.raises(TypeError, match="input must be a path to a local video file"):
        prepare_video(make_clients(), cast("str", value))
