"""The Fenon client: configuration, logging, the HTTP clients and one attribute per capability.

``Fenon()`` reads the ``FENON_*`` settings for any argument left out (docs/features/settings.md).
``_from_clients`` builds a client around existing HTTP clients; it is the test seam.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from ._core import LIMITS, ClientConfig, HttpClients, Settings, configure_logging, open_http_clients
from .models.pose import Pose


class Fenon:
    pose: Pose

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = LIMITS.default_timeout_s,
        max_retries: int = LIMITS.default_max_retries,
    ) -> None:
        settings = Settings()
        config = ClientConfig.resolve(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries, settings=settings
        )
        configure_logging(settings.log)
        self._attach(config, open_http_clients(config))

    @classmethod
    def _from_clients(cls, config: ClientConfig, clients: HttpClients) -> Self:
        client = cls.__new__(cls)
        client._attach(config, clients)
        return client

    def _attach(self, config: ClientConfig, clients: HttpClients) -> None:
        self._config = config
        self._clients = clients
        self.pose = Pose(clients)

    def close(self) -> None:
        self._clients.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Fenon(base_url={self._config.base_url!r})"
