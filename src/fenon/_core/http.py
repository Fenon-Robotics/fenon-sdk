"""HTTP access to the Fenon API and to presigned storage URLs."""

from __future__ import annotations

import enum
import json
import math
import os
import random
import re
import stat
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, BinaryIO, NoReturn, TypeVar, cast

import httpx2
from pydantic import BaseModel, ValidationError

from .._version import __version__
from .errors import (
    AuthError,
    InvalidInputError,
    NetworkError,
    ServerError,
    StorageUrlRejectedError,
    error_code_of,
    error_from_response,
    retry_after_of,
)
from .logs import logger
from .settings import ENV_PREFIX, LIMITS, Settings

_USER_AGENT = f"fenon-python/{__version__}"
_PERMANENT_TRANSPORT_ERRORS = (httpx2.UnsupportedProtocol, httpx2.LocalProtocolError)

ModelT = TypeVar("ModelT", bound=BaseModel)
_REQUEST_ID = re.compile(r"req_[0-9a-z]{12}")
_READ_FLAGS = os.O_RDONLY | os.O_BINARY if sys.platform == "win32" else os.O_RDONLY | os.O_NONBLOCK


@dataclass(frozen=True)
class ClientConfig:
    """Settings for talking to the Fenon API."""

    api_key: str | None = field(repr=False)
    base_url: str
    timeout: float
    max_retries: int

    @classmethod
    def resolve(
        cls,
        *,
        api_key: str | None,
        base_url: str | None,
        timeout: object,
        max_retries: object,
        settings: Settings | None = None,
    ) -> ClientConfig:
        """Build a configuration from explicit arguments, then ``FENON_*`` settings, then defaults."""
        source = Settings() if settings is None else settings
        key = api_key if api_key is not None else source.api_key
        if key and not _is_visible_ascii(key):
            raise _invalid_argument(
                "The API key contains spaces, line breaks or non-ASCII characters.",
                "Copy the key again, without surrounding whitespace.",
            )
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not math.isfinite(timeout):
            raise _invalid_argument("timeout must be a number of seconds.", "Pass a timeout such as 60.")
        if timeout <= 0:
            raise _invalid_argument("timeout must be greater than 0.", "Pass a timeout such as 60.")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise _invalid_argument(
                "max_retries must be an integer of 0 or more.", "Pass max_retries such as 2."
            )
        return cls(
            api_key=key or None,
            base_url=_normalise_base_url(source.base_url if base_url is None else base_url),
            timeout=float(timeout),
            max_retries=max_retries,
        )


def _utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def _new_idempotency_key() -> str:
    """Return a fresh random idempotency key."""
    return str(uuid.uuid4())


@dataclass(frozen=True)
class Runtime:
    """Time, randomness and key generation used by the HTTP clients; tests replace them with fakes."""

    sleep: Callable[[float], None] = time.sleep
    random: Callable[[], float] = random.random
    now: Callable[[], datetime] = _utc_now
    new_idempotency_key: Callable[[], str] = _new_idempotency_key


@dataclass(frozen=True)
class ApiResponse:
    """A successful response from the Fenon API."""

    status_code: int
    body: dict[str, Any] = field(repr=False)
    request_id: str | None
    headers: Mapping[str, str] = field(repr=False)

    def parse(self, model: type[ModelT]) -> ModelT:
        """Validate the body as ``model`` in strict mode, so text is never turned into numbers."""
        try:
            parsed = model.model_validate_json(json.dumps(self.body), strict=True)
        except ValidationError:
            parsed = None
        if parsed is None:
            raise ServerError(
                "The Fenon API sent a response the SDK can't read.",
                code="invalid_response",
                request_id=self.request_id,
                status_code=self.status_code,
            )
        return parsed


