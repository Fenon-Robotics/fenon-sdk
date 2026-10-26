"""Tests for the shared pose pieces: the video prepare step and fetch-once run outputs."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx2
import pytest
from _support import OUTPUT_BYTES, UPLOAD_ID, Router, json_response, reservation_body, run_body

from fenon._core._generated import Run
from fenon._core.errors import InvalidInputError
from fenon._core.http import HttpClients
from fenon.models.pose.base import RunOutput, prepare_video

MakeClients = Callable[..., HttpClients]
OUTPUT_PATH = "/out/annotations.jsonl"


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
    [("clip.mp4", "video/mp4"), ("clip.MOV", "video/quicktime"), ("clip.WebM", "video/webm")],
)
def test_uploads_with_the_extensions_content_type(
    make_clients: MakeClients, tmp_path: Path, name: str, content_type: str
) -> None:
    video = tmp_path / name
    video.write_bytes(b"video-bytes")
    api, storage = upload_routes(video)
    assert prepare_video(make_clients(api=api, storage=storage), video, "recorded") == {"video": UPLOAD_ID}
    assert json.loads(api.calls("POST", "/v1/uploads")[0].content)["content_type"] == content_type


def test_a_string_path_is_accepted(make_clients: MakeClients, tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video-bytes")
    api, storage = upload_routes(video)
    assert prepare_video(make_clients(api=api, storage=storage), str(video), "recorded") == {
        "video": UPLOAD_ID
    }


@pytest.mark.parametrize(
    ("name", "mode", "code"),
    [("clip.mp4", "live", "mode_not_supported"), ("clip.avi", "recorded", "unsupported_media_type")],
)
def test_mode_and_extension_fail_before_any_request(
    make_clients: MakeClients, tmp_path: Path, name: str, mode: str, code: str
) -> None:
    video = tmp_path / name
    video.write_bytes(b"video-bytes")
    with pytest.raises(InvalidInputError) as caught:
        prepare_video(make_clients(), video, mode)
    assert caught.value.code == code
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("value", [123, b"clip.mp4", None, BytesPath()])
def test_input_must_be_a_text_path(make_clients: MakeClients, value: object) -> None:
    with pytest.raises(TypeError, match="input must be a path to a local video file"):
        prepare_video(make_clients(), cast("str", value), "recorded")


class TestRunOutput:
    def test_downloads_once_however_often(self, make_clients: MakeClients) -> None:
        storage = Router({("GET", OUTPUT_PATH): [httpx2.Response(200, content=OUTPUT_BYTES)]})
        output = RunOutput(
            make_clients(storage=storage), Run.model_validate(run_body("succeeded")), "annotations"
        )
        assert [output.content() for _ in range(3)] == [OUTPUT_BYTES] * 3
        assert len(storage.requests) == 1

    def test_a_second_thread_waits_for_the_first_download(self, make_clients: MakeClients) -> None:
        entered = threading.Semaphore(0)
        release = threading.Event()
        router = Router({("GET", OUTPUT_PATH): [httpx2.Response(200, content=OUTPUT_BYTES)]})

        def slow(request: httpx2.Request) -> httpx2.Response:
            entered.release()
            release.wait(5)
            return router(request)

        output = RunOutput(
            make_clients(storage=slow), Run.model_validate(run_body("succeeded")), "annotations"
        )
        results: list[bytes] = []
        first = threading.Thread(target=lambda: results.append(output.content()))
        first.start()
        assert entered.acquire(timeout=5)
        others = [threading.Thread(target=lambda: results.append(output.content())) for _ in range(3)]
        for thread in others:
            thread.start()
        second_download_started = entered.acquire(timeout=0.5)
        release.set()
        for thread in [first, *others]:
            thread.join()
        assert not second_download_started
        assert results == [OUTPUT_BYTES] * 4
        assert len(router.requests) == 1

    def test_save_writes_the_exact_bytes_and_returns_the_path(
        self, make_clients: MakeClients, tmp_path: Path
    ) -> None:
        storage = Router({("GET", OUTPUT_PATH): [httpx2.Response(200, content=OUTPUT_BYTES)]})
        output = RunOutput(
            make_clients(storage=storage), Run.model_validate(run_body("succeeded")), "annotations"
        )
        target = tmp_path / "frames.jsonl"
        target.write_bytes(b"old content that is longer than the output")
        assert output.save(str(target)) == target
        assert target.read_bytes() == OUTPUT_BYTES

    def test_save_into_a_missing_folder_raises_the_os_error(
        self, make_clients: MakeClients, tmp_path: Path
    ) -> None:
        storage = Router({("GET", OUTPUT_PATH): [httpx2.Response(200, content=OUTPUT_BYTES)]})
        output = RunOutput(
            make_clients(storage=storage), Run.model_validate(run_body("succeeded")), "annotations"
        )
        with pytest.raises(FileNotFoundError):
            output.save(tmp_path / "missing" / "frames.jsonl")
