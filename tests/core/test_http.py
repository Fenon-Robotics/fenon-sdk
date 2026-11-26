"""Tests for the configuration, the API client and the storage client."""

from __future__ import annotations

import io
import logging
import os
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx2
import pytest
from _support import TEST_API_KEY, FakeRuntime, Recorder, error_response, json_response

from fenon import __version__
from fenon._core import http
from fenon._core._generated import RunUsage as Usage
from fenon._core.config import DEFAULT_BASE_URL
from fenon._core.errors import (
    AuthError,
    FenonError,
    InvalidInputError,
    NetworkError,
    ServerError,
    StorageUrlRejectedError,
)
from fenon._core.http import (
    ApiResponse,
    ClientConfig,
    HttpClients,
    Runtime,
    measure_upload_file,
)
from fenon._core.settings import Settings

MakeClients = Callable[..., HttpClients]
STORAGE_URL = "https://storage.example.test/bucket/upl_7k2mq9x4ab3d?X-Amz-Signature=secret-signature"


def resolve(
    *,
    api_key: str | None = "fenon_explicit",
    base_url: str | None = None,
    timeout: object = 60,
    max_retries: object = 2,
    settings: Settings | None = None,
) -> ClientConfig:
    return ClientConfig.resolve(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
        settings=environment() if settings is None else settings,
    )


def environment(api_key: str | None = None, base_url: str = DEFAULT_BASE_URL) -> Settings:
    return Settings(api_key=api_key, base_url=base_url, log=None)


class TestClientConfig:
    def test_explicit_key_wins_over_the_environment(self) -> None:
        config = resolve(api_key="fenon_explicit", settings=environment(api_key="fenon_env"))
        assert config.api_key == "fenon_explicit"

    def test_settings_key_is_used_when_none_is_given(self) -> None:
        assert resolve(api_key=None, settings=environment(api_key="fenon_env")).api_key == "fenon_env"

    @pytest.mark.parametrize("settings_key", [None, ""])
    def test_missing_or_empty_key_resolves_to_none(self, settings_key: str | None) -> None:
        assert resolve(api_key=None, settings=environment(api_key=settings_key)).api_key is None
        assert resolve(api_key="", settings=environment(api_key=settings_key)).api_key is None

    def test_default_and_custom_base_urls(self) -> None:
        assert resolve().base_url == DEFAULT_BASE_URL
        assert resolve(base_url="https://example.test/api/v1/").base_url == "https://example.test/api/v1"
        settings = environment(base_url="https://staging.example.test/v1/")
        assert resolve(settings=settings).base_url == "https://staging.example.test/v1"
        assert (
            resolve(base_url="https://other.test/v1", settings=settings).base_url == "https://other.test/v1"
        )

    def test_real_environment_variables_are_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FENON_API_KEY", "fenon_from_env")
        monkeypatch.setenv("FENON_BASE_URL", "https://env.example.test/v1")
        config = ClientConfig.resolve(api_key=None, base_url=None, timeout=60, max_retries=2)
        assert (config.api_key, config.base_url) == ("fenon_from_env", "https://env.example.test/v1")

    @pytest.mark.parametrize(
        "base_url",
        [
            "",
            "api.fenon.ai/v1",
            "ftp://api.fenon.ai",
            "https://",
            "not a url",
            "https://exa mple.test/\x00",
            "https://user:hunter2@api.fenon.ai/v1",
            "https://api.fenon.ai/v1?token=hunter2",
            "https://api.fenon.ai/v1#hunter2",
        ],
    )
    def test_invalid_base_url_is_rejected(self, base_url: str) -> None:
        with pytest.raises(InvalidInputError) as caught:
            resolve(base_url=base_url)
        assert caught.value.code == "invalid_argument"
        assert "hunter2" not in str(caught.value)

    @pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "60", None])
    def test_invalid_timeout_is_rejected(self, timeout: object) -> None:
        with pytest.raises(InvalidInputError) as caught:
            resolve(timeout=timeout)
        assert caught.value.code == "invalid_argument"

    @pytest.mark.parametrize("max_retries", [-1, 1.5, True, "2", None])
    def test_invalid_max_retries_is_rejected(self, max_retries: object) -> None:
        with pytest.raises(InvalidInputError) as caught:
            resolve(max_retries=max_retries)
        assert caught.value.code == "invalid_argument"

    @pytest.mark.parametrize(
        "api_key", ["fenon_key\n", " fenon_key", "fenon key", "fenon_k\u00e9y", "fenon\x00key"]
    )
    def test_key_that_cant_be_sent_in_a_header_is_rejected(self, api_key: str) -> None:
        for call in (
            lambda: resolve(api_key=api_key),
            lambda: resolve(api_key=None, settings=environment(api_key=api_key)),
        ):
            with pytest.raises(InvalidInputError) as caught:
                call()
            assert caught.value.code == "invalid_argument"
            assert api_key not in str(caught.value)

    def test_repr_never_shows_the_key(self) -> None:
        text = repr(resolve(api_key="fenon_very_secret_key"))
        assert "fenon_very_secret_key" not in text
        assert "base_url" in text


