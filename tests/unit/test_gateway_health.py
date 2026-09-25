"""Active health probing (PLAN.md P1 "active probe loop"/"warm preload", ADR 0153, design spec §5)
— `gateway/health.py`.

`ActiveProber`/`WarmKeeper` are the pieces of this file's own logic worth testing directly:
everything they call (`Provider.probe`/`.chat`, `GatewayCore.record_probe_result`/
`.warm_deployments`) is already covered where it's defined. What matters here is each loop's own
contract — every target gets visited, one failure doesn't take the rest of the pass down with it,
a reload is picked up for free, and `start`/`stop` behave like a well-mannered background task
should.
"""

from __future__ import annotations

import asyncio

from examlops.gateway.health import DEFAULT_INTERVAL_S, ActiveProber, WarmKeeper
from examlops.gateway.providers.base import ChatResult, ProbeResult, Usage
from examlops.gateway.routing import Catalog, Deployment, GatewayCore, Route


class FakeProvider:
    """A provider whose `.probe()`/`.chat()` are fully scripted — never touches a network."""

    type = "fake"

    def __init__(
        self,
        name: str,
        *,
        ok: bool = True,
        raises: Exception | None = None,
        resident: list[str] | None = None,
        chat_raises: Exception | None = None,
    ):
        self.name = name
        self.locality = "local"
        self.ok = ok
        self.raises = raises
        self.resident = resident or []
        self.chat_raises = chat_raises
        self.calls = 0
        self.chat_calls: list[str] = []  # models this provider was asked to chat with

    async def probe(self) -> ProbeResult:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return ProbeResult(self.ok, 1.0, resident=self.resident)

    async def chat(self, req) -> ChatResult:
        self.chat_calls.append(req.model)
        if self.chat_raises is not None:
            raise self.chat_raises
        return ChatResult(text="hi", model=req.model, provider=self.name, usage=Usage(1, 1))

    def chat_stream(self, req):  # pragma: no cover - unused by these tests
        raise NotImplementedError

    async def embed(self, model, inputs):  # pragma: no cover - unused by these tests
        raise NotImplementedError

    async def list_models(self):  # pragma: no cover - unused by these tests
        return []


class FakeRuntime:
    """Just enough of `Runtime` for `ActiveProber`/`WarmKeeper` — they only ever read
    `.providers` and `.core`."""

    def __init__(self, providers: dict[str, FakeProvider], core: GatewayCore):
        self.providers = providers
        self.core = core


def make_runtime(*providers: FakeProvider) -> FakeRuntime:
    routes = [Route(p.name, [Deployment(p, "m")]) for p in providers]
    core = GatewayCore(Catalog(routes))
    return FakeRuntime({p.name: p for p in providers}, core)


def make_warm_runtime(*providers: FakeProvider) -> FakeRuntime:
    """Like `make_runtime`, but every deployment is flagged `warm: true`."""
    routes = [Route(p.name, [Deployment(p, "m", warm=True)]) for p in providers]
    core = GatewayCore(Catalog(routes))
    return FakeRuntime({p.name: p for p in providers}, core)


# ── probe_once ───────────────────────────────────────────────────────────────


async def test_probe_once_probes_every_provider_and_feeds_the_breaker():
    healthy, dead = FakeProvider("healthy", ok=True), FakeProvider("dead", ok=False)
    rt = make_runtime(healthy, dead)
    prober = ActiveProber(lambda: rt)

    results = await prober.probe_once()

    assert results == {"healthy": True, "dead": False}
    assert healthy.calls == 1 and dead.calls == 1
    assert rt.core.snapshot()["healthy/m"]["breaker"] == "closed"
    # one failure alone must not open the breaker (fail_threshold=5) — proves record_probe_result
    # was actually reached, not just that the dict entry was set locally
    assert rt.core.snapshot()["dead/m"]["breaker"] == "closed"


async def test_probe_once_records_last_results():
    rt = make_runtime(FakeProvider("a", ok=True))
    prober = ActiveProber(lambda: rt)
    assert prober.last_results == {}
    await prober.probe_once()
    assert prober.last_results == {"a": True}


async def test_a_raising_provider_is_recorded_unhealthy_and_does_not_skip_the_rest():
    boom = FakeProvider("boom", raises=RuntimeError("connection refused"))
    fine = FakeProvider("fine", ok=True)
    rt = make_runtime(boom, fine)
    prober = ActiveProber(lambda: rt)

    results = await prober.probe_once()  # must not raise

    assert results == {"boom": False, "fine": True}
    assert fine.calls == 1  # the raise on `boom` did not prevent `fine` from being probed


