"""Shared by every pose model: the video prepare step and run outputs fetched once."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, cast

from ..._core._generated import Run
from ..._core.errors import InvalidInputError
from ..._core.http import HttpClients
from ..._core.runs import fetch_output
from ..._core.settings import LIMITS
from ..._core.uploads import upload_file

RECORDED = "recorded"


def prepare_video(clients: HttpClients, video: str | os.PathLike[str], mode: str) -> dict[str, Any]:
    """Check the mode and extension, upload the video, and return the run input ``{"video": upload_id}``."""
    path = _video_path(video)
    if mode != RECORDED:
        raise InvalidInputError(
            'Only mode="recorded" is supported.', code="mode_not_supported", fix='Pass mode="recorded".'
        )
    content_type = LIMITS.video_content_types.get(path.suffix.lower())
    if content_type is None:
        raise InvalidInputError(
            "The video must be an .mp4, .mov or .webm file.",
            code="unsupported_media_type",
            fix="Convert the video to MP4 and try again.",
        )
    return {"video": upload_file(clients, path, content_type=content_type)}


def _video_path(video: object) -> Path:
    """Return ``video`` as a path, refusing anything that isn't a ``str`` or ``os.PathLike[str]``."""
    raw_path: object = None
    if isinstance(video, str | os.PathLike):
        raw_path = os.fspath(cast("str | os.PathLike[str]", video))
    if not isinstance(raw_path, str):
        raise TypeError("input must be a path to a local video file (str or os.PathLike).")
    return Path(raw_path)


class RunOutput:
    """One named output of a succeeded run, downloaded on first use and kept in memory."""

    def __init__(self, clients: HttpClients, run: Run, name: str) -> None:
        self._clients = clients
        self._run = run
        self._name = name
        self._lock = threading.Lock()
        self._content: bytes | None = None

    def content(self) -> bytes:
        """Return the output's bytes, downloading them on the first call only."""
        with self._lock:
            if self._content is None:
                self._content = fetch_output(self._clients, self._run, self._name)
            return self._content

    def save(self, path: str | os.PathLike[str]) -> Path:
        """Write the output's bytes to ``path``, replacing any file there, and return the path."""
        target = Path(path)
        target.write_bytes(self.content())
        return target
