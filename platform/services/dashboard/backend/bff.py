"""Backend-for-Frontend aggregation substrate (F8 / ADR 0058).

Fans out to multiple data sources concurrently with a **per-source timeout** and composes a
single view-shaped payload. Partial failures never fail the whole response: a failed or
timed-out source is omitted and its name is listed under ``_partial`` so the client can render
a degraded-freshness badge instead of an error page (F8 R2, R5).

This is the sole place fan-out timeout/partial semantics live, so every BFF view endpoint gets
the same resilient behaviour by composing ``aggregate()``.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

# A source is a zero-arg callable returning its view fragment — async OR sync. A sync source is
# run in a worker thread; an async one is awaited on the loop.
Source = Callable[[], Awaitable[Any] | Any]

DEFAULT_TIMEOUT = 5.0


async def _call(fn: Source) -> Any:
    """Invoke one source so that blocking work cannot pin the event loop.

    A plain ``def`` source (sqlite reads, docker SDK, file I/O) runs via ``asyncio.to_thread`` —
    this is what makes the per-source timeout REAL for that class: a coroutine that blocks
    before its first await runs to completion on the loop and ``wait_for`` can never interrupt
    it, which also froze every other request (including /health) for the duration. Async
    sources are awaited as before.
    """
    if inspect.iscoroutinefunction(fn):
        return await fn()
    value = await asyncio.to_thread(fn)
    if inspect.isawaitable(value):
        return await value
    return value


async def aggregate(
    sources: Mapping[str, Source], *, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """Run every source concurrently and merge results into one payload.

    Each source gets its own ``timeout`` (seconds). A source that raises or times out is
    dropped from the payload and recorded under ``_partial`` (sorted). ``asyncio.CancelledError``
    from an outer scope is *not* swallowed (it is a ``BaseException``, not ``Exception``), so
    request cancellation still propagates. Prefer plain ``def`` sources for blocking work —
    see :func:`_call`.
    """

    async def _run(name: str, fn: Source) -> tuple[str, Any, bool]:
        try:
            return name, await asyncio.wait_for(_call(fn), timeout=timeout), True
        except Exception:  # noqa: BLE001 — a partial view beats a 500 for a dashboard
            return name, None, False

    results = await asyncio.gather(*(_run(name, fn) for name, fn in sources.items()))

    payload: dict[str, Any] = {}
    partial: list[str] = []
    for name, value, ok in results:
        if ok:
            payload[name] = value
        else:
            partial.append(name)
    if partial:
        payload["_partial"] = sorted(partial)
    return payload