def test_default_runtime_issues_random_uuid_keys_and_utc_time() -> None:
    runtime = Runtime()
    first, second = runtime.new_idempotency_key(), runtime.new_idempotency_key()
    assert uuid.UUID(first).version == 4
    assert first != second
    assert runtime.now().tzinfo is UTC


class TestApiClient:
    def test_successful_call_returns_body_request_id_and_headers(self, make_clients: MakeClients) -> None:
        recorder = Recorder(
            [json_response(200, {"id": "run_q3w8e1r5t2y6", "extra": 1}, {"X-Request-Id": "req_a"})]
        )
        response = make_clients(api=recorder).api.request("GET", "/runs/run_q3w8e1r5t2y6")
        assert response.status_code == 200
        assert response.body == {"id": "run_q3w8e1r5t2y6", "extra": 1}
        assert response.request_id == "req_a"
        assert response.headers["x-request-id"] == "req_a"

    def test_requests_carry_auth_user_agent_and_accept(self, make_clients: MakeClients) -> None:
        recorder = Recorder([json_response(200)])
        make_clients(api=recorder).api.request("GET", "/models")
        headers = recorder.requests[0].headers
        assert headers["authorization"] == f"Bearer {TEST_API_KEY}"
        assert headers["user-agent"] == f"fenon-python/{__version__}"
        assert headers["accept"] == "application/json"
        assert "idempotency-key" not in headers

    @pytest.mark.parametrize(
        ("base_url", "expected_url"),
        [
            (None, "https://api.fenon.ai/v1/uploads"),
            ("https://example.test/api/v1/", "https://example.test/api/v1/uploads"),
        ],
    )
    def test_paths_join_the_base_url(
        self, make_clients: MakeClients, base_url: str | None, expected_url: str
    ) -> None:
        recorder = Recorder([json_response(201)])
        make_clients(api=recorder, base_url=base_url).api.request("POST", "/uploads", json_body={})
        assert str(recorder.requests[0].url) == expected_url

    def test_post_reuses_one_idempotency_key_across_retries(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime
    ) -> None:
        recorder = Recorder(
            [error_response(503, "unavailable"), error_response(503, "unavailable"), json_response(201)]
        )
        clients = make_clients(api=recorder)
        clients.api.request("POST", "/runs", json_body={"model": "fenon/hand-pose-hm"})
        keys = {request.headers["idempotency-key"] for request in recorder.requests}
        assert keys == {"idempotency-key-1"}
        assert len(recorder.requests) == 3
        clients.api.request("POST", "/runs", json_body={"model": "fenon/hand-pose-hm"})
        assert recorder.requests[-1].headers["idempotency-key"] == "idempotency-key-2"
        assert fake_runtime.keys_issued == 2

    def test_post_body_is_sent_as_json(self, make_clients: MakeClients) -> None:
        recorder = Recorder([json_response(202)])
        make_clients(api=recorder).api.request(
            "POST", "/runs", json_body={"model": "m", "input": {"video": "u"}}
        )
        assert recorder.requests[0].headers["content-type"] == "application/json"
        assert recorder.bodies[0] == b'{"model":"m","input":{"video":"u"}}'

    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
    def test_retryable_statuses_are_retried_until_attempts_run_out(
        self, make_clients: MakeClients, status: int
    ) -> None:
        recorder = Recorder([error_response(status, "temporary", retryable=True)])
        with pytest.raises(FenonError) as caught:
            make_clients(api=recorder).api.request("GET", "/models")
        assert len(recorder.requests) == 3
        assert caught.value.status_code == status

    def test_retryable_status_then_success_returns_the_success(self, make_clients: MakeClients) -> None:
        recorder = Recorder([error_response(500, "internal_error"), json_response(200, {"ok": True})])
        assert make_clients(api=recorder).api.request("GET", "/models").body == {"ok": True}
        assert len(recorder.requests) == 2

    @pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 405, 413, 415, 422, 501, 600])
    def test_other_statuses_are_never_retried(self, make_clients: MakeClients, status: int) -> None:
        recorder = Recorder([error_response(status, "refused")])
        with pytest.raises(FenonError) as caught:
            make_clients(api=recorder).api.request("POST", "/runs", json_body={})
        assert len(recorder.requests) == 1
        assert caught.value.status_code == status

    def test_not_implemented_is_a_server_error_sent_once(self, make_clients: MakeClients) -> None:
        recorder = Recorder([error_response(501, "not_implemented", details={"port": "key_verifier"})])
        with pytest.raises(ServerError) as caught:
            make_clients(api=recorder).api.request("GET", "/models")
        assert len(recorder.requests) == 1
        assert caught.value.code == "not_implemented"
        assert caught.value.details == {"port": "key_verifier"}

    def test_idempotency_in_progress_is_retried_but_conflict_is_not(self, make_clients: MakeClients) -> None:
        in_progress = Recorder(
            [error_response(409, "idempotency_in_progress", retryable=True), json_response(202)]
        )
        make_clients(api=in_progress).api.request("POST", "/runs", json_body={})
        assert len(in_progress.requests) == 2
        conflict = Recorder([error_response(409, "idempotency_conflict")])
        with pytest.raises(ServerError) as caught:
            make_clients(api=conflict).api.request("POST", "/runs", json_body={})
        assert len(conflict.requests) == 1
        assert caught.value.code == "idempotency_conflict"

    @pytest.mark.parametrize(
        ("status", "retryable", "attempts"),
        [(503, False, 1), (500, False, 1), (429, False, 1), (409, True, 3), (422, True, 3)],
    )
    def test_the_error_body_decides_whether_to_retry(
        self, make_clients: MakeClients, status: int, retryable: bool, attempts: int
    ) -> None:
        recorder = Recorder([error_response(status, "signalled", retryable=retryable)])
        with pytest.raises(FenonError) as caught:
            make_clients(api=recorder).api.request("POST", "/runs", json_body={})
        assert len(recorder.requests) == attempts
        assert caught.value.retryable is retryable

    @pytest.mark.parametrize(
        ("status", "attempts"),
        [(408, 3), (429, 3), (500, 3), (502, 3), (503, 3), (504, 3), (400, 1), (409, 1), (501, 1), (505, 1)],
    )
    @pytest.mark.parametrize(
        "body",
        [
            b"<html>Bad Gateway</html>",
            b"",
            b'{"error": {"code": "unavailable", "message": "Down."}}',
            b'{"error": {"code": "unavailable", "message": "Down.", "retryable": "yes"}}',
        ],
    )
    def test_a_response_without_a_retryable_flag_follows_the_status_rule(
        self, make_clients: MakeClients, status: int, attempts: int, body: bytes
    ) -> None:
        recorder = Recorder([httpx2.Response(status, content=body)])
        with pytest.raises(FenonError) as caught:
            make_clients(api=recorder).api.request("POST", "/runs", json_body={})
        assert len(recorder.requests) == attempts
        assert caught.value.retryable is (attempts > 1)

    def test_zero_retries_means_one_attempt(self, make_clients: MakeClients) -> None:
        recorder = Recorder([error_response(503, "unavailable")])
        with pytest.raises(ServerError):
            make_clients(api=recorder, max_retries=0).api.request("GET", "/models")
        assert len(recorder.requests) == 1

    @pytest.mark.parametrize(
        ("random_value", "expected_sleeps"),
        [(1.0, [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]), (0.5, [0.25, 0.5, 1.0, 2.0, 4.0, 4.0]), (0.0, [0.0] * 6)],
    )
    def test_backoff_is_jittered_and_capped(
        self,
        make_clients: MakeClients,
        fake_runtime: FakeRuntime,
        random_value: float,
        expected_sleeps: list[float],
    ) -> None:
        fake_runtime.random_value = random_value
        recorder = Recorder([error_response(503, "unavailable")])
        with pytest.raises(ServerError):
            make_clients(api=recorder, max_retries=6).api.request("GET", "/models")
        assert fake_runtime.sleeps == expected_sleeps

    @pytest.mark.parametrize(
        ("headers", "body_hint", "expected_wait"),
        [
            ({"retry-after-ms": "1500"}, None, 1.5),
            ({"retry-after-ms": "1500", "retry-after": "9"}, 7, 1.5),
            ({"retry-after": "3"}, 7, 3.0),
            ({}, 7, 7.0),
            ({"retry-after-ms": "soon", "retry-after": "nan"}, None, 0.25),
            ({"retry-after-ms": "-5"}, None, 0.25),
        ],
    )
    def test_server_retry_hints_take_precedence_over_backoff(
        self,
        make_clients: MakeClients,
        fake_runtime: FakeRuntime,
        headers: dict[str, str],
        body_hint: float | None,
        expected_wait: float,
    ) -> None:
        first = httpx2.Response(
            429,
            json={"error": {"code": "rate_limited", "message": "Slow down.", "retry_after_s": body_hint}},
            headers=headers,
        )
        recorder = Recorder([first, json_response(200)])
        make_clients(api=recorder).api.request("GET", "/models")
        assert fake_runtime.sleeps == [expected_wait]

    @pytest.mark.parametrize("hint", [b"NaN", b"Infinity", b"-Infinity", b"1e309"])
    def test_non_finite_body_hints_fall_back_to_backoff(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime, hint: bytes
    ) -> None:
        first = httpx2.Response(
            429, content=b'{"error": {"code": "rate_limited", "retry_after_s": ' + hint + b"}}"
        )
        make_clients(api=Recorder([first, json_response(200)])).api.request("GET", "/models")
        assert fake_runtime.sleeps == [0.25]

    def test_retry_after_http_date_is_relative_to_now(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime
    ) -> None:
        later = format_datetime(fake_runtime.now_value + timedelta(seconds=10), usegmt=True)
        earlier = format_datetime(fake_runtime.now_value - timedelta(seconds=10), usegmt=True)
        recorder = Recorder(
            [
                httpx2.Response(503, headers={"retry-after": later}),
                httpx2.Response(503, headers={"retry-after": earlier}),
                json_response(200),
            ]
        )
        make_clients(api=recorder).api.request("GET", "/models")
        assert fake_runtime.sleeps == [10.0, 0.0]

    def test_hint_longer_than_the_ceiling_stops_retrying(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime
    ) -> None:
        recorder = Recorder([httpx2.Response(429, headers={"retry-after": "121"})])
        with pytest.raises(FenonError) as caught:
            make_clients(api=recorder).api.request("GET", "/models")
        assert len(recorder.requests) == 1
        assert caught.value.retry_after_s == 121.0
        assert fake_runtime.sleeps == []

    def test_network_errors_are_retried_then_raised_without_details(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime
    ) -> None:
        recorder = Recorder([httpx2.ConnectError("connection refused")])
        with pytest.raises(NetworkError) as caught:
            make_clients(api=recorder).api.request("GET", "/models")
        assert len(recorder.requests) == 3
        assert caught.value.code == "network_error"
        assert caught.value.retryable is True
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert len(fake_runtime.sleeps) == 2

    def test_network_error_then_success_returns_the_success(self, make_clients: MakeClients) -> None:
        recorder = Recorder([httpx2.ReadTimeout("timed out"), json_response(200, {"ok": 1})])
        assert make_clients(api=recorder).api.request("GET", "/models").body == {"ok": 1}

    @pytest.mark.parametrize(
        "content",
        [
            b"not json",
            b"[1, 2]",
            b"",
            b'{"a": NaN}',
            pytest.param(b"[" * 100_000 + b"]" * 100_000, id="deeply-nested-json"),
        ],
    )
    def test_success_without_a_json_object_is_invalid_response(
        self, make_clients: MakeClients, content: bytes
    ) -> None:
        recorder = Recorder([httpx2.Response(200, content=content, headers={"x-request-id": "req_b"})])
        with pytest.raises(ServerError) as caught:
            make_clients(api=recorder).api.request("GET", "/models")
        assert caught.value.code == "invalid_response"
        assert caught.value.request_id == "req_b"

    def test_redirects_are_not_followed(self, make_clients: MakeClients) -> None:
        recorder = Recorder([httpx2.Response(302, headers={"location": "https://elsewhere.example.test/"})])
        with pytest.raises(ServerError) as caught:
            make_clients(api=recorder).api.request("GET", "/models")
        assert len(recorder.requests) == 1
        assert caught.value.status_code == 302

    def test_undecodable_response_is_invalid_response_sent_once(self, make_clients: MakeClients) -> None:
        garbled = httpx2.Response(
            200, stream=httpx2.ByteStream(b"not gzip"), headers={"content-encoding": "gzip"}
        )
        recorder = Recorder([garbled])
        with pytest.raises(ServerError) as caught:
            make_clients(api=recorder).api.request("POST", "/runs", json_body={})
        assert len(recorder.requests) == 1
        assert caught.value.code == "invalid_response"
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    @pytest.mark.parametrize(
        "error", [httpx2.LocalProtocolError("illegal header"), httpx2.UnsupportedProtocol("no scheme")]
    )
    def test_request_that_cant_be_sent_is_not_retried(
        self, make_clients: MakeClients, error: Exception
    ) -> None:
        recorder = Recorder([error])
        with pytest.raises(ServerError) as caught:
            make_clients(api=recorder).api.request("GET", "/models")
        assert len(recorder.requests) == 1
        assert caught.value.code == "invalid_response"
        assert caught.value.__context__ is None

    @pytest.mark.parametrize(
        "path",
        [
            "https://attacker.example.test/steal",
            "http://api.fenon.ai/v1/models",
            "https://api.fenon.ai:8443/v1/models",
            "https://exa mple.test/\x00",
        ],
    )
    def test_requests_off_the_api_origin_are_refused_before_sending(
        self, make_clients: MakeClients, path: str
    ) -> None:
        recorder = Recorder([json_response(200)])
        with pytest.raises(InvalidInputError) as caught:
            make_clients(api=recorder).api.request("GET", path)
        assert caught.value.code == "invalid_argument"
        assert recorder.requests == []

    def test_network_path_reference_stays_on_the_api_host(self, make_clients: MakeClients) -> None:
        recorder = Recorder([json_response(200)])
        make_clients(api=recorder).api.request("GET", "//attacker.example.test/steal")
        assert recorder.requests[0].url.host == "api.fenon.ai"

    def test_missing_key_fails_before_any_request(self, make_clients: MakeClients) -> None:
        recorder = Recorder([json_response(200)])
        with pytest.raises(AuthError) as caught:
            make_clients(api=recorder, api_key=None).api.request("GET", "/models")
        assert caught.value.code == "missing_api_key"
        assert recorder.requests == []

    def test_error_fields_come_from_the_envelope_and_headers(self, make_clients: MakeClients) -> None:
        response = httpx2.Response(
            422,
            json={
                "error": {
                    "code": "input_invalid",
                    "message": "Bad input.",
                    "details": {"field": "/input/video"},
                }
            },
            headers={"x-request-id": "req_c"},
        )
        with pytest.raises(InvalidInputError) as caught:
            make_clients(api=Recorder([response])).api.request("POST", "/runs", json_body={})
        assert (caught.value.code, caught.value.request_id) == ("input_invalid", "req_c")
        assert caught.value.details == {"field": "/input/video"}


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4)
    return path


