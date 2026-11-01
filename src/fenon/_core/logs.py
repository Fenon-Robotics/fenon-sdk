"""The ``fenon`` logger and the ``FENON_LOG`` switch that turns it on (docs/features/logging.md).

``info`` or ``debug`` sends records to stderr; any other value leaves logging untouched.
"""

from __future__ import annotations

import logging

LOGGER_NAME = "fenon"

logger = logging.getLogger(LOGGER_NAME)
logger.addHandler(logging.NullHandler())

_LEVELS = {"info": logging.INFO, "debug": logging.DEBUG}
_ENV_HANDLER = logging.StreamHandler()
_ENV_HANDLER.setFormatter(logging.Formatter("[%(asctime)s %(name)s %(levelname)s] %(message)s"))


def configure_logging(level: str | None) -> None:
    chosen = _LEVELS.get((level or "").strip().lower())
    if chosen is None:
        return
    logger.setLevel(chosen)
    if _ENV_HANDLER not in logger.handlers:
        logger.addHandler(_ENV_HANDLER)
