"""Tests for the upload service: measuring, reserving, sending and renewing once."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest
from _support import UPLOAD_ID, UPLOAD_URL, Router, error_response, json_response, reservation_body

from fenon._core.errors import InvalidInputError, ServerError
from fenon._core.http import HttpClients
from fenon._core.uploads import upload_file

MakeClients = Callable[..., HttpClients]
SECOND_UPLOAD_ID = "upl_000000sec0nd"
SECOND_URL = "https://storage.example.test/bucket/upl_000000sec0nd?X-Amz-Signature=second-signature"


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4)
    return path


def reservation(video: Path, upload_id: str = UPLOAD_ID, url: str = UPLOAD_URL) -> httpx2.Response:
    return json_response(201, reservation_body(upload_id, url, video.stat().st_size))


def test_reserves_sends_the_file_and_returns_the_upload_id(make_clients: MakeClients, video: Path) -> None:
    api = Router({("POST", "/v1/uploads"): [reservation(video)]})
    storage = Router({("PUT", "/bucket/upl_7k2mq9x4ab3d"): [httpx2.Response(200)]})
    upload_id = upload_file(make_clients(api=api, storage=storage), video, content_type="video/mp4")
    assert upload_id == UPLOAD_ID
    post = api.calls("POST", "/v1/uploads")[0]
    assert json.loads(post.content) == {
        "filename": "clip.mp4",
        "content_type": "video/mp4",
        "size_bytes": video.stat().st_size,
    }
    assert post.headers["idempotency-key"]
    put = storage.calls("PUT", "/bucket/upl_7k2mq9x4ab3d")[0]
    assert put.content == video.read_bytes()
    assert put.headers["content-type"] == "video/mp4"
    assert put.headers["content-length"] == str(video.stat().st_size)
    assert "authorization" not in put.headers


def test_a_refused_url_gets_one_new_reservation_with_a_new_key(
    make_clients: MakeClients, video: Path
) -> None:
    api = Router(
        {("POST", "/v1/uploads"): [reservation(video), reservation(video, SECOND_UPLOAD_ID, SECOND_URL)]}
    )
    storage = Router(
        {
            ("PUT", "/bucket/upl_7k2mq9x4ab3d"): [httpx2.Response(403)],
            ("PUT", "/bucket/upl_000000sec0nd"): [httpx2.Response(200)],
        }
    )
    upload_id = upload_file(make_clients(api=api, storage=storage), video, content_type="video/mp4")
    assert upload_id == SECOND_UPLOAD_ID
    keys = [request.headers["idempotency-key"] for request in api.calls("POST", "/v1/uploads")]
    assert len(keys) == 2
    assert keys[0] != keys[1]
    assert storage.calls("PUT", "/bucket/upl_000000sec0nd")[0].content == video.read_bytes()


def test_a_second_refusal_is_upload_rejected(make_clients: MakeClients, video: Path) -> None:
    api = Router(
        {("POST", "/v1/uploads"): [reservation(video), reservation(video, SECOND_UPLOAD_ID, SECOND_URL)]}
    )
    storage = Router(
        {
            ("PUT", "/bucket/upl_7k2mq9x4ab3d"): [httpx2.Response(403)],
            ("PUT", "/bucket/upl_000000sec0nd"): [httpx2.Response(403)],
        }
    )
    with pytest.raises(ServerError) as caught:
        upload_file(make_clients(api=api, storage=storage), video, content_type="video/mp4")
    assert caught.value.code == "upload_rejected"
    assert caught.value.__context__ is None
    assert len(api.calls("POST", "/v1/uploads")) == 2
    assert len(storage.requests) == 2


@pytest.mark.parametrize(("status", "code"), [(413, "file_too_large"), (415, "unsupported_media_type")])
def test_limits_from_the_reservation_are_invalid_input(
    make_clients: MakeClients, video: Path, status: int, code: str
) -> None:
    api = Router({("POST", "/v1/uploads"): [error_response(status, code)]})
    with pytest.raises(InvalidInputError) as caught:
        upload_file(make_clients(api=api), video, content_type="video/mp4")
    assert (caught.value.status_code, caught.value.code) == (status, code)


def test_missing_file_and_directory_fail_before_any_request(
    make_clients: MakeClients, tmp_path: Path
) -> None:
    clients = make_clients()
    for path in (tmp_path / "gone.mp4", tmp_path):
        with pytest.raises(InvalidInputError) as caught:
            upload_file(clients, path, content_type="video/mp4")
        assert caught.value.code == "file_not_found"
        assert str(tmp_path) not in str(caught.value)


def test_unreadable_file_fails_before_any_request(make_clients: MakeClients, video: Path) -> None:
    video.chmod(0)
    try:
        if os.access(video, os.R_OK):
            pytest.skip("this platform or user can read a file with no permissions")
        with pytest.raises(InvalidInputError) as caught:
            upload_file(make_clients(), video, content_type="video/mp4")
        assert caught.value.code == "file_unreadable"
    finally:
        video.chmod(0o644)


def test_a_reservation_that_cant_be_read_is_invalid_response(make_clients: MakeClients, video: Path) -> None:
    body = {**reservation_body(), "method": "POST"}
    api = Router({("POST", "/v1/uploads"): [json_response(201, body)]})
    with pytest.raises(ServerError) as caught:
        upload_file(make_clients(api=api), video, content_type="video/mp4")
    assert caught.value.code == "invalid_response"
    assert caught.value.__context__ is None
    assert "upload-signature" not in str(caught.value)


def test_presigned_urls_never_reach_logs_or_errors(
    make_clients: MakeClients, video: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="fenon")
    api = Router(
        {("POST", "/v1/uploads"): [reservation(video), reservation(video, SECOND_UPLOAD_ID, SECOND_URL)]}
    )
    storage = Router(
        {
            ("PUT", "/bucket/upl_7k2mq9x4ab3d"): [httpx2.Response(503), httpx2.Response(403)],
            ("PUT", "/bucket/upl_000000sec0nd"): [httpx2.Response(403)],
        }
    )
    with pytest.raises(ServerError) as caught:
        upload_file(make_clients(api=api, storage=storage), video, content_type="video/mp4")
    text = "\n".join([caplog.text, str(caught.value), repr(caught.value)])
    assert caplog.records
    for secret in ("upload-signature", "second-signature", "storage.example.test", str(video)):
        assert secret not in text
