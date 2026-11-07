"""Tests for the run service: creation and contract checks, parsing, polling, jobs, outputs and Ctrl-C."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
from _support import (
    CONTRACT_ID,
    CONTRACT_SHA256,
    OUTPUT_BYTES,
    RUN_ID,
    FakeRuntime,
    Router,
    error_response,
    json_response,
    run_body,
    run_response,
)

from fenon._core._generated import Run
from fenon._core.errors import (
    FenonError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    RunFailedError,
    ServerError,
    UnsupportedModelError,
)
from fenon._core.http import HttpClients
from fenon._core.runs import (
    ContractSupport,
    Job,
    RunOutput,
    RunSnapshot,
    cancel_run,
    create_run,
    get_run,
)

MakeClients = Callable[..., HttpClients]
SUPPORT = ContractSupport(CONTRACT_ID, {"1.0.0": CONTRACT_SHA256})
RUN_PATH = f"/v1/runs/{RUN_ID}"
CANCEL_PATH = f"/v1/runs/{RUN_ID}/cancel"
OUTPUT_PATH = "/out/annotations.jsonl"
FRESH_URL = "https://storage.example.test/fresh/annotations.jsonl?X-Amz-Signature=fresh-signature"


class SampleJob(Job[str]):
    _supported = (SUPPORT,)

    def _make_result(self, run: Run) -> str:
        return f"{run.id}:{run.status}"


def snapshot(status: str = "queued", poll_after_s: float | None = None, **fields: Any) -> RunSnapshot:
    return RunSnapshot(Run.model_validate(run_body(status, **fields)), poll_after_s)


def contract(
    version: str = "1.0.0", sha: str = CONTRACT_SHA256, contract_id: str = CONTRACT_ID
) -> dict[str, str]:
    return {"id": contract_id, "version": version, "schema_sha256": sha}


class TestCreateRun:
    def test_sends_exactly_model_input_and_outputs_and_reads_the_poll_hint(
        self, make_clients: MakeClients
    ) -> None:
        api = Router({("POST", "/v1/runs"): [run_response("queued", poll_after_ms=1500)]})
        created = create_run(
            make_clients(api=api),
            model="fenon/test-model",
            run_input={"video": "upl_7k2mq9x4ab3d"},
            outputs=("annotations", "video"),
            supported=[SUPPORT],
        )
        request = api.calls("POST", "/v1/runs")[0]
        assert json.loads(request.content) == {
            "model": "fenon/test-model",
            "input": {"video": "upl_7k2mq9x4ab3d"},
            "outputs": ["annotations", "video"],
        }
        assert request.headers["idempotency-key"]
        assert (created.run.id, created.run.status, created.poll_after_s) == (RUN_ID, "queued", 1.5)

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("0", 0.25),
            ("600000", 60.0),
            ("1500", 1.5),
            ("000001500", 1.5),
            ("60000", 60.0),
            ("99999", 60.0),
            ("9" * 5000, 60.0),
            ("0" * 5000, 0.25),
            ("abc", None),
            ("-5", None),
            ("²", None),
            (None, None),
        ],
    )
    def test_poll_hint_is_clamped_or_ignored(
        self, make_clients: MakeClients, header: str | None, expected: float | None
    ) -> None:
        headers = [] if header is None else [(b"Fenon-Poll-After-Ms", header.encode("latin-1"))]
        api = Router({("POST", "/v1/runs"): [httpx2.Response(202, json=run_body(), headers=headers)]})
        created = create_run(
            make_clients(api=api), model="m", run_input={}, outputs=["annotations"], supported=[SUPPORT]
        )
        assert created.poll_after_s == expected

    @pytest.mark.parametrize(
        "reported",
        [contract(contract_id="fenon.other.contract"), contract(version="2.0.0"), contract(version="0.9.0")],
    )
    def test_unsupported_contract_cancels_then_raises(
        self, make_clients: MakeClients, reported: dict[str, str]
    ) -> None:
        api = Router(
            {
                ("POST", "/v1/runs"): [run_response(contract=reported)],
                ("POST", CANCEL_PATH): [run_response("canceled", contract=reported)],
            }
        )
        with pytest.raises(UnsupportedModelError) as caught:
            create_run(
                make_clients(api=api), model="m", run_input={}, outputs=["annotations"], supported=[SUPPORT]
            )
        assert caught.value.code == "unsupported_contract"
        assert caught.value.details == {
            "run_id": RUN_ID,
            "contract_id": reported["id"],
            "contract_version": reported["version"],
        }
        assert [r.url.path for r in api.requests] == ["/v1/runs", CANCEL_PATH]
        assert api.calls("POST", CANCEL_PATH)[0].headers["idempotency-key"]

    def test_hash_mismatch_on_a_shipped_version_cancels_then_raises(self, make_clients: MakeClients) -> None:
        reported = contract(sha="b" * 64)
        api = Router(
            {
                ("POST", "/v1/runs"): [run_response(contract=reported)],
                ("POST", CANCEL_PATH): [run_response("canceled", contract=reported)],
            }
        )
        with pytest.raises(ServerError) as caught:
            create_run(
                make_clients(api=api), model="m", run_input={}, outputs=["annotations"], supported=[SUPPORT]
            )
        assert caught.value.code == "contract_mismatch"
        assert len(api.calls("POST", CANCEL_PATH)) == 1

    @pytest.mark.parametrize("version", ["1.1.0", "1.0.1", "1.7.3", "001.0.0"])
    def test_versions_the_sdk_doesnt_ship_are_refused_even_in_a_known_major(
        self, make_clients: MakeClients, version: str
    ) -> None:
        reported = contract(version=version, sha="c" * 64)
        api = Router(
            {
                ("POST", "/v1/runs"): [run_response(contract=reported)],
                ("POST", CANCEL_PATH): [run_response("canceled", contract=reported)],
            }
        )
        with pytest.raises(UnsupportedModelError) as caught:
            create_run(
                make_clients(api=api), model="m", run_input={}, outputs=["annotations"], supported=[SUPPORT]
            )
        assert caught.value.code == "unsupported_contract"
        assert len(api.calls("POST", CANCEL_PATH)) == 1

    def test_every_shipped_version_is_accepted(self, make_clients: MakeClients) -> None:
        both = ContractSupport(CONTRACT_ID, {"1.0.0": CONTRACT_SHA256, "1.1.0": "c" * 64})
        api = Router({("POST", "/v1/runs"): [run_response(contract=contract(version="1.1.0", sha="c" * 64))]})
        created = create_run(
            make_clients(api=api), model="m", run_input={}, outputs=["annotations"], supported=[both]
        )
        assert created.run.contract.version == "1.1.0"
        assert api.calls("POST", CANCEL_PATH) == []

    def test_a_failed_cancel_doesnt_hide_the_refusal(
        self, make_clients: MakeClients, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="fenon")
        api = Router(
            {
                ("POST", "/v1/runs"): [run_response(contract=contract(version="2.0.0"))],
                ("POST", CANCEL_PATH): [error_response(500, "internal_error")],
            }
        )
        with pytest.raises(UnsupportedModelError):
            create_run(
                make_clients(api=api, max_retries=0),
                model="m",
                run_input={},
                outputs=["annotations"],
                supported=[SUPPORT],
            )
        assert f"run {RUN_ID}: cancel of a refused run failed" in caplog.text

    def test_an_unusable_major_version_is_refused_and_cancelled(self, make_clients: MakeClients) -> None:
        reported = contract(version="9" * 5000 + ".0.0")
        api = Router(
            {
                ("POST", "/v1/runs"): [run_response(contract=reported)],
                ("POST", CANCEL_PATH): [run_response("canceled", contract=reported)],
            }
        )
        with pytest.raises(UnsupportedModelError):
            create_run(
                make_clients(api=api), model="m", run_input={}, outputs=["annotations"], supported=[SUPPORT]
            )
        assert len(api.calls("POST", CANCEL_PATH)) == 1

    def test_cancel_failures_never_log_api_text(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="fenon")
        leaky = error_response(500, "https://s.test/o?X-Amz-Signature=code-leak", message="message-leak")
        api = Router(
            {
                ("POST", "/v1/runs"): [run_response(contract=contract(version="2.0.0"))],
                ("POST", CANCEL_PATH): [leaky],
            }
        )
        with pytest.raises(UnsupportedModelError):
            create_run(
                make_clients(api=api, max_retries=0),
                model="m",
                run_input={},
                outputs=["annotations"],
                supported=[SUPPORT],
            )
        fake_runtime.interrupt_at_sleep = 1
        with pytest.raises(KeyboardInterrupt):
            SampleJob(make_clients(api=api, max_retries=0), snapshot(), cancel_on_interrupt=True).result()
        assert caplog.records
        assert "code-leak" not in caplog.text
        assert "message-leak" not in caplog.text

    def test_idempotency_in_progress_is_retried_with_the_same_key(self, make_clients: MakeClients) -> None:
        api = Router(
            {
                ("POST", "/v1/runs"): [
                    error_response(409, "idempotency_in_progress", retryable=True),
                    run_response(),
                ]
            }
        )
        create_run(
            make_clients(api=api), model="m", run_input={}, outputs=["annotations"], supported=[SUPPORT]
        )
        keys = {r.headers["idempotency-key"] for r in api.calls("POST", "/v1/runs")}
        assert len(api.calls("POST", "/v1/runs")) == 2
        assert len(keys) == 1


class TestParsing:
    def test_unknown_status_is_reported_as_such(self, make_clients: MakeClients) -> None:
        api = Router({("GET", RUN_PATH): [json_response(200, run_body(status="paused"))]})
        with pytest.raises(ServerError) as caught:
            get_run(make_clients(api=api), RUN_ID)
        assert caught.value.code == "unknown_status"
        assert caught.value.details == {"status": "paused"}

    @pytest.mark.parametrize(
        "body",
        [
            run_body(id="run_../../uploads"),
            {k: v for k, v in run_body().items() if k != "contract"},
            {
                **run_body("succeeded"),
                "outputs": {"annotations": {"url": "https://s.test/o?X-Amz-Signature=sig-in-body"}},
            },
            {**run_body(), "status": 7},
        ],
    )
    def test_a_body_failing_the_model_is_invalid_response(
        self, make_clients: MakeClients, body: dict[str, Any]
    ) -> None:
        api = Router({("GET", RUN_PATH): [json_response(200, body)]})
        with pytest.raises(ServerError) as caught:
            get_run(make_clients(api=api), RUN_ID)
        assert caught.value.code == "invalid_response"
        assert caught.value.__context__ is None
        assert "sig-in-body" not in str(caught.value)


class TestPolling:
    def test_follows_hints_through_every_status(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime
    ) -> None:
        api = Router(
            {
                ("GET", RUN_PATH): [
                    run_response("queued", poll_after_ms=1000),
                    run_response("processing_frames", poll_after_ms=500),
                    run_response("processing_frames"),
                    run_response("succeeded"),
                ]
            }
        )
        job = SampleJob(make_clients(api=api), snapshot(poll_after_s=0.25))
        assert job.result() == f"{RUN_ID}:succeeded"
        assert fake_runtime.sleeps == [0.25, 1.0, 0.5, 2.0]
        assert len(api.calls("GET", RUN_PATH)) == 4

    def test_default_schedule_grows_to_ten_seconds(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime
    ) -> None:
        api = Router({("GET", RUN_PATH): [run_response("queued")] * 6 + [run_response("succeeded")]})
        SampleJob(make_clients(api=api), snapshot()).result()
        assert fake_runtime.sleeps == [2.0, 3.0, 4.5, 6.75, 10.0, 10.0, 10.0]

    def test_eight_failed_polls_raise_with_the_job_which_resumes(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime
    ) -> None:
        api = Router(
            {("GET", RUN_PATH): [error_response(500, "internal_error")] * 8 + [run_response("succeeded")]}
        )
        job = SampleJob(make_clients(api=api, max_retries=0), snapshot(), cancel_on_interrupt=True)
        with pytest.raises(ServerError) as caught:
            job.result()
        resumed = caught.value.job
        assert resumed is job
        assert len(api.calls("GET", RUN_PATH)) == 8
        assert resumed is not None
        assert resumed.result() == f"{RUN_ID}:succeeded"

    def test_seven_failed_polls_then_success_succeeds(self, make_clients: MakeClients) -> None:
        failures: list[httpx2.Response | BaseException] = [httpx2.ConnectError("down")] * 7
        api = Router({("GET", RUN_PATH): [*failures, run_response("succeeded")]})
        assert SampleJob(make_clients(api=api, max_retries=0), snapshot()).result() == f"{RUN_ID}:succeeded"

    def test_network_failures_count_and_raise_network_error(self, make_clients: MakeClients) -> None:
        api = Router({("GET", RUN_PATH): [httpx2.ConnectError("down")]})
        with pytest.raises(NetworkError) as caught:
            SampleJob(make_clients(api=api, max_retries=0), snapshot()).result()
        assert len(api.calls("GET", RUN_PATH)) == 8
        assert caught.value.job is not None

    def test_a_failed_poll_drops_the_hint(self, make_clients: MakeClients, fake_runtime: FakeRuntime) -> None:
        api = Router({("GET", RUN_PATH): [error_response(503, "unavailable"), run_response("succeeded")]})
        SampleJob(make_clients(api=api, max_retries=0), snapshot(poll_after_s=0.25)).result()
        assert fake_runtime.sleeps == [0.25, 2.0]

    @pytest.mark.parametrize(
        ("response", "error_type"),
        [
            (error_response(404, "not_found"), NotFoundError),
            (httpx2.Response(429, headers={"retry-after": "121"}), RateLimitError),
            (error_response(501, "not_implemented"), ServerError),
        ],
    )
    def test_other_poll_errors_raise_at_once_with_the_job(
        self, make_clients: MakeClients, response: httpx2.Response, error_type: type[FenonError]
    ) -> None:
        api = Router({("GET", RUN_PATH): [response]})
        job = SampleJob(make_clients(api=api, max_retries=0), snapshot())
        with pytest.raises(error_type) as caught:
            job.result()
        assert caught.value.job is job
        assert len(api.calls("GET", RUN_PATH)) == 1

    @pytest.mark.parametrize("retryable", [True, False])
    def test_a_failed_poll_continues_only_when_the_error_is_retryable(
        self, make_clients: MakeClients, retryable: bool
    ) -> None:
        failure = error_response(503, "unavailable", retryable=retryable)
        api = Router({("GET", RUN_PATH): [failure, run_response("succeeded")]})
        job = SampleJob(make_clients(api=api, max_retries=0), snapshot())
        if retryable:
            assert job.result() == f"{RUN_ID}:succeeded"
        else:
            with pytest.raises(ServerError):
                job.result()
        assert len(api.calls("GET", RUN_PATH)) == (2 if retryable else 1)

    def test_failed_run_raises_with_every_field(self, make_clients: MakeClients) -> None:
        api = Router({("GET", RUN_PATH): [run_response("failed")]})
        job = SampleJob(make_clients(api=api), snapshot())
        with pytest.raises(RunFailedError) as caught:
            job.result()
        error = caught.value
        assert (error.run_id, error.code, error.stage) == (RUN_ID, "input_unsupported", "processing_frames")
        assert error.message == "The model engine can't process this video."
        assert error.fix == "Check the video against the model's input requirements."
        assert error.details == {"reason": "odd_dimensions"}
        assert (error.retryable, error.retry_after_s) == (False, None)
        assert error.job is job

    @pytest.mark.parametrize("code", ["processing_failed", "deadline_exceeded"])
    def test_a_retryable_run_failure_says_so(self, make_clients: MakeClients, code: str) -> None:
        failed = run_body("failed")
        failed["error"].update(code=code, retryable=True, retry_after_s=30.0)
        api = Router({("GET", RUN_PATH): [json_response(200, failed)]})
        with pytest.raises(RunFailedError) as caught:
            SampleJob(make_clients(api=api), snapshot()).result()
        assert (caught.value.code, caught.value.retryable, caught.value.retry_after_s) == (code, True, 30.0)

    def test_canceled_run_raises_run_failed(self, make_clients: MakeClients) -> None:
        api = Router({("GET", RUN_PATH): [run_response("canceled")]})
        with pytest.raises(RunFailedError) as caught:
            SampleJob(make_clients(api=api), snapshot()).result()
        assert (caught.value.code, caught.value.stage) == ("canceled", "queued")

    def test_a_final_run_without_an_error_object_still_raises(self, make_clients: MakeClients) -> None:
        job = SampleJob(make_clients(), snapshot("failed", error=None))
        with pytest.raises(RunFailedError) as caught:
            job.result()
        assert (caught.value.message, caught.value.code) == ("The run ended failed.", "failed")


class TestJob:
    def test_status_fetches_until_final_then_caches(self, make_clients: MakeClients) -> None:
        api = Router({("GET", RUN_PATH): [run_response("queued"), run_response("succeeded")]})
        job = SampleJob(make_clients(api=api), snapshot())
        assert job.id == RUN_ID
        assert job.status == "queued"
        assert job.status == "succeeded"
        assert job.status == "succeeded"
        assert len(api.calls("GET", RUN_PATH)) == 2

    def test_cancel_sends_one_keyed_request_and_returns_none(self, make_clients: MakeClients) -> None:
        api = Router({("POST", CANCEL_PATH): [run_response("canceled")]})
        job = SampleJob(make_clients(api=api), snapshot("queued"))
        assert job.cancel() is None
        request = api.calls("POST", CANCEL_PATH)[0]
        assert request.headers["idempotency-key"]
        assert request.content == b""
        assert job.status == "canceled"
        assert len(api.requests) == 1

    def test_a_job_created_final_never_polls(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime
    ) -> None:
        assert SampleJob(make_clients(), snapshot("succeeded")).result() == f"{RUN_ID}:succeeded"
        assert fake_runtime.sleeps == []

    def test_cancel_run_honours_per_call_overrides(self, make_clients: MakeClients) -> None:
        api = Router({("POST", CANCEL_PATH): [error_response(503, "unavailable")]})
        with pytest.raises(ServerError):
            cancel_run(make_clients(api=api), RUN_ID, max_retries=0, timeout_s=5.0)
        request = api.calls("POST", CANCEL_PATH)[0]
        assert len(api.requests) == 1
        assert request.extensions["timeout"] == {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}


class TestRunOutputDownload:
    def test_returns_the_bytes_without_fenon_headers(self, make_clients: MakeClients) -> None:
        storage = Router({("GET", OUTPUT_PATH): [httpx2.Response(200, content=OUTPUT_BYTES)]})
        run = snapshot("succeeded").run
        assert RunOutput(make_clients(storage=storage), run, "annotations").content() == OUTPUT_BYTES
        assert "authorization" not in storage.requests[0].headers

    def test_a_refused_url_is_refreshed_once(self, make_clients: MakeClients) -> None:
        fresh = run_body("succeeded")
        fresh["outputs"]["annotations"]["url"] = FRESH_URL
        api = Router({("GET", RUN_PATH): [json_response(200, fresh)]})
        storage = Router(
            {
                ("GET", OUTPUT_PATH): [httpx2.Response(403)],
                ("GET", "/fresh/annotations.jsonl"): [httpx2.Response(200, content=OUTPUT_BYTES)],
            }
        )
        content = RunOutput(
            make_clients(api=api, storage=storage), snapshot("succeeded").run, "annotations"
        ).content()
        assert content == OUTPUT_BYTES
        assert len(api.calls("GET", RUN_PATH)) == 1

    def test_a_second_refusal_is_output_rejected(self, make_clients: MakeClients) -> None:
        api = Router({("GET", RUN_PATH): [run_response("succeeded")]})
        storage = Router({("GET", OUTPUT_PATH): [httpx2.Response(403)]})
        with pytest.raises(ServerError) as caught:
            RunOutput(
                make_clients(api=api, storage=storage), snapshot("succeeded").run, "annotations"
            ).content()
        assert caught.value.code == "output_rejected"
        assert caught.value.__context__ is None
        assert len(storage.requests) == 2

    @pytest.mark.parametrize("content", [OUTPUT_BYTES[:-1], OUTPUT_BYTES + b"x"])
    def test_a_size_mismatch_is_output_truncated(self, make_clients: MakeClients, content: bytes) -> None:
        storage = Router({("GET", OUTPUT_PATH): [httpx2.Response(200, content=content)]})
        with pytest.raises(ServerError) as caught:
            RunOutput(make_clients(storage=storage), snapshot("succeeded").run, "annotations").content()
        assert caught.value.code == "output_truncated"
        assert caught.value.details == {"expected_bytes": len(OUTPUT_BYTES), "received_bytes": len(content)}

    @pytest.mark.parametrize(("status", "name"), [("succeeded", "video"), ("failed", "annotations")])
    def test_a_missing_output_is_invalid_response(
        self, make_clients: MakeClients, status: str, name: str
    ) -> None:
        with pytest.raises(ServerError) as caught:
            RunOutput(make_clients(), snapshot(status).run, name).content()
        assert caught.value.code == "invalid_response"

    def test_output_urls_never_reach_logs_or_errors(
        self, make_clients: MakeClients, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="fenon")
        api = Router({("GET", RUN_PATH): [run_response("succeeded")]})
        storage = Router({("GET", OUTPUT_PATH): [httpx2.Response(503), httpx2.Response(403)]})
        with pytest.raises(ServerError) as caught:
            RunOutput(
                make_clients(api=api, storage=storage), snapshot("succeeded").run, "annotations"
            ).content()
        text = "\n".join([caplog.text, str(caught.value), repr(caught.value)])
        assert caplog.records
        assert "output-signature" not in text
        assert "storage.example.test" not in text


class TestRunOutput:
    def test_downloads_once_however_often(self, make_clients: MakeClients) -> None:
        storage = Router({("GET", OUTPUT_PATH): [httpx2.Response(200, content=OUTPUT_BYTES)]})
        output = RunOutput(
            make_clients(storage=storage), Run.model_validate(run_body("succeeded")), "annotations"
        )
        assert [output.content() for _ in range(3)] == [OUTPUT_BYTES] * 3
        assert len(storage.requests) == 1

    def test_a_second_thread_waits_for_the_first_download(self, make_clients: MakeClients) -> None:
        entered = threading.Semaphore(0)
        release = threading.Event()
        router = Router({("GET", OUTPUT_PATH): [httpx2.Response(200, content=OUTPUT_BYTES)]})

        def slow(request: httpx2.Request) -> httpx2.Response:
            entered.release()
            release.wait(5)
            return router(request)

        output = RunOutput(
            make_clients(storage=slow), Run.model_validate(run_body("succeeded")), "annotations"
        )
        results: list[bytes] = []
        first = threading.Thread(target=lambda: results.append(output.content()))
        first.start()
        assert entered.acquire(timeout=5)
        others = [threading.Thread(target=lambda: results.append(output.content())) for _ in range(3)]
        for thread in others:
            thread.start()
        second_download_started = entered.acquire(timeout=0.5)
        release.set()
        for thread in [first, *others]:
            thread.join()
        assert not second_download_started
        assert results == [OUTPUT_BYTES] * 4
        assert len(router.requests) == 1

    def test_save_writes_the_exact_bytes_and_returns_the_path(
        self, make_clients: MakeClients, tmp_path: Path
    ) -> None:
        storage = Router({("GET", OUTPUT_PATH): [httpx2.Response(200, content=OUTPUT_BYTES)]})
        output = RunOutput(
            make_clients(storage=storage), Run.model_validate(run_body("succeeded")), "annotations"
        )
        target = tmp_path / "frames.jsonl"
        target.write_bytes(b"old content that is longer than the output")
        assert output.save(str(target)) == target
        assert target.read_bytes() == OUTPUT_BYTES

    def test_save_into_a_missing_folder_raises_the_os_error(
        self, make_clients: MakeClients, tmp_path: Path
    ) -> None:
        storage = Router({("GET", OUTPUT_PATH): [httpx2.Response(200, content=OUTPUT_BYTES)]})
        output = RunOutput(
            make_clients(storage=storage), Run.model_validate(run_body("succeeded")), "annotations"
        )
        with pytest.raises(FileNotFoundError):
            output.save(tmp_path / "missing" / "frames.jsonl")


class TestCtrlC:
    def test_interrupt_while_polling_cancels_once_then_reraises(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime
    ) -> None:
        fake_runtime.interrupt_at_sleep = 2
        api = Router(
            {("GET", RUN_PATH): [run_response("queued")], ("POST", CANCEL_PATH): [run_response("canceled")]}
        )
        with pytest.raises(KeyboardInterrupt) as caught:
            SampleJob(make_clients(api=api), snapshot(), cancel_on_interrupt=True).result()
        assert caught.value is fake_runtime.interrupt
        cancels = api.calls("POST", CANCEL_PATH)
        assert len(cancels) == 1
        assert cancels[0].headers["idempotency-key"]
        assert cancels[0].extensions["timeout"]["read"] == 5.0

    def test_a_failing_cancel_still_reraises_the_interrupt(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="fenon")
        fake_runtime.interrupt_at_sleep = 1
        api = Router({("POST", CANCEL_PATH): [error_response(503, "unavailable")]})
        job = SampleJob(make_clients(api=api, max_retries=2), snapshot(), cancel_on_interrupt=True)
        with pytest.raises(KeyboardInterrupt) as caught:
            job.result()
        assert caught.value is fake_runtime.interrupt
        assert len(api.calls("POST", CANCEL_PATH)) == 1
        assert f"run {RUN_ID}: cancel after interrupt failed" in caplog.text
        fake_runtime.interrupt = KeyboardInterrupt()
        fake_runtime.interrupt_at_sleep = len(fake_runtime.sleeps) + 1
        with pytest.raises(KeyboardInterrupt):
            job.result()
        assert len(api.calls("POST", CANCEL_PATH)) == 1

    def test_a_second_interrupt_during_the_cancel_propagates(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime
    ) -> None:
        fake_runtime.interrupt_at_sleep = 1
        api = Router({("POST", CANCEL_PATH): [KeyboardInterrupt()]})
        with pytest.raises(KeyboardInterrupt):
            SampleJob(make_clients(api=api), snapshot(), cancel_on_interrupt=True).result()
        assert len(api.calls("POST", CANCEL_PATH)) == 1

    def test_a_job_without_the_flag_never_cancels(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime
    ) -> None:
        fake_runtime.interrupt_at_sleep = 1
        with pytest.raises(KeyboardInterrupt):
            SampleJob(make_clients(), snapshot()).result()

    def test_a_job_handed_out_through_an_error_never_cancels(
        self, make_clients: MakeClients, fake_runtime: FakeRuntime
    ) -> None:
        api = Router({("GET", RUN_PATH): [error_response(404, "not_found"), run_response("queued")]})
        job = SampleJob(make_clients(api=api, max_retries=0), snapshot(), cancel_on_interrupt=True)
        with pytest.raises(NotFoundError) as caught:
            job.result()
        fake_runtime.interrupt_at_sleep = len(fake_runtime.sleeps) + 1
        assert caught.value.job is job
        with pytest.raises(KeyboardInterrupt):
            job.result()
        assert api.calls("POST", CANCEL_PATH) == []

    def test_an_interrupt_after_the_run_became_final_sends_no_cancel(self, make_clients: MakeClients) -> None:
        class InterruptedAfterFinal(SampleJob):
            def _update(self, snapshot: RunSnapshot) -> None:
                super()._update(snapshot)
                if snapshot.run.status == "succeeded":
                    raise KeyboardInterrupt

        api = Router({("GET", RUN_PATH): [run_response("succeeded")]})
        with pytest.raises(KeyboardInterrupt):
            InterruptedAfterFinal(make_clients(api=api), snapshot(), cancel_on_interrupt=True).result()
        assert api.calls("POST", CANCEL_PATH) == []


def test_four_threads_share_nothing_they_shouldnt(make_clients: MakeClients) -> None:
    run_ids = [f"run_thread00000{n}" for n in range(4)]
    routes: dict[tuple[str, str], list[httpx2.Response]] = {
        ("POST", "/v1/runs"): [run_response(id=run_id) for run_id in run_ids]
    }
    for run_id in run_ids:
        routes[("GET", f"/v1/runs/{run_id}")] = [
            run_response("queued", id=run_id),
            run_response("succeeded", id=run_id),
        ]
    router = Router(routes)
    together = threading.Barrier(len(run_ids), timeout=5)

    def overlapping(request: httpx2.Request) -> httpx2.Response:
        together.wait()
        return router(request)

    clients = make_clients(api=overlapping)
    results: list[str] = []
    lock = threading.Lock()

    def work() -> None:
        created = create_run(clients, model="m", run_input={}, outputs=["annotations"], supported=[SUPPORT])
        outcome = SampleJob(clients, created).result()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=work) for _ in run_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == sorted(f"{run_id}:succeeded" for run_id in run_ids)
    keys = {request.headers["idempotency-key"] for request in router.calls("POST", "/v1/runs")}
    assert len(keys) == len(run_ids)


class TestFinalContractCheck:
    @pytest.mark.parametrize(
        ("reported", "error_type", "code"),
        [
            (contract(contract_id="fenon.other.contract"), UnsupportedModelError, "unsupported_contract"),
            (contract(sha="b" * 64), ServerError, "contract_mismatch"),
        ],
    )
    def test_a_final_run_reporting_another_contract_is_refused(
        self, make_clients: MakeClients, reported: dict[str, str], error_type: type[FenonError], code: str
    ) -> None:
        final = httpx2.Response(
            200, json=run_body("succeeded", contract=reported), headers={"X-Request-Id": "req_finalpoll0001"}
        )
        api = Router({("GET", RUN_PATH): [final]})
        job = SampleJob(make_clients(api=api), snapshot())
        with pytest.raises(error_type) as caught:
            job.result()
        assert caught.value.code == code
        assert caught.value.job is job
        assert caught.value.request_id == "req_finalpoll0001"

    def test_a_failed_run_carries_the_final_request_id(self, make_clients: MakeClients) -> None:
        final = httpx2.Response(200, json=run_body("failed"), headers={"X-Request-Id": "req_finalpoll0002"})
        with pytest.raises(RunFailedError) as caught:
            SampleJob(make_clients(api=Router({("GET", RUN_PATH): [final]})), snapshot()).result()
        assert caught.value.request_id == "req_finalpoll0002"


def test_text_where_a_number_belongs_is_invalid_response(make_clients: MakeClients) -> None:
    body = run_body("succeeded")
    body["outputs"]["annotations"]["size_bytes"] = "123"
    with pytest.raises(ServerError) as caught:
        get_run(make_clients(api=Router({("GET", RUN_PATH): [json_response(200, body)]})), RUN_ID)
    assert caught.value.code == "invalid_response"
