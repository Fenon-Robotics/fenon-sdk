"""End-to-end tests of recorded-mode ``client.pose.hand`` over real sockets (recorded mode: coming soon)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _support import (
    HAND_END,
    HAND_START,
    RUN_ID,
    UPLOAD_ID,
    annotations_output,
    hand_run_body,
    reservation_body,
)
from _wire import Reply, Wire, WireRequest, json_reply

from fenon import AuthError, Fenon, ServerError

TEMP_KEY = "fenon_temp_e2e_key_0123456789"
POLL = {"Fenon-Poll-After-Ms": "250"}


@pytest.fixture
def api() -> Iterator[Wire]:
    wire = Wire()
    yield wire
    wire.close()


@pytest.fixture
def storage() -> Iterator[Wire]:
    wire = Wire()
    yield wire
    wire.close()


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.delenv("FENON_LOG", raising=False)


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(bytes(range(256)) * 4096)
    return path


def run_reply(status: str, **fields: Any) -> Reply:
    body = hand_run_body(status, **fields)
    return json_reply(202 if status == "queued" else 200, body, None if status == "succeeded" else POLL)


def test_the_readme_call_end_to_end(api: Wire, storage: Wire, video: Path, tmp_path: Path) -> None:
    content = annotations_output()
    succeeded = hand_run_body("succeeded")
    succeeded["outputs"]["annotations"]["url"] = f"{storage.url}/out/annotations.jsonl?X-Amz-Signature=o1"
    reservation = reservation_body(
        UPLOAD_ID, f"{storage.url}/bucket/up?X-Amz-Signature=u1", video.stat().st_size
    )
    api.route("POST", "/v1/uploads", json_reply(201, reservation))
    api.route("POST", "/v1/runs", run_reply("queued"))
    api.route(
        "GET",
        f"/v1/runs/{RUN_ID}",
        run_reply("queued"),
        run_reply("processing_frames"),
        run_reply("processing_frames"),
        json_reply(200, succeeded),
    )
    storage.route("PUT", "/bucket/up", (200, {}, b""))
    storage.route("GET", "/out/annotations.jsonl", (200, {}, content))

    with Fenon(api_key=TEMP_KEY, base_url=f"{api.url}/v1") as client:
        result = client.pose.hand(model="fenon/hand-pose-hm", input=str(video), mode="recorded")
        frames = list(result.frames)
        saved = result.save(tmp_path / "clip.jsonl")

    assert [frame.frame_index for frame in frames] == list(range(HAND_START, HAND_END))
    assert frames[0].hands[0].handedness == "right"
    assert (result.usage.unit, result.usage.quantity) == ("frame", HAND_END - HAND_START)
    assert saved.read_bytes() == content
    assert json.loads(api.received("POST", "/v1/runs")[0].body) == {
        "model": "fenon/hand-pose-hm",
        "input": {"video": UPLOAD_ID},
        "outputs": ["annotations"],
    }
    assert storage.received("PUT", "/bucket/up")[0].body == video.read_bytes()
    assert all(r.headers["authorization"] == f"Bearer {TEMP_KEY}" for r in api.requests)
    assert all("authorization" not in r.headers for r in storage.requests)


def first_cut(request: WireRequest) -> Reply:
    if request.headers.get("authorization") != f"Bearer {TEMP_KEY}":
        error = {"code": "invalid_api_key", "message": "The API key isn't valid.", "fix": None}
        return (
            401,
            {"Content-Type": "application/json", "WWW-Authenticate": "Bearer"},
            json.dumps(
                {"error": {**error, "retryable": False, "retry_after_s": None, "details": {}}}
            ).encode(),
        )
    return json_reply(
        501,
        {
            "error": {
                "code": "not_implemented",
                "message": "This deployment can't store uploads yet.",
                "fix": None,
                "retryable": False,
                "retry_after_s": None,
                "details": {"port": "idempotency_store"},
            }
        },
    )


def test_against_a_scripted_stand_in_for_the_first_cut_api(api: Wire, storage: Wire, video: Path) -> None:
    api.route("POST", "/v1/uploads", first_cut)
    with Fenon(api_key=TEMP_KEY, base_url=f"{api.url}/v1") as client, pytest.raises(ServerError) as caught:
        client.pose.hand(model="fenon/hand-pose-hm", input=video, mode="recorded")
    assert (caught.value.code, caught.value.status_code, caught.value.retryable) == (
        "not_implemented",
        501,
        False,
    )
    assert caught.value.details == {"port": "idempotency_store"}
    assert len(api.received("POST", "/v1/uploads")) == 1
    assert storage.requests == []

    with Fenon(api_key="fenon_some_other_key", base_url=f"{api.url}/v1") as client, pytest.raises(AuthError):
        client.pose.hand(model="fenon/hand-pose-hm", input=video, mode="recorded")
    assert len(api.received("POST", "/v1/uploads")) == 2
