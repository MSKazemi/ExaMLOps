"""examlops.resilience — one shared fault-tolerance foundation.

A single home for the retry, circuit-breaker, timeout, and SQLite-hardening logic
that used to be copy-pasted (or missing) across services. Import from here rather
than re-implementing:

    from examlops.resilience import (
        CircuitBreaker, CircuitOpenError,
        retry_call, is_transient_network, is_locked_error,
        request_json, arequest_json,
        httpx_timeout, DB_BUSY_TIMEOUT_MS,
    )
    from examlops.resilience import db  # db.connect / db.harden / db.write_retry
"""

from __future__ import annotations

from . import db
from .circuit import CircuitBreaker, CircuitOpenError
from .http import arequest_json, request_json
from .retry import is_locked_error, is_transient_network, retry_call
from .timeouts import (
    CONNECT_TIMEOUT,
    DB_BUSY_TIMEOUT_MS,
    READ_TIMEOUT,
    httpx_timeout,
)

__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "retry_call",
    "is_transient_network",
    "is_locked_error",
    "request_json",
    "arequest_json",
    "httpx_timeout",
    "CONNECT_TIMEOUT",
    "READ_TIMEOUT",
    "DB_BUSY_TIMEOUT_MS",
    "db",
]
