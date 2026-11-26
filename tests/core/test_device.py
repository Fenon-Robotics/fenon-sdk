"""Device grant wire format, credential storage and API header isolation."""

import os
from pathlib import Path

import httpx2
import pytest

from fenon._core.device import (
    ConsoleSession,
    DeviceAccess,
    DeviceCredentials,
    device_access,
    private_url,
    read_credentials,
    save_credentials,
)
from fenon._core.errors import AuthError, ServerError
from fenon._core.http import ClientConfig, open_http_clients

CONSOLE = "https://console.fenon.ai"
API = "https://api.fenon.ai/v1"
PROJECT = "proj_0123456789ab"


def credentials() -> DeviceCredentials:
    return DeviceCredentials("device-secret", CONSOLE, API, PROJECT)


def test_credentials_are_private_and_bound_to_the_api_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "credentials"
    monkeypatch.setattr("fenon._core.device.credential_path", lambda: path)
    assert read_credentials(path) is None
    save_credentials(path, credentials())
    assert read_credentials(path) == credentials()
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        path.chmod(0o644)
        with pytest.raises(AuthError, match="private file permissions"):
            read_credentials(path)
        path.chmod(0o600)
    assert device_access(API, None) is not None
    with pytest.raises(AuthError, match="different API URL"):
        device_access("https://preview.example/v1", None)


@pytest.mark.parametrize(
    "url", ["http://example.com", "https://user@example.com", "https://example.com#token"]
)
def test_credentials_require_a_safe_url(url: str):
    with pytest.raises(AuthError):
        private_url(url)


