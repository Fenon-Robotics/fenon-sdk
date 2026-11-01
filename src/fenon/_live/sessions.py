"""fenon-api's live-session calls and the checks on a session's connection (docs/features/live.md)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import httpx2
from pydantic import TypeAdapter

from .._core import ApiClient, ApiResponse, InvalidInputError, ServerError, SessionLimitError, parse_contract
from ._generated import CreatedLiveSession, IceServer, LiveSession
from .config import LIVE_LIMITS

ShapeT = TypeVar("ShapeT")

_CREATED_SESSION = TypeAdapter(CreatedLiveSession)
_SESSION = TypeAdapter(LiveSession)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class GatewayAccess:
    session_id: str
    offer_url: str
    session_url: str
    connect_by: datetime
    token: str = field(repr=False)
    ice_servers: tuple[IceServer, ...] = field(repr=False)


class LiveSessions:
    def __init__(self, api: ApiClient) -> None:
        self._api = api

    def create(self, *, model: str, input_duration_s: float) -> CreatedLiveSession:
        body = {
            "model": model,
            "max_duration_s": session_duration_s(input_duration_s),
        }
        try:
            response = self._api.request("POST", "/live/sessions", json_body=body)
        except SessionLimitError as error:
            error.fix = "Wait for one of this project's running live calls to finish, then try again."
            raise
        return _parse(_CREATED_SESSION, response)

    def get(self, session_id: str) -> LiveSession:
        return _parse(_SESSION, self._api.request("GET", f"/live/sessions/{session_id}"))

    def end(self, session_id: str) -> LiveSession:
        return _parse(_SESSION, self._api.request("DELETE", f"/live/sessions/{session_id}"))


def session_duration_s(input_duration_s: float) -> int:
    if not math.isfinite(input_duration_s) or input_duration_s <= 0:
        raise InvalidInputError(
            "The video has no playable duration.",
            code="invalid_input",
            fix="Pass a video file with at least one frame.",
        )
    longest_s = LIVE_LIMITS.max_duration_s - LIVE_LIMITS.duration_margin_s
    if input_duration_s > longest_s:
        raise InvalidInputError(
            f"The video is longer than one live session can run ({longest_s} s).",
            code="video_too_long",
            fix=f"Split the video into parts of at most {longest_s} s and send each part.",
        )
    needed_s = math.ceil(input_duration_s) + LIVE_LIMITS.duration_margin_s
    return max(LIVE_LIMITS.min_duration_s, needed_s)


def gateway_access(session: CreatedLiveSession, now: datetime) -> GatewayAccess | None:
    connection = session.connection
    if connection is None:
        return None
    connect_by = datetime.strptime(connection.token_connect_by, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    if connect_by - timedelta(seconds=LIVE_LIMITS.connect_margin_s) <= now:
        return None
    _check_gateway_urls(connection.offer_url, connection.session_url)
    return GatewayAccess(
        session_id=session.id,
        offer_url=connection.offer_url,
        session_url=connection.session_url,
        connect_by=connect_by,
        token=connection.token,
        ice_servers=tuple(connection.ice_servers),
    )


def _check_gateway_urls(*urls: str) -> None:
    try:
        parsed = [httpx2.URL(url) for url in urls]
    except httpx2.InvalidURL:
        parsed = []
    origins = {(url.scheme, url.host, url.port) for url in parsed}
    safe = bool(parsed) and all(
        url.scheme == "https" or (url.scheme == "http" and url.host in _LOOPBACK_HOSTS) for url in parsed
    )
    if not safe or len(origins) != 1:
        raise ServerError(
            "fenon-api gave gateway URLs the SDK won't send a session token to.",
            code="unsafe_gateway_url",
            fix="Report this to Fenon.",
        )


def _parse(shape: TypeAdapter[ShapeT], response: ApiResponse) -> ShapeT:
    return parse_contract(
        shape,
        response.body,
        what="live session",
        request_id=response.request_id,
        status_code=response.status_code,
    )