class _Failure(enum.Enum):
    """Why an attempt ended without a response; only a ``TRANSIENT`` failure is worth retrying."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"


class ApiClient:
    """Sends authenticated requests to the Fenon API and retries them where the contract allows."""

    def __init__(self, config: ClientConfig, http: httpx2.Client, runtime: Runtime) -> None:
        self._config = config
        self._http = http
        self._runtime = runtime

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str | int] | None = None,
        max_retries: int | None = None,
        timeout_s: float | None = None,
    ) -> ApiResponse:
        """Send one logical request, retrying network errors and retryable statuses."""
        if self._config.api_key is None:
            raise AuthError(
                "No Fenon API key was given.",
                code="missing_api_key",
                fix=f"Pass api_key=... to Fenon() or set {ENV_PREFIX}API_KEY.",
            )
        request = self._build(method, path, json_body, params, timeout_s)
        request.headers["Authorization"] = f"Bearer {self._config.api_key}"
        if method.upper() == "POST":
            request.headers["Idempotency-Key"] = self._runtime.new_idempotency_key()
        attempts = (self._config.max_retries if max_retries is None else max_retries) + 1
        attempt = 0
        while True:
            attempt += 1
            outcome = _attempt(self._http, request)
            if outcome is _Failure.PERMANENT:
                logger.info("%s %s failed without a readable response", method, path)
                raise ServerError(
                    "The Fenon API sent a response the SDK can't read.", code="invalid_response"
                )
            if outcome is _Failure.TRANSIENT:
                if attempt == attempts:
                    raise NetworkError(
                        "Couldn't reach the Fenon API.",
                        code="network_error",
                        fix="Check the network connection and try again.",
                        retryable=True,
                    )
                self._wait(method, path, attempt, "a network error", self._backoff(attempt - 1))
                continue
            status = outcome.status_code
            request_id = outcome.headers.get("x-request-id")
            payload = decode_json(outcome.content)
            if 200 <= status < 300:
                logger.info("%s %s %s request=%s", method, path, status, _loggable(request_id))
                if not isinstance(payload, dict):
                    raise ServerError(
                        "The Fenon API sent a response the SDK can't read.",
                        code="invalid_response",
                        request_id=request_id,
                        status_code=status,
                    )
                body = cast("dict[str, Any]", payload)
                return ApiResponse(status, body, request_id, _lowercase_headers(outcome.headers))
            retryable = _is_retryable(status, error_code_of(payload))
            hint = _retry_hint_s(outcome.headers, payload, self._runtime.now())
            hint_too_long = hint is not None and hint > LIMITS.max_retry_hint_s
            if not retryable or attempt == attempts or hint_too_long:
                logger.info("%s %s %s request=%s", method, path, status, _loggable(request_id))
                raise error_from_response(
                    status, payload, request_id=request_id, retryable=retryable, retry_after_s=hint
                )
            wait_s = hint if hint is not None else self._backoff(attempt - 1)
            self._wait(method, path, attempt, f"HTTP {status}", wait_s)

    def close(self) -> None:
        """Release the connections."""
        self._http.close()

    def _build(
        self,
        method: str,
        path: str,
        json_body: Mapping[str, Any] | None,
        params: Mapping[str, str | int] | None,
        timeout_s: float | None,
    ) -> httpx2.Request:
        """Build the request for ``path``, refusing any URL outside the API's scheme, host and port."""
        timeout = httpx2.USE_CLIENT_DEFAULT if timeout_s is None else httpx2.Timeout(timeout_s)
        try:
            request = self._http.build_request(method, path, json=json_body, params=params, timeout=timeout)
        except httpx2.InvalidURL:
            request = None
        if request is None or not _same_origin(request.url, self._http.base_url):
            raise InvalidInputError(
                "The request path must stay on the Fenon API's base URL.", code="invalid_argument"
            )
        return request

    def _backoff(self, retry_index: int) -> float:
        """Return the jittered wait before retry ``retry_index`` (counted from 0)."""
        return self._runtime.random() * min(LIMITS.max_backoff_s, LIMITS.initial_backoff_s * 2**retry_index)

    def _wait(self, method: str, path: str, attempt: int, reason: str, wait_s: float) -> None:
        """Log a retry and sleep before it."""
        logger.debug(
            "%s %s attempt %d failed with %s; retrying in %.2f s", method, path, attempt, reason, wait_s
        )
        self._runtime.sleep(wait_s)


