"""Opt-in: the live runner and public call against fenon-app's local echo-engine stack (live.md)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fenon import Fenon, ServerError
from fenon._core.http import ClientConfig, open_http_clients
from fenon._live import run_file

API_KEY = os.environ.get("FENON_LIVE_STACK_API_KEY")
BASE_URL = os.environ.get("FENON_LIVE_STACK_BASE_URL", "http://127.0.0.1:8000/v1")
FIXTURES = Path(__file__).parents[1] / "live" / "fixtures"

pytestmark = pytest.mark.skipif(
    not API_KEY, reason="needs fenon-app's local live stack (docs/features/live.md)"
)


@pytest.mark.parametrize(("name", "size"), [("rotated-90.mp4", (48, 64)), ("odd-size.mp4", (62, 46))])
def test_the_runner_streams_a_file_and_matches_every_result(name: str, size: tuple[int, int]) -> None:
    config = ClientConfig.resolve(api_key=API_KEY, base_url=BASE_URL, timeout=60, max_retries=2)
    clients = open_http_clients(config)
    try:
        run = run_file(clients.api, clients.runtime, model="fenon/hand-pose-hm", path=FIXTURES / name)
    finally:
        clients.close()
    assert [frame.frame_index for frame in run.frames] == list(range(run.frames_sent))
    assert run.frames_sent > 0
    assert run.unmatched_results == 0
    assert {(frame.width, frame.height) for frame in run.frames} == {size}
    assert all("sha256" in frame.data for frame in run.frames)
    assert (run.end_reason, run.usage.unit, run.gaps) == ("client_closed", "second", ())


def test_the_public_call_runs_the_session_then_refuses_echo_data_as_not_hand_pose() -> None:
    with Fenon(api_key=API_KEY, base_url=BASE_URL) as client, pytest.raises(ServerError) as raised:
        client.pose.hand(model="fenon/hand-pose-hm", input=FIXTURES / "rotated-90.mp4", mode="live")
    assert raised.value.code == "contract_mismatch"
    assert "hand-pose data of frame 0" in raised.value.message
