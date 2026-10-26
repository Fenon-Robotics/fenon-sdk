"""Errors raised by the Fenon SDK, and how Fenon API error responses map onto them."""

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
    """The API key is missing, invalid, or not allowed to do this."""


class CreditsError(FenonError):
    """The workspace is out of credits."""


class InvalidInputError(FenonError, ValueError):
    """The request or its input was rejected, by the SDK before sending or by the API."""


class NotFoundError(FenonError):
    """The model, upload or run doesn't exist, or isn't visible to this API key."""


class RateLimitError(FenonError):
    """The API is still rate-limiting this key after the SDK's retries."""


class ServerError(FenonError):
    """The API or storage failed, isn't available yet, or answered in a way the SDK can't use."""


class NetworkError(FenonError, ConnectionError):
    """The SDK couldn't reach the API or storage, or the connection timed out, after its retries."""


class RunFailedError(FenonError):
    def __init__(
        self,
        message: str,
        *,
        run_id: str,
        stage: str | None = None,
        frame: int | None = None,
        timestamp_s: float | None = None,
        code: str | None = None,
        fix: str | None = None,
        details: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, fix=fix, details=details, request_id=request_id)
        self.run_id = run_id
        self.stage = stage
        self.frame = frame
        self.timestamp_s = timestamp_s


class UnsupportedModelError(FenonError):
    """The model uses a contract this version of the SDK doesn't support."""


class StorageUrlRejectedError(ServerError):
    """Storage refused a presigned URL (expired or forbidden); the caller may request a fresh one."""


def error_class_for_status(status_code: int) -> type[FenonError]:
    """Return the error class the contract assigns to an HTTP status."""
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
    retryable: bool,
    retry_after_s: float | None,
) -> FenonError:
    envelope = _envelope(payload)
    message = _text(envelope.get("message")) or f"The Fenon API answered with HTTP {status_code}."
    body_request_id = _text(envelope.get("request_id"))
    body_retryable = envelope.get("retryable")
    details = envelope.get("details")
    detail_map = cast("dict[str, Any]", details) if isinstance(details, dict) else None
    return error_class_for_status(status_code)(
        message,
        code=_text(envelope.get("code")),
        fix=_text(envelope.get("fix")),
        details=detail_map,
        request_id=request_id or body_request_id,
        status_code=status_code,
        retryable=body_retryable if isinstance(body_retryable, bool) else retryable,
        retry_after_s=retry_after_s,
    )


def error_code_of(payload: object) -> str | None:
    """Return the ``error.code`` of a decoded error body, or ``None`` when it has none."""
    return _text(_envelope(payload).get("code"))


def retry_after_of(payload: object) -> float | None:
    """Return the ``error.retry_after_s`` of a decoded error body when it is a finite number of 0 or more."""
    value = _envelope(payload).get("retry_after_s")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    seconds = float(value)
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _envelope(payload: object) -> dict[str, Any]:
    """Return the ``error`` object of a decoded body, or an empty dict."""
    if isinstance(payload, dict):
        error = cast("dict[str, Any]", payload).get("error")
        if isinstance(error, dict):
            return cast("dict[str, Any]", error)
    return {}


def _text(value: object) -> str | None:
    """Return ``value`` if it is a non-empty string, else ``None``."""
    return value if isinstance(value, str) and value else None
