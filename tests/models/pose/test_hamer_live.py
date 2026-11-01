"""Tests for hand pose over a live session: typed frames, the shared Hand, saving, the public call."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _support import (
    LIVE_SESSION_ID,
    FakeMedia,
    Router,
    hand_pose_live_data,
    install_fake_media,
    json_response,
    live_created_body,
    live_ended_body,
)

import fenon._live
from fenon import Fenon, Hand, HandPoseLiveResult, LiveSessionError, ServerError
from fenon._live import results
from fenon.models.pose.hamer._generated_live import Hand as LiveHand
from fenon.models.pose.hamer.live import hand_pose_live_result

CLIP = Path(__file__).parents[2] / "live" / "fixtures" / "rotated-90.mp4"
SESSIONS = "/v1/live/sessions"
SESSION = f"{SESSIONS}/{LIVE_SESSION_ID}"


def without_descriptions(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: without_descriptions(v) for k, v in schema.items() if k not in ("description", "title")}
    if isinstance(schema, list):
        return [without_descriptions(item) for item in schema]
    return schema


def live_run(data: dict[str, Any], count: int = 3) -> results.LiveRun:
    frames = tuple(
        results.LiveFrame(frame_index=i, time_s=i / 30, width=1280, height=720, data=data)
        for i in range(count)
    )
    return results.LiveRun(
        session_id=LIVE_SESSION_ID,
        model="fenon/hand-pose-hm",
        frames=frames,
        gaps=(results.LiveGap(reason="packet_loss", after_frame_index=1),),
        end_reason="client_closed",
        usage=fenon._live.LiveUsage.model_validate({"unit": "second", "quantity": 10, "final": True}),
        frames_sent=count,
        unmatched_results=0,
    )


def api() -> Router:
    return Router(
        {
            ("POST", SESSIONS): [json_response(201, live_created_body())],
            ("DELETE", SESSION): [json_response(200, live_ended_body())],
        }
    )


def test_the_live_hand_is_the_recorded_hand_field_for_field() -> None:
    assert without_descriptions(LiveHand.model_json_schema()) == without_descriptions(
        Hand.model_json_schema()
    )


def test_each_frames_data_becomes_typed_hands_of_the_shared_type() -> None:
    result = hand_pose_live_result(live_run(hand_pose_live_data()))
    assert [frame.frame_index for frame in result.frames] == [0, 1, 2]
    frame = result.frames[1]
    assert frame.image_size == (1280, 720)
    assert frame.camera.focal_length_px == 1000.0
    assert len(frame.hands) == 1
    assert isinstance(frame.hands[0], Hand)
    assert frame.hands[0].handedness == "right"
    assert result.joint_order[0] == "wrist"
    assert (result.gaps[0].after_frame_index, result.usage.unit, result.end_reason) == (
        1,
        "second",
        "client_closed",
    )


def test_data_outside_the_draft_schema_is_a_contract_mismatch_naming_the_frame() -> None:
    with pytest.raises(ServerError) as raised:
        hand_pose_live_result(live_run({"keyframe": True, "sha256": "secret-looking-value"}))
    assert raised.value.code == "contract_mismatch"
    assert "frame 0" in raised.value.message
    assert "secret-looking-value" not in raised.value.message


def test_save_writes_one_json_line_per_frame(tmp_path: Path) -> None:
    result = hand_pose_live_result(live_run(hand_pose_live_data()))
    path = result.save(tmp_path / "hands.jsonl")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["frame_index"] for row in rows] == [0, 1, 2]
    assert rows[0]["image_size"] == {"width": 1280, "height": 720}
    assert rows[0]["hands"][0]["handedness"] == "right"
    assert set(rows[0]) == {
        "frame_index",
        "time_s",
        "image_size",
        "camera",
        "raw_hand_count",
        "suppressed_duplicate_count",
        "hands",
    }


def test_pose_hand_live_returns_typed_results(
    make_fenon: Callable[..., Fenon], monkeypatch: pytest.MonkeyPatch
) -> None:
    media: FakeMedia = install_fake_media(monkeypatch)
    media.data = hand_pose_live_data()
    result = make_fenon(api()).pose.hand(model="fenon/hand-pose-hm", input=CLIP, mode="live")
    assert isinstance(result, HandPoseLiveResult)
    assert len(result.frames) == 9
    assert all(isinstance(frame.hands[0], Hand) for frame in result.frames)


def test_an_early_end_carries_the_typed_partial_result(
    make_fenon: Callable[..., Fenon], monkeypatch: pytest.MonkeyPatch
) -> None:
    media = install_fake_media(monkeypatch)
    media.data = hand_pose_live_data()
    media.lose_after = 2
    with pytest.raises(LiveSessionError) as raised:
        make_fenon(api()).pose.hand(model="fenon/hand-pose-hm", input=CLIP, mode="live")
    assert isinstance(raised.value.result, HandPoseLiveResult)
    assert len(raised.value.result.frames) == 2
