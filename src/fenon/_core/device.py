"""CLI device login, origin-bound credentials and in-memory API access tokens."""

from __future__ import annotations

import json
import os
import platform
import re
import socket
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Lock
from typing import NoReturn, TypeVar

import httpx2
from pydantic import BaseModel, Field, SecretStr, ValidationError

from .._version import __version__
from .config import DEVICE_TOKEN_LIFETIME_S, DEVICE_TOKEN_MARGIN_S, LIMITS
from .errors import AuthError, InvalidInputError, NetworkError, ServerError

if os.name == "nt":
    import msvcrt
else:
    import fcntl

PROJECT_ID = re.compile(r"proj_[0123456789abcdefghjkmnpqrstvwxyz]{12}")
_TOKEN = re.compile(r"[!-~]+")
_Model = TypeVar("_Model", bound=BaseModel)
_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


class _StoredSession(BaseModel):
    token: SecretStr
    console: str


class _Stored(_StoredSession):
    api_base: str
    project: str
    retired: _StoredSession | None = None


@dataclass(frozen=True)
class DeviceSession:
    token: str = field(repr=False)
    console: str


@dataclass(frozen=True)
class DeviceCredentials(DeviceSession):
    api_base: str
    project: str
    retired: DeviceSession | None = field(default=None, repr=False)


def private_url(value: str, *, origin_only: bool = False) -> str:
    try:
        url = httpx2.URL(value.rstrip("/"))
    except (ValueError, httpx2.InvalidURL):
        url = None
    if (
        url is None
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
        or (origin_only and url.path != "/")
        or (
            url.scheme != "https"
            and not (url.scheme == "http" and url.host in {"localhost", "127.0.0.1", "::1"})
        )
    ):
        raise AuthError(
            "Use an HTTPS Fenon URL or a loopback URL for local development.", code="invalid_origin"
        )
    return str(url).rstrip("/")


def credential_path() -> Path:
    return Path.home() / ".config" / "fenon" / "credentials"


def _check_config_directory(path: Path) -> None:
    if os.name != "posix":
        return
    for directory in (path.parent.parent, path.parent):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise AuthError("Fenon needs a user-owned configuration directory.", code="unsafe_credentials")


def _check_private_file(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or (
        os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077)
    ):
        raise AuthError("Fenon credentials need private file permissions.", code="unsafe_credentials")


def _stored_session(data: _StoredSession) -> DeviceSession:
    token = data.token.get_secret_value()
    if not _TOKEN.fullmatch(token):
        raise ValueError
    return DeviceSession(token, private_url(data.console, origin_only=True))


def read_credentials(path: Path) -> DeviceCredentials | None:
    try:
        _check_config_directory(path)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        with os.fdopen(os.open(path, flags), "rb") as stream:
            _check_private_file(stream.fileno())
            data = _Stored.model_validate_json(stream.read(8192))
        current = _stored_session(data)
        if not PROJECT_ID.fullmatch(data.project):
            raise ValueError
        retired = None if data.retired is None else _stored_session(data.retired)
        return DeviceCredentials(
            current.token, current.console, private_url(data.api_base), data.project, retired
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise AuthError(
            "Couldn't read Fenon credentials. Remove the credentials file and run `fenon login` again.",
            code="invalid_credentials",
        ) from None


def save_credentials(path: Path, credentials: DeviceCredentials) -> None:
    scratch = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_config_directory(path)
        retired = credentials.retired
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as stream:
            scratch = Path(stream.name)
            json.dump(
                {
                    "token": credentials.token,
                    "console": credentials.console,
                    "api_base": credentials.api_base,
                    "project": credentials.project,
                    "retired": {"token": retired.token, "console": retired.console} if retired else None,
                },
                stream,
            )
        os.replace(scratch, path)
    except OSError:
        raise AuthError("Couldn't save Fenon credentials.", code="credentials_write_failed") from None
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)


@contextmanager
def edit_credentials(path: Path) -> Generator[None]:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_config_directory(path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path.parent / "credentials.lock", flags, 0o600), "r+b") as lock:
            _check_private_file(lock.fileno())
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
    except OSError:
        raise AuthError(
            "Couldn't update Fenon credentials. Try again.", code="credentials_write_failed"
        ) from None


def device_access(base_url: str, project: str | None) -> DeviceAccess | None:
    credentials = read_credentials(credential_path())
    if credentials is None:
        return None
    if private_url(base_url) != credentials.api_base:
        raise AuthError(
            "The saved Fenon login is for a different API URL.", code="credentials_origin_mismatch"
        )
    if project is not None and not PROJECT_ID.fullmatch(project):
        raise InvalidInputError("Pass a public project ID starting with proj_.", code="invalid_argument")
    return DeviceAccess(credentials, project=project)


def _agent() -> str:
    def safe(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.:+-]", "-", value)[:100]

    device = f"{safe(sys.platform)}; {safe(platform.machine())}; host={safe(socket.gethostname())}"
    return f"fenon/{__version__} ({device})"


class _Grant(BaseModel):
    device_code: SecretStr
    user_code: str = Field(pattern=r"^[A-Z0-9]{8}$")
    verification_uri_complete: str
    expires_in: int = Field(gt=0)
    interval: int = Field(gt=0)


