"""Tests for the HaMeR hand-pose job: result validation, typed frames and the frame-by-frame checks."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
from _support import (
    HAND_END,
    HAND_START,
    OUTPUT_URL,
    RUN_ID,
    Router,
    annotations_output,
    hand_result,
    hand_row,
    hand_run_body,
)

from fenon._core._generated import Run
from fenon._core.errors import ServerError
from fenon._core.http import HttpClients
from fenon._core.runs import RunSnapshot
from fenon.models.pose.hamer import CONTRACT, JOINT_ORDER, HandPoseFrame, HandPoseJob, HandPoseResult

MakeClients = Callable[..., HttpClients]
OUTPUT_PATH = "/out/annotations.jsonl"
FRAMES = HAND_END - HAND_START


def succeeded(content: bytes = annotations_output(), **fields: Any) -> RunSnapshot:
    return RunSnapshot(Run.model_validate(hand_run_body("succeeded", content, **fields)), None)


def finished_result(
    make_clients: MakeClients, content: bytes = annotations_output()
) -> tuple[HandPoseResult, Router]:
    storage = Router({("GET", OUTPUT_PATH): [httpx2.Response(200, content=content)]})
    return HandPoseJob(make_clients(storage=storage), succeeded(content)).result(), storage


def lines(*rows: dict[str, Any] | bytes) -> bytes:
    return b"".join((row if isinstance(row, bytes) else json.dumps(row).encode()) + b"\n" for row in rows)


def test_the_contract_and_joint_order_come_from_the_generated_types() -> None:
    assert (CONTRACT.id, list(CONTRACT.shipped)) == ("fenon.hand_pose.video", ["1.1.0"])
    assert len(JOINT_ORDER) == 21
    assert (JOINT_ORDER[0], JOINT_ORDER[-1]) == ("wrist", "pinky_tip")


def test_result_carries_the_run_video_camera_and_usage(make_clients: MakeClients) -> None:
    result, storage = finished_result(make_clients)
    assert (result.id, result.model, result.model_version) == (RUN_ID, "fenon/test-model", "tm_2026_10_26")
    assert (result.video.width, result.video.height, result.video.fps) == (1280, 720, 30.0)
    assert result.joint_order == JOINT_ORDER
    assert (result.camera.focal_length_px, result.camera.assumed) == (25000.0, True)
    assert (result.usage.unit, result.usage.quantity) == ("frame", FRAMES)
    assert storage.requests == []
    assert "output-signature" not in repr(result)


def test_frames_are_typed_reiterable_and_downloaded_once(make_clients: MakeClients) -> None:
    result, storage = finished_result(make_clients)
    first = list(result.frames)
    second = list(result.frames)
    assert [frame.frame_index for frame in first] == list(range(HAND_START, HAND_END))
    assert all(isinstance(frame, HandPoseFrame) for frame in first)
    assert first == second
    assert first[0].hands[0].handedness == "right"
    assert first[0].hands[0].pixel_landmarks[20] == [None, None]
    assert len(storage.requests) == 1


def test_save_writes_the_exact_output(make_clients: MakeClients, tmp_path: Path) -> None:
    content = annotations_output()
    result, _ = finished_result(make_clients, content)
    assert result.save(tmp_path / "frames.jsonl").read_bytes() == content


def test_unknown_fields_are_ignored(make_clients: MakeClients) -> None:
    content = lines(*(hand_row(i, track_id="t", future_field=1) for i in range(HAND_START, HAND_END)))
    result, _ = finished_result(make_clients, content)
    assert len(list(result.frames)) == FRAMES


@pytest.mark.parametrize(
    ("content", "line"),
    [
        (lines(hand_row(60), b"{not json", hand_row(62)), 2),
        (lines(hand_row(60), hand_row(61), hand_row(62, hands=[{"handedness": "middle"}])), 3),
        (lines(hand_row(60), hand_row(62), hand_row(63)), 2),
        (lines(hand_row(60), hand_row(60), hand_row(61)), 2),
        (lines(hand_row(61), hand_row(60), hand_row(62)), 1),
        (lines(hand_row(60), b"", hand_row(61), hand_row(62)), 2),
        (lines(hand_row(60), hand_row(61), hand_row(62), hand_row(63)), 4),
        (lines(hand_row(60), hand_row(61), hand_row(62)).replace(b"812.4", b"1e309", 1), 1),
        (lines(hand_row(60), hand_row(61), hand_row(62)).replace(b"812.4", b"NaN", 1), 1),
        (lines(hand_row(60), hand_row(61, joint_order="wrist"), hand_row(62)), 2),
    ],
    ids=[
        "bad-json",
        "bad-row",
        "gap",
        "duplicate",
        "reordered",
        "blank",
        "too-many",
        "overflow",
        "nan",
        "joints",
    ],
)
def test_a_row_breaking_the_contract_is_named(make_clients: MakeClients, content: bytes, line: int) -> None:
    result, _ = finished_result(make_clients, content)
    with pytest.raises(ServerError) as caught:
        list(result.frames)
    assert caught.value.code == "output_invalid"
    assert caught.value.details == {"line": line}
    assert caught.value.__context__ is None


def test_too_few_rows_is_reported_with_both_counts(make_clients: MakeClients) -> None:
    result, _ = finished_result(make_clients, lines(hand_row(60), hand_row(61)))
    with pytest.raises(ServerError) as caught:
        list(result.frames)
    assert caught.value.code == "output_invalid"
    assert caught.value.details == {"expected_frames": FRAMES, "received_frames": 2}


@pytest.mark.parametrize(
    "fields",
    [
        {"result": hand_result(joint_order=list(reversed(JOINT_ORDER)))},
        {"result": hand_result(camera=None)},
        {"result": None},
        {"usage": None},
        {"outputs": None},
        {
            "outputs": {
                "annotations": {
                    "content_type": "text/plain",
                    "url": OUTPUT_URL,
                    "url_expires_at": "2026-10-26T13:00:00.000Z",
                    "size_bytes": 10,
                }
            }
        },
    ],
    ids=["joint-order", "no-camera", "no-result", "no-usage", "no-outputs", "wrong-output-type"],
)
def test_a_run_breaking_the_contract_is_invalid_response(
    make_clients: MakeClients, fields: dict[str, Any]
) -> None:
    with pytest.raises(ServerError) as caught:
        HandPoseJob(make_clients(), succeeded(**fields)).result()
    assert caught.value.code == "invalid_response"
    assert caught.value.__context__ is None
    assert "output-signature" not in str(caught.value)


def text_numbers(content: bytes) -> bytes:
    return content.replace(b'"frame_index": 60', b'"frame_index": "60"', 1)


@pytest.mark.parametrize(
    "content",
    [
        text_numbers(annotations_output()),
        annotations_output().replace(b"[812.4, 455.1]", b'["NaN", 455.1]', 1),
    ],
    ids=["frame-index-as-text", "nan-as-text"],
)
def test_numbers_sent_as_text_are_refused(make_clients: MakeClients, content: bytes) -> None:
    result, _ = finished_result(make_clients, content)
    with pytest.raises(ServerError) as caught:
        list(result.frames)
    assert caught.value.details == {"line": 1}


@pytest.mark.parametrize(
    "fields",
    [
        {"usage": {"unit": "second", "quantity": FRAMES}},
        {"usage": {"unit": "frame", "quantity": 999}},
        {
            "result": hand_result(
                video={"width": "1280", "height": 720, "fps": 30.0, "frame_count": 300, "duration_s": 10.0}
            )
        },
    ],
    ids=["wrong-unit", "wrong-quantity", "width-as-text"],
)
def test_usage_must_match_the_frames_and_types_stay_strict(
    make_clients: MakeClients, fields: dict[str, Any]
) -> None:
    with pytest.raises(ServerError) as caught:
        HandPoseJob(make_clients(), succeeded(**fields)).result()
    assert caught.value.code == "invalid_response"


def test_errors_building_the_result_carry_the_job_and_request_id(make_clients: MakeClients) -> None:
    snapshot = RunSnapshot(
        Run.model_validate(hand_run_body("succeeded", usage=None)), None, "req_m4n7b2v9c1x5"
    )
    job = HandPoseJob(make_clients(), snapshot)
    with pytest.raises(ServerError) as caught:
        job.result()
    assert caught.value.job is job
    assert caught.value.request_id == "req_m4n7b2v9c1x5"
