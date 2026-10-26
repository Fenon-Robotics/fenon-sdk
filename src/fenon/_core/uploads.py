"""Upload service: measure a local file, reserve an upload, send the bytes, and renew the reservation once."""

from __future__ import annotations

from pathlib import Path

from ._generated import UploadReservation
from .errors import ServerError, StorageUrlRejectedError
from .http import HttpClients, measure_upload_file
from .logs import logger
from .settings import LIMITS


def upload_file(clients: HttpClients, path: Path, *, content_type: str) -> str:
    size_bytes = measure_upload_file(path)
    body = {"filename": path.name, "content_type": content_type, "size_bytes": size_bytes}
    for _ in range(LIMITS.upload_reservations):
        reservation = clients.api.request("POST", "/uploads", json_body=body).parse(UploadReservation)
        try:
            clients.storage.upload_file(
                reservation.upload_url,
                method=reservation.method,
                headers=reservation.headers,
                path=path,
                size_bytes=size_bytes,
            )
        except StorageUrlRejectedError:
            logger.info("upload %s refused by storage", reservation.id)
            continue
        return reservation.id
    raise ServerError(
        "Storage refused the upload again after a new reservation.",
        code="upload_rejected",
        fix="Try again later.",
    )
