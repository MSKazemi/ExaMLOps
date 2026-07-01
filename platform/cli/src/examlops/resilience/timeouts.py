"""Central timeout constants for the ExaMLOps platform.

Replaces the hardcoded ``10.0``/``5.0`` timeouts scattered across the bridge,
control plane, dashboard client, and agent tools with one env-configurable source
of truth. Every outbound HTTP call and every SQLite connection should derive its
timeout from here so operators can tune resilience without editing code.
"""

from __future__ import annotations

import os

import httpx


def _f(env: str, default: float) -> float:
    try:
        return float(os.getenv(env, str(default)))
    except (TypeError, ValueError):
        return default


def _i(env: str, default: int) -> int:
    try:
        return int(os.getenv(env, str(default)))
    except (TypeError, ValueError):
        return default


# HTTP timeouts (seconds)
CONNECT_TIMEOUT: float = _f("EXAMLOPS_HTTP_CONNECT_TIMEOUT", 5.0)
READ_TIMEOUT: float = _f("EXAMLOPS_HTTP_READ_TIMEOUT", 10.0)
WRITE_TIMEOUT: float = _f("EXAMLOPS_HTTP_WRITE_TIMEOUT", 10.0)
POOL_TIMEOUT: float = _f("EXAMLOPS_HTTP_POOL_TIMEOUT", 5.0)

# SQLite busy-wait: how long a writer waits for a competing lock before raising
# ``database is locked``. 0 (the SQLite default) means fail immediately.
DB_BUSY_TIMEOUT_MS: int = _i("EXAMLOPS_DB_BUSY_TIMEOUT_MS", 5000)


def httpx_timeout(
    read: float | None = None,
    connect: float | None = None,
) -> httpx.Timeout:
    """Build an ``httpx.Timeout`` from the central constants.

    Callers that need a longer read budget (e.g. model downloads, LLM streams)
    pass ``read=...`` explicitly; everything else inherits the platform defaults.
    """
    return httpx.Timeout(
        connect=CONNECT_TIMEOUT if connect is None else connect,
        read=READ_TIMEOUT if read is None else read,
        write=WRITE_TIMEOUT,
        pool=POOL_TIMEOUT,
    )
