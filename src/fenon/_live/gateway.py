"""The live gateway's offer and hang-up for one session, with its session token (docs/features/live.md)."""

from __future__ import annotations

import enum
from datetime import timedelta

import httpx2

from .._core import (
    LIMITS,
    FenonError,
    NetworkError,
    Runtime,
    ServerError,
    decode_json,
    error_from_response,
    logger,
    retry_after_of,
)
from .._version import __version__
from .config import LIVE_LIMITS
from .sessions import GatewayAccess


class HangUp(enum.Enum):
    ENDED = "ended"
    NOT_CONNECTED = "not_connected"
    REFUSED = "refused"
    UNREACHABLE = "unreachable"


class GatewayClient:
    def __init__(
        self, access: GatewayAccess, runtime: Runtime, transport: httpx2.BaseTransport | None = None
    ) -> None:
        self._access = access
        self._runtime = runtime
        self._http = httpx2.Client(
            headers={"User-Agent": f"fenon-python/{__version__}"},
            timeout=httpx2.Timeout(LIVE_LIMITS.offer_timeout_s, connect=LIMITS.connect_timeout_s),
            follow_redirects=False,
            transport=transport,
        )

    def offer(self, sdp: str) -> str:
        body = sdp.encode()
        if len(body) > LIVE_LIMITS.max_offer_bytes:
            raise ServerError(
                "The SDK built an SDP offer over the gateway's 64 KB limit.",
                code="offer_too_large",
                fix="Report this to Fenon.",
            )
        deadline = self._access.connect_by - timedelta(seconds=LIVE_LIMITS.connect_margin_s)
        headers = {"Authorization": f"Bearer {self._access.token}", "Content-Type": "application/sdp"}
        attempt = 0
        while True:
            attempt += 1
            if self._runtime.now() >= deadline:
                raise ServerError(
                    "The live session's connect window closed before the gateway accepted the offer.",
                    code="connect_window_closed",
                    fix="Try again; a new session gets a new window.",
                )
            try:
                response = self._http.post(self._access.offer_url, content=body, headers=headers)
            except httpx2.TransportError:
                error: FenonError = NetworkError(
                    "Couldn't reach the live gateway.",
                    code="network_error",
                    fix="Check the network connection and try again.",
                    retryable=True,
                )
                wait_s = self._backoff_s(attempt)
            else:
                logger.info(
                    "live gateway offer for %s answered %s", self._access.session_id, response.status_code
                )
                if response.status_code == 201:
                    if response.text:
                        return response.text
                    raise ServerError(
                        "The live gateway accepted the offer without an answer.", code="invalid_response"
                    )
                payload = decode_json(response.content)
                retry_after_s = retry_after_of(payload)
                error = error_from_response(
                    response.status_code,
                    payload,
                    request_id=response.headers.get("x-request-id"),
                    retry_after_s=retry_after_s,
                    retryable_without_error_body=False,
                )
                wait_s = retry_after_s if retry_after_s is not None else self._backoff_s(attempt)
            lands_in_time = self._runtime.now() + timedelta(seconds=wait_s) < deadline
            if not error.retryable or attempt >= LIVE_LIMITS.max_offer_attempts or not lands_in_time:
                raise error
            self._runtime.sleep(wait_s)

    def hang_up(self) -> HangUp:
        try:
            response = self._http.delete(
                self._access.session_url,
                headers={"Authorization": f"Bearer {self._access.token}"},
                timeout=httpx2.Timeout(LIVE_LIMITS.hang_up_timeout_s, connect=LIMITS.connect_timeout_s),
            )
        except httpx2.TransportError:
            outcome = HangUp.UNREACHABLE
        else:
            if response.status_code == 200:
                outcome = HangUp.ENDED
            elif response.status_code == 404:
                outcome = HangUp.NOT_CONNECTED
            else:
                outcome = HangUp.REFUSED
        logger.info("live gateway hang-up for %s: %s", self._access.session_id, outcome.value)
        return outcome

    def close(self) -> None:
        self._http.close()

    def _backoff_s(self, attempt: int) -> float:
        return self._runtime.random() * min(
            LIMITS.max_backoff_s, LIMITS.initial_backoff_s * 2 ** (attempt - 1)
        )
