"""Tests for the error classes and the mapping of API error responses."""

from __future__ import annotations

import pytest

from fenon._core.errors import (
    AuthError,
    CreditsError,
    FenonError,
    InvalidInputError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    RunFailedError,
    ServerError,
    SessionLimitError,
    StorageUrlRejectedError,
    UnsupportedModelError,
    error_class_for_status,
    error_from_response,
    retry_after_of,
)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, InvalidInputError),
        (401, AuthError),
        (402, CreditsError),
        (403, AuthError),
        (404, NotFoundError),
        (405, FenonError),
        (408, NetworkError),
        (409, ServerError),
        (410, FenonError),
        (413, InvalidInputError),
        (415, InvalidInputError),
        (422, InvalidInputError),
        (429, RateLimitError),
        (500, ServerError),
        (501, ServerError),
        (503, ServerError),
        (302, ServerError),
    ],
)
def test_status_maps_to_the_contract_error_class(status: int, expected: type[FenonError]) -> None:
    assert error_class_for_status(status) is expected


def test_error_from_response_uses_the_envelope() -> None:
    payload = {
        "error": {
            "code": "input_invalid",
            "message": "The input doesn't match the contract.",
            "fix": "Send a video upload ID.",
            "retryable": False,
            "retry_after_s": None,
            "request_id": "req_bodybodybody",
            "details": {"field": "/input/video"},
        }
    }
    error = error_from_response(
        422, payload, request_id="req_headerheader", retry_after_s=None, retryable_without_error_body=False
    )
    assert isinstance(error, InvalidInputError)
    assert error.code == "input_invalid"
    assert error.message == "The input doesn't match the contract."
    assert str(error) == "The input doesn't match the contract."
    assert error.fix == "Send a video upload ID."
    assert error.details == {"field": "/input/video"}
    assert error.request_id == "req_headerheader"
    assert error.status_code == 422
    assert error.retryable is False


def test_error_from_response_falls_back_to_the_body_request_id() -> None:
    payload = {"error": {"code": "not_found", "message": "No such run.", "request_id": "req_bodybodybody"}}
    error = error_from_response(
        404, payload, request_id=None, retry_after_s=None, retryable_without_error_body=False
    )
    assert error.request_id == "req_bodybodybody"


def test_error_from_response_without_an_envelope_names_the_status() -> None:
    error = error_from_response(
        502, None, request_id=None, retry_after_s=1.5, retryable_without_error_body=True
    )
    assert isinstance(error, ServerError)
    assert error.message == "The Fenon API answered with HTTP 502."
    assert error.code is None
    assert error.details == {}
    assert error.retryable is True
    assert error.retry_after_s == 1.5


def test_the_body_retryable_flag_wins_over_the_status_rule() -> None:
    payload = {"error": {"code": "unavailable", "message": "Down.", "retryable": False}}
    error = error_from_response(
        503, payload, request_id=None, retry_after_s=None, retryable_without_error_body=True
    )
    assert error.retryable is False


def test_malformed_envelope_fields_are_ignored() -> None:
    payload = {"error": {"code": 7, "message": "", "details": ["not", "a", "dict"], "retryable": "yes"}}
    error = error_from_response(
        500, payload, request_id=None, retry_after_s=None, retryable_without_error_body=True
    )
    assert error.code is None
    assert error.message == "The Fenon API answered with HTTP 500."
    assert error.details == {}
    assert error.retryable is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (3, 3.0),
        (0.25, 0.25),
        (0, 0.0),
        (-1, None),
        (True, None),
        ("3", None),
        (None, None),
        (float("inf"), None),
        (float("nan"), None),
    ],
)
def test_retry_after_of(value: object, expected: float | None) -> None:
    assert retry_after_of({"error": {"retry_after_s": value}}) == expected


def test_errors_keep_their_builtin_bases() -> None:
    assert issubclass(InvalidInputError, ValueError)
    assert issubclass(NetworkError, ConnectionError)
    assert issubclass(StorageUrlRejectedError, ServerError)
    network = NetworkError("Couldn't reach the Fenon API.", code="network_error")
    assert str(network) == "Couldn't reach the Fenon API."
    assert network.args == ("Couldn't reach the Fenon API.",)


def test_repr_shows_only_identifying_fields() -> None:
    error = AuthError("Bad key fenon_secret.", code="invalid_api_key", status_code=401, request_id="req_x")
    assert repr(error) == "AuthError(code='invalid_api_key', status_code=401, request_id='req_x')"


def test_run_failed_error_carries_the_run_fields() -> None:
    error = RunFailedError(
        "The model engine can't process this video.",
        run_id="run_q3w8e1r5t2y6",
        stage="processing_frames",
        code="input_unsupported",
    )
    assert (error.run_id, error.stage) == ("run_q3w8e1r5t2y6", "processing_frames")
    assert error.code == "input_unsupported"
    assert error.retryable is False


def test_every_error_is_a_fenon_error() -> None:
    for error_class in (
        AuthError,
        CreditsError,
        InvalidInputError,
        NotFoundError,
        RateLimitError,
        ServerError,
        NetworkError,
        UnsupportedModelError,
        StorageUrlRejectedError,
    ):
        assert issubclass(error_class, FenonError)
    assert issubclass(RunFailedError, FenonError)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("active_session_limit", SessionLimitError),
        ("idempotency_conflict", ServerError),
        ("already_connected", ServerError),
    ],
)
def test_a_409_maps_by_its_code(code: str, expected: type[FenonError]) -> None:
    error = error_from_response(
        409,
        {"error": {"code": code}},
        request_id=None,
        retry_after_s=None,
        retryable_without_error_body=False,
    )
    assert type(error) is expected
