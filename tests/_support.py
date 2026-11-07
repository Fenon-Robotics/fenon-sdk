"""Test helpers: a fake runtime, recording mock-transport handlers, response builders and API fixtures."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import httpx2

from fenon._core.config import DEFAULT_BASE_URL
from fenon._core.errors import NetworkError
from fenon._core.http import Runtime
from fenon._core.settings import Settings
from fenon._live import runner
from fenon._live.config import LIVE_LIMITS
from fenon._live.gateway import HangUp
from fenon._live.results import media_time_90k
from fenon.models.pose.hamer import JOINT_ORDER
from fenon.models.pose.hamer._generated import CONTRACT_VERSION, SCHEMA_SHA256

Handler = Callable[[httpx2.Request], httpx2.Response]

TEST_API_KEY = "fenon_test_key_0123456789"
ISOLATED = Settings(api_key=None, base_url=DEFAULT_BASE_URL, log=None)
RETRYABLE_CODES = frozenset(
    {"internal_error", "unavailable", "rate_limited", "idempotency_in_progress", "engine_unavailable"}
)
UPLOAD_ID = "upl_7k2mq9x4ab3d"
RUN_ID = "run_q3w8e1r5t2y6"
CONTRACT_ID = "fenon.test.contract"
CONTRACT_SHA256 = "a" * 64
UPLOAD_URL = "https://storage.example.test/bucket/upl_7k2mq9x4ab3d?X-Amz-Signature=upload-signature"
OUTPUT_URL = "https://storage.example.test/out/annotations.jsonl?X-Amz-Signature=output-signature"
OUTPUT_BYTES = b'{"frame_index": 0}\n{"frame_index": 1}\n'
VIDEO_URL = "https://storage.example.test/out/video.mp4?X-Amz-Signature=video-signature"
VIDEO_BYTES = bytes(range(256)) * 64


@dataclass
class FakeRuntime:
    random_value: float = 0.5
    now_value: datetime = field(default_factory=lambda: datetime(2026, 10, 26, 12, 0, tzinfo=UTC))
    sleeps: list[float] = field(default_factory=list)
    keys_issued: int = 0
    interrupt_at_sleep: int | None = None
    interrupt: KeyboardInterrupt = field(default_factory=KeyboardInterrupt)
    key_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.interrupt_at_sleep == len(self.sleeps):
            raise self.interrupt

    def random(self) -> float:
        return self.random_value

    def now(self) -> datetime:
        return self.now_value

    def new_key(self) -> str:
        with self.key_lock:
            self.keys_issued += 1
            return f"idempotency-key-{self.keys_issued}"

    def as_runtime(self) -> Runtime:
        return Runtime(sleep=self.sleep, random=self.random, now=self.now, new_idempotency_key=self.new_key)


@dataclass
class Recorder:
    responses: list[httpx2.Response | Exception]
    requests: list[httpx2.Request] = field(default_factory=list)
    bodies: list[bytes] = field(default_factory=list)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        self.bodies.append(request.read())
        outcome = self.responses[min(len(self.requests), len(self.responses)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def json_response(status: int, body: Any = None, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(status, json=body if body is not None else {}, headers=headers)


def error_response(status: int, code: str, **fields: Any) -> httpx2.Response:
    retryable = code in RETRYABLE_CODES
    envelope = {"code": code, "message": f"Error {code}.", "fix": None, "retryable": retryable, **fields}
    return httpx2.Response(status, json={"error": envelope})


def refuse(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f"unexpected request to {request.url.host}")


@dataclass
class Router:
    routes: Mapping[tuple[str, str], Sequence[httpx2.Response | BaseException]]
    requests: list[httpx2.Request] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    scripts: dict[tuple[str, str], list[httpx2.Response | BaseException]] = field(init=False)

    def __post_init__(self) -> None:
        self.scripts = {route: list(outcomes) for route, outcomes in self.routes.items()}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        request.read()
        key = (request.method, request.url.path)
        with self.lock:
            self.requests.append(request)
            script = self.scripts.get(key)
            if script is None:
                raise AssertionError(f"unexpected request {request.method} {request.url.path}")
            outcome = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def calls(self, method: str, path: str) -> list[httpx2.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]


def reservation_body(
    upload_id: str = UPLOAD_ID, url: str = UPLOAD_URL, size_bytes: int = 1024
) -> dict[str, Any]:
    return {
        "id": upload_id,
        "status": "pending",
        "upload_url": url,
        "method": "PUT",
        "headers": {"Content-Type": "video/mp4", "Content-Length": str(size_bytes)},
        "expires_at": "2026-10-26T12:15:00.000Z",
    }


def run_body(status: str = "queued", **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": RUN_ID,
        "object": "run",
        "model": "fenon/test-model",
        "model_version": None,
        "contract": {"id": CONTRACT_ID, "version": "1.0.0", "schema_sha256": CONTRACT_SHA256},
        "status": status,
        "input": {"video": UPLOAD_ID},
        "input_files": {"video": {"id": UPLOAD_ID, "filename": "clip.mp4", "size_bytes": 1024}},
        "outputs_requested": ["annotations"],
        "progress": None,
        "usage_estimate": None,
        "outputs": None,
        "outputs_expire_at": None,
        "outputs_expired": False,
        "result": None,
        "usage": None,
        "error": None,
        "created_at": "2026-10-26T12:00:00.000Z",
        "started_at": None,
        "finished_at": None,
    }
    if status in ("processing_frames", "succeeded"):
        body["started_at"] = "2026-10-26T12:00:05.000Z"
        body["progress"] = {"completed": 300 if status == "succeeded" else 120, "total": 300, "unit": "frame"}
    if status == "succeeded":
        body["outputs"] = {
            "annotations": {
                "content_type": "application/x-ndjson",
                "url": OUTPUT_URL,
                "url_expires_at": "2026-10-26T13:00:00.000Z",
                "size_bytes": len(OUTPUT_BYTES),
            }
        }
        body["model_version"] = "tm_2026_10_26"
        body["outputs_expire_at"] = "2026-10-27T12:01:00.000Z"
        body["result"] = {}
        body["usage"] = {"unit": "second", "quantity": 10}
    if status in ("failed", "canceled"):
        body["usage"] = {"unit": "second", "quantity": 0}
        body["error"] = {
            "code": "input_unsupported" if status == "failed" else "canceled",
            "message": "The model engine can't process this video." if status == "failed" else "Canceled.",
            "fix": "Check the video against the model's input requirements." if status == "failed" else None,
            "retryable": False,
            "retry_after_s": None,
            "details": {"reason": "odd_dimensions"} if status == "failed" else {},
            "stage": "processing_frames" if status == "failed" else "queued",
        }
    if status in FINAL:
        body["finished_at"] = "2026-10-26T12:01:00.000Z"
    body.update(fields)
    return body


FINAL = ("succeeded", "failed", "canceled")


def run_response(status: str = "queued", poll_after_ms: int | None = None, **fields: Any) -> httpx2.Response:
    headers = {} if poll_after_ms is None else {"Fenon-Poll-After-Ms": str(poll_after_ms)}
    code = 202 if status == "queued" else 200
    return httpx2.Response(code, json=run_body(status, **fields), headers=headers)


HAND_CONTRACT = {"id": "fenon.hand_pose.video", "version": CONTRACT_VERSION, "schema_sha256": SCHEMA_SHA256}
HAND_START = 60
HAND_END = 63
HAND_BILLED_SECONDS = 7


def hand_row(frame_index: int, **fields: Any) -> dict[str, Any]:
    points: list[list[Any]] = [[812.4 + i, 455.1 + i] for i in range(20)] + [[None, None]]
    normalized = [[x / 1280, y / 720] if x is not None and y is not None else [None, None] for x, y in points]
    row: dict[str, Any] = {
        "frame_index": frame_index,
        "timestamp_sec": frame_index / 30.0,
        "image_size": {"width": 1280, "height": 720},
        "joint_order": ",".join(JOINT_ORDER),
        "raw_hand_count": 1,
        "suppressed_duplicate_count": 0,
        "hands": [
            {
                "handedness": "right",
                "pixel_landmarks": points,
                "normalized_landmarks": normalized,
                "landmarks_3d_camera": [[0.01 * i, 0.02, 0.6] for i in range(21)],
                "bbox_xyxy": [812.4, 455.1, 831.4, 474.1],
                "joint_state": ["in_frame_predicted"] * 20 + ["outside_frame_predicted"],
                "joint_confidence": [None] * 21,
                "hand_score": None,
            }
        ],
    }
    row.update(fields)
    return row


def annotations_output(frame_indexes: Sequence[int] = tuple(range(HAND_START, HAND_END))) -> bytes:
    return b"".join(json.dumps(hand_row(index)).encode() + b"\n" for index in frame_indexes)


def hand_result(**fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "video": {"width": 1280, "height": 720, "fps": 30.0, "frame_count": 300, "duration_s": 10.0},
        "range": {"start_s": 2.0, "end_s": 2.1, "start_frame": HAND_START, "end_frame": HAND_END},
        "joint_order": list(JOINT_ORDER),
        "camera": {
            "model": "pinhole",
            "focal_length_px": 25000.0,
            "principal_point_px": [640.0, 360.0],
            "assumed": True,
        },
    }
    result.update(fields)
    return result


def hand_run_body(
    status: str = "queued", content: bytes | None = None, video: bytes | None = None, **fields: Any
) -> dict[str, Any]:
    body = run_body(status, contract=HAND_CONTRACT)
    if video is not None:
        body["outputs_requested"] = ["annotations", "video"]
    if status == "succeeded":
        body["result"] = hand_result()
        body["outputs"]["annotations"]["size_bytes"] = len(
            annotations_output() if content is None else content
        )
        if video is not None:
            body["outputs"]["video"] = {
                "content_type": "video/mp4",
                "url": VIDEO_URL,
                "url_expires_at": "2026-10-26T13:00:00.000Z",
                "size_bytes": len(video),
            }
        body["usage"] = {"unit": "second", "quantity": HAND_BILLED_SECONDS}
    body.update(fields)
    return body


def hand_response(
    status: str = "queued",
    content: bytes | None = None,
    poll_after_ms: int | None = None,
    video: bytes | None = None,
    **fields: Any,
) -> httpx2.Response:
    headers = {} if poll_after_ms is None else {"Fenon-Poll-After-Ms": str(poll_after_ms)}
    code = 202 if status == "queued" else 200
    return httpx2.Response(code, json=hand_run_body(status, content, video, **fields), headers=headers)


LIVE_SESSION_ID = "ls_30srkafwa0jd"
LIVE_TOKEN = "session-token-secret-value"


def live_session_body(**fields: Any) -> dict[str, Any]:
    return {
        "id": LIVE_SESSION_ID,
        "object": "live_session",
        "model": "fenon/hand-pose-hm",
        "status": "waiting_for_media",
        "end_reason": None,
        "max_duration_s": 13,
        "idle_timeout_s": 15,
        "usage": {"unit": "second", "quantity": 0, "final": False},
        "created_at": "2026-10-26T12:00:00.000Z",
        "connected_at": None,
        "ended_at": None,
        **fields,
    }


def live_created_body(**connection: Any) -> dict[str, Any]:
    return live_session_body(
        connection={
            "transport": "webrtc",
            "offer_url": f"https://gateway.fenon.test/v1/sessions/{LIVE_SESSION_ID}/offer",
            "session_url": f"https://gateway.fenon.test/v1/sessions/{LIVE_SESSION_ID}",
            "results_channel": "results",
            "token": LIVE_TOKEN,
            "token_connect_by": "2026-10-26T12:01:00.000Z",
            "ice_servers": [{"urls": "stun:stun.cloudflare.com:3478"}],
            **connection,
        }
    )


def live_ended_body(end_reason: str = "client_closed") -> dict[str, Any]:
    return live_session_body(
        status="ended",
        end_reason=end_reason,
        usage={"unit": "second", "quantity": 10, "final": True},
        connected_at="2026-10-26T12:00:01.000Z",
        ended_at="2026-10-26T12:00:09.000Z",
    )


def hand_pose_live_data() -> dict[str, Any]:
    row = hand_row(0)
    return {
        "camera": {
            "model": "pinhole",
            "focal_length_px": 1000.0,
            "principal_point_px": [640.0, 360.0],
            "assumed": True,
        },
        "joint_order": row["joint_order"],
        "raw_hand_count": row["raw_hand_count"],
        "suppressed_duplicate_count": row["suppressed_duplicate_count"],
        "hands": row["hands"],
    }


@dataclass
class FakeMedia:
    data: dict[str, Any] = field(default_factory=lambda: {"echo": True})
    offer_errors: list[Exception] = field(default_factory=list)
    lose_after: int | None = None
    interrupt_after: int | None = None
    raw_message: str | None = None
    offers: int = 0
    hang_ups: int = 0
    sent: int = 0


def install_fake_media(monkeypatch: Any) -> FakeMedia:
    media = FakeMedia()

    class FakeGateway:
        def __init__(self, access: Any, runtime: Any) -> None:
            pass

        def offer(self, sdp: str) -> str:
            media.offers += 1
            if media.offer_errors:
                raise media.offer_errors.pop(0)
            return "v=0\r\n"

        def hang_up(self) -> Any:
            media.hang_ups += 1
            return HangUp.ENDED

        def close(self) -> None:
            pass

    class FakeTransport:
        def __init__(self, ice_servers: Any, on_message: Any) -> None:
            self.on_message = on_message
            self.lost = asyncio.Event()

        async def create_offer(self) -> str:
            return "v=0\r\n"

        async def accept_answer(self, sdp: str) -> None:
            pass

        async def wait_connected(self) -> None:
            pass

        async def send(self, frame: Any) -> None:
            if media.interrupt_after is not None and media.sent == media.interrupt_after:
                raise KeyboardInterrupt
            if media.lose_after is not None and media.sent >= media.lose_after:
                self.lost.set()
                raise NetworkError("lost", code="connection_lost", retryable=True)
            message = {
                "type": "result",
                "frame_id": media.sent,
                "rtp_timestamp": 0,
                "media_time_90k": media_time_90k(frame.pts, frame.time_base),
                "frame_width": 48,
                "frame_height": 64,
                "received_at_ms": 0,
                "sent_at_ms": 0,
                "data": media.data,
            }
            media.sent += 1
            self.on_message(media.raw_message or json.dumps(message))

        async def end_media(self) -> None:
            pass

        async def close(self) -> None:
            pass

    monkeypatch.setattr(runner, "GatewayClient", FakeGateway)
    monkeypatch.setattr(runner, "LiveTransport", FakeTransport)
    monkeypatch.setattr(runner, "LIVE_LIMITS", replace(LIVE_LIMITS, end_of_input_wait_s=0.0))
    return media
