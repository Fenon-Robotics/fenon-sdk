"""Strict validation of payloads against fenon-api's pinned contract (docs/features/contract-sync.md).

``parse_contract`` validates the JSON text in strict mode, so text is never turned into numbers. A payload
that doesn't match raises ``contract_mismatch``, naming at most five failing fields (never their values) and
the pinned ``API_CONTRACT_VERSION``.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import TypeAdapter, ValidationError

from ._generated import API_CONTRACT_VERSION
from .errors import ServerError

_MAX_REPORTED_FIELDS = 5

ShapeT = TypeVar("ShapeT")


def parse_contract(
    shape: TypeAdapter[ShapeT],
    payload: object,
    *,
    what: str,
    request_id: str | None = None,
    status_code: int | None = None,
) -> ShapeT:
    text = payload if isinstance(payload, str | bytes) else json.dumps(payload)
    try:
        return shape.validate_json(text, strict=True)
    except ValidationError as error:
        failures = error.errors(include_url=False, include_input=False, include_context=False)
        fields = "; ".join(_describe(failure) for failure in failures[:_MAX_REPORTED_FIELDS])
        raise ServerError(
            f"The {what} doesn't match fenon-api's contract {API_CONTRACT_VERSION}: {fields}.",
            code="contract_mismatch",
            fix="Upgrade the fenon package; if it is current, report this to Fenon with the request ID.",
            request_id=request_id,
            status_code=status_code,
        ) from None


def _describe(failure: Any) -> str:
    location = ".".join(str(part) for part in failure["loc"]) or "body"
    return f"{location}: {failure['msg']}"
