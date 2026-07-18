"""Non-blocking async HPC job wait (enterprise-readiness Phase 1, item 1.4).

Proves the async poller yields between polls (no blocking sleep), reaches terminal states, fires the
per-poll callback, honors the timeout, and awaits an async status_fn — all with injected sleep/clock
so there are no real waits.
"""

from __future__ import annotations

import pytest

from examlops.hpc_poll import poll_until_complete


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


async def _nosleep(_s):  # injected: advance nothing (clock is driven explicitly)
    return None


@pytest.mark.asyncio
async def test_reaches_terminal_state():
    seq = iter(["PENDING", "RUNNING", "RUNNING", "COMPLETED"])
    result = await poll_until_complete(
        lambda: next(seq), interval_s=1, sleep=_nosleep, clock=_FakeClock()
    )
    assert result["status"] == "COMPLETED"
    assert result["polls"] == 4 and result["timed_out"] is False


@pytest.mark.asyncio
async def test_callback_fires_each_poll():
    seq = iter(["RUNNING", "COMPLETED"])
    seen: list[tuple[str, int]] = []
    await poll_until_complete(
        lambda: next(seq),
        interval_s=1,
        sleep=_nosleep,
        clock=_FakeClock(),
        on_poll=lambda s, n: seen.append((s, n)),
    )
    assert seen == [("RUNNING", 1), ("COMPLETED", 2)]


@pytest.mark.asyncio
async def test_timeout_returns_timed_out():
    clock = _FakeClock()

    async def _advance(_s):
        clock.t += 10  # each sleep advances the clock 10s

    # Never terminal → should time out after >= 25s.
    result = await poll_until_complete(
        lambda: "RUNNING", interval_s=10, timeout_s=25, sleep=_advance, clock=clock
    )
    assert result["timed_out"] is True and result["status"] == "RUNNING"


@pytest.mark.asyncio
async def test_async_status_fn_is_awaited():
    calls = {"n": 0}

    async def _status():
        calls["n"] += 1
        return "COMPLETED" if calls["n"] >= 2 else "RUNNING"

    result = await poll_until_complete(_status, interval_s=1, sleep=_nosleep, clock=_FakeClock())
    assert result["status"] == "COMPLETED" and result["polls"] == 2


@pytest.mark.asyncio
async def test_yields_between_polls():
    """The poller must await sleep (yield) between non-terminal polls — not busy-loop."""
    sleeps = {"n": 0}

    async def _count_sleep(_s):
        sleeps["n"] += 1

    seq = iter(["RUNNING", "RUNNING", "COMPLETED"])
    await poll_until_complete(
        lambda: next(seq), interval_s=5, sleep=_count_sleep, clock=_FakeClock()
    )
    assert (
        sleeps["n"] == 2
    )  # slept after each of the 2 non-terminal polls, not after the terminal one
