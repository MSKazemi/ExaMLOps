"""Unified retry-with-exponential-backoff and transient-error classifiers.

Replaces four incompatible ad-hoc retry idioms (agent ``_http.py``, dashboard
``control_plane_client.py``, control-plane ``PrefectGateway``, and the raw
``urllib``/``boto3`` calls in the pipeline). One backoff curve, one place to
decide what "transient" means.

Classifiers are cheap predicates so call sites can compose them, e.g.::

    retry_call(fn, retry_on=lambda e: is_transient_network(e) or is_locked_error(e))
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

# httpx is optional at import time (pipelines/DB code may not have it on the path).
try:  # pragma: no cover - trivial import guard
    import httpx

    _HTTPX_REQUEST_ERROR: tuple[type[BaseException], ...] = (httpx.RequestError,)
except Exception:  # pragma: no cover
    _HTTPX_REQUEST_ERROR = ()


def is_transient_network(exc: BaseException) -> bool:
    """True for connection/timeout errors that are worth retrying.

    Covers httpx transport errors and the stdlib ``ConnectionError``/``TimeoutError``
    families (raised by ``urllib``, ``boto3``, sockets). Does NOT match HTTP status
    errors (a 4xx/5xx is a definitive server response — the caller decides).
    """
    if _HTTPX_REQUEST_ERROR and isinstance(exc, _HTTPX_REQUEST_ERROR):
        return True
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def is_locked_error(exc: BaseException) -> bool:
    """True for SQLite ``database is locked`` / ``busy`` contention errors."""
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        return "locked" in msg or "busy" in msg
    return False


def retry_call[T](
    fn: Callable[[], T],
    *,
    retries: int = 2,
    base_delay: float = 0.1,
    max_delay: float = 5.0,
    retry_on: Callable[[BaseException], bool] = is_transient_network,
    sleep: Callable[[float], None] = time.sleep,
    label: str = "call",
) -> T:
    """Call ``fn``; retry on exceptions matching ``retry_on`` with exponential backoff.

    Retries up to ``retries`` times (so ``retries + 1`` total attempts). Delay for
    attempt ``i`` is ``min(max_delay, base_delay * 2**i)``. Non-matching exceptions
    propagate immediately. The final matching exception is re-raised after exhaustion.

    ``sleep`` is injectable so tests can run without real delays.
    """
    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            if not retry_on(exc):
                raise
            last_exc = exc
            if attempt < retries:
                delay = min(max_delay, base_delay * (2**attempt))
                logger.debug(
                    "%s failed (attempt %d/%d): %s — retrying in %.2fs",
                    label,
                    attempt + 1,
                    retries + 1,
                    exc,
                    delay,
                )
                sleep(delay)
    assert last_exc is not None  # unreachable: loop only exits here after a failure
    raise last_exc
