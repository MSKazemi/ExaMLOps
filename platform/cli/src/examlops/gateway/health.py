"""Active health probing (ADR 0153; PLAN.md P1's "active probe loop").

Passive health — recorded from real request/response outcomes — already lives inline in
:class:`~examlops.gateway.routing.DeploymentState` (``ok``/``errors``/``ewma_ttft_ms``, updated by
every attempt `GatewayCore` makes). This module is the *active* counterpart the design's package
layout table named but never built: independent of request traffic, it periodically calls every
configured provider's own :meth:`~examlops.gateway.providers.base.Provider.probe` and feeds the
result into the same circuit breakers real requests already share
(:meth:`~examlops.gateway.routing.GatewayCore.record_probe_result`), so a dead upstream is caught
*before* the next real request reaches it — during a quiet period with no traffic at all, passive
health never gets a sample to learn from; this is what closes that gap.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import Callable

from examlops.gateway.config import Runtime
from examlops.gateway.providers.base import ChatRequest

logger = logging.getLogger(__name__)

#: A quiet-by-default cadence — a probe is a real network call to every configured provider, so
#: this trades detection latency against load; PLAN.md names no specific number, and every other
#: gateway timing knob in this package defaults to a round, documented value rather than an
#: arbitrary one, so this follows the same convention.
DEFAULT_INTERVAL_S = 30.0
#: Design spec §5 "Cold start" says "every keep_alive/2" — deriving that would mean parsing every
#: provider's own `keep_alive` duration string (Ollama's format: "30m", "1h", a bare-seconds
#: number, "-1" for never-unload), which this deliberately does not attempt. A fixed, documented
#: default is an honest simplification; a real per-deployment `keep_alive/2` derivation is tracked
#: as future work rather than silently approximated and called equivalent.
DEFAULT_WARM_INTERVAL_S = 300.0


class ActiveProber:
    """Owns one background ``asyncio`` task that probes every provider on a fixed interval.

    Started/stopped explicitly (:meth:`start`/:meth:`stop`), never on construction — the service's
    own lifespan decides exactly when this runs, the same as every other background piece here
    (the config file has no analogous poller today, but this follows the shape one would take).

    ``get_runtime`` is a callable, not a fixed :class:`Runtime` snapshot, because the service's
    runtime can be replaced wholesale by a reload (``POST /admin/reload``) — reading it fresh on
    every tick means a reload is picked up for free on the *next* probe, with no need to restart
    or rebind this prober around the swap.
    """

    def __init__(
        self,
        get_runtime: Callable[[], Runtime | None],
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self._get_runtime = get_runtime
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        #: The most recent pass's results, provider name → probe outcome — read by `/admin/health`
        #: or a test; never authoritative on its own (the breaker state it already wrote into is).
        self.last_results: dict[str, bool] = {}

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Idempotent: a second call while already running is a no-op, not a second task.

        ``interval_s <= 0`` disables active probing entirely (the ``RAY_RELOAD_POLL_SECONDS``
        convention this codebase already uses elsewhere for "0 = off") rather than spinning a
        tight loop that probes continuously with no pause between passes.
        """
        if self.interval_s > 0 and not self.running:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            await self.probe_once()
            await asyncio.sleep(self.interval_s)

    async def probe_once(self) -> dict[str, bool]:
        """One probe pass over every currently-configured provider.

        Public and directly callable (not only via the interval loop) so a test, or a future
        manual "probe now" admin action, can trigger exactly one pass without waiting on — or
        needing to run — the background task at all. Never raises: a provider whose own
        ``probe()`` raises is recorded as unhealthy, the same as one that answers ``ok=False``,
        rather than aborting the whole pass over every *other* provider.
        """
        rt = self._get_runtime()
        if rt is None:
            return {}
        results: dict[str, bool] = {}
        for name, provider in rt.providers.items():
            resident: list[str] | None = None
            try:
                probed = await provider.probe()
                ok = probed.ok
                resident = probed.resident  # `None` on a raise below: leave prior data untouched
            except Exception as exc:  # noqa: BLE001 - one bad provider must not skip the rest
                logger.warning(
                    "active probe for provider %r raised %s: %s", name, type(exc).__name__, exc
                )
                ok = False
            results[name] = ok
            rt.core.record_probe_result(name, ok, resident=resident)
        self.last_results = results
        return results


class WarmKeeper:
    """Owns one background ``asyncio`` task that sends a minimal keep-alive chat to every
    deployment flagged ``warm: true`` (design spec §5 "Cold start"), so a model an operator has
    decided must always answer fast never goes cold from being merely quiet for a while.

    Same shape as :class:`ActiveProber` deliberately — explicit ``start``/``stop``, a callable
    ``get_runtime`` rather than a frozen snapshot so a reload's new warm list is picked up on the
    next tick, ``interval_s <= 0`` disables it. A successful or failed ping feeds the same breaker
    :meth:`~examlops.gateway.routing.GatewayCore.record_probe_result` already shares with active
    probing — a keep-alive is a real signal about the provider's health, the same class of
    out-of-band evidence a probe is, not a real user-attributable request.
    """

    def __init__(
        self,
        get_runtime: Callable[[], Runtime | None],
        *,
        interval_s: float = DEFAULT_WARM_INTERVAL_S,
    ) -> None:
        self._get_runtime = get_runtime
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        #: Deployment key → whether its last keep-alive ping succeeded.
        self.last_results: dict[str, bool] = {}

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Idempotent, and "at start" per the design spec: the first warm pass runs as soon as
        the loop's task is scheduled, not after waiting a full `interval_s` — the same shape
        `ActiveProber.start` already uses."""
        if self.interval_s > 0 and not self.running:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            await self.warm_once()
            await asyncio.sleep(self.interval_s)

    async def warm_once(self) -> dict[str, bool]:
        """One keep-alive pass over every currently-``warm``-flagged deployment.

        Public and directly callable (not only via the interval loop), matching
        :meth:`ActiveProber.probe_once`. Never raises: a deployment whose ping fails is recorded
        as a failed keep-alive, the same as any other out-of-band health signal, rather than
        aborting the pass over every *other* warm deployment.
        """
        rt = self._get_runtime()
        if rt is None:
            return {}
        results: dict[str, bool] = {}
        req = ChatRequest(model="", messages=[{"role": "user", "content": "hi"}], max_tokens=1)
        for dep in rt.core.warm_deployments():
            try:
                await dep.provider.chat(dataclasses.replace(req, model=dep.model))
                ok = True
            except Exception as exc:  # noqa: BLE001 - one bad deployment must not skip the rest
                logger.warning(
                    "warm keep-alive for %s raised %s: %s", dep.key, type(exc).__name__, exc
                )
                ok = False
            results[dep.key] = ok
            rt.core.record_probe_result(dep.provider.name, ok)
        self.last_results = results
        return results