def test_grant_uses_json_and_obeys_pending_and_slow_down():
    polling = iter(
        [
            (400, {"error": "authorization_pending"}),
            (400, {"error": "slow_down"}),
            (200, {"access_token": "device-secret"}),
        ]
    )

    def handler(request: httpx2.Request):
        assert request.headers["user-agent"].startswith("fenon/")
        assert "authorization" not in request.headers
        if request.url.path == "/api/auth/device/code":
            return httpx2.Response(
                200,
                json={
                    "device_code": "private-device-code",
                    "user_code": "ABCDEFGH",
                    "verification_uri_complete": f"{CONSOLE}/device?user_code=ABCDEFGH",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        assert request.url.path == "/api/auth/device/token"
        assert request.headers["content-type"] == "application/json"
        status, data = next(polling)
        return httpx2.Response(status, json=data)

    console = ConsoleSession(CONSOLE, transport=httpx2.MockTransport(handler))
    sleeps: list[float] = []
    grant = console.begin()
    assert console.redeem(grant, sleep=sleeps.append, monotonic=lambda: sum(sleeps)) == "device-secret"
    assert sleeps == [5, 5, 10]


def test_token_endpoint_never_follows_a_redirect():
    def handler(request: httpx2.Request):
        assert request.url.host == "console.fenon.ai"
        return httpx2.Response(307, headers={"Location": "https://other.example/"})

    with pytest.raises(ServerError, match="couldn't complete"):
        ConsoleSession(CONSOLE, transport=httpx2.MockTransport(handler)).mint("device-secret")


def test_sdk_caches_access_tokens_and_key_auth_still_wins():
    calls: list[tuple[str, str | None]] = []
    exchanges: list[str] = []
    clock = [0.0]

    def mint(request: httpx2.Request):
        assert request.url.path == "/api/auth/token"
        exchanges.append(request.headers["authorization"])
        return httpx2.Response(200, json={"token": f"temporary-{len(exchanges)}"})

    def run(request: httpx2.Request):
        calls.append((request.headers["authorization"], request.headers.get("fenon-project")))
        return httpx2.Response(200, json={})

    console = ConsoleSession(CONSOLE, transport=httpx2.MockTransport(mint))
    override = "proj_0123456789ac"
    session = DeviceAccess(credentials(), project=override, console=console, monotonic=lambda: clock[0])
    for api_key in [None, "fn_sk_explicit"]:
        clients = open_http_clients(
            ClientConfig(api_key, API, 5, 0), device=session, api_transport=httpx2.MockTransport(run)
        )
        try:
            clients.api.request("GET", "/runs")
            clients.api.request("GET", "/runs")
            if api_key is None:
                clock[0] = 900.0
                clients.api.request("GET", "/runs")
        finally:
            clients.close()
    assert exchanges == ["Bearer device-secret"] * 2
    assert calls == [
        ("Bearer temporary-1", override),
        ("Bearer temporary-1", override),
        ("Bearer temporary-2", override),
        ("Bearer fn_sk_explicit", None),
        ("Bearer fn_sk_explicit", None),
    ]


def test_group_writable_config_directory_is_refused(tmp_path: Path):
    if os.name != "posix":
        pytest.skip("POSIX mode bits")
    folder = tmp_path / "fenon"
    folder.mkdir(mode=0o700)
    path = folder / "credentials"
    save_credentials(path, credentials())
    folder.chmod(0o770)
    with pytest.raises(AuthError, match="user-owned configuration directory"):
        read_credentials(path)
    with pytest.raises(AuthError, match="user-owned configuration directory"):
        save_credentials(path, credentials())


@pytest.mark.parametrize("saved", [False, True])
def test_interrupted_login_preserves_the_usable_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved: bool
):
    from argparse import Namespace
    from types import SimpleNamespace

    from fenon import __main__ as cli
    from fenon._core import device

    path = tmp_path / "credentials"
    save_credentials(path, credentials())
    revoked: list[str] = []
    console = SimpleNamespace(
        origin=CONSOLE,
        begin=lambda: SimpleNamespace(
            user_code="ABCDEFGH",
            verification_uri_complete=CONSOLE + "/device?user_code=ABCDEFGH",
        ),
        redeem=lambda grant: "new-secret",
        revoke=revoked.append,
    )

    def interrupt(path: Path, value: DeviceCredentials):
        if saved:
            save_credentials(path, value)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "credential_path", lambda: path)
    monkeypatch.setattr(cli, "_choose", lambda *args: (PROJECT, "developer@example.com"))
    monkeypatch.setattr(cli, "ConsoleSession", lambda *args: console)
    monkeypatch.setattr(device, "ConsoleSession", lambda *args: console)
    monkeypatch.setattr(cli, "save_credentials", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cli._login(Namespace(project=None, no_browser=True))
    current = read_credentials(path)
    assert current is not None
    if saved:
        assert current.token == "new-secret"
        assert current.retired is not None
        assert current.retired.token == "device-secret"
        assert revoked == []
        assert device.finish_replacement(path, current).retired is None
        assert revoked == ["device-secret"]
    else:
        assert current == credentials()
        assert revoked == ["new-secret"]


def test_logout_revokes_active_even_if_retired_console_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from fenon import __main__ as cli
    from fenon._core import device

    path = tmp_path / "credentials"
    previous = device.DeviceSession("previous-secret", "https://preview.example")
    active = DeviceCredentials("active-secret", CONSOLE, API, PROJECT, previous)
    save_credentials(path, active)
    calls: list[str] = []
    fail = True

    class Console:
        def __init__(self, _: str):
            pass

        def revoke(self, token: str):
            calls.append(token)
            if fail and token == previous.token:
                raise AuthError("Couldn't reach the previous console.")

    monkeypatch.setattr(cli, "credential_path", lambda: path)
    monkeypatch.setattr(cli, "ConsoleSession", Console)
    monkeypatch.setattr(device, "ConsoleSession", Console)
    with pytest.raises(AuthError, match="This device signed out"):
        cli._logout()
    assert calls == [active.token, previous.token]
    assert read_credentials(path) == active
    fail = False
    cli._logout()
    assert calls == [active.token, previous.token] * 2
    assert read_credentials(path) is None
