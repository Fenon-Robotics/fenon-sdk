"""A local simulation of every Fenon v1 endpoint for development; it is never shipped in the package."""

from __future__ import annotations

import argparse
import json
import math
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from typing_extensions import override

from fenon.models.pose.hamer import JOINT_ORDER
from fenon.models.pose.hamer._generated import CONTRACT_ID, CONTRACT_VERSION, SCHEMA_SHA256

DEFAULT_PORT = 8787
DEFAULT_KEY = "fenon_dev_local_simulation_key_000"
POLL_AFTER_MS = 500
FAKE_FPS = 30.0
FAKE_FRAMES = 90
FAKE_WIDTH, FAKE_HEIGHT = 1280, 720
STAGES = (("preparing_input", 1.0), ("queued", 1.0), ("processing_frames", 1.5), ("writing_results", 0.5))
MODEL_ID = "fenon/hand-pose-hm"
MODEL = {
    "id": MODEL_ID,
    "model_version": "hp_2026_10_26",
    "modes": ["recorded"],
    "contract": {"id": CONTRACT_ID, "version": CONTRACT_VERSION, "schema_sha256": SCHEMA_SHA256},
    "limits": {
        "content_types": ["video/mp4", "video/quicktime", "video/webm"],
        "max_size_bytes": 200_000_000,
        "max_duration_s": 60,
        "max_run_s": 1800,
    },
    "usage_unit": "frame",
}

Reply = tuple[int, dict[str, str], bytes]


def new_id(prefix: str) -> str:
    """Return an ID in the contract's form: a prefix and 12 characters from 0-9a-z."""
    return prefix + "".join(secrets.choice("0123456789abcdefghijklmnopqrstuvwxyz") for _ in range(12))


def now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    """Return an RFC 3339 UTC timestamp."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def reply(status: int, body: Any, headers: dict[str, str] | None = None) -> Reply:
    """Return a JSON reply."""
    return status, {"Content-Type": "application/json", **(headers or {})}, json.dumps(body).encode()


def error(status: int, code: str, message: str, details: dict[str, Any] | None = None) -> Reply:
    """Return the contract's error envelope."""
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    body = {
        "error": {
            "code": code,
            "message": message,
            "fix": None,
            "retryable": False,
            "retry_after_s": None,
            "details": details or {},
        }
    }
    return reply(status, body, headers)


