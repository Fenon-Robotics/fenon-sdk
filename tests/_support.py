"""Test helpers: a fake runtime, recording mock-transport handlers, response builders and API fixtures."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx2

from fenon._core.http import Runtime
from fenon._core.settings import DEFAULT_BASE_URL, Settings
from fenon.models.pose.hamer import JOINT_ORDER
from fenon.models.pose.hamer._generated import SCHEMA_SHA256

Handler = Callable[[httpx2.Request], httpx2.Response]

TEST_API_KEY = "fenon_test_key_0123456789"
ISOLATED = Settings(api_key=None, base_url=DEFAULT_BASE_URL, log=None)
UPLOAD_ID = "upl_7k2mq9x4ab3d"
RUN_ID = "run_q3w8e1r5t2y6"
CONTRACT_ID = "fenon.test.contract"
CONTRACT_SHA256 = "a" * 64
UPLOAD_URL = "https://storage.example.test/bucket/upl_7k2mq9x4ab3d?X-Amz-Signature=upload-signature"
OUTPUT_URL = "https://storage.example.test/out/annotations.jsonl?X-Amz-Signature=output-signature"
OUTPUT_BYTES = b'{"frame_index": 0}\n{"frame_index": 1}\n'


@dataclass
class FakeRuntime:
    """Deterministic time, jitter and keys that record what the clients asked for."""

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
    """A mock transport handler that records requests and plays back scripted responses."""

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
    """Return a JSON response with the given status, body and headers."""
    return httpx2.Response(status, json=body if body is not None else {}, headers=headers)


def error_response(status: int, code: str, **fields: Any) -> httpx2.Response:
    """Return a response carrying the contract's error envelope."""
    envelope = {"code": code, "message": f"Error {code}.", "fix": None, "retryable": False, **fields}
    return httpx2.Response(status, json={"error": envelope})


def refuse(request: httpx2.Request) -> httpx2.Response:
    """Fail the test if a transport that shouldn't be used receives a request."""
    raise AssertionError(f"unexpected request to {request.url.host}")


@dataclass
class Router:
    """A thread-safe mock transport handler that answers by method and path."""

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
        """Return the recorded requests for one route."""
        return [r for r in self.requests if r.method == method and r.url.path == path]


def reservation_body(
    upload_id: str = UPLOAD_ID, url: str = UPLOAD_URL, size_bytes: int = 1024
) -> dict[str, Any]:
    """Return a ``POST /v1/uploads`` 201 body."""
    return {
        "id": upload_id,
        "status": "pending",
        "upload_url": url,
        "method": "PUT",
        "headers": {"Content-Type": "video/mp4", "Content-Length": str(size_bytes)},
        "expires_at": "2026-10-26T12:15:00Z",
    }


def run_body(status: str = "preparing_input", **fields: Any) -> dict[str, Any]:
    """Return a run object whose non-null fields follow the contract's table for ``status``."""
    body: dict[str, Any] = {
        "id": RUN_ID,
        "object": "run",
        "model": "fenon/test-model",
        "model_version": "tm_2026_10_26",
        "contract": {"id": CONTRACT_ID, "version": "1.0.0", "schema_sha256": CONTRACT_SHA256},
        "status": status,
        "input": {"video": UPLOAD_ID},
        "input_files": {"video": {"id": UPLOAD_ID, "filename": "clip.mp4", "size_bytes": 1024}},
        "progress": None,
        "usage_estimate": None,
        "outputs": None,
        "result": None,
        "usage": None,
        "error": None,
        "created_at": "2026-10-26T12:00:00Z",
        "started_at": None,
        "finished_at": None,
    }
    if status != "preparing_input":
        body["usage_estimate"] = {"unit": "frame", "quantity": 300}
    if status in ("processing_frames", "writing_results", "succeeded"):
        body["started_at"] = "2026-10-26T12:00:05Z"
        body["progress"] = {"completed": 300 if status == "succeeded" else 120, "total": 300, "unit": "frame"}
    if status == "succeeded":
        body["outputs"] = {
            "annotations": {
                "content_type": "application/x-ndjson",
                "url": OUTPUT_URL,
                "url_expires_at": "2026-10-26T13:00:00Z",
                "size_bytes": len(OUTPUT_BYTES),
            }
        }
        body["result"] = {}
        body["usage"] = {"unit": "frame", "quantity": 300}
    if status in ("failed", "canceled"):
        body["usage"] = {"unit": "frame", "quantity": 0}
        body["error"] = {
            "code": "variable_frame_rate" if status == "failed" else "canceled",
            "message": "The frame rate changes partway through this file."
            if status == "failed"
            else "Canceled.",
            "fix": "Export it again at a constant frame rate." if status == "failed" else None,
            "retryable": False,
            "retry_after_s": None,
            "details": {},
            "stage": "preparing_input" if status == "failed" else "queued",
            "frame": 141 if status == "failed" else None,
            "timestamp_s": 4.7 if status == "failed" else None,
        }
    if status in FINAL:
        body["finished_at"] = "2026-10-26T12:01:00Z"
    body.update(fields)
    return body


FINAL = ("succeeded", "failed", "canceled")


def run_response(
    status: str = "preparing_input", poll_after_ms: int | None = None, **fields: Any
) -> httpx2.Response:
    """Return a run response, with ``Fenon-Poll-After-Ms`` when given."""
    headers = {} if poll_after_ms is None else {"Fenon-Poll-After-Ms": str(poll_after_ms)}
    code = 202 if status == "preparing_input" else 200
    return httpx2.Response(code, json=run_body(status, **fields), headers=headers)


HAND_CONTRACT = {"id": "fenon.hand_pose.video", "version": "1.0.0", "schema_sha256": SCHEMA_SHA256}
HAND_START = 60
HAND_END = 63


def hand_row(frame_index: int, **fields: Any) -> dict[str, Any]:
    """Return one valid annotations row for ``frame_index``."""
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
    """Return a JSON Lines annotations output with one valid row per frame index."""
    return b"".join(json.dumps(hand_row(index)).encode() + b"\n" for index in frame_indexes)


def hand_result(**fields: Any) -> dict[str, Any]:
    """Return a valid hand-pose result for frames ``HAND_START`` to ``HAND_END``."""
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
    status: str = "preparing_input", content: bytes | None = None, **fields: Any
) -> dict[str, Any]:
    """Return a hand-pose run; a succeeded one has a valid result, usage and outputs sized for ``content``."""
    body = run_body(status, contract=HAND_CONTRACT)
    if status == "succeeded":
        body["result"] = hand_result()
        body["outputs"]["annotations"]["size_bytes"] = len(
            annotations_output() if content is None else content
        )
        body["usage"] = {"unit": "frame", "quantity": HAND_END - HAND_START}
    body.update(fields)
    return body


def hand_response(
    status: str = "preparing_input",
    content: bytes | None = None,
    poll_after_ms: int | None = None,
    **fields: Any,
) -> httpx2.Response:
    """Return a hand-pose run response, with ``Fenon-Poll-After-Ms`` when given."""
    headers = {} if poll_after_ms is None else {"Fenon-Poll-After-Ms": str(poll_after_ms)}
    code = 202 if status == "preparing_input" else 200
    return httpx2.Response(code, json=hand_run_body(status, content, **fields), headers=headers)
