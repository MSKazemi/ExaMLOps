"""Non-blocking HPC job wait — async poll/callback (Phase 1 item 1.4).

An HPC job can queue + run for up to 24h. The mock/slurm adapters ``wait_until_complete`` with a
blocking ``time.sleep`` loop, so a worker (or the control-plane thread) is pinned for the whole job —
at fleet scale that's thousands of blocked workers. This is the async replacement: ``poll_until_complete``
awaits between polls, yielding the event loop so one worker can shepherd many jobs concurrently, with a
per-poll callback (for progress/heartbeat), a hard timeout, and injectable sleep/clock so it's testable
without real waits. Wire it behind the ``SchedulerAdapter`` seam; the status_fn may be sync or async.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

_DEFAULT_TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY"})


async def poll_until_complete(
    status_fn: Callable[[], Any],
    *,
    terminal: frozenset[str] = _DEFAULT_TERMINAL,
    interval_s: float = 30.0,
    timeout_s: float | None = 24 * 3600,
    on_poll: Callable[[str, int], Any] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Poll ``status_fn`` until it returns a terminal state (non-blocking).

    Returns ``{"status", "polls", "timed_out", "elapsed_s"}``. ``status_fn`` and ``on_poll`` may be
    sync or async (awaited if a coroutine is returned). Between polls it ``await sleep(interval_s)``,
    so the calling coroutine yields — the whole point vs the blocking ``time.sleep`` loop. A
    ``timeout_s`` of ``None`` waits forever; otherwise it returns with ``timed_out=True`` once elapsed.
    """
    clock = clock or asyncio.get_event_loop().time
    start = clock()
    polls = 0
    while True:
        result = status_fn()
        if asyncio.iscoroutine(result):
            result = await result
        status = str(result)
        polls += 1
        if on_poll is not None:
            cb = on_poll(status, polls)
            if asyncio.iscoroutine(cb):
                await cb
        if status in terminal:
            return {
                "status": status,
                "polls": polls,
                "timed_out": False,
                "elapsed_s": clock() - start,
            }
        if timeout_s is not None and (clock() - start) >= timeout_s:
            return {
                "status": status,
                "polls": polls,
                "timed_out": True,
                "elapsed_s": clock() - start,
            }
        await sleep(interval_s)
