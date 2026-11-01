"""Tests for the live gateway client: the offer's auth, limits and retry window, and the hang-up outcomes."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from _support import FakeRuntime, Router, error_response

from fenon._core.errors import AuthError, NetworkError, ServerError
from fenon._live.gateway import GatewayClient, HangUp
from fenon._live.sessions import GatewayAccess

SESSION_ID = "ls_30srkafwa0jd"
TOKEN = "session-token-secret-value"
OFFER_PATH = f"/v1/sessions/{SESSION_ID}/offer"
SESSION_PATH = f"/v1/sessions/{SESSION_ID}"
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
ANSWER = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n"


def access(connect_in_s: float = 60) -> GatewayAccess:
    return GatewayAccess(
        session_id=SESSION_ID,
        offer_url=f"https://gateway.fenon.test{OFFER_PATH}",
        session_url=f"https://gateway.fenon.test{SESSION_PATH}",
        connect_by=NOW + timedelta(seconds=connect_in_s),
        token=TOKEN,
        ice_servers=(),
    )


def sdp_answer(status: int = 201, body: str = ANSWER) -> httpx2.Response:
    return httpx2.Response(status, text=body, headers={"Content-Type": "application/sdp"})


@pytest.fixture
def gateway_for(fake_runtime: FakeRuntime) -> Iterator[Callable[..., GatewayClient]]:
    built: list[GatewayClient] = []

    def factory(router: Router, connect_in_s: float = 60) -> GatewayClient:
        client = GatewayClient(access(connect_in_s), fake_runtime.as_runtime(), httpx2.MockTransport(router))
        built.append(client)
        return client

    yield factory
    for client in built:
        client.close()


def test_offer_posts_sdp_with_the_session_token_and_returns_the_answer(
    gateway_for: Callable[..., GatewayClient],
) -> None:
    router = Router({("POST", OFFER_PATH): [sdp_answer()]})
    assert gateway_for(router).offer("v=0\r\n") == ANSWER
    request = router.calls("POST", OFFER_PATH)[0]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Content-Type"] == "application/sdp"
    assert request.content == b"v=0\r\n"


def test_a_retryable_gateway_error_is_retried_after_its_hint(
    gateway_for: Callable[..., GatewayClient], fake_runtime: FakeRuntime
) -> None:
    router = Router(
        {
            ("POST", OFFER_PATH): [
                error_response(503, "engine_unavailable", retryable=True, retry_after_s=2),
                sdp_answer(),
            ]
        }
    )
    assert gateway_for(router).offer("v=0\r\n") == ANSWER
    assert len(router.calls("POST", OFFER_PATH)) == 2
    assert fake_runtime.sleeps == [2]


def test_a_network_failure_is_retried(gateway_for: Callable[..., GatewayClient]) -> None:
    router = Router({("POST", OFFER_PATH): [httpx2.ConnectError("refused"), sdp_answer()]})
    assert gateway_for(router).offer("v=0\r\n") == ANSWER


def test_a_network_failure_that_persists_is_a_network_error(
    gateway_for: Callable[..., GatewayClient],
) -> None:
    router = Router({("POST", OFFER_PATH): [httpx2.ConnectError("refused")]})
    with pytest.raises(NetworkError) as raised:
        gateway_for(router).offer("v=0\r\n")
    assert raised.value.code == "network_error"
    assert len(router.calls("POST", OFFER_PATH)) == 3


def test_already_connected_is_raised_at_once_for_the_caller_to_replace_the_session(
    gateway_for: Callable[..., GatewayClient],
) -> None:
    router = Router({("POST", OFFER_PATH): [error_response(409, "already_connected")]})
    with pytest.raises(ServerError) as raised:
        gateway_for(router).offer("v=0\r\n")
    assert raised.value.code == "already_connected"
    assert len(router.calls("POST", OFFER_PATH)) == 1


def test_an_invalid_token_is_an_auth_error_without_retry(gateway_for: Callable[..., GatewayClient]) -> None:
    router = Router({("POST", OFFER_PATH): [error_response(401, "invalid_token")]})
    with pytest.raises(AuthError) as raised:
        gateway_for(router).offer("v=0\r\n")
    assert raised.value.code == "invalid_token"
    assert len(router.calls("POST", OFFER_PATH)) == 1


def test_no_retry_is_attempted_that_would_land_after_the_connect_window(
    gateway_for: Callable[..., GatewayClient], fake_runtime: FakeRuntime
) -> None:
    router = Router(
        {("POST", OFFER_PATH): [error_response(503, "unavailable", retryable=True, retry_after_s=10)]}
    )
    with pytest.raises(ServerError) as raised:
        gateway_for(router, connect_in_s=12).offer("v=0\r\n")
    assert raised.value.code == "unavailable"
    assert len(router.calls("POST", OFFER_PATH)) == 1
    assert fake_runtime.sleeps == []


def test_no_offer_is_sent_once_the_connect_window_has_closed(
    gateway_for: Callable[..., GatewayClient],
) -> None:
    router = Router({("POST", OFFER_PATH): [sdp_answer()]})
    with pytest.raises(ServerError) as raised:
        gateway_for(router, connect_in_s=5).offer("v=0\r\n")
    assert raised.value.code == "connect_window_closed"
    assert router.requests == []


def test_an_offer_over_64_kb_is_refused_before_sending(gateway_for: Callable[..., GatewayClient]) -> None:
    router = Router({("POST", OFFER_PATH): [sdp_answer()]})
    with pytest.raises(ServerError) as raised:
        gateway_for(router).offer("a" * (64 * 1024 + 1))
    assert raised.value.code == "offer_too_large"
    assert router.requests == []


def test_an_accepted_offer_without_an_answer_is_unusable(gateway_for: Callable[..., GatewayClient]) -> None:
    router = Router({("POST", OFFER_PATH): [sdp_answer(body="")]})
    with pytest.raises(ServerError) as raised:
        gateway_for(router).offer("v=0\r\n")
    assert raised.value.code == "invalid_response"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (httpx2.Response(200, json={"status": "ended"}), HangUp.ENDED),
        (error_response(404, "not_found"), HangUp.NOT_CONNECTED),
        (error_response(401, "invalid_token"), HangUp.REFUSED),
        (httpx2.ReadTimeout("slow"), HangUp.UNREACHABLE),
    ],
)
def test_hang_up_reports_its_outcome_and_never_raises(
    gateway_for: Callable[..., GatewayClient], outcome: httpx2.Response | Exception, expected: HangUp
) -> None:
    router = Router({("DELETE", SESSION_PATH): [outcome]})
    assert gateway_for(router).hang_up() is expected
    assert router.calls("DELETE", SESSION_PATH)[0].headers["Authorization"] == f"Bearer {TOKEN}"
