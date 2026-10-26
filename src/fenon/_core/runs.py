"""Run service: create a run and check its contract, poll it, cancel it, and fetch its outputs."""

from __future__ import annotations

import abc
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

from ._generated import ContractRef, OutputFile, Run
from .errors import (
    FenonError,
    NetworkError,
    RunFailedError,
    ServerError,
    StorageUrlRejectedError,
    UnsupportedModelError,
)
from .http import ApiResponse, HttpClients
from .logs import logger
from .settings import LIMITS

RUN_STATUSES = (
    "preparing_input",
    "queued",
    "processing_frames",
    "writing_results",
    "succeeded",
    "failed",
    "canceled",
)
FINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})
_MAX_POLL_MS_DIGITS = len(str(int(LIMITS.max_poll_s * 1000)))

ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class ContractSupport:
    id: str
    major: int
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
    supported: Sequence[ContractSupport],
) -> RunSnapshot:
    """Start a run and check that its contract is one the caller can read."""
    response = clients.api.request("POST", "/runs", json_body={"model": model, "input": dict(run_input)})
    snapshot = _snapshot(response)
    refusal = _contract_refusal(snapshot.run, supported, response.request_id)
    if refusal is not None:
        _cancel_refused_run(clients, snapshot.run.id)
        raise refusal
    return snapshot


def get_run(clients: HttpClients, run_id: str) -> RunSnapshot:
    """Fetch a run."""
    return _snapshot(clients.api.request("GET", f"/runs/{run_id}"))


def cancel_run(
    clients: HttpClients, run_id: str, *, max_retries: int | None = None, timeout_s: float | None = None
) -> Run:
    """Cancel a run; the API returns a run that is already final unchanged."""
    response = clients.api.request(
        "POST", f"/runs/{run_id}/cancel", max_retries=max_retries, timeout_s=timeout_s
    )
    return _snapshot(response).run


def fetch_output(clients: HttpClients, run: Run, name: str) -> bytes:
    """Download one named output of a succeeded run into memory."""
    content = _download_unless_refused(clients, _output_file(run, name))
    if content is None:
        logger.info("run %s output %s refused by storage; fetching a fresh URL", run.id, name)
        fresh = get_run(clients, run.id).run
        content = _download_unless_refused(clients, _output_file(fresh, name))
    if content is None:
        raise ServerError(
            "Storage refused the output again after a fresh URL.",
            code="output_rejected",
            fix="Try again later.",
        )
    return content


class Job(abc.ABC, Generic[ResultT]):
    """A run being waited on; model packages subclass it, list their contracts and build the result."""

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
        """The run ID."""
        return self._run.id

    @property
    def status(self) -> str:
        """The run's status, fetched from the API while the run isn't final."""
        if self._run.status not in FINAL_STATUSES:
            self._update(get_run(self._clients, self._run.id))
        return self._run.status

    def result(self) -> ResultT:
        """Wait for the run to finish and return the model's result."""
        try:
            run = self._wait()
            refusal = _contract_refusal(run, self._supported, self._request_id)
            if refusal is not None:
                raise refusal
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
        """Ask the API to cancel the run; a run that is already final is left unchanged."""
        self._run = cancel_run(self._clients, self._run.id)
        self._poll_after_s = None

    @abc.abstractmethod
    def _make_result(self, run: Run) -> ResultT:
        """Build the model's result from the succeeded run."""

    def _wait(self) -> Run:
        """Poll until the run is final; return it when it succeeded."""
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
        """Return the server's hint, or the next step of the default schedule (2 s, then x1.5, up to 10 s)."""
        if self._poll_after_s is not None:
            return self._poll_after_s
        wait_s = self._default_wait_s
        self._default_wait_s = min(LIMITS.max_default_poll_s, wait_s * LIMITS.default_poll_growth)
        return wait_s

    def _update(self, snapshot: RunSnapshot) -> None:
        """Remember the latest run and poll hint."""
        self._run = snapshot.run
        self._poll_after_s = snapshot.poll_after_s
        self._request_id = snapshot.request_id

    def _cancel_after_interrupt(self) -> None:
        """Send one quick cancel for a run that isn't known to be final; a failure is only logged."""
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
    """Read a run response, detecting an unknown status before validating the rest."""
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
    """Return ``Fenon-Poll-After-Ms`` in seconds, clamped to 0.25 to 60 s; ``None`` when absent or invalid."""
    if value is None or not (value.isascii() and value.isdigit()):
        return None
    digits = value.lstrip("0") or "0"
    if len(digits) > _MAX_POLL_MS_DIGITS:
        return LIMITS.max_poll_s
    return min(LIMITS.max_poll_s, max(LIMITS.min_poll_s, int(digits) / 1000))


def _contract_refusal(
    run: Run, supported: Sequence[ContractSupport], request_id: str | None
) -> FenonError | None:
    """Return the error for a run whose contract the caller can't read, or ``None`` when it can."""
    contract: ContractRef = run.contract
    details = {"run_id": run.id, "contract_id": contract.id, "contract_version": contract.version}
    major = contract.version.split(".", 1)[0].lstrip("0") or "0"
    for support in supported:
        if support.id == contract.id and str(support.major) == major:
            shipped_sha256 = support.shipped.get(contract.version)
            if shipped_sha256 is None or shipped_sha256 == contract.schema_sha256:
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
    """Cancel a run the SDK refuses to read; a failure is only logged, since the refusal is raised anyway."""
    try:
        cancel_run(clients, run_id)
    except FenonError:
        logger.info("run %s: cancel of a refused run failed", run_id)
        return
    logger.info("run %s: cancelled because its contract isn't supported", run_id)


def _poll_may_continue(error: FenonError) -> bool:
    """Return whether a failed poll counts toward the limit instead of ending the wait at once."""
    if error.retry_after_s is not None and error.retry_after_s > LIMITS.max_retry_hint_s:
        return False
    if isinstance(error, NetworkError):
        return True
    status = error.status_code
    return status is not None and (status in (408, 429) or (500 <= status < 600 and status != 501))


def _run_failed_error(run: Run) -> RunFailedError:
    """Return the error for a run that ended ``failed`` or ``canceled``."""
    failure = run.error
    if failure is None:
        return RunFailedError(f"The run ended {run.status}.", run_id=run.id, code=run.status)
    return RunFailedError(
        failure.message,
        run_id=run.id,
        stage=failure.stage,
        frame=failure.frame,
        timestamp_s=failure.timestamp_s,
        code=failure.code,
        fix=failure.fix,
        details=failure.details,
    )


def _output_file(run: Run, name: str) -> OutputFile:
    """Return a succeeded run's named output."""
    outputs = run.outputs if run.status == "succeeded" else None
    if outputs is None or name not in outputs:
        raise ServerError(f"The run has no {name} output.", code="invalid_response")
    return outputs[name]


def _download_unless_refused(clients: HttpClients, output: OutputFile) -> bytes | None:
    """Download an output; ``None`` when storage refuses its URL."""
    try:
        content = clients.storage.download(output.url)
    except StorageUrlRejectedError:
        return None
    if len(content) != output.size_bytes:
        raise ServerError(
            "The output's size doesn't match what the API reported.",
            code="output_truncated",
            fix="Try again; if it persists, report it with the run ID.",
            details={"expected_bytes": output.size_bytes, "received_bytes": len(content)},
        )
    return content