class StorageClient:
    """Moves bytes to and from presigned storage URLs without sending any Fenon credentials."""

    def __init__(self, http: httpx2.Client, runtime: Runtime) -> None:
        self._http = http
        self._runtime = runtime

    def upload_file(
        self, url: str, *, method: str, headers: Mapping[str, str], path: Path, size_bytes: int
    ) -> None:
        """Send a file to a presigned upload URL, reopening it for every attempt."""
        _check_storage_target(method, url, headers)
        request_headers = dict(headers)

        def send() -> httpx2.Response | _Failure:
            with _open_for_upload(path, size_bytes) as stream:
                request = self._http.build_request(method, url, content=stream, headers=request_headers)
                return _attempt(self._http, request)

        self._transfer(method, send)

    def download(self, url: str) -> bytes:
        """Fetch the bytes behind a presigned download URL."""
        _check_storage_target("GET", url, {})
        request = self._http.build_request("GET", url)
        return self._transfer("GET", lambda: _attempt(self._http, request)).content

    def close(self) -> None:
        """Release the connections."""
        self._http.close()

    def _transfer(self, method: str, send: Callable[[], httpx2.Response | _Failure]) -> httpx2.Response:
        """Run ``send`` with the storage retry schedule and return the successful response."""
        waits = iter(LIMITS.storage_waits_s)
        attempt = 0
        while True:
            attempt += 1
            outcome = send()
            if outcome is _Failure.PERMANENT:
                logger.info("%s storage failed without a readable response", method)
                raise ServerError("Storage couldn't complete the transfer.", code="storage_error")
            if outcome is not _Failure.TRANSIENT and outcome.is_success:
                logger.info("%s storage %s", method, outcome.status_code)
                return outcome
            status = None if outcome is _Failure.TRANSIENT else outcome.status_code
            if status == 403:
                logger.info("%s storage %s", method, status)
                raise StorageUrlRejectedError(
                    "Storage refused the presigned URL.", code="storage_url_rejected", status_code=status
                )
            retryable = status is None or status in LIMITS.storage_retry_statuses
            wait_s = next(waits, None)
            if retryable and wait_s is not None:
                reason = "a network error" if status is None else f"HTTP {status}"
                logger.debug(
                    "%s storage attempt %d failed with %s; retrying in %.2f s",
                    method,
                    attempt,
                    reason,
                    wait_s,
                )
                self._runtime.sleep(wait_s)
                continue
            if status is None:
                raise NetworkError(
                    "Couldn't reach storage.",
                    code="network_error",
                    fix="Check the network connection and try again.",
                    retryable=True,
                )
            logger.info("%s storage %s", method, status)
            raise ServerError(
                f"Storage answered with HTTP {status}.",
                code="storage_error",
                status_code=status,
                retryable=retryable,
            )


@dataclass(frozen=True)
class HttpClients:
    """The API client and the storage client, closed together."""

    api: ApiClient
    storage: StorageClient
    runtime: Runtime

    def close(self) -> None:
        """Release the connections of both clients."""
        self.api.close()
        self.storage.close()


def open_http_clients(
    config: ClientConfig,
    *,
    runtime: Runtime | None = None,
    api_transport: httpx2.BaseTransport | None = None,
    storage_transport: httpx2.BaseTransport | None = None,
) -> HttpClients:
    """Build the API and storage clients for a configuration."""
    effective_runtime = runtime or Runtime()
    api_http = httpx2.Client(
        base_url=config.base_url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        timeout=httpx2.Timeout(config.timeout, connect=LIMITS.connect_timeout_s),
        follow_redirects=False,
        transport=api_transport,
    )
    storage_http = httpx2.Client(
        timeout=httpx2.Timeout(LIMITS.storage_timeout_s, connect=LIMITS.connect_timeout_s),
        follow_redirects=False,
        transport=storage_transport,
    )
    return HttpClients(
        api=ApiClient(config, api_http, effective_runtime),
        storage=StorageClient(storage_http, effective_runtime),
        runtime=effective_runtime,
    )


def measure_upload_file(path: Path) -> int:
    """Return the size in bytes of a local file that can be uploaded."""
    with _open_regular_file(path) as stream:
        return os.fstat(stream.fileno()).st_size


def _attempt(http: httpx2.Client, request: httpx2.Request) -> httpx2.Response | _Failure:
    """Send one attempt, returning the response or the kind of failure."""
    try:
        return http.send(request)
    except _PERMANENT_TRANSPORT_ERRORS:
        return _Failure.PERMANENT
    except httpx2.TransportError:
        return _Failure.TRANSIENT
    except httpx2.RequestError:
        return _Failure.PERMANENT


def _check_storage_target(method: str, url: str, headers: Mapping[str, str]) -> None:
    """Raise unless a presigned URL and its headers form a request the SDK can send."""
    try:
        target = httpx2.Request(method, url, headers=headers).url
    except (httpx2.InvalidURL, UnicodeEncodeError):
        target = None
    if target is None or target.scheme not in ("http", "https") or not target.host:
        raise ServerError("The Fenon API sent a storage URL the SDK can't use.", code="invalid_response")


def _same_origin(url: httpx2.URL, base: httpx2.URL) -> bool:
    """Return whether two URLs share scheme, host and port."""
    return (url.scheme, url.host, url.port) == (base.scheme, base.host, base.port)


def _is_visible_ascii(text: str) -> bool:
    """Return whether every character is visible ASCII: no spaces, control or non-ASCII characters."""
    return all("!" <= character <= "~" for character in text)