def reserved_headers(path: Path) -> dict[str, str]:
    return {"Content-Type": "video/mp4", "Content-Length": str(path.stat().st_size)}


class TestStorageClient:
    def test_upload_sends_exactly_the_file_and_no_fenon_headers(
        self, make_clients: MakeClients, video: Path
    ) -> None:
        recorder = Recorder([httpx2.Response(200)])
        make_clients(storage=recorder).storage.upload_file(
            STORAGE_URL,
            method="PUT",
            headers=reserved_headers(video),
            path=video,
            size_bytes=video.stat().st_size,
        )
        request = recorder.requests[0]
        assert request.method == "PUT"
        assert str(request.url) == STORAGE_URL
        assert recorder.bodies[0] == video.read_bytes()
        assert request.headers["content-type"] == "video/mp4"
        assert request.headers["content-length"] == str(video.stat().st_size)
        assert "authorization" not in request.headers
        assert "idempotency-key" not in request.headers
        assert "fenon" not in request.headers.get("user-agent", "")

    def test_upload_rereads_the_file_on_every_attempt(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime, video: Path
    ) -> None:
        recorder = Recorder([httpx2.Response(503), httpx2.Response(500), httpx2.Response(200)])
        make_clients(storage=recorder).storage.upload_file(
            STORAGE_URL,
            method="PUT",
            headers=reserved_headers(video),
            path=video,
            size_bytes=video.stat().st_size,
        )
        assert recorder.bodies == [video.read_bytes()] * 3
        assert fake_runtime.sleeps == [1.0, 2.0]

    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
    def test_retryable_statuses_get_five_attempts(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime, video: Path, status: int
    ) -> None:
        recorder = Recorder([httpx2.Response(status)])
        with pytest.raises(ServerError) as caught:
            make_clients(storage=recorder).storage.upload_file(
                STORAGE_URL,
                method="PUT",
                headers=reserved_headers(video),
                path=video,
                size_bytes=video.stat().st_size,
            )
        assert len(recorder.requests) == 5
        assert fake_runtime.sleeps == [1.0, 2.0, 4.0, 8.0]
        assert (caught.value.code, caught.value.status_code, caught.value.retryable) == (
            "storage_error",
            status,
            True,
        )

    def test_forbidden_url_is_rejected_at_once(self, make_clients: MakeClients, video: Path) -> None:
        recorder = Recorder([httpx2.Response(403)])
        with pytest.raises(StorageUrlRejectedError) as caught:
            make_clients(storage=recorder).storage.upload_file(
                STORAGE_URL,
                method="PUT",
                headers=reserved_headers(video),
                path=video,
                size_bytes=video.stat().st_size,
            )
        assert len(recorder.requests) == 1
        assert caught.value.code == "storage_url_rejected"

    @pytest.mark.parametrize("status", [400, 401, 404, 409, 410, 413, 501, 505, 600])
    def test_other_statuses_are_not_retried(
        self, make_clients: MakeClients, video: Path, status: int
    ) -> None:
        recorder = Recorder([httpx2.Response(status)])
        with pytest.raises(ServerError) as caught:
            make_clients(storage=recorder).storage.upload_file(
                STORAGE_URL,
                method="PUT",
                headers=reserved_headers(video),
                path=video,
                size_bytes=video.stat().st_size,
            )
        assert len(recorder.requests) == 1
        assert (caught.value.code, caught.value.status_code, caught.value.retryable) == (
            "storage_error",
            status,
            False,
        )

    @pytest.mark.parametrize(
        ("url", "headers"),
        [
            ("https://exa mple.test/\x00", {}),
            ("ftp://storage.example.test/o?X-Amz-Signature=secret-signature", {}),
            ("/o?X-Amz-Signature=secret-signature", {}),
            ("https:///o?X-Amz-Signature=secret-signature", {}),
            (STORAGE_URL, {"Content-Type": "vid\u00e9o/mp4"}),
        ],
    )
    def test_unusable_url_or_headers_are_refused_before_sending(
        self, make_clients: MakeClients, video: Path, url: str, headers: dict[str, str]
    ) -> None:
        recorder = Recorder([httpx2.Response(200)])
        storage = make_clients(storage=recorder).storage
        calls: list[Callable[[], object]] = [
            lambda: storage.upload_file(url, method="PUT", headers=headers, path=video, size_bytes=1)
        ]
        if not headers:
            calls.append(lambda: storage.download(url))
        for call in calls:
            with pytest.raises(ServerError) as caught:
                call()
            assert caught.value.code == "invalid_response"
            assert caught.value.__context__ is None
            assert "secret-signature" not in str(caught.value)
        assert recorder.requests == []

    @pytest.mark.parametrize(
        "outcome",
        [
            httpx2.Response(200, stream=httpx2.ByteStream(b"not gzip"), headers={"content-encoding": "gzip"}),
            httpx2.LocalProtocolError("illegal header"),
        ],
    )
    def test_transfer_that_cant_complete_is_not_retried(
        self, make_clients: MakeClients, outcome: httpx2.Response | Exception
    ) -> None:
        recorder = Recorder([outcome])
        with pytest.raises(ServerError) as caught:
            make_clients(storage=recorder).storage.download(STORAGE_URL)
        assert len(recorder.requests) == 1
        assert caught.value.code == "storage_error"
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert "secret-signature" not in str(caught.value)

    def test_network_failures_never_expose_the_url(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime, video: Path
    ) -> None:
        recorder = Recorder([httpx2.ConnectError(f"cannot connect to {STORAGE_URL}")])
        with pytest.raises(NetworkError) as caught:
            make_clients(storage=recorder).storage.upload_file(
                STORAGE_URL,
                method="PUT",
                headers=reserved_headers(video),
                path=video,
                size_bytes=video.stat().st_size,
            )
        assert len(recorder.requests) == 5
        assert fake_runtime.sleeps == [1.0, 2.0, 4.0, 8.0]
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert "secret-signature" not in str(caught.value)

    def test_changed_file_is_rejected_before_sending(self, make_clients: MakeClients, video: Path) -> None:
        recorder = Recorder([httpx2.Response(200)])
        with pytest.raises(InvalidInputError) as caught:
            make_clients(storage=recorder).storage.upload_file(
                STORAGE_URL, method="PUT", headers=reserved_headers(video), path=video, size_bytes=1
            )
        assert caught.value.code == "file_changed"
        assert recorder.requests == []

    def test_missing_file_is_reported_without_its_path(
        self, make_clients: MakeClients, tmp_path: Path
    ) -> None:
        missing = tmp_path / "gone.mp4"
        with pytest.raises(InvalidInputError) as caught:
            make_clients(storage=Recorder([httpx2.Response(200)])).storage.upload_file(
                STORAGE_URL, method="PUT", headers={}, path=missing, size_bytes=10
            )
        assert caught.value.code == "file_not_found"
        assert caught.value.__context__ is None
        assert str(tmp_path) not in str(caught.value)

    def test_a_directory_is_not_a_file(self, make_clients: MakeClients, tmp_path: Path) -> None:
        recorder = Recorder([httpx2.Response(200)])
        with pytest.raises(InvalidInputError) as caught:
            make_clients(storage=recorder).storage.upload_file(
                STORAGE_URL, method="PUT", headers={}, path=tmp_path, size_bytes=10
            )
        assert caught.value.code == "file_not_found"
        assert caught.value.__context__ is None
        assert recorder.requests == []

    def test_unreadable_file_is_reported(self, make_clients: MakeClients, video: Path) -> None:
        video.chmod(0)
        try:
            if os.access(video, os.R_OK):
                pytest.skip("this platform or user can read a file with no permissions")
            with pytest.raises(InvalidInputError) as caught:
                make_clients(storage=Recorder([httpx2.Response(200)])).storage.upload_file(
                    STORAGE_URL, method="PUT", headers={}, path=video, size_bytes=video.stat().st_size
                )
            assert caught.value.code == "file_unreadable"
            assert caught.value.__context__ is None
        finally:
            video.chmod(0o644)

    def test_download_returns_the_bytes(self, make_clients: MakeClients, fake_runtime: FakeRuntime) -> None:
        recorder = Recorder([httpx2.Response(502), httpx2.Response(200, content=b'{"frame_index": 0}\n')])
        content = make_clients(storage=recorder).storage.download(STORAGE_URL)
        assert content == b'{"frame_index": 0}\n'
        assert [request.method for request in recorder.requests] == ["GET", "GET"]
        assert "authorization" not in recorder.requests[0].headers
        assert fake_runtime.sleeps == [1.0]

    def test_download_with_a_rejected_url(self, make_clients: MakeClients) -> None:
        with pytest.raises(StorageUrlRejectedError):
            make_clients(storage=Recorder([httpx2.Response(403)])).storage.download(STORAGE_URL)

    def test_download_to_streams_into_the_file_and_restarts_after_a_dropped_body(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime
    ) -> None:
        body = bytes(range(256)) * 8
        recorder = Recorder(
            [
                httpx2.Response(200, stream=DroppedBody(body[:300])),
                httpx2.Response(200, content=body),
            ]
        )
        destination = io.BytesIO(b"stale bytes that must not survive")
        written = make_clients(storage=recorder).storage.download_to(STORAGE_URL, destination)
        assert (written, destination.getvalue()) == (len(body), body)
        assert len(recorder.requests) == 2
        assert fake_runtime.sleeps == [1.0]

    @pytest.mark.parametrize("status", [403, 404])
    def test_download_to_with_a_refused_or_missing_object(
        self, make_clients: MakeClients, status: int
    ) -> None:
        with pytest.raises(StorageUrlRejectedError):
            make_clients(storage=Recorder([httpx2.Response(status)])).storage.download_to(
                STORAGE_URL, io.BytesIO()
            )

    def test_an_upload_answered_404_is_a_storage_error_not_a_refusal(
        self, make_clients: MakeClients, video: Path
    ) -> None:
        with pytest.raises(ServerError) as caught:
            make_clients(storage=Recorder([httpx2.Response(404)])).storage.upload_file(
                STORAGE_URL,
                method="PUT",
                headers=reserved_headers(video),
                path=video,
                size_bytes=video.stat().st_size,
            )
        assert not isinstance(caught.value, StorageUrlRejectedError)
        assert caught.value.code == "storage_error"

    def test_storage_never_receives_the_api_key(self, make_clients: MakeClients, video: Path) -> None:
        recorder = Recorder([httpx2.Response(200, content=b"x")])
        clients = make_clients(storage=recorder)
        clients.storage.download(STORAGE_URL)
        clients.storage.upload_file(
            STORAGE_URL,
            method="PUT",
            headers=reserved_headers(video),
            path=video,
            size_bytes=video.stat().st_size,
        )
        for request in recorder.requests:
            assert "authorization" not in request.headers
            assert all(TEST_API_KEY not in value for _, value in request.headers.multi_items())


