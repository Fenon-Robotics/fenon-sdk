"""Errors raised by the Fenon SDK, and how Fenon API error responses map onto them (docs/features/errors.md).

``error_from_response`` picks the class from the envelope's ``code`` first (``active_session_limit``), then
from the status. The body's boolean ``retryable`` wins; the caller's status rule applies only when the body
has none.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from .runs import Job


class FenonError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        fix: str | None = None,
        details: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.fix = fix
        self.details: dict[str, Any] = dict(details) if details else {}
        self.request_id = request_id
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after_s = retry_after_s
        self.job: Job[Any] | None = None

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, status_code={self.status_code!r}, "
            f"request_id={self.request_id!r})"
        )


class AuthError(FenonError):
    pass


class CreditsError(FenonError):
    pass


class InvalidInputError(FenonError, ValueError):
    pass


class NotFoundError(FenonError):
    pass


class RateLimitError(FenonError):
    pass


class ServerError(FenonError):
    pass


class NetworkError(FenonError, ConnectionError):
    pass


class RunFailedError(FenonError):
    def __init__(
        self,
        message: str,
        *,
        run_id: str,
        stage: str | None = None,
        code: str | None = None,
        fix: str | None = None,
        details: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        retryable: bool = False,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            fix=fix,
            details=details,
            request_id=request_id,
            retryable=retryable,
            retry_after_s=retry_after_s,
        )
        self.run_id = run_id
        self.stage = stage


class UnsupportedModelError(FenonError):
    pass


class StorageUrlRejectedError(ServerError):
    pass


class SessionLimitError(FenonError):
    pass


class LiveSessionError(FenonError):
    result: object = None


_ERROR_CLASS_FOR_CODE: dict[str, type[FenonError]] = {"active_session_limit": SessionLimitError}


def error_class_for_status(status_code: int) -> type[FenonError]:
    if status_code in (400, 413, 415, 422):
        return InvalidInputError
    if status_code in (401, 403):
        return AuthError
    if status_code == 402:
        return CreditsError
    if status_code == 404:
        return NotFoundError
    if status_code == 408:
        return NetworkError
    if status_code == 429:
        return RateLimitError
    if 400 <= status_code < 500 and status_code != 409:
        return FenonError
    return ServerError


def error_from_response(
    status_code: int,
    payload: object,
    *,
    request_id: str | None,
    retry_after_s: float | None,
    retryable_without_error_body: bool,
) -> FenonError:
    envelope = _envelope(payload)
    code = _text(envelope.get("code"))
    error_class = _ERROR_CLASS_FOR_CODE.get(code or "") or error_class_for_status(status_code)
    message = _text(envelope.get("message")) or f"The Fenon API answered with HTTP {status_code}."
    body_request_id = _text(envelope.get("request_id"))
    body_retryable = envelope.get("retryable")
    details = envelope.get("details")
    detail_map = cast("dict[str, Any]", details) if isinstance(details, dict) else None
    return error_class(
        message,
        code=code,
        fix=_text(envelope.get("fix")),
        details=detail_map,
        request_id=request_id or body_request_id,
        status_code=status_code,
        retryable=body_retryable if isinstance(body_retryable, bool) else retryable_without_error_body,
        retry_after_s=retry_after_s,
    )


def retry_after_of(payload: object) -> float | None:
    value = _envelope(payload).get("retry_after_s")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    seconds = float(value)
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _envelope(payload: object) -> dict[str, Any]:
    if isinstance(payload, dict):
        error = cast("dict[str, Any]", payload).get("error")
        if isinstance(error, dict):
            return cast("dict[str, Any]", error)
    return {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
