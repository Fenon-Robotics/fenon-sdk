"""Tests for ``Fenon`` and ``client.pose.hand`` end to end on mock transports."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from _support import (
    HAND_BILLED_SECONDS,
    HAND_END,
    HAND_START,
    RUN_ID,
    TEST_API_KEY,
    UPLOAD_ID,
    VIDEO_BYTES,
    FakeRuntime,
    Router,
    annotations_output,
    error_response,
    hand_response,
    hand_run_body,
    json_response,
    reservation_body,
)

from fenon import (
    Fenon,
    HandPoseJob,
    HandPoseResult,
    InvalidInputError,
    NotFoundError,
    ServerError,
    UnsupportedModelError,
)
from fenon._core import logs
from fenon.models.pose import base as pose_base

MakeFenon = Callable[..., Fenon]
RUN_PATH = f"/v1/runs/{RUN_ID}"
CANCEL_PATH = f"/v1/runs/{RUN_ID}/cancel"
MODEL = "fenon/hand-pose-hm"


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(bytes(range(256)) * 16)
    return path


def full_api(video: Path, *polls: httpx2.Response) -> tuple[Router, Router]:
    api = Router(
        {
            ("POST", "/v1/uploads"): [json_response(201, reservation_body(size_bytes=video.stat().st_size))],
            ("POST", "/v1/runs"): [hand_response("queued", poll_after_ms=250)],
            ("GET", RUN_PATH): list(polls) or [hand_response("succeeded")],
            ("POST", CANCEL_PATH): [hand_response("canceled")],
        }
    )
    storage = Router(
        {
            ("PUT", f"/bucket/{UPLOAD_ID}"): [httpx2.Response(200)],
            ("GET", "/out/annotations.jsonl"): [httpx2.Response(200, content=annotations_output())],
        }
    )
    return api, storage


def test_one_call_uploads_runs_polls_and_parses(
    make_fenon: MakeFenon, fake_runtime: FakeRuntime, video: Path, tmp_path: Path
) -> None:
    api, storage = full_api(
        video,
        hand_response("queued", poll_after_ms=1000),
        hand_response("processing_frames", poll_after_ms=1000),
        hand_response("processing_frames"),
        hand_response("succeeded"),
    )
    result = make_fenon(api=api, storage=storage).pose.hand(model=MODEL, input=video, mode="recorded")
    assert isinstance(result, HandPoseResult)
    assert json.loads(api.calls("POST", "/v1/runs")[0].content) == {
        "model": MODEL,
        "input": {"video": UPLOAD_ID},
        "outputs": ["annotations"],
    }
    assert storage.calls("PUT", f"/bucket/{UPLOAD_ID}")[0].content == video.read_bytes()
    assert fake_runtime.sleeps == [0.25, 1.0, 1.0, 2.0]
    assert [frame.frame_index for frame in result.frames] == list(range(HAND_START, HAND_END))
    assert (result.usage.unit, result.usage.quantity) == ("second", HAND_BILLED_SECONDS)
    assert result.save(tmp_path / "out.jsonl").read_bytes() == annotations_output()
    assert len(storage.calls("GET", "/out/annotations.jsonl")) == 1


def test_wait_false_returns_a_job_before_any_poll(make_fenon: MakeFenon, video: Path) -> None:
    api, storage = full_api(video, hand_response("queued"), hand_response("succeeded"))
    job = make_fenon(api=api, storage=storage).pose.hand(
        model=MODEL, input=video, mode="recorded", wait=False
    )
    assert isinstance(job, HandPoseJob)
    assert api.calls("GET", RUN_PATH) == []
    assert job.id == RUN_ID
    assert job.status == "queued"
    assert isinstance(job.result(), HandPoseResult)
    assert job.cancel() is None


def test_ctrl_c_in_the_blocking_call_cancels_once(
    make_fenon: MakeFenon, fake_runtime: FakeRuntime, video: Path
) -> None:
    fake_runtime.interrupt_at_sleep = 2
    api, storage = full_api(video, hand_response("queued"))
    with pytest.raises(KeyboardInterrupt) as caught:
        make_fenon(api=api, storage=storage).pose.hand(model=MODEL, input=video, mode="recorded")
    assert caught.value is fake_runtime.interrupt
    assert len(api.calls("POST", CANCEL_PATH)) == 1


def test_ctrl_c_on_a_job_from_wait_false_never_cancels(
    make_fenon: MakeFenon, fake_runtime: FakeRuntime, video: Path
) -> None:
    fake_runtime.interrupt_at_sleep = 1
    api, storage = full_api(video, hand_response("queued"))
    job = make_fenon(api=api, storage=storage).pose.hand(
        model=MODEL, input=video, mode="recorded", wait=False
    )
    with pytest.raises(KeyboardInterrupt):
        job.result()
    assert api.calls("POST", CANCEL_PATH) == []


@pytest.mark.parametrize(
    ("arguments", "error", "code"),
    [
        ({"input": "gone.mp4"}, InvalidInputError, "file_not_found"),
        ({"input": "folder.mp4"}, InvalidInputError, "file_not_found"),
        ({"input": "clip.avi"}, InvalidInputError, "unsupported_media_type"),
        ({"mode": "stream"}, InvalidInputError, "mode_not_supported"),
        ({"mode": "live", "wait": False}, InvalidInputError, "invalid_argument"),
        ({"mode": "live", "video": True}, InvalidInputError, "invalid_argument"),
        ({"video": "yes"}, TypeError, None),
        ({"wait": 1}, TypeError, None),
        ({"model": " "}, InvalidInputError, "invalid_argument"),
        ({"model": 123}, TypeError, None),
        ({"input": 123}, TypeError, None),
    ],
)
def test_local_checks_send_nothing(
    make_fenon: MakeFenon,
    video: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    error: type[Exception],
    code: str | None,
) -> None:
    monkeypatch.chdir(video.parent)
    (video.parent / "folder.mp4").mkdir()
    call: dict[str, Any] = {"model": MODEL, "input": video, "mode": "recorded", **arguments}
    with pytest.raises(error) as caught:
        make_fenon().pose.hand(**call)
    assert getattr(caught.value, "code", None) == code


def test_live_mode_is_refused_before_any_call_while_it_is_switched_off(
    make_fenon: MakeFenon, video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pose_base, "LIVE_MODE_AVAILABLE", False)
    with pytest.raises(InvalidInputError) as raised:
        make_fenon().pose.hand(model=MODEL, input=video, mode="live")
    assert raised.value.code == "mode_not_available"
    assert raised.value.fix == 'Use mode="recorded" (the default).'


def test_another_contract_is_cancelled_and_refused(make_fenon: MakeFenon, video: Path) -> None:
    other = {"id": "fenon.other.contract", "version": "1.0.0", "schema_sha256": "b" * 64}
    api, storage = full_api(video)
    api.scripts[("POST", "/v1/runs")] = [hand_response("queued", contract=other)]
    with pytest.raises(UnsupportedModelError):
        make_fenon(api=api, storage=storage).pose.hand(model=MODEL, input=video, mode="recorded")
    assert len(api.calls("POST", CANCEL_PATH)) == 1


def test_a_not_implemented_upload_fails_after_one_request(make_fenon: MakeFenon, video: Path) -> None:
    api = Router(
        {
            ("POST", "/v1/uploads"): [
                error_response(501, "not_implemented", details={"port": "idempotency_store"})
            ]
        }
    )
    with pytest.raises(ServerError) as caught:
        make_fenon(api=api).pose.hand(model=MODEL, input=video, mode="recorded")
    assert (caught.value.code, caught.value.status_code) == ("not_implemented", 501)
    assert caught.value.details == {"port": "idempotency_store"}
    assert len(api.requests) == 1


def test_repr_never_shows_the_key(make_fenon: MakeFenon) -> None:
    client = make_fenon()
    assert TEST_API_KEY not in repr(client)
    assert repr(client) == "Fenon(base_url='https://api.fenon.ai/v1')"


def test_the_context_manager_closes_the_client(make_fenon: MakeFenon, video: Path) -> None:
    api, storage = full_api(video)
    with make_fenon(api=api, storage=storage) as client:
        pass
    with pytest.raises(RuntimeError):
        client.pose.hand(model=MODEL, input=video, mode="recorded")


def test_a_real_client_reads_the_key_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FENON_API_KEY", "fenon_from_env")
    with Fenon(base_url="https://example.test/v1/") as client:
        assert repr(client) == "Fenon(base_url='https://example.test/v1')"
        assert cast("Any", client)._config.api_key == "fenon_from_env"


def test_the_client_turns_on_fenon_log(monkeypatch: pytest.MonkeyPatch) -> None:
    level, handlers = logs.logger.level, list(logs.logger.handlers)
    monkeypatch.setenv("FENON_LOG", "debug")
    try:
        with Fenon(api_key="fenon_k"):
            assert logs.logger.level == logging.DEBUG
    finally:
        logs.logger.setLevel(level)
        logs.logger.handlers[:] = handlers


def video_api(video: Path, *polls: httpx2.Response) -> tuple[Router, Router]:
    api, storage = full_api(video, *(polls or (hand_response("succeeded", video=VIDEO_BYTES),)))
    api.scripts[("POST", "/v1/runs")] = [hand_response("queued", video=VIDEO_BYTES)]
    storage.scripts[("GET", "/out/video.mp4")] = [httpx2.Response(200, content=VIDEO_BYTES)]
    return api, storage


def test_mode_left_out_is_a_recorded_run(make_fenon: MakeFenon, video: Path) -> None:
    api, storage = full_api(video)
    result = make_fenon(api=api, storage=storage).pose.hand(model=MODEL, input=video)
    assert isinstance(result, HandPoseResult)
    assert len(api.calls("POST", "/v1/uploads")) == 1


def test_video_true_asks_for_the_video_and_save_video_streams_it(
    make_fenon: MakeFenon, video: Path, tmp_path: Path
) -> None:
    api, storage = video_api(video)
    result = make_fenon(api=api, storage=storage).pose.hand(model=MODEL, input=video, video=True)
    assert json.loads(api.calls("POST", "/v1/runs")[0].content)["outputs"] == ["annotations", "video"]
    target = tmp_path / "drawn.mp4"
    assert result.save_video(target) == target
    assert target.read_bytes() == VIDEO_BYTES
    assert storage.calls("GET", "/out/annotations.jsonl") == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["clip.mp4", "drawn.mp4"]


def test_save_video_without_video_true_is_refused_before_any_download(
    make_fenon: MakeFenon, video: Path, tmp_path: Path
) -> None:
    api, storage = full_api(video)
    result = make_fenon(api=api, storage=storage).pose.hand(model=MODEL, input=video)
    with pytest.raises(InvalidInputError) as caught:
        result.save_video(tmp_path / "drawn.mp4")
    assert (caught.value.code, caught.value.fix) == (
        "video_not_requested",
        "Run the video again with video=True.",
    )
    assert storage.calls("GET", "/out/video.mp4") == []
    assert not (tmp_path / "drawn.mp4").exists()


@pytest.mark.parametrize("refusal", [403, 404])
def test_save_video_gets_one_fresh_url_when_storage_refuses_the_link(
    make_fenon: MakeFenon, video: Path, tmp_path: Path, refusal: int
) -> None:
    fresh_url = "https://storage.example.test/out/fresh-video.mp4?X-Amz-Signature=fresh"
    fresh = hand_run_body("succeeded", video=VIDEO_BYTES)
    fresh["outputs"]["video"]["url"] = fresh_url
    api, storage = video_api(video)
    api.scripts[("GET", RUN_PATH)] = [
        hand_response("succeeded", video=VIDEO_BYTES),
        json_response(200, fresh),
    ]
    storage.scripts[("GET", "/out/video.mp4")] = [httpx2.Response(refusal)]
    storage.scripts[("GET", "/out/fresh-video.mp4")] = [httpx2.Response(200, content=VIDEO_BYTES)]
    result = make_fenon(api=api, storage=storage).pose.hand(model=MODEL, input=video, video=True)
    assert result.save_video(tmp_path / "drawn.mp4").read_bytes() == VIDEO_BYTES
    assert len(api.calls("GET", RUN_PATH)) == 2


def test_a_failed_save_video_keeps_the_old_file_and_leaves_no_partial_file(
    make_fenon: MakeFenon, video: Path, tmp_path: Path
) -> None:
    api, storage = video_api(video)
    storage.scripts[("GET", "/out/video.mp4")] = [httpx2.Response(200, content=VIDEO_BYTES[:100])]
    result = make_fenon(api=api, storage=storage).pose.hand(model=MODEL, input=video, video=True)
    target = tmp_path / "drawn.mp4"
    target.write_bytes(b"older video")
    with pytest.raises(ServerError) as caught:
        result.save_video(target)
    assert caught.value.code == "output_truncated"
    assert target.read_bytes() == b"older video"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["clip.mp4", "drawn.mp4"]


def test_a_run_read_after_its_outputs_expired_keeps_result_and_usage(
    make_fenon: MakeFenon, video: Path, tmp_path: Path
) -> None:
    expired = hand_response("succeeded", video=VIDEO_BYTES, outputs=None, outputs_expired=True)
    api, storage = video_api(video, expired)
    result = make_fenon(api=api, storage=storage).pose.hand(model=MODEL, input=video, video=True)
    assert (result.usage.unit, result.usage.quantity) == ("second", HAND_BILLED_SECONDS)
    assert result.outputs_expire_at == datetime(2026, 10, 27, 12, 1, tzinfo=UTC)
    for read in (lambda: list(result.frames), lambda: result.save(tmp_path / "a.jsonl")):
        with pytest.raises(NotFoundError) as caught:
            read()
        assert caught.value.code == "outputs_expired"
        assert caught.value.details["outputs_expire_at"] == "2026-10-27T12:01:00.000Z"
    with pytest.raises(NotFoundError, match="expired"):
        result.save_video(tmp_path / "drawn.mp4")
    assert storage.calls("GET", "/out/annotations.jsonl") == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["clip.mp4"]


def test_outputs_that_expired_after_the_result_are_reported_as_expired(
    make_fenon: MakeFenon, video: Path
) -> None:
    api, storage = full_api(video)
    api.scripts[("GET", RUN_PATH)] = [
        hand_response("succeeded"),
        hand_response("succeeded", outputs=None, outputs_expired=True),
    ]
    storage.scripts[("GET", "/out/annotations.jsonl")] = [httpx2.Response(404)]
    result = make_fenon(api=api, storage=storage).pose.hand(model=MODEL, input=video)
    with pytest.raises(NotFoundError) as caught:
        list(result.frames)
    assert caught.value.code == "outputs_expired"
    assert caught.value.fix == "Run the video again, and save its outputs before outputs_expire_at."


def test_a_succeeded_run_without_a_requested_output_is_an_invalid_response(
    make_fenon: MakeFenon, video: Path
) -> None:
    missing_video = hand_response("succeeded", outputs_requested=["annotations", "video"])
    api, storage = video_api(video, missing_video)
    with pytest.raises(ServerError) as caught:
        make_fenon(api=api, storage=storage).pose.hand(model=MODEL, input=video, video=True)
    assert caught.value.code == "invalid_response"