class _SessionToken(BaseModel):
    access_token: SecretStr


class _AccessToken(BaseModel):
    token: SecretStr


class _GrantError(BaseModel):
    error: str


class _Project(BaseModel):
    id: str = Field(pattern=PROJECT_ID.pattern)
    name: str


class _Workspace(BaseModel):
    name: str
    projects: list[_Project]


class _Account(BaseModel):
    email: str


class _Catalog(BaseModel):
    user: _Account
    workspaces: list[_Workspace]


class ConsoleSession:
    def __init__(self, origin: str, *, transport: httpx2.BaseTransport | None = None) -> None:
        self.origin = private_url(origin, origin_only=True)
        self._transport = transport

    def _send(
        self, method: str, path: str, *, token: str | None = None, payload: Mapping[str, str] | None = None
    ) -> httpx2.Response:
        headers = {"User-Agent": _agent(), "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx2.Client(
                base_url=self.origin,
                follow_redirects=False,
                timeout=LIMITS.connect_timeout_s,
                transport=self._transport,
            ) as client:
                return client.request(method, path, headers=headers, json=payload)
        except (httpx2.RequestError, httpx2.InvalidURL):
            raise NetworkError(
                "Couldn't reach the Fenon console.", code="console_unreachable", retryable=True
            ) from None

    def begin(self) -> _Grant:
        response = self._send("POST", "/api/auth/device/code", payload={"client_id": "fenon-cli"})
        grant = _response(response, _Grant)
        if grant.verification_uri_complete != f"{self.origin}/device?user_code={grant.user_code}":
            raise ServerError(
                "The console returned an unexpected verification link.", code="invalid_response"
            )
        return grant

    def redeem(
        self,
        grant: _Grant,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> str:
        ends = monotonic() + grant.expires_in
        interval = grant.interval
        while monotonic() + interval < ends:
            sleep(interval)
            response = self._send(
                "POST",
                "/api/auth/device/token",
                payload={
                    "client_id": "fenon-cli",
                    "grant_type": _GRANT_TYPE,
                    "device_code": grant.device_code.get_secret_value(),
                },
            )
            if response.is_success:
                return _credential(_response(response, _SessionToken).access_token)
            if response.status_code != 400:
                _response(response, _SessionToken)
            error = _parse(response, _GrantError).error
            if error == "slow_down":
                interval += 5
            elif error != "authorization_pending":
                message = (
                    "The device was not approved."
                    if error == "access_denied"
                    else "The device request ended. Try again."
                )
                raise AuthError(message, code="device_authorization_failed")
        raise AuthError("The device code expired. Run `fenon login` again.", code="device_code_expired")

    def projects(self, token: str) -> _Catalog:
        return _response(self._send("GET", "/api/cli/projects", token=token), _Catalog)

    def mint(self, token: str) -> str:
        response = self._send("GET", "/api/auth/token", token=token)
        return _credential(_response(response, _AccessToken).token)

    def revoke(self, token: str) -> None:
        response = self._send("POST", "/api/auth/sign-out", token=token, payload={})
        if not response.is_success and response.status_code != 401:
            _reject(response)


def _credential(secret: SecretStr) -> str:
    token = secret.get_secret_value()
    if not _TOKEN.fullmatch(token):
        raise ServerError("The console sent an unreadable credential.", code="invalid_response")
    return token


def _reject(response: httpx2.Response) -> NoReturn:
    if response.status_code in (401, 403):
        raise AuthError(
            "Fenon sign-in is no longer valid. Run `fenon login` again.", code="invalid_credentials"
        )
    raise ServerError(
        "The Fenon console couldn't complete this request.",
        code="console_error",
        status_code=response.status_code,
    )


def _parse(response: httpx2.Response, model: type[_Model]) -> _Model:
    try:
        return model.model_validate_json(response.content, strict=True)
    except ValidationError:
        raise ServerError("The Fenon console sent an unreadable response.", code="invalid_response") from None


def _response(response: httpx2.Response, model: type[_Model]) -> _Model:
    if not response.is_success:
        _reject(response)
    return _parse(response, model)


class DeviceAccess:
    def __init__(
        self,
        credentials: DeviceCredentials,
        *,
        project: str | None,
        console: ConsoleSession | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credentials = credentials
        self.project = project or credentials.project
        self._console = console or ConsoleSession(credentials.console)
        self._monotonic = monotonic
        self._lock = Lock()
        self._access: str | None = None
        self._renew_at = 0.0

    def token(self) -> str:
        with self._lock:
            started = self._monotonic()
            if self._access is None or started >= self._renew_at:
                self._access = self._console.mint(self._credentials.token)
                self._renew_at = started + DEVICE_TOKEN_LIFETIME_S - DEVICE_TOKEN_MARGIN_S
            return self._access


def finish_replacement(path: Path, credentials: DeviceCredentials) -> DeviceCredentials:
    retired = credentials.retired
    if retired is not None:
        ConsoleSession(retired.console).revoke(retired.token)
        credentials = replace(credentials, retired=None)
        save_credentials(path, credentials)
    return credentials


def changed_project(credentials: DeviceCredentials, project: str) -> DeviceCredentials:
    return replace(credentials, project=project)
