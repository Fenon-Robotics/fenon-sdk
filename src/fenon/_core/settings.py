"""What a user may set with ``FENON_*`` environment variables; ``Fenon()`` arguments win.

Fixed values live in ``config.py`` (docs/features/settings.md).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from .config import DEFAULT_BASE_URL, DEFAULT_CONSOLE_URL

ENV_PREFIX = "FENON_"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, extra="ignore", frozen=True)

    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    console_url: str = DEFAULT_CONSOLE_URL
    log: str | None = None
