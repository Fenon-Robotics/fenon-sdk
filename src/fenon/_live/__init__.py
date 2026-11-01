"""Live sessions: stream a video to a Fenon model over WebRTC and read its results (docs/features/live.md)."""

from __future__ import annotations

import os

from .._core import ApiClient, Runtime
from ._generated import Usage as LiveUsage
from .results import LiveFrame, LiveGap, LiveRun

__all__ = ["LiveFrame", "LiveGap", "LiveRun", "LiveUsage", "run_file"]


def run_file(api: ApiClient, runtime: Runtime, *, model: str, path: str | os.PathLike[str]) -> LiveRun:
    from .runner import run_file as run_with_media

    return run_with_media(api, runtime, model=model, path=path)