class TestMeasureUploadFile:
    def test_returns_the_size(self, video: Path) -> None:
        assert measure_upload_file(video) == video.stat().st_size

    def test_missing_path_and_directory_are_not_found(self, tmp_path: Path) -> None:
        for path in (tmp_path / "gone.mp4", tmp_path, tmp_path / "gone" / "deeper.mp4"):
            with pytest.raises(InvalidInputError) as caught:
                measure_upload_file(path)
            assert caught.value.code == "file_not_found"
            assert caught.value.__context__ is None
            assert str(tmp_path) not in str(caught.value)

    def test_a_file_used_as_a_directory_is_not_found(self, video: Path) -> None:
        with pytest.raises(InvalidInputError) as caught:
            measure_upload_file(video / "inside.mp4")
        assert caught.value.code == "file_not_found"

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes need POSIX")
    def test_a_named_pipe_is_never_opened(self, tmp_path: Path) -> None:
        pipe = tmp_path / "pipe.mp4"
        os.mkfifo(pipe)
        with pytest.raises(InvalidInputError) as caught:
            measure_upload_file(pipe)
        assert caught.value.code == "file_not_found"

    def test_an_unreachable_parent_is_unreadable(self, tmp_path: Path) -> None:
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "clip.mp4").write_bytes(b"x")
        locked.chmod(0)
        try:
            if os.access(locked, os.X_OK):
                pytest.skip("this platform or user can enter a directory with no permissions")
            with pytest.raises(InvalidInputError) as caught:
                measure_upload_file(locked / "clip.mp4")
            assert caught.value.code == "file_unreadable"
        finally:
            locked.chmod(0o755)


