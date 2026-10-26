"""Tests for ``Fenon`` and ``client.pose.hand`` end to end on mock transports."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from _support import (
    HAND_END,
    HAND_START,
    RUN_ID,
    TEST_API_KEY,
    UPLOAD_ID,
    FakeRuntime,
    Router,
    annotations_output,
    error_response,
    hand_response,
    json_response,
    reservation_body,
)

from fenon import Fenon, HandPoseJob, HandPoseResult, InvalidInputError, ServerError, UnsupportedModelError
from fenon._core import logs

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
            ("POST", "/v1/runs"): [hand_response("preparing_input", poll_after_ms=250)],
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
        hand_response("writing_results"),
        hand_response("succeeded"),
    )
    result = make_fenon(api=api, storage=storage).pose.hand(model=MODEL, input=video)
    assert isinstance(result, HandPoseResult)
    assert json.loads(api.calls("POST", "/v1/runs")[0].content) == {
        "model": MODEL,
        "input": {"video": UPLOAD_ID},
    }
    assert storage.calls("PUT", f"/bucket/{UPLOAD_ID}")[0].content == video.read_bytes()
    assert fake_runtime.sleeps == [0.25, 1.0, 1.0, 2.0]
    assert [frame.frame_index for frame in result.frames] == list(range(HAND_START, HAND_END))
    assert (result.usage.unit, result.usage.quantity) == ("frame", HAND_END - HAND_START)
    assert result.save(tmp_path / "out.jsonl").read_bytes() == annotations_output()
    assert len(storage.calls("GET", "/out/annotations.jsonl")) == 1


def test_wait_false_returns_a_job_before_any_poll(make_fenon: MakeFenon, video: Path) -> None:
    api, storage = full_api(video, hand_response("queued"), hand_response("succeeded"))
    job = make_fenon(api=api, storage=storage).pose.hand(model=MODEL, input=video, wait=False)
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
        make_fenon(api=api, storage=storage).pose.hand(model=MODEL, input=video)
    assert caught.value is fake_runtime.interrupt
    assert len(api.calls("POST", CANCEL_PATH)) == 1


def test_ctrl_c_on_a_job_from_wait_false_never_cancels(
    make_fenon: MakeFenon, fake_runtime: FakeRuntime, video: Path
) -> None:
    fake_runtime.interrupt_at_sleep = 1
    api, storage = full_api(video, hand_response("queued"))
    job = make_fenon(api=api, storage=storage).pose.hand(model=MODEL, input=video, wait=False)
    with pytest.raises(KeyboardInterrupt):
        job.result()
    assert api.calls("POST", CANCEL_PATH) == []


@pytest.mark.parametrize(
    ("arguments", "error", "code"),
    [
        ({"input": "gone.mp4"}, InvalidInputError, "file_not_found"),
        ({"input": "folder.mp4"}, InvalidInputError, "file_not_found"),
        ({"input": "clip.avi"}, InvalidInputError, "unsupported_media_type"),
        ({"mode": "live"}, InvalidInputError, "mode_not_supported"),
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
    call: dict[str, Any] = {"model": MODEL, "input": video, **arguments}
    with pytest.raises(error) as caught:
        make_fenon().pose.hand(**call)
    assert getattr(caught.value, "code", None) == code


def test_another_contract_is_cancelled_and_refused(make_fenon: MakeFenon, video: Path) -> None:
    other = {"id": "fenon.other.contract", "version": "1.0.0", "schema_sha256": "b" * 64}
    api, storage = full_api(video)
    api.scripts[("POST", "/v1/runs")] = [hand_response("preparing_input", contract=other)]
    with pytest.raises(UnsupportedModelError):
        make_fenon(api=api, storage=storage).pose.hand(model=MODEL, input=video)
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
        make_fenon(api=api).pose.hand(model=MODEL, input=video)
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
        client.pose.hand(model=MODEL, input=video)


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
