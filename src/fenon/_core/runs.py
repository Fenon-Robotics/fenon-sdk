"""Run service: create a run and check its contract, poll it, cancel it, and read or save its outputs.

- A run is read only when its contract version is one the caller ships with a matching hash. A refused run is
  cancelled (a failed cancel is only logged) and ``unsupported_contract`` or ``contract_mismatch`` is raised.
- An unknown run status is reported before the rest of the run is validated.
- Polling follows ``Fenon-Poll-After-Ms`` (clamped to 0.25 to 60 s), else waits 2 s growing x1.5 up to 10 s. A
  failed poll continues only when its error is retryable, at most eight times in a row. Ctrl-C sends one quick
  cancel.
- ``RunOutput`` downloads an output once, or streams it to a temporary file beside the target and replaces the
  target only when every byte arrived. A refused URL gets one fresh read of the run; a size other than
  ``size_bytes`` is ``output_truncated``; expired outputs raise ``outputs_expired``. Details:
  docs/features/uploads-and-runs.md.
"""

from __future__ import annotations

import abc
import os
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, ClassVar, Generic, TypeVar, get_args

from ._generated import ContractRef, OutputFile, Run, RunStatus
from .config import LIMITS
from .errors import (
    FenonError,
    NotFoundError,
    RunFailedError,
    ServerError,
    StorageUrlRejectedError,
    UnsupportedModelError,
)
from .http import ApiResponse, HttpClients
from .logs import logger

RUN_STATUSES: tuple[str, ...] = get_args(RunStatus.__value__)
FINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})
_MAX_POLL_MS_DIGITS = len(str(int(LIMITS.max_poll_s * 1000)))

ResultT = TypeVar("ResultT")
TransferT = TypeVar("TransferT")


@dataclass(frozen=True)
class ContractSupport:
    id: str
    shipped: Mapping[str, str]


@dataclass(frozen=True)
class RunSnapshot:
    run: Run
    poll_after_s: float | None
    request_id: str | None = None


def create_run(
    clients: HttpClients,
    *,
    model: str,
    run_input: Mapping[str, Any],
    outputs: Sequence[str],
    supported: Sequence[ContractSupport],
) -> RunSnapshot:
    body = {"model": model, "input": dict(run_input), "outputs": list(outputs)}
    response = clients.api.request("POST", "/runs", json_body=body)
    snapshot = _snapshot(response)
    refusal = _contract_refusal(snapshot.run, supported, response.request_id)
    if refusal is not None:
        _cancel_refused_run(clients, snapshot.run.id)
        raise refusal
    return snapshot


def get_run(clients: HttpClients, run_id: str) -> RunSnapshot:
    return _snapshot(clients.api.request("GET", f"/runs/{run_id}"))


def cancel_run(
    clients: HttpClients, run_id: str, *, max_retries: int | None = None, timeout_s: float | None = None
) -> Run:
    response = clients.api.request(
        "POST", f"/runs/{run_id}/cancel", max_retries=max_retries, timeout_s=timeout_s
    )
    return _snapshot(response).run


class RunOutput:
    def __init__(self, clients: HttpClients, run: Run, name: str) -> None:
        self._clients = clients
        self._run = run
        self._name = name
        self._lock = threading.Lock()
        self._content: bytes | None = None

    def content(self) -> bytes:
        with self._lock:
            if self._content is None:
                self._content = _fetch_output(self._clients, self._run, self._name)
            return self._content

    def save(self, path: str | os.PathLike[str]) -> Path:
        target = Path(path)
        with self._lock:
            if self._content is not None:
                target.write_bytes(self._content)
                return target
        return _save_output(self._clients, self._run, self._name, target)