def annotation_rows(frames: int) -> bytes:
    """Return a JSON Lines output with one synthetic right hand per frame, moving in a circle."""
    lines: list[bytes] = []
    for index in range(frames):
        angle = 2 * math.pi * index / frames
        cx, cy = 640 + 120 * math.cos(angle), 360 + 80 * math.sin(angle)
        pixels = [[round(cx + 6 * (j % 5), 2), round(cy - 9 * (j // 5), 2)] for j in range(21)]
        lines.append(
            json.dumps(
                {
                    "frame_index": index,
                    "timestamp_sec": round(index / FAKE_FPS, 6),
                    "image_size": {"width": FAKE_WIDTH, "height": FAKE_HEIGHT},
                    "joint_order": ",".join(JOINT_ORDER),
                    "raw_hand_count": 1,
                    "suppressed_duplicate_count": 0,
                    "hands": [
                        {
                            "handedness": "right",
                            "pixel_landmarks": pixels,
                            "normalized_landmarks": [[x / FAKE_WIDTH, y / FAKE_HEIGHT] for x, y in pixels],
                            "landmarks_3d_camera": [[0.002 * j, -0.003 * j, 0.6] for j in range(21)],
                            "bbox_xyxy": [
                                min(p[0] for p in pixels),
                                min(p[1] for p in pixels),
                                max(p[0] for p in pixels),
                                max(p[1] for p in pixels),
                            ],
                            "joint_state": ["in_frame_predicted"] * 21,
                            "joint_confidence": [None] * 21,
                            "hand_score": None,
                        }
                    ],
                }
            ).encode()
        )
    return b"\n".join(lines) + b"\n"


@dataclass
class Upload:
    """A reserved upload and, once the PUT arrives, its bytes."""

    id: str
    filename: str
    content_type: str
    size_bytes: int
    token: str
    created_at: datetime
    received: bytes | None = None


@dataclass
class Run:
    """A simulated run whose status moves forward with time."""

    id: str
    model: str
    upload: Upload
    created_at: datetime
    started: float
    canceled_in: str | None = None
    finished_at: datetime | None = None
    output: bytes = field(default_factory=lambda: annotation_rows(FAKE_FRAMES))


class Simulation:
    """The in-memory state of the simulated API and storage."""

    def __init__(
        self,
        base_url: str,
        key: str,
        stages: tuple[tuple[str, float], ...] = STAGES,
        poll_after_ms: int = POLL_AFTER_MS,
        first_cut: bool = False,
    ) -> None:
        self.base_url = base_url
        self.key = key
        self.stages = stages
        self.poll_after_ms = poll_after_ms
        self.first_cut = first_cut
        self.lock = threading.Lock()
        self.uploads: dict[str, Upload] = {}
        self.runs: dict[str, Run] = {}
        self.replays: dict[tuple[str, str], tuple[str, Reply]] = {}

    def status_of(self, run: Run) -> str:
        """Return the run's status from the time elapsed since it was created."""
        if run.canceled_in is not None:
            return "canceled"
        if run.upload.filename.startswith("fail"):
            return "failed" if time.monotonic() - run.started > self.stages[0][1] else "preparing_input"
        elapsed = time.monotonic() - run.started
        for status, seconds in self.stages:
            if elapsed < seconds:
                return status
            elapsed -= seconds
        return "succeeded"

    def run_body(self, run: Run) -> dict[str, Any]:
        """Return the run object, with fields filled as the contract's status table requires."""
        status = self.status_of(run)
        if status in ("succeeded", "failed", "canceled") and run.finished_at is None:
            run.finished_at = now()
        progress_done = {"preparing_input": 0, "queued": 0, "processing_frames": FAKE_FRAMES // 2}.get(
            status, FAKE_FRAMES
        )
        body: dict[str, Any] = {
            "id": run.id,
            "object": "run",
            "model": run.model,
            "model_version": MODEL["model_version"],
            "contract": MODEL["contract"],
            "status": status,
            "input": {"video": run.upload.id},
            "input_files": {
                "video": {
                    "id": run.upload.id,
                    "filename": run.upload.filename,
                    "size_bytes": run.upload.size_bytes,
                }
            },
            "progress": None,
            "usage_estimate": None,
            "outputs": None,
            "result": None,
            "usage": None,
            "error": None,
            "created_at": stamp(run.created_at),
            "started_at": None,
            "finished_at": None if run.finished_at is None else stamp(run.finished_at),
        }
        if status != "preparing_input":
            body["usage_estimate"] = {"unit": "frame", "quantity": FAKE_FRAMES}
        if status in ("processing_frames", "writing_results", "succeeded"):
            body["started_at"] = stamp(run.created_at + timedelta(seconds=2))
            body["progress"] = {"completed": progress_done, "total": FAKE_FRAMES, "unit": "frame"}
        if status == "succeeded":
            body["outputs"] = {
                "annotations": {
                    "content_type": "application/x-ndjson",
                    "url": f"{self.base_url}/storage/outputs/{run.id}?X-Amz-Signature={run.upload.token}",
                    "url_expires_at": stamp(now() + timedelta(hours=1)),
                    "size_bytes": len(run.output),
                }
            }
            body["result"] = {
                "video": {
                    "width": FAKE_WIDTH,
                    "height": FAKE_HEIGHT,
                    "fps": FAKE_FPS,
                    "frame_count": FAKE_FRAMES,
                    "duration_s": FAKE_FRAMES / FAKE_FPS,
                },
                "range": {
                    "start_s": 0.0,
                    "end_s": FAKE_FRAMES / FAKE_FPS,
                    "start_frame": 0,
                    "end_frame": FAKE_FRAMES,
                },
                "joint_order": list(JOINT_ORDER),
                "camera": {
                    "model": "pinhole",
                    "focal_length_px": 5000.0 / 256 * max(FAKE_WIDTH, FAKE_HEIGHT),
                    "principal_point_px": [FAKE_WIDTH / 2, FAKE_HEIGHT / 2],
                    "assumed": True,
                },
            }
            body["usage"] = {"unit": "frame", "quantity": FAKE_FRAMES}
        if status in ("failed", "canceled"):
            body["usage"] = {"unit": "frame", "quantity": 0}
            failed = status == "failed"
            body["error"] = {
                "code": "variable_frame_rate" if failed else "canceled",
                "message": "The frame rate changes partway through this file."
                if failed
                else "The run was canceled.",
                "fix": "Export it again at a constant frame rate." if failed else None,
                "retryable": False,
                "retry_after_s": None,
                "details": {},
                "stage": "preparing_input" if failed else (run.canceled_in or "queued"),
                "frame": 141 if failed else None,
                "timestamp_s": 4.7 if failed else None,
            }
        return body


class Handler(BaseHTTPRequestHandler):
    """Routes requests to the simulation."""

    simulation: Simulation
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """Handle a GET."""
        self._dispatch()

    def do_POST(self) -> None:
        """Handle a POST."""
        self._dispatch()

    def do_PUT(self) -> None:
        """Handle a PUT."""
        self._dispatch()

    @override
    def log_message(self, format: str, *args: Any) -> None:
        """Print one line per request."""
        print(
            f"  {self.command} {urlsplit(self.path).path} -> {args[1] if len(args) > 1 else ''}", flush=True
        )

    def _dispatch(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        parts = urlsplit(self.path)
        query = {name: values[0] for name, values in parse_qs(parts.query).items()}
        with self.simulation.lock:
            status, headers, payload = self._route(parts.path, query, body)
        self.send_response(status)
        self.send_header("X-Request-Id", new_id("req_"))
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _route(self, path: str, query: dict[str, str], body: bytes) -> Reply:
        sim = self.simulation
        segments = path.strip("/").split("/")
        if segments[0] == "storage":
            return self._storage(segments, query, body)
        if segments[0] != "v1":
            return error(404, "not_found", "No such path.")
        if self.headers.get("Authorization") != f"Bearer {sim.key}":
            return error(401, "invalid_api_key", "The API key isn't valid.")
        handlers: dict[tuple[str, int], Callable[[], Reply]] = {
            ("GET", 2): lambda: self._list_models(query) if segments[1] == "models" else self._list_runs(),
            ("POST", 2): lambda: self._idempotent(
                path, body, self._create_upload if segments[1] == "uploads" else self._create_run
            ),
            ("GET", 3): lambda: (
                self._get_upload(segments[2]) if segments[1] == "uploads" else self._get_run(segments[2])
            ),
            ("POST", 4): lambda: self._idempotent(path, body, lambda _: self._cancel_run(segments[2])),
        }
        handler = handlers.get((self.command, len(segments)))
        known = segments[1] in ("models", "uploads", "runs") and (
            len(segments) < 4 or segments[3] == "cancel"
        )
        if handler is None or not known:
            return error(404, "not_found", "No such endpoint.")
        if sim.first_cut and segments[1] != "models":
            port = "idempotency_store" if self.command == "POST" else f"{segments[1][:-1]}_store"
            return error(501, "not_implemented", "This deployment can't do this yet.", {"port": port})
        return handler()

    def _idempotent(self, path: str, body: bytes, create: Callable[[dict[str, Any]], Reply]) -> Reply:
        key = self.headers.get("Idempotency-Key")
        if not key:
            return error(400, "invalid_request", "Send an Idempotency-Key header.")
        stored = self.simulation.replays.get((key, path))
        if stored is not None:
            fingerprint, saved = stored
            if fingerprint != body.decode():
                return error(409, "idempotency_conflict", "This key was used with a different body.")
            status, headers, payload = saved
            return status, {**headers, "Idempotent-Replayed": "true"}, payload
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            return error(400, "invalid_request", "The body isn't valid JSON.")
        result = create(parsed if isinstance(parsed, dict) else {})
        if 200 <= result[0] < 300:
            self.simulation.replays[(key, path)] = (body.decode(), result)
        return result

    def _list_models(self, query: dict[str, str]) -> Reply:
        limit = query.get("limit", "100")
        if not limit.isdigit() or not 1 <= int(limit) <= 100:
            return error(400, "invalid_request", "limit must be 1 to 100.", {"parameter": "limit"})
        if "after" in query and query["after"] != MODEL_ID:
            return error(
                400, "invalid_request", "after isn't a model ID from a previous page.", {"parameter": "after"}
            )
        data = [] if "after" in query else [MODEL]
        return reply(200, {"data": data, "next_cursor": None})

    def _create_upload(self, body: dict[str, Any]) -> Reply:
        sim = self.simulation
        filename, content_type, size = body.get("filename"), body.get("content_type"), body.get("size_bytes")
        if (
            not isinstance(filename, str)
            or not isinstance(content_type, str)
            or not isinstance(size, int)
            or size < 1
        ):
            return error(400, "invalid_request", "Send filename, content_type and size_bytes.")
        if content_type not in MODEL["limits"]["content_types"]:
            return error(415, "unsupported_media_type", "No model accepts this video type.")
        if size > MODEL["limits"]["max_size_bytes"]:
            return error(413, "file_too_large", "The video is larger than any model accepts.")
        upload = Upload(new_id("upl_"), filename, content_type, size, secrets.token_hex(8), now())
        sim.uploads[upload.id] = upload
        return reply(
            201,
            {
                "id": upload.id,
                "status": "pending",
                "upload_url": f"{sim.base_url}/storage/uploads/{upload.id}?X-Amz-Signature={upload.token}",
                "method": "PUT",
                "headers": {"Content-Type": content_type, "Content-Length": str(size)},
                "expires_at": stamp(now() + timedelta(minutes=15)),
            },
        )

    def _get_upload(self, upload_id: str) -> Reply:
        upload = self.simulation.uploads.get(upload_id)
        if upload is None:
            return error(404, "not_found", "No such upload.")
        ready = upload.received is not None
        media = {
            "duration_s": FAKE_FRAMES / FAKE_FPS,
            "fps": FAKE_FPS,
            "frame_count": FAKE_FRAMES,
            "width": FAKE_WIDTH,
            "height": FAKE_HEIGHT,
            "constant_frame_rate": True,
        }
        return reply(
            200,
            {
                "id": upload.id,
                "filename": upload.filename,
                "status": "ready" if ready else "pending",
                "content_type": upload.content_type,
                "size_bytes": upload.size_bytes,
                "media": media if ready else None,
                "error": None,
                "created_at": stamp(upload.created_at),
            },
        )

    def _create_run(self, body: dict[str, Any]) -> Reply:
        sim = self.simulation
        model, run_input = body.get("model"), body.get("input")
        if model != MODEL_ID:
            return error(404, "model_not_found", "No such model.", {"field": "/model"})
        video = run_input.get("video") if isinstance(run_input, dict) else None
        upload = sim.uploads.get(video) if isinstance(video, str) else None
        if upload is None:
            return error(
                422,
                "input_invalid",
                "input.video isn't an upload in this project.",
                {"field": "/input/video"},
            )
        if upload.received is None:
            return error(
                422, "upload_not_ready", "The upload's bytes haven't arrived yet.", {"field": "/input/video"}
            )
        run = Run(new_id("run_"), MODEL_ID, upload, now(), time.monotonic())
        sim.runs[run.id] = run
        return reply(202, sim.run_body(run), {"Fenon-Poll-After-Ms": str(sim.poll_after_ms)})

    def _list_runs(self) -> Reply:
        runs = sorted(self.simulation.runs.values(), key=lambda run: run.started, reverse=True)
        return reply(200, {"data": [self.simulation.run_body(run) for run in runs], "next_cursor": None})

    def _get_run(self, run_id: str) -> Reply:
        run = self.simulation.runs.get(run_id)
        if run is None:
            return error(404, "not_found", "No such run.")
        body = self.simulation.run_body(run)
        final = body["status"] in ("succeeded", "failed", "canceled")
        return reply(
            200, body, None if final else {"Fenon-Poll-After-Ms": str(self.simulation.poll_after_ms)}
        )

    def _cancel_run(self, run_id: str) -> Reply:
        run = self.simulation.runs.get(run_id)
        if run is None:
            return error(404, "not_found", "No such run.")
        status = self.simulation.status_of(run)
        if status not in ("succeeded", "failed", "canceled"):
            run.canceled_in = status
        return reply(200, self.simulation.run_body(run))

    def _storage(self, segments: list[str], query: dict[str, str], body: bytes) -> Reply:
        sim = self.simulation
        if len(segments) != 3:
            return (404, {}, b"")
        kind, object_id = segments[1], segments[2]
        if kind == "uploads" and self.command == "PUT":
            upload = sim.uploads.get(object_id)
            if (
                upload is None
                or query.get("X-Amz-Signature") != upload.token
                or "Authorization" in self.headers
            ):
                return (403, {}, b"")
            if len(body) != upload.size_bytes:
                return (400, {}, b"")
            upload.received = body
            return (200, {}, b"")
        if kind == "outputs" and self.command == "GET":
            run = sim.runs.get(object_id)
            if run is None or query.get("X-Amz-Signature") != run.upload.token:
                return (403, {}, b"")
            return (200, {"Content-Type": "application/x-ndjson"}, run.output)
        return (404, {}, b"")


def serve(
    port: int,
    key: str,
    stages: tuple[tuple[str, float], ...] = STAGES,
    poll_after_ms: int = POLL_AFTER_MS,
    first_cut: bool = False,
) -> ThreadingHTTPServer:
    """Start the simulation on 127.0.0.1 in a background thread and return the server."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    simulation = Simulation(
        f"http://127.0.0.1:{server.server_address[1]}", key, stages, poll_after_ms, first_cut
    )
    server.RequestHandlerClass = type("BoundHandler", (Handler,), {"simulation": simulation})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    """Run the simulation until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--key", default=DEFAULT_KEY)
    parser.add_argument(
        "--first-cut", action="store_true", help="answer 501 everywhere except GET /v1/models"
    )
    args = parser.parse_args()
    server = serve(args.port, args.key, first_cut=args.first_cut)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Fenon dev API (simulation, not the real service) on {base}/v1")
    print(f"  FENON_API_KEY={args.key}")
    print(f"  FENON_BASE_URL={base}/v1")
    print("  Upload a file named fail*.mp4 to simulate a failed run.")
    if args.first_cut:
        print("  First-cut mode: only GET /v1/models works; everything else answers 501 not_implemented.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
