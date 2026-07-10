"""Resilient HTTP helpers composing timeout + retry + circuit breaker over httpx.

Sync (:func:`request_json`) and async (:func:`arequest_json`) variants share the
same contract, generalized from the agent's ``skipper/tools/_http.py``:

    returns ``(data, None)`` on success or ``(None, error_string)`` on failure.

Semantics:
  * Transient transport errors (connect/read/timeout) are retried with backoff.
  * A definitive HTTP status error (4xx/5xx) is returned immediately (no retry).
  * An optional :class:`~examlops.resilience.circuit.CircuitBreaker` fast-fails
    the call while a dependency is known-down instead of burning the full timeout.
"""

from __future__ import annotations

import asyncio

import httpx

from .circuit import CircuitBreaker, CircuitOpenError
from .retry import is_transient_network, retry_call
from .timeouts import httpx_timeout


def _format_error(service: str, url: str, exc: Exception) -> str:
    if isinstance(exc, CircuitOpenError):
        return f"Error: {service} circuit open (repeated failures) — not attempting {url}"
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text[:300]
        return f"Error: {service} returned {exc.response.status_code}: {body}"
    return f"Error: cannot reach {service} at {url} — {exc}"


def _parse(resp: httpx.Response):
    resp.raise_for_status()
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        # Non-JSON 2xx body (e.g. a health endpoint returning "OK")
        return resp.text


def request_json(
    service: str,
    method: str,
    url: str,
    *,
    retries: int = 2,
    breaker: CircuitBreaker | None = None,
    read_timeout: float | None = None,
    base_delay: float = 0.1,
    **kwargs,
):
    """Synchronous resilient JSON request. Returns ``(data, None)`` or ``(None, error)``."""

    def _once():
        with httpx.Client(timeout=httpx_timeout(read=read_timeout)) as client:
            return _parse(client.request(method, url, **kwargs))

    def _guarded():
        return breaker.call(_once) if breaker is not None else _once()

    try:
        data = retry_call(
            _guarded,
            retries=retries,
            base_delay=base_delay,
            retry_on=is_transient_network,
            label=f"{service} {method} {url}",
        )
        return data, None
    except (httpx.HTTPStatusError, httpx.RequestError, CircuitOpenError, OSError) as exc:
        return None, _format_error(service, url, exc)


async def arequest_json(
    service: str,
    method: str,
    url: str,
    *,
    retries: int = 2,
    breaker: CircuitBreaker | None = None,
    read_timeout: float | None = None,
    base_delay: float = 0.1,
    client: httpx.AsyncClient | None = None,
    **kwargs,
):
    """Async resilient JSON request. Returns ``(data, None)`` or ``(None, error)``.

    Retries transient transport errors with backoff (via ``asyncio.sleep``); returns
    HTTP status errors immediately. An optional shared ``client`` is reused across
    calls; otherwise a short-lived client is created per attempt.
    """

    async def _once() -> object:
        if breaker is not None and breaker.state == CircuitBreaker.OPEN:
            raise CircuitOpenError(breaker.name)
        try:
            if client is not None:
                resp = await client.request(method, url, **kwargs)
            else:
                async with httpx.AsyncClient(timeout=httpx_timeout(read=read_timeout)) as c:
                    resp = await c.request(method, url, **kwargs)
            data = _parse(resp)
        except BaseException as exc:  # noqa: BLE001 — feed breaker then re-raise
            if breaker is not None and not isinstance(exc, httpx.HTTPStatusError):
                breaker._on_failure()  # transport failure trips the breaker
            raise
        else:
            if breaker is not None:
                breaker._on_success()
            return data

    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return await _once(), None
        except httpx.HTTPStatusError as exc:
            return None, _format_error(service, url, exc)
        except CircuitOpenError as exc:
            return None, _format_error(service, url, exc)
        except (httpx.RequestError, OSError) as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(min(5.0, base_delay * (2**attempt)))
    return None, _format_error(service, url, last_exc)  # type: ignore[arg-type]
