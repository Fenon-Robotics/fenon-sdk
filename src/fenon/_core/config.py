"""Fixed values of the core services: timeouts, retries, uploads and run polling. Not user settings.

What a user may set lives in ``settings.py``. ``DEFAULT_BASE_URL`` is provisional until the console and the
API share one database (docs/features/settings.md). ``retry_statuses_without_error_body`` applies only to
responses with no Fenon error body (proxies, storage); fenon-api's own errors say whether they are retryable
(docs/features/http.md).
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.fenon.ai/v1"


@dataclass(frozen=True)
class Limits:
    default_timeout_s: float = 60.0
    default_max_retries: int = 2
    connect_timeout_s: float = 10.0
    storage_timeout_s: float = 300.0
    max_retry_hint_s: float = 120.0
    initial_backoff_s: float = 0.5
    max_backoff_s: float = 8.0
    retry_statuses_without_error_body: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
    storage_waits_s: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)
    upload_reservations: int = 2
    min_poll_s: float = 0.25
    max_poll_s: float = 60.0
    first_default_poll_s: float = 2.0
    default_poll_growth: float = 1.5
    max_default_poll_s: float = 10.0
    max_poll_failures: int = 8
    interrupt_cancel_timeout_s: float = 5.0


LIMITS = Limits()
