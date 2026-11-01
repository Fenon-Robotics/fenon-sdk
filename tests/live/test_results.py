"""Tests for the live results log: strict parsing, ordering, matching to the frames sent, and gaps."""

from __future__ import annotations

import json
from fractions import Fraction
from typing import Any

import pytest

from fenon._core.errors import ServerError
from fenon._live.results import ResultLog, media_time_90k

TIME_BASE = Fraction(1, 15360)
TICKS = 512


def result(frame_id: int, media_time: int, **extra: Any) -> str:
    return json.dumps(
        {
            "type": "result",
            "frame_id": frame_id,
            "rtp_timestamp": 7000 + media_time,
            "media_time_90k": media_time,
            "frame_width": 48,
            "frame_height": 64,
            "received_at_ms": 1,
            "sent_at_ms": 2,
            "data": {"frame": frame_id},
            **extra,
        }
    )


def log_with_sent(count: int) -> ResultLog:
    log = ResultLog()
    for index in range(count):
        log.record_sent(index * TICKS, TIME_BASE, index, float(index * TICKS * TIME_BASE))
    return log


def test_media_time_follows_aiortcs_conversion() -> None:
    assert media_time_90k(TICKS, TIME_BASE) == 3000
    assert media_time_90k(1, TIME_BASE) == 5


def test_results_are_matched_to_their_frames_in_order_whatever_the_arrival_order() -> None:
    log = log_with_sent(5)
    for frame_id in (2, 0, 1, 4, 3):
        log.record_message(result(frame_id, frame_id * 3000, unknown_field=True))
    frames, gaps, unmatched = log.collect()
    assert [frame.frame_index for frame in frames] == [0, 1, 2, 3, 4]
    assert [frame.data for frame in frames] == [{"frame": index} for index in range(5)]
    assert frames[3].time_s == pytest.approx(0.1)
    assert (frames[0].width, frames[0].height) == (48, 64)
    assert (gaps, unmatched, log.frames_sent) == ((), 0, 5)


def test_a_result_for_no_sent_frame_is_counted_never_guessed() -> None:
    log = log_with_sent(2)
    log.record_message(result(0, 1500))
    frames, _, unmatched = log.collect()
    assert (frames, unmatched) == ((), 1)


def test_gaps_point_at_the_last_frame_that_had_a_result() -> None:
    log = log_with_sent(3)
    log.record_message(result(1, 3000))
    for after in (1, None, 99):
        log.record_message(json.dumps({"type": "gap", "reason": "packet_loss", "after_frame_id": after}))
    log.record_message(json.dumps({"type": "end", "reason": "client_closed", "connected_ms": 900}))
    _, gaps, _ = log.collect()
    assert [(gap.reason, gap.after_frame_index) for gap in gaps] == [
        ("packet_loss", 1),
        ("packet_loss", None),
        ("packet_loss", None),
    ]


@pytest.mark.parametrize(
    "message",
    [
        json.dumps({"type": "pong"}),
        json.dumps({"type": "gap", "reason": "too_tired", "after_frame_id": 1}),
        json.dumps({"type": "result", "frame_id": 0, "data": {"secret": "do-not-echo"}}),
        "not json",
    ],
)
def test_a_message_outside_the_contract_is_a_contract_mismatch_that_shows_no_values(message: str) -> None:
    log = log_with_sent(1)
    log.record_message(message)
    with pytest.raises(ServerError) as raised:
        log.collect()
    assert raised.value.code == "contract_mismatch"
    assert "do-not-echo" not in raised.value.message
    assert "too_tired" not in raised.value.message
