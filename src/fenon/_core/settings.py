"""Every setting and limit the SDK uses, in one place: what users may set, and what the contract fixes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "FENON_"
DEFAULT_BASE_URL = "https://api.fenon.ai/v1"


class Settings(BaseSettings):
    """What a user may set with ``FENON_*`` environment variables; ``Fenon()`` arguments win."""

    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, extra="ignore", frozen=True)

    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    log: str | None = None


class Limits(BaseModel):
    """Defaults, timeouts, retry schedules and limits fixed by the contract; users can't change them."""

    model_config = ConfigDict(frozen=True)

    default_timeout_s: float = 60.0
    default_max_retries: int = 2
    connect_timeout_s: float = 10.0
    storage_timeout_s: float = 300.0
    max_retry_hint_s: float = 120.0
    initial_backoff_s: float = 0.5
    max_backoff_s: float = 8.0
    storage_waits_s: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)
    storage_retry_statuses: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
    upload_reservations: int = 2
    min_poll_s: float = 0.25
    max_poll_s: float = 60.0
    first_default_poll_s: float = 2.0
    default_poll_growth: float = 1.5
    max_default_poll_s: float = 10.0
    max_poll_failures: int = 8
    interrupt_cancel_timeout_s: float = 5.0
    video_content_types: dict[str, str] = Field(
        default_factory=lambda: {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
    )


LIMITS = Limits()
