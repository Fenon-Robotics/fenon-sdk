"""Tests for the ``fenon`` logger, the ``FENON_LOG`` switch and secret redaction."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx2
import pytest
from _support import ISOLATED, Recorder, error_response, json_response

from fenon._core import logs
from fenon._core._generated import OutputFile, UploadReservation
from fenon._core.errors import FenonError
from fenon._core.http import ApiResponse, ClientConfig, HttpClients
from fenon.models.pose.hamer._generated import AnnotationsFile

SECRET_KEY = "fenon_sentinel_secret_key"
SECRET_URL = "https://storage.example.test/obj?X-Amz-Signature=sentinel-signature"


@pytest.fixture
def restore_logger() -> Iterator[None]:
    level = logs.logger.level
    handlers = list(logs.logger.handlers)
    yield
    logs.logger.setLevel(level)
    logs.logger.handlers[:] = handlers


@pytest.mark.usefixtures("restore_logger")
class TestConfigureFromEnv:
    @pytest.mark.parametrize(
        ("value", "level"), [("info", logging.INFO), ("debug", logging.DEBUG), (" DEBUG ", logging.DEBUG)]
    )
    def test_known_values_set_the_level_and_add_one_handler(self, value: str, level: int) -> None:
        logs.configure_logging(value)
        logs.configure_logging(value)
        assert logs.logger.level == level
        stream_handlers = [h for h in logs.logger.handlers if isinstance(h, logging.StreamHandler)]
        assert len(stream_handlers) == 1

    @pytest.mark.parametrize("value", [None, "warning", ""])
    def test_other_values_change_nothing(self, value: str | None) -> None:
        before = (logs.logger.level, list(logs.logger.handlers))
        logs.configure_logging(value)
        assert (logs.logger.level, list(logs.logger.handlers)) == before

    def test_the_library_logger_has_a_null_handler(self) -> None:
        assert logs.LOGGER_NAME == "fenon"
        assert any(isinstance(h, logging.NullHandler) for h in logs.logger.handlers)


def test_no_secret_reaches_logs_errors_or_reprs(
    make_clients: Callable[..., HttpClients], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=logs.LOGGER_NAME)
    video = tmp_path / "private-clip.mp4"
    video.write_bytes(b"video-bytes")
    api = Recorder(
        [error_response(503, "unavailable"), json_response(200), error_response(401, "invalid_api_key")]
    )
    storage = Recorder([httpx2.Response(503), httpx2.Response(200, content=b"x"), httpx2.Response(403)])
    clients = make_clients(api=api, storage=storage, api_key=SECRET_KEY)
    captured: list[str] = []
    clients.api.request("POST", "/runs", json_body={"model": "secret-body-marker"})
    clients.storage.download(SECRET_URL)
    for call in (
        lambda: clients.api.request("GET", "/models"),
        lambda: clients.storage.upload_file(SECRET_URL, method="PUT", headers={}, path=video, size_bytes=11),
    ):
        with pytest.raises(FenonError) as caught:
            call()
        captured.extend([str(caught.value), repr(caught.value)])
    config = ClientConfig.resolve(
        api_key=SECRET_KEY, base_url=None, timeout=60, max_retries=2, settings=ISOLATED
    )
    captured.append(repr(config))
    everything = "\n".join([caplog.text, *captured])
    assert caplog.records
    for secret in (
        SECRET_KEY,
        "sentinel-signature",
        "storage.example.test",
        "secret-body-marker",
        str(video),
    ):
        assert secret not in everything


def test_reprs_never_show_bodies_or_presigned_urls() -> None:
    file = {"url": SECRET_URL, "url_expires_at": "2026-10-26T12:15:00Z", "size_bytes": 10}
    objects = [
        ApiResponse(200, {"upload_url": SECRET_URL}, "req_x", {"x-signed": SECRET_URL}),
        UploadReservation.model_validate(
            {
                "id": "upl_7k2mq9x4ab3d",
                "status": "pending",
                "upload_url": SECRET_URL,
                "method": "PUT",
                "headers": {"Content-Type": "video/mp4"},
                "expires_at": "2026-10-26T12:15:00Z",
            }
        ),
        OutputFile.model_validate({**file, "content_type": "application/x-ndjson"}),
        AnnotationsFile.model_validate({**file, "content_type": "application/x-ndjson"}),
    ]
    for value in objects:
        assert "sentinel-signature" not in repr(value)
        assert "sentinel-signature" not in str(value)