class TestPerCallSettings:
    def test_max_retries_and_timeout_override_the_configuration(self, make_clients: MakeClients) -> None:
        recorder = Recorder([error_response(503, "unavailable")])
        with pytest.raises(ServerError):
            make_clients(api=recorder, max_retries=2).api.request(
                "POST", "/runs", max_retries=0, timeout_s=5.0
            )
        assert len(recorder.requests) == 1
        assert recorder.requests[0].extensions["timeout"] == {
            "connect": 5.0,
            "read": 5.0,
            "write": 5.0,
            "pool": 5.0,
        }

    def test_the_configured_timeout_applies_by_default(self, make_clients: MakeClients) -> None:
        recorder = Recorder([json_response(200)])
        make_clients(api=recorder).api.request("GET", "/models")
        assert recorder.requests[0].extensions["timeout"] == {
            "connect": 10.0,
            "read": 60.0,
            "write": 60.0,
            "pool": 60.0,
        }

    def test_clients_expose_the_runtime_they_use(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime
    ) -> None:
        clients = make_clients()
        clients.runtime.sleep(1.5)
        assert fake_runtime.sleeps == [1.5]


class TestApiResponseParse:
    def test_valid_body_becomes_the_model(self) -> None:
        response = ApiResponse(200, {"unit": "frame", "quantity": 3}, "req_x", {})
        assert response.parse(Usage) == Usage(unit="frame", quantity=3)

    def test_invalid_body_is_invalid_response_without_pydantic_text(self) -> None:
        body = {"unit": "https://s.test/o?X-Amz-Signature=sig-in-body", "quantity": -1}
        with pytest.raises(ServerError) as caught:
            ApiResponse(201, body, "req_x", {}).parse(Usage)
        assert (caught.value.code, caught.value.request_id, caught.value.status_code) == (
            "invalid_response",
            "req_x",
            201,
        )
        assert caught.value.__context__ is None
        assert "sig-in-body" not in str(caught.value)


