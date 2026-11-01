"""A real HTTP server on 127.0.0.1 for end-to-end tests: it replays scripted replies and records requests."""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from typing_extensions import override

Reply = tuple[int, dict[str, str], bytes]


@dataclass(frozen=True)
class WireRequest:
    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes


Responder = Reply | Callable[[WireRequest], Reply]


def json_reply(status: int, body: Any, headers: dict[str, str] | None = None) -> Reply:
    return status, {"Content-Type": "application/json", **(headers or {})}, json.dumps(body).encode()


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    @override
    def handle_error(self, request: Any, client_address: Any) -> None:
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


class Wire:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._routes: dict[tuple[str, str], list[Responder]] = {}
        self.requests: list[WireRequest] = []
        wire = self

        class Handler(BaseHTTPRequestHandler):
            def handle_any(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                path, _, query = self.path.partition("?")
                request = WireRequest(
                    self.command,
                    path,
                    query,
                    {name.lower(): value for name, value in self.headers.items()},
                    self.rfile.read(length) if length else b"",
                )
                status, headers, body = wire.reply(request)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self.handle_any()

            def do_POST(self) -> None:
                self.handle_any()

            def do_PUT(self) -> None:
                self.handle_any()

            @override
            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = _QuietServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def route(self, method: str, path: str, *replies: Responder) -> None:
        with self._lock:
            self._routes[(method, path)] = list(replies)

    def reply(self, request: WireRequest) -> Reply:
        with self._lock:
            self.requests.append(request)
            script = self._routes.get((request.method, request.path))
            if not script:
                return 599, {}, b"unrouted"
            responder = script.pop(0) if len(script) > 1 else script[0]
        return responder(request) if callable(responder) else responder

    def received(self, method: str, path: str) -> list[WireRequest]:
        with self._lock:
            return [r for r in self.requests if r.method == method and r.path == path]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