class Job(abc.ABC, Generic[ResultT]):
    _supported: ClassVar[tuple[ContractSupport, ...]] = ()

    def __init__(
        self, clients: HttpClients, snapshot: RunSnapshot, *, cancel_on_interrupt: bool = False
    ) -> None:
        self._clients = clients
        self._run = snapshot.run
        self._poll_after_s = snapshot.poll_after_s
        self._request_id = snapshot.request_id
        self._default_wait_s = LIMITS.first_default_poll_s
        self._cancel_on_interrupt = cancel_on_interrupt

    @property
    def id(self) -> str:
        return self._run.id

    @property
    def status(self) -> str:
        if self._run.status not in FINAL_STATUSES:
            self._update(get_run(self._clients, self._run.id))
        return self._run.status

    def result(self) -> ResultT:
        try:
            run = self._wait()
            refusal = _contract_refusal(run, self._supported, self._request_id)
            if refusal is not None:
                raise refusal
            _check_requested_outputs(run)
            result = self._make_result(run)
        except KeyboardInterrupt:
            if self._cancel_on_interrupt:
                self._cancel_on_interrupt = False
                self._cancel_after_interrupt()
            raise
        except FenonError as error:
            self._cancel_on_interrupt = False
            error.job = self
            if error.request_id is None:
                error.request_id = self._request_id
            raise
        return result

    def cancel(self) -> None:
        self._run = cancel_run(self._clients, self._run.id)
        self._poll_after_s = None

    @abc.abstractmethod
    def _make_result(self, run: Run) -> ResultT: ...

    def _wait(self) -> Run:
        failures = 0
        while self._run.status not in FINAL_STATUSES:
            wait_s = self._next_wait_s()
            logger.debug("run %s is %s; next poll in %.2f s", self._run.id, self._run.status, wait_s)
            self._clients.runtime.sleep(wait_s)
            try:
                snapshot = get_run(self._clients, self._run.id)
            except FenonError as error:
                failures += 1
                if failures < LIMITS.max_poll_failures and _poll_may_continue(error):
                    self._poll_after_s = None
                    continue
                raise
            failures = 0
            self._update(snapshot)
        if self._run.status != "succeeded":
            raise _run_failed_error(self._run)
        return self._run

    def _next_wait_s(self) -> float:
        if self._poll_after_s is not None:
            return self._poll_after_s
        wait_s = self._default_wait_s
        self._default_wait_s = min(LIMITS.max_default_poll_s, wait_s * LIMITS.default_poll_growth)
        return wait_s

    def _update(self, snapshot: RunSnapshot) -> None:
        self._run = snapshot.run
        self._poll_after_s = snapshot.poll_after_s
        self._request_id = snapshot.request_id

    def _cancel_after_interrupt(self) -> None:
        if self._run.status in FINAL_STATUSES:
            return
        try:
            self._run = cancel_run(
                self._clients, self._run.id, max_retries=0, timeout_s=LIMITS.interrupt_cancel_timeout_s
            )
        except FenonError:
            logger.info("run %s: cancel after interrupt failed", self._run.id)
            return
        logger.info("run %s: cancelled after interrupt", self._run.id)


def _snapshot(response: ApiResponse) -> RunSnapshot:
    status = response.body.get("status")
    if isinstance(status, str) and status not in RUN_STATUSES:
        raise ServerError(
            "The Fenon API sent a run status this version of the SDK doesn't know.",
            code="unknown_status",
            fix="Upgrade the fenon package.",
            details={"status": status},
            request_id=response.request_id,
            status_code=response.status_code,
        )
    return RunSnapshot(
        response.parse(Run), _poll_after_s(response.headers.get("fenon-poll-after-ms")), response.request_id
    )


def _poll_after_s(value: str | None) -> float | None:
    if value is None or not (value.isascii() and value.isdigit()):
        return None
    digits = value.lstrip("0") or "0"
    if len(digits) > _MAX_POLL_MS_DIGITS:
        return LIMITS.max_poll_s
    return min(LIMITS.max_poll_s, max(LIMITS.min_poll_s, int(digits) / 1000))


def _contract_refusal(
    run: Run, supported: Sequence[ContractSupport], request_id: str | None
) -> FenonError | None:
    contract: ContractRef = run.contract
    details = {"run_id": run.id, "contract_id": contract.id, "contract_version": contract.version}
    for support in supported:
        shipped_sha256 = support.shipped.get(contract.version) if support.id == contract.id else None
        if shipped_sha256 is not None:
            if shipped_sha256 == contract.schema_sha256:
                return None
            return ServerError(
                f"The API's contract {contract.id} {contract.version} doesn't match the copy shipped with "
                "this SDK.",
                code="contract_mismatch",
                fix="Upgrade the fenon package; if it persists, report it with the request ID.",
                details=details,
                request_id=request_id,
            )
    return UnsupportedModelError(
        f"The model returns contract {contract.id} {contract.version}, which this version of the SDK "
        "can't read.",
        code="unsupported_contract",
        fix="Upgrade the fenon package, or use a model this version supports.",
        details=details,
        request_id=request_id,
    )