def _invalid_argument(message: str, fix: str) -> InvalidInputError:
    """Return the error for an invalid configuration argument."""
    return InvalidInputError(message, code="invalid_argument", fix=fix)


def _normalise_base_url(base_url: str) -> str:
    """Return ``base_url`` without a trailing slash, after checking it is an absolute HTTP(S) URL."""
    trimmed = base_url.rstrip("/")
    try:
        parsed = httpx2.URL(trimmed)
    except httpx2.InvalidURL:
        parsed = None
    plain = parsed is not None and not (parsed.userinfo or parsed.query or parsed.fragment)
    if parsed is None or not plain or parsed.scheme not in ("http", "https") or not parsed.host:
        raise _invalid_argument(
            "base_url must be an absolute http(s) URL without credentials, a query or a fragment.",
            "Pass a URL such as https://api.fenon.ai/v1.",
        )
    return trimmed


def _open_for_upload(path: Path, size_bytes: int) -> BinaryIO:
    """Open ``path`` for an upload attempt after checking it still has the reserved size."""
    stream = _open_regular_file(path)
    if os.fstat(stream.fileno()).st_size != size_bytes:
        stream.close()
        raise InvalidInputError(
            "The file changed after its upload was reserved.",
            code="file_changed",
            fix="Leave the file unchanged while it uploads, then try again.",
        )
    return stream


def _open_regular_file(path: Path) -> BinaryIO:
    """Open a local regular file without blocking, checking the opened descriptor is still a regular file."""
    problem = _regular_file_problem(path)
    if problem is None:
        try:
            descriptor = os.open(path, _READ_FLAGS)
        except FileNotFoundError:
            problem = "file_not_found"
        except OSError:
            problem = "file_unreadable"
        else:
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                return os.fdopen(descriptor, "rb")
            os.close(descriptor)
            problem = "file_not_found"
    if problem == "file_not_found":
        raise InvalidInputError(
            "The file doesn't exist or isn't a regular file.",
            code="file_not_found",
            fix="Check the path and try again.",
        )
    raise InvalidInputError(
        "The file can't be read.", code="file_unreadable", fix="Check the file's permissions."
    )


def _regular_file_problem(path: Path) -> str | None:
    """Return ``file_not_found`` or ``file_unreadable`` when ``path`` can't be read as a regular file."""
    try:
        mode = path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return "file_not_found"
    except OSError:
        return "file_unreadable"
    return None if stat.S_ISREG(mode) else "file_not_found"


def decode_json(content: bytes) -> object:
    """Return decoded JSON, or ``None`` when ``content`` is empty or not strict JSON with finite numbers."""
    if not content:
        return None
    try:
        return json.loads(content, parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, RecursionError):
        return None


def _reject_constant(name: str) -> NoReturn:
    """Refuse the non-standard JSON constants ``NaN``, ``Infinity`` and ``-Infinity``."""
    raise ValueError(name)


def _finite_float(text: str) -> float:
    """Parse a JSON number with a fraction or exponent, refusing one too large to be finite."""
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(text)
    return number


def _loggable(request_id: str | None) -> str:
    """Return a request ID for a log record only when it has the contract's ``req_`` form."""
    return request_id if request_id is not None and _REQUEST_ID.fullmatch(request_id) else "unrecognised"


def _lowercase_headers(headers: httpx2.Headers) -> dict[str, str]:
    """Return the headers as a plain dict with lower-cased names."""
    return {name.lower(): value for name, value in headers.items()}


def _is_retryable(status: int, code: str | None) -> bool:
    """Return whether the contract allows retrying a response with this status and error code."""
    if status == 409:
        return code == "idempotency_in_progress"
    if status in (408, 429):
        return True
    return 500 <= status < 600 and status != 501


def _retry_hint_s(headers: httpx2.Headers, payload: object, now: datetime) -> float | None:
    """Return the server's retry hint in seconds: ``retry-after-ms``, then ``Retry-After``, then the body."""
    milliseconds = _non_negative_float(headers.get("retry-after-ms"))
    if milliseconds is not None:
        return milliseconds / 1000
    retry_after = headers.get("retry-after")
    if retry_after is not None:
        seconds = _non_negative_float(retry_after)
        if seconds is not None:
            return seconds
        when = _http_date(retry_after)
        if when is not None:
            return max(0.0, (when - now).total_seconds())
    return retry_after_of(payload)


def _non_negative_float(value: str | None) -> float | None:
    """Parse a non-negative finite number, or return ``None``."""
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _http_date(value: str) -> datetime | None:
    """Parse an HTTP date as an aware UTC datetime, or return ``None``."""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
