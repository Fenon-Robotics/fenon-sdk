"""Shared by every pose model: the modes, the live-mode switch, and the step that uploads a video for a run.

``LIVE_MODE_AVAILABLE`` stays ``False`` until live mode ships; ``mode="live"`` is then refused with
``mode_not_available`` (docs/features/pose.md).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

from ..._core import VIDEO_CONTENT_TYPES, HttpClients, InvalidInputError, upload_file

RECORDED = "recorded"
LIVE = "live"
LIVE_MODE_AVAILABLE = False


def prepare_video(clients: HttpClients, video: str | os.PathLike[str]) -> dict[str, Any]:
    path = _video_path(video)
    content_type = VIDEO_CONTENT_TYPES.get(path.suffix.lower())
    if content_type is None:
        raise InvalidInputError(
            "The video must be an .mp4 or .mov file.",
            code="unsupported_media_type",
            fix="Convert the video to MP4 and try again.",
        )
    return {"video": upload_file(clients, path, content_type=content_type)}


def _video_path(video: object) -> Path:
    raw_path: object = None
    if isinstance(video, str | os.PathLike):
        raw_path = os.fspath(cast("str | os.PathLike[str]", video))
    if not isinstance(raw_path, str):
        raise TypeError("input must be a path to a local video file (str or os.PathLike).")
    return Path(raw_path)
