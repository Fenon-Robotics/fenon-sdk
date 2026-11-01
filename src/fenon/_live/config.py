"""Live-session values fixed by fenon-api's contract or chosen for streaming. Not user settings.

See docs/features/settings.md and docs/features/live.md.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LiveLimits:
    min_duration_s: int = 10
    max_duration_s: int = 3600
    duration_margin_s: int = 5
    connect_margin_s: float = 5.0
    offer_timeout_s: float = 30.0
    max_offer_bytes: int = 64 * 1024
    max_offer_attempts: int = 3
    hang_up_timeout_s: float = 45.0
    connect_timeout_s: float = 30.0
    keyframe_interval_s: float = 2.0
    send_queue_frames: int = 8
    end_of_input_wait_s: float = 1.0
    max_sessions_per_call: int = 2


LIVE_LIMITS = LiveLimits()