def test_a_file_removed_between_the_check_and_the_open_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = tmp_path / "gone.mp4"
    monkeypatch.setattr(http, "_regular_file_problem", lambda path: None)
    with pytest.raises(InvalidInputError) as caught:
        measure_upload_file(gone)
    assert caught.value.code == "file_not_found"
    assert caught.value.__context__ is None
    assert str(gone) not in str(caught.value)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes need POSIX")
def test_a_file_swapped_for_a_pipe_after_the_check_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipe = tmp_path / "swapped.mp4"
    os.mkfifo(pipe)
    monkeypatch.setattr(http, "_regular_file_problem", lambda path: None)
    codes: list[str | None] = []

    def measure() -> None:
        try:
            measure_upload_file(pipe)
        except InvalidInputError as error:
            codes.append(error.code)

    worker = threading.Thread(target=measure, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert codes == ["file_not_found"]


def test_request_ids_are_logged_only_in_the_contract_form(
    make_clients: MakeClients, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="fenon")
    recorder = Recorder(
        [
            json_response(200, {}, {"X-Request-Id": "req_m4n7b2v9c1x5"}),
            json_response(200, {}, {"X-Request-Id": "https://s.test/o?X-Amz-Signature=id-leak"}),
        ]
    )
    clients = make_clients(api=recorder)
    clients.api.request("GET", "/models")
    second = clients.api.request("GET", "/models")
    assert "request=req_m4n7b2v9c1x5" in caplog.text
    assert "request=unrecognised" in caplog.text
    assert "id-leak" not in caplog.text
    assert second.request_id == "https://s.test/o?X-Amz-Signature=id-leak"


class DroppedBody(httpx2.SyncByteStream):
    def __init__(self, first_part: bytes) -> None:
        self._first_part = first_part

    def __iter__(self) -> Iterator[bytes]:
        yield self._first_part
        raise httpx2.ReadError("connection dropped mid-body")
