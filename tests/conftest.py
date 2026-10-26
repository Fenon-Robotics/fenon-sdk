"""Shared fixtures: a fake runtime and HTTP clients backed by ``httpx2.MockTransport``."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import httpx2
import pytest
from _support import ISOLATED, TEST_API_KEY, FakeRuntime, Handler, refuse

from fenon import Fenon
from fenon._core.http import ClientConfig, HttpClients, open_http_clients


@pytest.fixture(autouse=True)
def no_fenon_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep FENON_* variables from the developer's shell out of every test."""
    for name in list(os.environ):
        if name.upper().startswith("FENON_"):
            monkeypatch.delenv(name)


@pytest.fixture
def fake_runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.fixture
def make_clients(fake_runtime: FakeRuntime) -> Iterator[Callable[..., HttpClients]]:
    """Build HTTP clients on mock transports; every client built is closed after the test."""
    built: list[HttpClients] = []

    def factory(
        api: Handler | None = None,
        storage: Handler | None = None,
        *,
        api_key: str | None = TEST_API_KEY,
        base_url: str | None = None,
        max_retries: int = 2,
    ) -> HttpClients:
        config = ClientConfig.resolve(
            api_key=api_key, base_url=base_url, timeout=60, max_retries=max_retries, settings=ISOLATED
        )
        clients = open_http_clients(
            config,
            runtime=fake_runtime.as_runtime(),
            api_transport=httpx2.MockTransport(api or refuse),
            storage_transport=httpx2.MockTransport(storage or refuse),
        )
        built.append(clients)
        return clients

    yield factory
    for clients in built:
        clients.close()


@pytest.fixture
def make_fenon(fake_runtime: FakeRuntime) -> Iterator[Callable[..., Fenon]]:
    """Build ``Fenon`` clients on mock transports; every client built is closed after the test."""
    built: list[Fenon] = []

    def factory(api: Handler | None = None, storage: Handler | None = None, *, max_retries: int = 2) -> Fenon:
        config = ClientConfig.resolve(
            api_key=TEST_API_KEY, base_url=None, timeout=60, max_retries=max_retries, settings=ISOLATED
        )
        clients = open_http_clients(
            config,
            runtime=fake_runtime.as_runtime(),
            api_transport=httpx2.MockTransport(api or refuse),
            storage_transport=httpx2.MockTransport(storage or refuse),
        )
        client = Fenon._from_clients(config, clients)
        built.append(client)
        return client

    yield factory
    for client in built:
        client.close()