async def test_probe_once_forwards_residency_into_record_probe_result():
    """The residency-aware deadline gate (`GatewayCore._min_useful_s`) is only useful if the
    active loop actually feeds it real data — this proves the `ProbeResult.resident` a provider
    reports reaches `GatewayCore`, not just that `probe_once()`'s own return value looks right."""
    a = FakeProvider("a", ok=True, resident=["warm-model"])
    rt = make_runtime(a)
    prober = ActiveProber(lambda: rt)

    await prober.probe_once()

    assert rt.core._resident.get("a") == ["warm-model"]  # noqa: SLF001


async def test_probe_once_on_a_raise_never_erases_prior_residency_data():
    a = FakeProvider("a", ok=True, resident=["m"])
    rt = make_runtime(a)
    prober = ActiveProber(lambda: rt)
    await prober.probe_once()
    assert rt.core._resident.get("a") == ["m"]  # noqa: SLF001

    a.raises = RuntimeError("transient")
    await prober.probe_once()
    assert rt.core._resident.get("a") == ["m"]  # noqa: SLF001 - still there, not wiped by the raise


async def test_probe_once_with_no_runtime_yet_is_a_clean_no_op():
    prober = ActiveProber(lambda: None)
    assert await prober.probe_once() == {}


async def test_probe_once_reads_the_runtime_fresh_each_call_so_a_reload_is_picked_up_free():
    """`get_runtime` is a callable, not a frozen snapshot, exactly so a reload (a whole new
    `Runtime` object replacing the old one) is reflected on the *next* probe without needing to
    rebuild or rebind the prober around the swap."""
    first = make_runtime(FakeProvider("old", ok=True))
    second = make_runtime(FakeProvider("new", ok=True))
    current = [first]
    prober = ActiveProber(lambda: current[0])

    assert await prober.probe_once() == {"old": True}
    current[0] = second  # simulates `state.runtime = new` on reload
    assert await prober.probe_once() == {"new": True}


# ── the breaker actually opens after enough active-probe failures ─────────────


async def test_enough_consecutive_probe_failures_open_the_breaker():
    dead = FakeProvider("dead", ok=False)
    rt = make_runtime(dead)
    prober = ActiveProber(lambda: rt)
    for _ in range(5):  # CircuitBreaker's default fail_threshold
        await prober.probe_once()
    assert rt.core.snapshot()["dead/m"]["breaker"] == "open"


# ── start/stop lifecycle ────────────────────────────────────────────────────


async def test_default_interval_is_a_documented_constant():
    rt = make_runtime(FakeProvider("a"))
    prober = ActiveProber(lambda: rt)
    assert prober.interval_s == DEFAULT_INTERVAL_S


async def test_not_started_is_not_running():
    prober = ActiveProber(lambda: None)
    assert prober.running is False


async def test_a_non_positive_interval_disables_start_entirely():
    """`interval_s <= 0` is "off" (the `RAY_RELOAD_POLL_SECONDS` convention this codebase already
    uses for a background poller), not a tight loop probing continuously with no pause."""
    rt = make_runtime(FakeProvider("a"))
    prober = ActiveProber(lambda: rt, interval_s=0.0)
    prober.start()
    assert prober.running is False
    await asyncio.sleep(0.02)
    assert prober.running is False  # still nothing running, not merely "not yet started"


async def test_start_runs_the_loop_repeatedly_until_stopped():
    a = FakeProvider("a", ok=True)
    rt = make_runtime(a)
    prober = ActiveProber(lambda: rt, interval_s=0.01)

    prober.start()
    try:
        assert prober.running is True
        await asyncio.sleep(0.05)  # several intervals at 0.01s
        assert a.calls >= 2  # the loop actually iterated, not just ran once
    finally:
        await prober.stop()

    assert prober.running is False


async def test_start_is_idempotent_a_second_call_does_not_spawn_a_second_task():
    rt = make_runtime(FakeProvider("a"))
    prober = ActiveProber(lambda: rt, interval_s=10.0)
    prober.start()
    task1 = prober._task  # noqa: SLF001 - the only way to prove no second task was spawned
    prober.start()
    assert prober._task is task1  # noqa: SLF001
    await prober.stop()


async def test_stop_before_start_is_a_clean_no_op():
    prober = ActiveProber(lambda: None)
    await prober.stop()  # must not raise
    assert prober.running is False