def _cancel_refused_run(clients: HttpClients, run_id: str) -> None:
    try:
        cancel_run(clients, run_id)
    except FenonError:
        logger.info("run %s: cancel of a refused run failed", run_id)
        return
    logger.info("run %s: cancelled because its contract isn't supported", run_id)


def _poll_may_continue(error: FenonError) -> bool:
    hint_too_long = error.retry_after_s is not None and error.retry_after_s > LIMITS.max_retry_hint_s
    return error.retryable and not hint_too_long


def _fetch_output(clients: HttpClients, run: Run, name: str) -> bytes:
    return _with_fresh_url(clients, run, name, lambda output: _download_unless_refused(clients, output))


def _save_output(clients: HttpClients, run: Run, name: str, path: Path) -> Path:
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", suffix=".part", delete=False
    ) as part:
        part_path = Path(part.name)
        try:
            _with_fresh_url(clients, run, name, lambda output: _stream_unless_refused(clients, output, part))
        except BaseException:
            part.close()
            part_path.unlink(missing_ok=True)
            raise
    os.replace(part_path, path)
    return path


def _run_failed_error(run: Run) -> RunFailedError:
    failure = run.error
    if failure is None:
        return RunFailedError(f"The run ended {run.status}.", run_id=run.id, code=run.status)
    return RunFailedError(
        failure.message,
        run_id=run.id,
        stage=failure.stage,
        code=failure.code,
        fix=failure.fix,
        details=failure.details,
        retryable=failure.retryable,
        retry_after_s=failure.retry_after_s,
    )


def _check_requested_outputs(run: Run) -> None:
    if run.outputs_expired:
        return
    present = set(run.outputs or {})
    if not set(run.outputs_requested) <= present:
        raise ServerError(
            "The run succeeded without every output it asked for.",
            code="invalid_response",
            fix="Report it with the run ID.",
            details={"run_id": run.id},
        )


def _with_fresh_url(
    clients: HttpClients, run: Run, name: str, transfer: Callable[[OutputFile], TransferT | None]
) -> TransferT:
    done = transfer(_output_file(run, name))
    if done is None:
        logger.info("run %s output %s refused by storage; fetching a fresh URL", run.id, name)
        done = transfer(_output_file(get_run(clients, run.id).run, name))
    if done is None:
        raise ServerError(
            "Storage refused the output again after a fresh URL.",
            code="output_rejected",
            fix="Try again later.",
        )
    return done


def _output_file(run: Run, name: str) -> OutputFile:
    if run.outputs_expired:
        raise NotFoundError(
            "This run's outputs have expired and were deleted.",
            code="outputs_expired",
            fix="Run the video again, and save its outputs before outputs_expire_at.",
            details={"run_id": run.id, "outputs_expire_at": run.outputs_expire_at},
        )
    outputs = run.outputs if run.status == "succeeded" else None
    if outputs is None or name not in outputs:
        raise ServerError(f"The run has no {name} output.", code="invalid_response")
    return outputs[name]


def _download_unless_refused(clients: HttpClients, output: OutputFile) -> bytes | None:
    try:
        content = clients.storage.download(output.url)
    except StorageUrlRejectedError:
        return None
    _check_size(output, len(content))
    return content


def _stream_unless_refused(clients: HttpClients, output: OutputFile, destination: IO[bytes]) -> int | None:
    try:
        written = clients.storage.download_to(output.url, destination)
    except StorageUrlRejectedError:
        return None
    _check_size(output, written)
    return written


def _check_size(output: OutputFile, received_bytes: int) -> None:
    if received_bytes != output.size_bytes:
        raise ServerError(
            "The output's size doesn't match what the API reported.",
            code="output_truncated",
            fix="Try again; if it persists, report it with the run ID.",
            details={"expected_bytes": output.size_bytes, "received_bytes": received_bytes},
        )
