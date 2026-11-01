"""Core services shared by every Fenon model: HTTP, errors, logging, configuration, uploads, runs, contracts.

This is the package's whole interface: ``_live``, ``models`` and the client import from ``fenon._core``, never
from its modules (docs/CODEBASE.md; ``tests/test_package.py`` enforces it).
"""

from ._generated import Run
from ._generated import RunUsage as Usage
from .config import LIMITS
from .contract import parse_contract
from .errors import (
    AuthError,
    CreditsError,
    FenonError,
    InvalidInputError,
    LiveSessionError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    RunFailedError,
    ServerError,
    SessionLimitError,
    UnsupportedModelError,
    error_from_response,
    retry_after_of,
)
from .http import ApiClient, ApiResponse, ClientConfig, HttpClients, Runtime, decode_json, open_http_clients
from .logs import configure_logging, logger
from .runs import ContractSupport, Job, RunOutput, create_run
from .settings import Settings
from .uploads import VIDEO_CONTENT_TYPES, upload_file

__all__ = [
    "LIMITS",
    "VIDEO_CONTENT_TYPES",
    "ApiClient",
    "ApiResponse",
    "AuthError",
    "ClientConfig",
    "ContractSupport",
    "CreditsError",
    "FenonError",
    "HttpClients",
    "InvalidInputError",
    "Job",
    "LiveSessionError",
    "NetworkError",
    "NotFoundError",
    "RateLimitError",
    "Run",
    "RunFailedError",
    "RunOutput",
    "Runtime",
    "ServerError",
    "SessionLimitError",
    "Settings",
    "UnsupportedModelError",
    "Usage",
    "configure_logging",
    "create_run",
    "decode_json",
    "error_from_response",
    "logger",
    "open_http_clients",
    "parse_contract",
    "retry_after_of",
    "upload_file",
]