async def test_stop_is_idempotent():
    rt = make_runtime(FakeProvider("a"))
    prober = ActiveProber(lambda: rt, interval_s=10.0)
    prober.start()
    await prober.stop()
    await prober.stop()  # must not raise a second time
    assert prober.running is False


# ── WarmKeeper (design spec §5 "Cold start": warm preload) ──────────────────────


async def test_warm_once_pings_only_warm_flagged_deployments():
    warm = FakeProvider("warm")
    cold = FakeProvider("cold")
    warm_rt = make_warm_runtime(warm)
    cold_rt = make_runtime(cold)  # warm=False by default
    combined = FakeRuntime(
        {"warm": warm, "cold": cold},
        GatewayCore(
            Catalog([*warm_rt.core.catalog.routes.values(), *cold_rt.core.catalog.routes.values()])
        ),
    )
    keeper = WarmKeeper(lambda: combined)

    results = await keeper.warm_once()

    assert results == {"warm/m": True}
    assert warm.chat_calls == ["m"]
    assert cold.chat_calls == []  # never pinged — not flagged warm


async def test_warm_once_sends_a_one_token_keep_alive():
    a = FakeProvider("a")
    rt = make_warm_runtime(a)
    keeper = WarmKeeper(lambda: rt)
    await keeper.warm_once()
    assert a.chat_calls == ["m"]  # the request reached the provider with the right model name


async def test_warm_once_records_last_results():
    a = FakeProvider("a")
    rt = make_warm_runtime(a)
    keeper = WarmKeeper(lambda: rt)
    assert keeper.last_results == {}
    await keeper.warm_once()
    assert keeper.last_results == {"a/m": True}


async def test_a_failed_keep_alive_is_recorded_and_does_not_skip_the_rest():
    boom = FakeProvider("boom", chat_raises=RuntimeError("connection refused"))
    fine = FakeProvider("fine")
    boom_rt, fine_rt = make_warm_runtime(boom), make_warm_runtime(fine)
    combined = FakeRuntime(
        {"boom": boom, "fine": fine},
        GatewayCore(
            Catalog([*boom_rt.core.catalog.routes.values(), *fine_rt.core.catalog.routes.values()])
        ),
    )
    keeper = WarmKeeper(lambda: combined)

    results = await keeper.warm_once()  # must not raise

    assert results == {"boom/m": False, "fine/m": True}
    assert fine.chat_calls == ["m"]  # boom's failure did not prevent fine from being pinged


async def test_a_failed_keep_alive_feeds_the_same_breaker_active_probing_uses():
    dead = FakeProvider("dead", chat_raises=RuntimeError("refused"))
    rt = make_warm_runtime(dead)
    keeper = WarmKeeper(lambda: rt)
    for _ in range(5):  # CircuitBreaker's default fail_threshold
        await keeper.warm_once()
    assert rt.core.snapshot()["dead/m"]["breaker"] == "open"


async def test_warm_once_with_no_runtime_yet_is_a_clean_no_op():
    keeper = WarmKeeper(lambda: None)
    assert await keeper.warm_once() == {}


async def test_warm_once_with_nothing_flagged_warm_is_a_clean_no_op():
    a = FakeProvider("a")
    rt = make_runtime(a)  # no warm=True deployments anywhere
    keeper = WarmKeeper(lambda: rt)
    assert await keeper.warm_once() == {}
    assert a.chat_calls == []


async def test_a_non_positive_warm_interval_disables_start_entirely():
    rt = make_warm_runtime(FakeProvider("a"))
    keeper = WarmKeeper(lambda: rt, interval_s=0.0)
    keeper.start()
    assert keeper.running is False


async def test_warm_start_runs_the_loop_repeatedly_until_stopped():
    a = FakeProvider("a")
    rt = make_warm_runtime(a)
    keeper = WarmKeeper(lambda: rt, interval_s=0.01)
    keeper.start()
    try:
        assert keeper.running is True
        await asyncio.sleep(0.05)
        assert len(a.chat_calls) >= 2  # the loop actually iterated
    finally:
        await keeper.stop()
    assert keeper.running is False


async def test_warm_start_is_idempotent():
    rt = make_warm_runtime(FakeProvider("a"))
    keeper = WarmKeeper(lambda: rt, interval_s=10.0)
    keeper.start()
    task1 = keeper._task  # noqa: SLF001
    keeper.start()
    assert keeper._task is task1  # noqa: SLF001
    await keeper.stop()


async def test_warm_stop_before_start_is_a_clean_no_op():
    keeper = WarmKeeper(lambda: None)
    await keeper.stop()  # must not raise
    assert keeper.running is False
