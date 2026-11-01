"""End-to-end tests of the upload and run services over real sockets, with the SDK's real clock and sleep."""

from __future__ import annotations

import _thread
import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from _support import CONTRACT_SHA256, ISOLATED, OUTPUT_BYTES, RUN_ID, UPLOAD_ID, reservation_body, run_body
from _wire import Reply, Wire, WireRequest, json_reply

from fenon._core._generated import Run
from fenon._core.errors import RunFailedError, UnsupportedModelError
from fenon._core.http import ClientConfig, HttpClients, open_http_clients
from fenon._core.runs import ContractSupport, Job, RunOutput, create_run
from fenon._core.uploads import upload_file

API_KEY = "fenon_e2e_key_0123456789"
SUPPORT = ContractSupport("fenon.test.contract", {"1.0.0": CONTRACT_SHA256})
RUN_PATH = f"/v1/runs/{RUN_ID}"
CANCEL_PATH = f"/v1/runs/{RUN_ID}/cancel"
SECOND_UPLOAD_ID = "upl_000000sec0nd"
POLL = {"Fenon-Poll-After-Ms": "250"}


class OutputJob(Job[bytes]):
    _supported = (SUPPORT,)

    def _make_result(self, run: Run) -> bytes:
        return RunOutput(self._clients, run, "annotations").content()


@pytest.fixture
def api() -> Iterator[Wire]:
    wire = Wire()
    yield wire
    wire.close()


@pytest.fixture
def storage() -> Iterator[Wire]:
    wire = Wire()
    yield wire
    wire.close()


@pytest.fixture
def clients(api: Wire, monkeypatch: pytest.MonkeyPatch) -> Iterator[HttpClients]:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    config = ClientConfig.resolve(
        api_key=API_KEY, base_url=f"{api.url}/v1", timeout=10, max_retries=2, settings=ISOLATED
    )
    built = open_http_clients(config)
    yield built
    built.close()


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(bytes(range(256)) * 4096)
    return path


def run_reply(status: str, poll: bool = True, **fields: object) -> Reply:
    code = 202 if status == "queued" else 200
    return json_reply(code, run_body(status, **fields), POLL if poll else None)


def succeeded_with(url: str) -> Reply:
    body = run_body("succeeded")
    body["outputs"]["annotations"]["url"] = url
    return json_reply(200, body)


def test_upload_run_poll_and_output_over_real_sockets(
    api: Wire, storage: Wire, clients: HttpClients, video: Path
) -> None:
    api.route(
        "POST",
        "/v1/uploads",
        json_reply(
            201,
            reservation_body(
                UPLOAD_ID, f"{storage.url}/bucket/first?X-Amz-Signature=s1", video.stat().st_size
            ),
        ),
        json_reply(
            201,
            reservation_body(
                SECOND_UPLOAD_ID, f"{storage.url}/bucket/second?X-Amz-Signature=s2", video.stat().st_size
            ),
        ),
    )
    storage.route("PUT", "/bucket/first", (403, {}, b""))
    storage.route("PUT", "/bucket/second", (200, {}, b""))
    api.route("POST", "/v1/runs", run_reply("queued"))
    api.route(
        "GET",
        RUN_PATH,
        run_reply("queued"),
        run_reply("processing_frames"),
        run_reply("processing_frames"),
        succeeded_with(f"{storage.url}/out/first?X-Amz-Signature=o1"),
        succeeded_with(f"{storage.url}/out/fresh?X-Amz-Signature=o2"),
    )
    storage.route("GET", "/out/first", (403, {}, b""))
    storage.route("GET", "/out/fresh", (200, {}, OUTPUT_BYTES))

    started = time.monotonic()
    upload_id = upload_file(clients, video, content_type="video/mp4")
    created = create_run(
        clients,
        model="fenon/test-model",
        run_input={"video": upload_id},
        outputs=["annotations"],
        supported=[SUPPORT],
    )
    output = OutputJob(clients, created, cancel_on_interrupt=True).result()
    elapsed = time.monotonic() - started

    assert upload_id == SECOND_UPLOAD_ID
    assert output == OUTPUT_BYTES
    assert elapsed >= 4 * 0.25
    puts = storage.received("PUT", "/bucket/second")
    assert puts[0].body == video.read_bytes()
    assert puts[0].headers["content-type"] == "video/mp4"
    posts = api.received("POST", "/v1/uploads")
    assert posts[0].headers["idempotency-key"] != posts[1].headers["idempotency-key"]
    assert json.loads(api.received("POST", "/v1/runs")[0].body) == {
        "model": "fenon/test-model",
        "input": {"video": SECOND_UPLOAD_ID},
        "outputs": ["annotations"],
    }
    assert len(api.received("GET", RUN_PATH)) == 5
    assert api.received("POST", CANCEL_PATH) == []
    assert all(r.headers["authorization"] == f"Bearer {API_KEY}" for r in api.requests)
    assert all("idempotency-key" in r.headers for r in api.requests if r.method == "POST")
    assert all("idempotency-key" not in r.headers for r in api.requests if r.method == "GET")
    assert all("authorization" not in r.headers for r in storage.requests)
    assert all(API_KEY not in value for r in storage.requests for value in r.headers.values())


def test_a_failed_run_raises_with_its_fields(api: Wire, clients: HttpClients) -> None:
    api.route("POST", "/v1/runs", run_reply("queued"))
    api.route("GET", RUN_PATH, run_reply("queued"), run_reply("failed", poll=False))
    job = OutputJob(
        clients, create_run(clients, model="m", run_input={}, outputs=["annotations"], supported=[SUPPORT])
    )
    with pytest.raises(RunFailedError) as caught:
        job.result()
    assert (caught.value.code, caught.value.stage) == ("input_unreadable", "processing_frames")
    assert caught.value.job is job


def test_an_unsupported_contract_is_cancelled_on_the_server(api: Wire, clients: HttpClients) -> None:
    other = {"id": "fenon.other.contract", "version": "1.0.0", "schema_sha256": "b" * 64}
    api.route("POST", "/v1/runs", run_reply("queued", contract=other))
    api.route("POST", CANCEL_PATH, run_reply("canceled", poll=False, contract=other))
    with pytest.raises(UnsupportedModelError):
        create_run(clients, model="m", run_input={}, outputs=["annotations"], supported=[SUPPORT])
    cancels = api.received("POST", CANCEL_PATH)
    assert len(cancels) == 1
    assert cancels[0].headers["idempotency-key"]


def test_ctrl_c_while_waiting_sends_exactly_one_cancel(api: Wire, clients: HttpClients) -> None:
    polls: list[WireRequest] = []

    def poll(request: WireRequest) -> Reply:
        polls.append(request)
        if len(polls) == 2:
            _thread.interrupt_main()
        return run_reply("queued")

    api.route("POST", "/v1/runs", run_reply("queued"))
    api.route("GET", RUN_PATH, poll)
    api.route("POST", CANCEL_PATH, run_reply("canceled", poll=False))
    job = OutputJob(
        clients,
        create_run(clients, model="m", run_input={}, outputs=["annotations"], supported=[SUPPORT]),
        cancel_on_interrupt=True,
    )
    with pytest.raises(KeyboardInterrupt):
        job.result()
    cancels = api.received("POST", CANCEL_PATH)
    assert len(cancels) == 1
    assert cancels[0].headers["authorization"] == f"Bearer {API_KEY}"
    assert cancels[0].headers["idempotency-key"]
    assert cancels[0].body == b""
