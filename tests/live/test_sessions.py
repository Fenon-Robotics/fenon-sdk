"""Tests for fenon-api's live-session calls, the session duration rule and the checks on a created session's
connection.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from _support import FakeRuntime, Router, error_response, json_response

from fenon._core.errors import InvalidInputError, ServerError, SessionLimitError
from fenon._core.http import HttpClients
from fenon._live.sessions import LiveSessions, gateway_access, session_duration_s

SESSION_ID = "ls_30srkafwa0jd"
TOKEN = "session-token-secret-value"
TURN_SECRET = "turn-credential-secret-value"
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
SESSIONS = "/v1/live/sessions"


def session_body(**fields: Any) -> dict[str, Any]:
    return {
        "id": SESSION_ID,
        "object": "live_session",
        "model": "fenon/hand-pose-hm",
        "status": "waiting_for_media",
        "end_reason": None,
        "max_duration_s": 13,
        "idle_timeout_s": 15,
        "usage": {"unit": "second", "quantity": 0, "final": False},
        "created_at": "2026-10-26T12:00:00.000Z",
        "connected_at": None,
        "ended_at": None,
        **fields,
    }


def connection_body(**fields: Any) -> dict[str, Any]:
    return {
        "transport": "webrtc",
        "offer_url": f"https://gateway.fenon.test/v1/sessions/{SESSION_ID}/offer",
        "session_url": f"https://gateway.fenon.test/v1/sessions/{SESSION_ID}",
        "results_channel": "results",
        "token": TOKEN,
        "token_connect_by": "2026-10-26T12:01:00.000Z",
        "ice_servers": [
            {"urls": "stun:stun.cloudflare.com:3478"},
            {"urls": ["turn:turn.cloudflare.com:3478"], "username": "user", "credential": TURN_SECRET},
        ],
        **fields,
    }


def created_body(**connection: Any) -> dict[str, Any]:
    return session_body(connection=connection_body(**connection))


@pytest.fixture
def sessions_for(make_clients: Callable[..., HttpClients]) -> Callable[[Router], LiveSessions]:
    return lambda router: LiveSessions(make_clients(router).api)


def test_create_asks_for_the_file_rate_and_a_duration_covering_the_video(
    sessions_for: Callable[[Router], LiveSessions],
) -> None:
    router = Router({("POST", SESSIONS): [json_response(201, created_body())]})
    created = sessions_for(router).create(model="fenon/hand-pose-hm", input_duration_s=7.5)
    request = router.calls("POST", SESSIONS)[0]
    assert json.loads(request.content) == {
        "model": "fenon/hand-pose-hm",
        "max_duration_s": 13,
    }
    assert request.headers["Idempotency-Key"]
    assert created.id == SESSION_ID
    assert created.connection is not None
    assert created.connection.results_channel == "results"
    assert TOKEN not in repr(created)
    assert TURN_SECRET not in repr(created)


def test_create_retries_in_progress_with_the_same_idempotency_key(
    sessions_for: Callable[[Router], LiveSessions], fake_runtime: FakeRuntime
) -> None:
    router = Router(
        {
            ("POST", SESSIONS): [
                error_response(409, "idempotency_in_progress", retryable=True, retry_after_s=1),
                json_response(201, created_body()),
            ]
        }
    )
    sessions_for(router).create(model="fenon/hand-pose-hm", input_duration_s=7.5)
    keys = {request.headers["Idempotency-Key"] for request in router.calls("POST", SESSIONS)}
    assert len(router.calls("POST", SESSIONS)) == 2
    assert len(keys) == 1
    assert fake_runtime.sleeps == [1]


def test_active_session_limit_is_a_session_limit_error_without_retry(
    sessions_for: Callable[[Router], LiveSessions],
) -> None:
    router = Router({("POST", SESSIONS): [error_response(409, "active_session_limit", details={"limit": 5})]})
    with pytest.raises(SessionLimitError) as raised:
        sessions_for(router).create(model="fenon/hand-pose-hm", input_duration_s=7.5)
    assert raised.value.details == {"limit": 5}
    assert raised.value.fix == "Wait for one of this project's running live calls to finish, then try again."
    assert len(router.calls("POST", SESSIONS)) == 1


def test_an_unknown_status_is_a_contract_mismatch_that_never_shows_the_token(
    sessions_for: Callable[[Router], LiveSessions],
) -> None:
    body = created_body()
    body["status"] = "paused"
    router = Router({("POST", SESSIONS): [json_response(201, body)]})
    with pytest.raises(ServerError) as raised:
        sessions_for(router).create(model="fenon/hand-pose-hm", input_duration_s=7.5)
    assert raised.value.code == "contract_mismatch"
    assert "status" in raised.value.message
    assert TOKEN not in raised.value.message


def test_unknown_fields_are_ignored(sessions_for: Callable[[Router], LiveSessions]) -> None:
    router = Router(
        {("GET", f"{SESSIONS}/{SESSION_ID}"): [json_response(200, session_body(metadata={"a": 1}))]}
    )
    assert sessions_for(router).get(SESSION_ID).status == "waiting_for_media"


def test_end_returns_the_ended_session_with_its_usage(sessions_for: Callable[[Router], LiveSessions]) -> None:
    ended = session_body(
        status="ended",
        end_reason="client_closed",
        usage={"unit": "second", "quantity": 10, "final": True},
        connected_at="2026-10-26T12:00:01.000Z",
        ended_at="2026-10-26T12:00:09.000Z",
    )
    router = Router({("DELETE", f"{SESSIONS}/{SESSION_ID}"): [json_response(200, ended)]})
    session = sessions_for(router).end(SESSION_ID)
    assert (session.end_reason, session.usage.quantity, session.usage.final) == ("client_closed", 10, True)


@pytest.mark.parametrize(("duration_s", "expected"), [(7.5, 13), (2.0, 10), (0.01, 10), (3595.0, 3600)])
def test_session_duration_covers_the_video_within_the_contract_range(
    duration_s: float, expected: int
) -> None:
    assert session_duration_s(duration_s) == expected


@pytest.mark.parametrize(
    ("duration_s", "code"),
    [(3595.5, "video_too_long"), (0.0, "invalid_input"), (float("nan"), "invalid_input")],
)
def test_videos_a_session_cannot_cover_are_refused_before_any_call(duration_s: float, code: str) -> None:
    with pytest.raises(InvalidInputError) as raised:
        session_duration_s(duration_s)
    assert raised.value.code == code


def parsed(sessions_for: Callable[[Router], LiveSessions], body: dict[str, Any]) -> Any:
    router = Router({("POST", SESSIONS): [json_response(201, body)]})
    return sessions_for(router).create(model="fenon/hand-pose-hm", input_duration_s=7.5)


def test_gateway_access_carries_the_connection_and_hides_its_secrets(
    sessions_for: Callable[[Router], LiveSessions],
) -> None:
    access = gateway_access(parsed(sessions_for, created_body()), NOW)
    assert access is not None
    assert access.session_id == SESSION_ID
    assert access.token == TOKEN
    assert len(access.ice_servers) == 2
    assert TOKEN not in repr(access)
    assert TURN_SECRET not in repr(access)


def test_a_replay_without_a_connection_has_no_gateway_access(
    sessions_for: Callable[[Router], LiveSessions],
) -> None:
    assert gateway_access(parsed(sessions_for, session_body(connection=None)), NOW) is None


@pytest.mark.parametrize("seconds_left", [0, 3, 5])
def test_a_token_inside_the_connect_margin_has_no_gateway_access(
    sessions_for: Callable[[Router], LiveSessions], seconds_left: int
) -> None:
    connect_by = (NOW + timedelta(seconds=seconds_left)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert gateway_access(parsed(sessions_for, created_body(token_connect_by=connect_by)), NOW) is None


@pytest.mark.parametrize(
    ("offer_url", "session_url"),
    [
        ("http://gateway.fenon.test/v1/sessions/x/offer", "http://gateway.fenon.test/v1/sessions/x"),
        ("https://gateway.fenon.test/v1/sessions/x/offer", "https://elsewhere.test/v1/sessions/x"),
        ("https://gateway.fenon.test:8443/v1/sessions/x/offer", "https://gateway.fenon.test/v1/sessions/x"),
    ],
)
def test_gateway_urls_off_https_or_across_origins_are_refused(
    sessions_for: Callable[[Router], LiveSessions], offer_url: str, session_url: str
) -> None:
    session = parsed(sessions_for, created_body(offer_url=offer_url, session_url=session_url))
    with pytest.raises(ServerError) as raised:
        gateway_access(session, NOW)
    assert raised.value.code == "unsafe_gateway_url"


def test_plain_http_is_allowed_only_on_a_loopback_gateway(
    sessions_for: Callable[[Router], LiveSessions],
) -> None:
    session = parsed(
        sessions_for,
        created_body(
            offer_url=f"http://localhost:8790/v1/sessions/{SESSION_ID}/offer",
            session_url=f"http://localhost:8790/v1/sessions/{SESSION_ID}",
        ),
    )
    access = gateway_access(session, NOW)
    assert access is not None
    assert httpx2.URL(access.offer_url).host == "localhost"
