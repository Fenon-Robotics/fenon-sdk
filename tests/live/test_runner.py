"""Tests for the live runner on fake media: new sessions, matched results, early ends, Ctrl-C, Jupyter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from _support import (
    LIVE_SESSION_ID,
    FakeMedia,
    Router,
    error_response,
    install_fake_media,
    json_response,
    live_created_body,
    live_ended_body,
    live_session_body,
)

from fenon._core.errors import LiveSessionError, ServerError, SessionLimitError
from fenon._core.http import HttpClients
from fenon._live import runner
from fenon._live.results import LiveRun

CLIP = Path(__file__).parent / "fixtures" / "rotated-90.mp4"
SESSIONS = "/v1/live/sessions"
SESSION = f"{SESSIONS}/{LIVE_SESSION_ID}"


@pytest.fixture
def media(monkeypatch: pytest.MonkeyPatch) -> FakeMedia:
    return install_fake_media(monkeypatch)


def api(*created: dict[str, object], end_reason: str = "client_closed") -> Router:
    return Router(
        {
            ("POST", SESSIONS): [json_response(201, body) for body in created],
            ("DELETE", SESSION): [json_response(200, live_ended_body(end_reason))],
        }
    )


def run(make_clients: Callable[..., HttpClients], router: Router) -> LiveRun:
    clients = make_clients(router)
    return runner.run_file(clients.api, clients.runtime, model="fenon/hand-pose-hm", path=CLIP)


def test_a_file_streams_through_one_session_and_every_result_is_matched(
    make_clients: Callable[..., HttpClients], media: FakeMedia
) -> None:
    router = api(live_created_body())
    result = run(make_clients, router)
    assert [frame.frame_index for frame in result.frames] == list(range(9))
    assert [round(frame.time_s * 30) for frame in result.frames] == list(range(9))
    assert (result.frames_sent, result.unmatched_results, result.end_reason) == (9, 0, "client_closed")
    assert (result.usage.unit, result.usage.quantity, result.usage.final) == ("second", 10, True)
    assert (media.offers, media.hang_ups) == (1, 1)
    assert len(router.calls("DELETE", SESSION)) == 1


def test_a_replay_without_a_connection_is_ended_and_replaced(
    make_clients: Callable[..., HttpClients], media: FakeMedia
) -> None:
    router = api(live_session_body(connection=None), live_created_body())
    result = run(make_clients, router)
    assert len(result.frames) == 9
    assert (len(router.calls("POST", SESSIONS)), len(router.calls("DELETE", SESSION))) == (2, 2)
    assert media.offers == 1


def test_an_offer_refused_as_already_connected_gets_one_new_session(
    make_clients: Callable[..., HttpClients], media: FakeMedia
) -> None:
    media.offer_errors.append(ServerError("taken", code="already_connected"))
    router = api(live_created_body(), live_created_body())
    assert len(run(make_clients, router).frames) == 9
    assert (media.offers, len(router.calls("POST", SESSIONS)), len(router.calls("DELETE", SESSION))) == (
        2,
        2,
        2,
    )


def test_a_second_refusal_is_raised_after_both_sessions_are_ended(
    make_clients: Callable[..., HttpClients], media: FakeMedia
) -> None:
    media.offer_errors.extend(ServerError("taken", code="already_connected") for _ in range(2))
    router = api(live_created_body(), live_created_body())
    with pytest.raises(ServerError) as raised:
        run(make_clients, router)
    assert raised.value.code == "already_connected"
    assert len(router.calls("DELETE", SESSION)) == 2


def test_other_offer_errors_are_raised_without_a_new_session(
    make_clients: Callable[..., HttpClients], media: FakeMedia
) -> None:
    media.offer_errors.append(ServerError("no engine", code="not_implemented"))
    router = api(live_created_body())
    with pytest.raises(ServerError) as raised:
        run(make_clients, router)
    assert raised.value.code == "not_implemented"
    assert (len(router.calls("POST", SESSIONS)), len(router.calls("DELETE", SESSION))) == (1, 1)


def test_a_lost_connection_raises_with_the_results_that_arrived(
    make_clients: Callable[..., HttpClients], media: FakeMedia
) -> None:
    media.lose_after = 3
    router = api(live_created_body(), end_reason="gateway_lost")
    with pytest.raises(LiveSessionError) as raised:
        run(make_clients, router)
    assert raised.value.code == "gateway_lost"
    assert isinstance(raised.value.result, LiveRun)
    assert [frame.frame_index for frame in raised.value.result.frames] == [0, 1, 2]
    assert (media.hang_ups, len(router.calls("DELETE", SESSION))) == (1, 1)


def test_a_session_the_sdk_did_not_end_is_an_early_end_even_with_every_frame_sent(
    make_clients: Callable[..., HttpClients], media: FakeMedia
) -> None:
    router = api(live_created_body(), end_reason="max_duration")
    with pytest.raises(LiveSessionError) as raised:
        run(make_clients, router)
    assert raised.value.code == "max_duration"
    assert isinstance(raised.value.result, LiveRun)
    assert len(raised.value.result.frames) == 9


@pytest.mark.parametrize(
    ("name", "code", "good_frames"),
    [("truncated.mp4", "file_damaged", 21), ("size-change.ts", "video_size_changes", 6)],
)
def test_a_file_that_breaks_partway_reports_its_own_cause_with_the_frames_before_it(
    make_clients: Callable[..., HttpClients], media: FakeMedia, name: str, code: str, good_frames: int
) -> None:
    router = api(live_created_body())
    clients = make_clients(router)
    with pytest.raises(LiveSessionError) as raised:
        runner.run_file(clients.api, clients.runtime, model="fenon/hand-pose-hm", path=CLIP.parent / name)
    assert raised.value.code == code
    assert raised.value.retryable is False
    assert isinstance(raised.value.result, LiveRun)
    assert len(raised.value.result.frames) == good_frames
    assert (media.hang_ups, len(router.calls("DELETE", SESSION))) == (1, 1)


def test_ctrl_c_still_hangs_up_and_ends_the_session(
    make_clients: Callable[..., HttpClients], media: FakeMedia
) -> None:
    media.interrupt_after = 4
    router = api(live_created_body())
    with pytest.raises(KeyboardInterrupt):
        run(make_clients, router)
    assert (media.hang_ups, len(router.calls("DELETE", SESSION))) == (1, 1)


def test_results_outside_the_contract_fail_after_the_session_is_ended(
    make_clients: Callable[..., HttpClients], media: FakeMedia
) -> None:
    media.raw_message = '{"type": "pong"}'
    router = api(live_created_body())
    with pytest.raises(ServerError) as raised:
        run(make_clients, router)
    assert raised.value.code == "contract_mismatch"
    assert len(router.calls("DELETE", SESSION)) == 1


def test_a_call_from_inside_a_running_event_loop_runs_on_a_worker_thread(
    make_clients: Callable[..., HttpClients], media: FakeMedia
) -> None:
    router = api(live_created_body())

    async def inside_a_notebook() -> LiveRun:
        return run(make_clients, router)

    assert len(asyncio.run(inside_a_notebook()).frames) == 9


def test_api_errors_on_create_reach_the_caller_unchanged(
    make_clients: Callable[..., HttpClients], media: FakeMedia
) -> None:
    router = Router({("POST", SESSIONS): [error_response(409, "active_session_limit", details={"limit": 5})]})
    with pytest.raises(SessionLimitError):
        run(make_clients, router)
    assert media.offers == 0
