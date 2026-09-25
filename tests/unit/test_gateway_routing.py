"""Routing core (ADR 0153 d1–d9): candidates, failover, breaker, budget, locality, streaming.

Providers are scripted fakes, so each failure class is injected on purpose and every claim in the
ADR's error-classification table has a test that would fail if it stopped being true.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from examlops.gateway.providers import (
    Capabilities,
    ChatChunk,
    ChatRequest,
    ChatResult,
    ProviderError,
    Usage,
)
from examlops.gateway.resilience import CircuitBreaker, RetryBudget
from examlops.gateway.routing import Catalog, Deployment, GatewayCore, Route


class Fake:
    """A scripted provider. ``fail`` is raised on every call; ``script`` is consumed first."""

    type = "fake"

    def __init__(self, name, locality="local", *, fail=None, script=None, delay=0.0, chunks=None):
        self.name, self.locality = name, locality
        self.fail, self.script, self.delay = fail, list(script or []), delay
        self.chunks = chunks if chunks is not None else ["a", "b", "c"]
        self.stream_error_after: int | None = None
        self.stream_error_kind = "stream_interrupted"
        self.calls = 0
        self.inflight = 0

    def _err(self, e):
        e.provider = self.name
        return e

    async def chat(self, req):
        self.calls += 1
        self.inflight += 1
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            item = self.script.pop(0) if self.script else self.fail
            if item is not None:
                raise self._err(item)
            return ChatResult(
                text="ok", model=req.model, provider=self.name, usage=Usage(3, 2), ttft_ms=12.0
            )
        finally:
            self.inflight -= 1

    async def chat_stream(self, req):
        self.calls += 1
        item = self.script.pop(0) if self.script else self.fail
        if item is not None:
            raise self._err(item)
        for i, text in enumerate(self.chunks):
            if self.stream_error_after is not None and i == self.stream_error_after:
                raise self._err(ProviderError(self.stream_error_kind, "cut"))
            yield ChatChunk(text=text, ttft_ms=5.0 if i == 0 else None)
        yield ChatChunk(finish_reason="stop", usage=Usage(3, len(self.chunks)))

    async def embed(self, model, inputs): ...
    async def list_models(self):
        return []

    async def probe(self): ...


def upstream_error():
    return ProviderError("upstream_error", "boom")


def req(**kw):
    kw.setdefault("model", "m")
    kw.setdefault("messages", [{"role": "user", "content": "hi"}])
    return ChatRequest(**kw)


def core(routes, *, aliases=None, **kw):
    kw.setdefault("rng", random.Random(1))
    return GatewayCore(Catalog(routes, aliases or {}), **kw)


def dep(p, model="m", **kw):
    return Deployment(p, model, **kw)


# ── basic routing ─────────────────────────────────────────────────────────────


async def test_priority_serves_from_the_first_healthy_deployment_and_records_the_attempt():
    a, b = Fake("a"), Fake("b")
    attempts: list = []
    res = await core([Route("r", [dep(a), dep(b)])]).chat("r", req(), attempts=attempts)
    assert res.provider == "a" and a.calls == 1 and b.calls == 0
    assert [(x["provider"], x["outcome"]) for x in attempts] == [("a", "ok")]


async def test_aliases_resolve_and_unknown_models_are_typed():
    a = Fake("a")
    c = core([Route("r", [dep(a)])], aliases={"default": "r"})
    assert (await c.chat("default", req())).provider == "a"
    with pytest.raises(ProviderError) as ei:
        await c.chat("nope", req())
    assert ei.value.kind == "model_not_found"


async def test_failover_to_the_next_deployment_on_an_upstream_fault():
    a, b = Fake("a", fail=upstream_error()), Fake("b")
    attempts: list = []
    res = await core([Route("r", [dep(a), dep(b)])]).chat("r", req(), attempts=attempts)
    assert res.provider == "b"
    assert [(x["provider"], x["outcome"]) for x in attempts] == [
        ("a", "upstream_error"),
        ("b", "ok"),
    ]


async def test_a_client_error_is_raised_and_never_failed_over():
    a = Fake("a", fail=ProviderError("invalid_request", "bad"))
    b = Fake("b")
    with pytest.raises(ProviderError) as ei:
        await core([Route("r", [dep(a), dep(b)])]).chat("r", req())
    assert ei.value.kind == "invalid_request" and b.calls == 0
    assert ei.value.attempts[0]["provider"] == "a"


async def test_a_deployment_that_lacks_the_model_fails_over_without_tripping_its_breaker():
    a = Fake("a", fail=ProviderError("model_not_found", "no such model"))
    b = Fake("b")
    c = core([Route("r", [dep(a), dep(b)])])
    assert (await c.chat("r", req())).provider == "b"
    assert c.snapshot()["a/m"]["breaker"] == "closed"


async def test_fallback_routes_are_tried_after_the_primary_deployments():
    a, s = Fake("a", fail=upstream_error()), Fake("small")
    c = core([Route("r", [dep(a)], fallbacks=["s"]), Route("s", [dep(s)])])
    assert (await c.chat("r", req())).provider == "small"


async def test_all_failing_raises_the_last_error_with_every_attempt():
    a, b = (
        Fake("a", fail=upstream_error()),
        Fake("b", fail=ProviderError("upstream_timeout", "slow")),
    )
    with pytest.raises(ProviderError) as ei:
        await core([Route("r", [dep(a), dep(b)])]).chat("r", req())
    assert ei.value.kind == "upstream_timeout"
    assert [x["provider"] for x in ei.value.attempts] == ["a", "b"]


# ── locality (ADR 0154 d3) ────────────────────────────────────────────────────


async def test_an_external_only_route_is_denied_by_default():
    ext = Fake("omni", "external")
    with pytest.raises(ProviderError) as ei:
        await core([Route("r", [dep(ext, external_ok=True)])]).chat("r", req())
    assert ei.value.kind == "locality_denied" and ext.calls == 0


async def test_external_needs_both_the_deployment_switch_and_the_callers_permission():
    ext = Fake("omni", "external")
    permitted = ("local", "site", "external")
    with pytest.raises(ProviderError) as ei:  # caller permits, deployment does not
        await core([Route("r", [dep(ext)])]).chat("r", req(), allowed_localities=permitted)
    assert ei.value.kind == "locality_denied" and ext.calls == 0
    ok = await core([Route("r", [dep(ext, external_ok=True)])]).chat(
        "r", req(), allowed_localities=permitted
    )
    assert ok.provider == "omni"


async def test_a_fallback_can_never_widen_locality():
    local, ext = Fake("local", fail=upstream_error()), Fake("omni", "external")
    c = core([Route("r", [dep(local)], fallbacks=["e"]), Route("e", [dep(ext, external_ok=True)])])
    with pytest.raises(ProviderError) as ei:
        await c.chat("r", req())
    assert ei.value.kind == "upstream_error"  # the real failure, not a silent external answer
    assert ext.calls == 0


def _random_locality_topology(rng: random.Random) -> tuple[list[Route], dict[str, Fake]]:
    """A random route/fallback graph (PLAN.md P5: "fallback widening property test", spec §12.4).

    Every deployment gets its own never-shared `Fake` provider so "was this specific deployment
    permitted?" is unambiguous — a provider reused across two routes with different `external_ok`
    would make that question meaningless. Fallback edges are drawn freely, including edges that
    form cycles or point back at the start: `_chain`'s BFS is documented cycle-safe, so a property
    test of this exact claim should not dodge the case that claim exists to cover.
    """
    n_routes = rng.randint(2, 5)
    names = [f"r{i}" for i in range(n_routes)]
    routes: list[Route] = []
    providers_by_name: dict[str, Fake] = {}
    for name in names:
        deps = []
        for j in range(rng.randint(1, 3)):
            loc = rng.choice(_ALL_LOCALITIES)
            ext_ok = rng.choice([True, False])
            provider = Fake(f"{name}-p{j}", loc)
            deps.append(dep(provider, external_ok=ext_ok))
            providers_by_name[provider.name] = provider
        fallbacks = rng.sample(names, k=rng.randint(0, min(2, n_routes)))
        routes.append(Route(name, deps, fallbacks=fallbacks))
    return routes, providers_by_name


@pytest.mark.parametrize("trial", range(200))
async def test_property_a_fallback_never_widens_locality_across_random_topologies(trial):
    """200 randomized route/fallback graphs (deterministic per `trial`, so a failure reproduces by
    its parametrize id): whatever the topology, the request either lands on a deployment permitted
    under `allowed`/`external_ok`, or fails outright — a non-permitted deployment's `Fake.calls`
    must be 0 in either case. This is the structural guarantee behind ADR 0154 d3's claim (checked
    here as a property, not just the one worked example above), not a probabilistic one — every
    trial must hold, not "most"."""
    rng = random.Random(20260924_000 + trial)
    routes, providers_by_name = _random_locality_topology(rng)
    allowed = tuple(rng.sample(_ALL_LOCALITIES, k=rng.randint(1, len(_ALL_LOCALITIES))))

    all_deps: dict[str, Deployment] = {d.provider.name: d for r in routes for d in r.deployments}
    permitted_names = {
        name
        for name, d in all_deps.items()
        if d.provider.locality in allowed and (d.provider.locality != "external" or d.external_ok)
    }

    c = core(routes, rng=random.Random(rng.random()))
    try:
        result = await c.chat(routes[0].name, req(), allowed_localities=allowed)
    except ProviderError:
        result = None

    called = {name for name, p in providers_by_name.items() if p.calls > 0}
    assert called <= permitted_names, (
        f"trial {trial}: a locality-forbidden deployment was called "
        f"(allowed={allowed}, called={called}, permitted={permitted_names})"
    )
    if result is not None:
        assert result.provider in permitted_names


# ── capability ────────────────────────────────────────────────────────────────


async def test_a_request_needing_tools_never_reaches_a_model_without_them():
    plain = Fake("plain")
    d = dep(plain, capabilities=Capabilities(tools=False))
    with pytest.raises(ProviderError) as ei:
        await core([Route("r", [d])]).chat("r", req(tools=[{"type": "function"}]))
    assert ei.value.kind == "capability_unavailable" and "tools" in ei.value.message
    assert plain.calls == 0


async def test_capability_routing_picks_the_capable_deployment():
    plain, tooly = Fake("plain"), Fake("tooly")
    c = core(
        [
            Route(
                "r",
                [
                    dep(plain, capabilities=Capabilities(tools=False)),
                    dep(tooly, capabilities=Capabilities(tools=True)),
                ],
            )
        ]
    )
    assert (await c.chat("r", req(tools=[{"type": "function"}]))).provider == "tooly"


async def test_unknown_capabilities_are_permissive_and_context_windows_are_enforced():
    unknown = Fake("u")
    assert (await core([Route("r", [dep(unknown)])]).chat("r", req(tools=[{}]))).provider == "u"
    small = dep(Fake("s"), capabilities=Capabilities(context_window=10))
    with pytest.raises(ProviderError) as ei:
        await core([Route("r", [small])]).chat(
            "r", req(messages=[{"role": "user", "content": "x" * 400}])
        )
    assert ei.value.kind == "capability_unavailable" and "context" in ei.value.message


async def test_an_image_request_needs_a_vision_model():
    text_only = dep(Fake("t"), capabilities=Capabilities(vision=False))
    msg = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}]
    with pytest.raises(ProviderError) as ei:
        await core([Route("r", [text_only])]).chat("r", req(messages=msg))
    assert ei.value.kind == "capability_unavailable"


# ── breaker integration ───────────────────────────────────────────────────────


def fast_breaker():
    return CircuitBreaker(fail_threshold=2, jitter=0.0, base_open_s=60)


async def test_an_open_breaker_is_skipped_and_all_open_is_unavailable():
    a, b = Fake("a", fail=upstream_error()), Fake("b")
    c = core([Route("r", [dep(a), dep(b)])], breaker_factory=fast_breaker)
    for _ in range(3):
        await c.chat("r", req())
    assert a.calls == 2  # opened after two failures, then skipped
    assert c.snapshot()["a/m"]["breaker"] == "open"

    only = Fake("only", fail=upstream_error())
    c2 = core([Route("r", [dep(only)])], breaker_factory=fast_breaker)
    for _ in range(2):
        with pytest.raises(ProviderError):
            await c2.chat("r", req())
    with pytest.raises(ProviderError) as ei:
        await c2.chat("r", req())
    assert ei.value.kind == "upstream_unavailable" and only.calls == 2


async def test_rate_limits_fail_over_but_do_not_trip_the_breaker():
    a, b = Fake("a", fail=ProviderError("rate_limited", "slow", retry_after=1)), Fake("b")
    c = core(
        [Route("r", [dep(a), dep(b)])],
        breaker_factory=fast_breaker,
        retry_budget=RetryBudget(ratio=1.0, min_retries=100),  # this test is about the breaker
    )
    for _ in range(6):
        assert (await c.chat("r", req())).provider == "b"
    assert c.snapshot()["a/m"]["breaker"] == "closed"


# ── retry budget ──────────────────────────────────────────────────────────────


async def test_a_retry_storm_is_bounded_by_the_budget():
    bad, good = Fake("bad", fail=upstream_error()), Fake("good")
    c = core(
        [Route("r", [dep(bad), dep(good)])],
        breaker_factory=lambda: CircuitBreaker(fail_threshold=10**9, error_ratio=2.0),
        retry_budget=RetryBudget(ratio=0.2, min_retries=3),
    )
    served = failed = 0
    for _ in range(300):
        try:
            await c.chat("r", req())
            served += 1
        except ProviderError as e:
            assert e.kind == "upstream_unavailable" and "retry budget" in e.message
            failed += 1
    assert good.calls == served <= 0.2 * 300 + 3  # only a bounded share was re-driven
    assert failed == 300 - served  # the rest failed fast instead of amplifying load


# ── deadline and bulkhead ─────────────────────────────────────────────────────


async def test_the_callers_budget_bounds_an_attempt():
    slow = Fake("slow", delay=0.5)
    with pytest.raises(ProviderError) as ei:
        await core([Route("r", [dep(slow)])]).chat("r", req(), caller_budget_ms=50)
    assert ei.value.kind == "upstream_timeout"


async def test_no_attempt_starts_once_the_budget_is_spent():
    a, b = Fake("a", delay=0.08, fail=upstream_error()), Fake("b")
    with pytest.raises(ProviderError) as ei:
        await core([Route("r", [dep(a), dep(b)])]).chat("r", req(), caller_budget_ms=60)
    assert b.calls == 0
    assert ei.value.kind == "upstream_timeout"


async def test_a_full_bulkhead_fails_over_for_free():
    a, b = Fake("a", delay=0.15), Fake("b")
    c = core(
        [Route("r", [dep(a, max_concurrency=1, max_queue=0), dep(b)])],
        retry_budget=RetryBudget(min_retries=0, ratio=0.0),  # a capacity failover costs no retry
    )
    first = asyncio.create_task(c.chat("r", req()))
    await asyncio.sleep(0.02)
    second = await c.chat("r", req())
    assert second.provider == "b"  # queue_full on `a` → straight to `b`
    assert (await first).provider == "a"
    assert c.snapshot()["a/m"]["breaker"] == "closed"


# ── strategies ────────────────────────────────────────────────────────────────


async def test_weighted_spreads_in_proportion_and_is_reproducible():
    def run(seed):
        a, b = Fake("a"), Fake("b")
        c = core(
            [Route("r", [dep(a, weight=3), dep(b, weight=1)], strategy="weighted")],
            rng=random.Random(seed),
        )
        return c, a, b

    c, a, b = run(7)
    for _ in range(400):
        await c.chat("r", req())
    assert 0.65 < a.calls / 400 < 0.85
    c2, a2, _ = run(7)
    for _ in range(400):
        await c2.chat("r", req())
    assert a2.calls == a.calls  # same seed, same decisions


async def test_least_inflight_prefers_the_idler_deployment():
    busy, idle = Fake("busy", delay=0.2), Fake("idle")
    c = core([Route("r", [dep(busy), dep(idle)], strategy="least_inflight")])
    hold = asyncio.create_task(c.chat("r", req()))  # goes to `busy` (tie → first)
    await asyncio.sleep(0.03)
    assert (await c.chat("r", req())).provider == "idle"
    await hold


async def test_lowest_latency_learns_from_observed_ttft():
    slow, fast = Fake("slow"), Fake("fast")
    c = core([Route("r", [dep(slow), dep(fast)], strategy="lowest_latency")])
    c._state(c.catalog.routes["r"].deployments[0]).ewma_ttft_ms = 900.0
    c._state(c.catalog.routes["r"].deployments[1]).ewma_ttft_ms = 20.0
    assert (await c.chat("r", req())).provider == "fast"


async def test_priority_groups_are_honoured_before_the_strategy():
    lo, hi = Fake("lo"), Fake("hi")
    c = core([Route("r", [dep(lo, priority=1), dep(hi, priority=0)])])
    assert (await c.chat("r", req())).provider == "hi"


_ALL_LOCALITIES = ("local", "site", "external")


async def test_cost_aware_prefers_local_over_external(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_LLM_ROUTING_PROVIDER", raising=False)
    cloud, onsite = Fake("cloud", locality="external"), Fake("onsite", locality="local")
    c = core([Route("r", [dep(cloud, external_ok=True), dep(onsite)], strategy="cost_aware")])
    result = await c.chat("r", req(), allowed_localities=_ALL_LOCALITIES)
    assert result.provider == "onsite"


async def test_cost_aware_ties_among_externals_keep_the_original_order():
    a, b = Fake("a", locality="external"), Fake("b", locality="external")
    c = core(
        [Route("r", [dep(a, external_ok=True), dep(b, external_ok=True)], strategy="cost_aware")]
    )
    result = await c.chat("r", req(), allowed_localities=_ALL_LOCALITIES)
    assert result.provider == "a"  # equal marker cost: stable sort, no churn


async def test_cost_aware_consults_the_llm_routing_provider(monkeypatch):
    """A custom llm_routing provider can override which deployment `cost_aware` prefers."""
    from examlops.providers import Provider, ProviderMeta, register_provider

    class PreferExternal(Provider):
        name, version = "prefer-external", "1.0"

        def metadata(self):
            return ProviderMeta(outputs=("score",), params=("cost_usd",))

        def compute(self, inputs):
            return {"score": inputs["cost_usd"]}  # inverted: a HIGHER cost now wins

    register_provider("llm_routing", "prefer-external", PreferExternal)
    monkeypatch.setenv("EXAMLOPS_LLM_ROUTING_PROVIDER", "prefer-external")
    cloud, onsite = Fake("cloud", locality="external"), Fake("onsite", locality="local")
    c = core([Route("r", [dep(onsite), dep(cloud, external_ok=True)], strategy="cost_aware")])
    result = await c.chat("r", req(), allowed_localities=_ALL_LOCALITIES)
    assert result.provider == "cloud"


# ── streaming ─────────────────────────────────────────────────────────────────


async def collect(agen):
    return [c async for c in agen]


async def test_stream_fails_over_before_the_first_chunk():
    a, b = Fake("a", fail=upstream_error()), Fake("b")
    attempts: list = []
    chunks = await collect(
        core([Route("r", [dep(a), dep(b)])]).chat_stream("r", req(), attempts=attempts)
    )
    assert "".join(ch.text for ch in chunks) == "abc"
    assert [x["outcome"] for x in attempts] == ["upstream_error", "ok"]


async def test_stream_never_fails_over_after_the_first_chunk():
    a, b = Fake("a"), Fake("b")
    a.stream_error_after = 1
    c = core([Route("r", [dep(a), dep(b)])], breaker_factory=fast_breaker)
    seen = []
    with pytest.raises(ProviderError) as ei:
        async for ch in c.chat_stream("r", req()):
            seen.append(ch.text)
    assert seen == ["a"] and ei.value.kind == "stream_interrupted"
    assert b.calls == 0  # ADR 0153 d9
    assert c.snapshot()["a/m"]["errors"] == 1


async def test_a_retryable_error_after_the_first_chunk_is_still_never_failed_over():
    """The guard is `committed`, not the error's own retryability: a mid-stream `upstream_error`
    is retryable by kind, and re-sending it to another provider would splice two answers."""
    a, b = Fake("a"), Fake("b")
    a.stream_error_after, a.stream_error_kind = 1, "upstream_error"
    seen = []
    with pytest.raises(ProviderError) as ei:
        async for ch in core([Route("r", [dep(a), dep(b)])]).chat_stream("r", req()):
            seen.append(ch.text)
    assert seen == ["a"] and ei.value.kind == "upstream_error"
    assert b.calls == 0


async def test_a_completed_stream_counts_as_a_success_and_a_dropped_one_releases_the_slot():
    a = Fake("a")
    c = core([Route("r", [dep(a, max_concurrency=1, max_queue=0)])])
    await collect(c.chat_stream("r", req()))
    stream = c.chat_stream("r", req())
    await stream.__anext__()
    await stream.aclose()  # the client went away mid-stream
    await collect(c.chat_stream("r", req()))  # the slot was released, not leaked
    assert c.snapshot()["a/m"]["ok"] >= 2


# ── observability of routing state ────────────────────────────────────────────


async def test_snapshot_reports_health_for_the_admin_surface():
    a = Fake("a")
    c = core([Route("r", [dep(a)])])
    await c.chat("r", req())
    snap = c.snapshot()["a/m"]
    assert snap["breaker"] == "closed" and snap["ok"] == 1 and snap["errors"] == 0
    assert snap["ewma_ttft_ms"] == pytest.approx(12.0)
    assert snap["inflight"] == 0


# ── active probe result feed-in (PLAN.md P1 "active probe loop", gateway/health.py) ───────────


def test_deployments_for_provider_finds_every_route_sharing_it():
    a, b = Fake("a"), Fake("b")
    c = core(
        [
            Route("r1", [dep(a, model="m1"), dep(b, model="m1")]),
            Route("r2", [dep(a, model="m2")]),  # same provider `a`, a different route AND model
        ]
    )
    keys = {d.key for d in c.deployments_for_provider("a")}
    assert keys == {"a/m1", "a/m2"}  # both of a's deployments, not b's
    assert {d.key for d in c.deployments_for_provider("b")} == {"b/m1"}
    assert c.deployments_for_provider("nonexistent") == []


def test_warm_deployments_returns_only_the_ones_flagged_warm():
    a, b = Fake("a"), Fake("b")
    c = core(
        [
            Route("r1", [dep(a, model="m1", warm=True), dep(b, model="m1", warm=False)]),
            Route("r2", [dep(a, model="m2")]),  # warm defaults to False
        ]
    )
    assert {d.key for d in c.warm_deployments()} == {"a/m1"}


def test_warm_deployments_deduplicates_by_key_across_routes():
    """The same (provider, model) pair reachable from two route names must only ever produce one
    warm target — a keep-alive ping is sent once per real upstream deployment, not once per route
    that happens to reference it."""
    a = Fake("a")
    shared = dep(a, model="m", warm=True)
    c = core([Route("r1", [shared]), Route("r2", [shared])])
    assert [d.key for d in c.warm_deployments()] == ["a/m"]


async def test_record_probe_result_opens_the_breaker_for_every_deployment_of_that_provider():
    """A provider is shared by many routes/models; health is a property of the connection, not
    any one route — an active probe failure must be visible everywhere that provider is used, not
    only on the one route/model combination a request happened to hit."""
    a = Fake("a")
    c = core([Route("r1", [dep(a, model="m1")]), Route("r2", [dep(a, model="m2")])])
    for _ in range(5):  # CircuitBreaker's default fail_threshold
        c.record_probe_result("a", ok=False)
    assert c.snapshot()["a/m1"]["breaker"] == "open"
    assert c.snapshot()["a/m2"]["breaker"] == "open"  # the other route's deployment too


async def test_record_probe_result_never_touches_real_request_stats():
    """Deliberately breaker-only (see the method's own docstring): a probe is not a real request,
    and must not make `ok`/`errors`/`ewma_ttft_ms` — which describe real traffic — lie."""
    a = Fake("a")
    c = core([Route("r", [dep(a)])])
    await c.chat("r", req())
    before = dict(c.snapshot()["a/m"])
    c.record_probe_result("a", ok=False)
    c.record_probe_result("a", ok=True)
    after = c.snapshot()["a/m"]
    assert after["ok"] == before["ok"] and after["errors"] == before["errors"]
    assert after["ewma_ttft_ms"] == before["ewma_ttft_ms"]
    assert after["last_error"] == before["last_error"]


def test_record_probe_result_for_an_unknown_provider_is_a_silent_no_op():
    """A provider that answers to no configured route (a stale name, a discovery race) must not
    raise — the active probe loop iterates whatever providers the runtime currently has, and a
    momentary mismatch during a reload must not crash the whole probe pass over every other one."""
    c = core([Route("r", [dep(Fake("a"))])])
    c.record_probe_result("ghost", ok=False)  # must not raise
    assert c.snapshot()["a/m"]["breaker"] == "closed"  # unaffected


# ── residency-aware min_useful deadline gate (design spec §5 "Deadline"/"Cold start") ──────────


def test_min_useful_s_is_the_bare_floor_for_a_non_ollama_provider():
    """§5 "Cold start" is explicitly scoped to `ollama`'s own residency reporting — no other
    provider type is ever gated by it, even with no residency data at all."""
    a = Fake("a")  # Fake.type == "fake", never "ollama"
    c = core([Route("r", [dep(a)])])
    assert c._min_useful_s(dep(a)) == c.min_useful_s  # noqa: SLF001


def test_min_useful_s_is_the_bare_floor_when_residency_is_unknown():
    a = Fake("a")
    a.type = "ollama"
    c = core([Route("r", [dep(a)])])
    assert c._min_useful_s(dep(a)) == c.min_useful_s  # noqa: SLF001 - never probed yet


def test_min_useful_s_is_the_cold_load_budget_for_a_confirmed_cold_ollama_model():
    a = Fake("a")
    a.type = "ollama"
    c = core([Route("r", [dep(a, model="cold-model")])])
    c.record_probe_result("a", ok=True, resident=["a-different-model"])
    assert c._min_useful_s(dep(a, model="cold-model")) == c.cold_load_budget_s  # noqa: SLF001


def test_min_useful_s_is_the_bare_floor_for_a_confirmed_resident_ollama_model():
    a = Fake("a")
    a.type = "ollama"
    c = core([Route("r", [dep(a, model="warm-model")])])
    c.record_probe_result("a", ok=True, resident=["warm-model"])
    assert c._min_useful_s(dep(a, model="warm-model")) == c.min_useful_s  # noqa: SLF001


def test_record_probe_result_with_resident_none_never_erases_prior_residency_data():
    """A probe that raised (or otherwise cannot say) passes `resident=None` — that must not reset
    a provider from "confirmed nothing is resident" back to "unknown/permissive", or a transient
    probe hiccup would silently widen every subsequent deadline check."""
    a = Fake("a")
    a.type = "ollama"
    c = core([Route("r", [dep(a, model="m")])])
    c.record_probe_result("a", ok=True, resident=[])  # confirmed: nothing resident
    assert c._min_useful_s(dep(a, model="m")) == c.cold_load_budget_s  # noqa: SLF001
    c.record_probe_result("a", ok=False, resident=None)  # this probe couldn't say
    assert c._min_useful_s(dep(a, model="m")) == c.cold_load_budget_s  # noqa: SLF001 - unchanged


async def test_a_short_deadline_skips_a_confirmed_cold_deployment_without_ever_calling_it():
    a = Fake("a")
    a.type = "ollama"
    c = core([Route("r", [dep(a, model="cold")])], cold_load_budget_s=10.0)
    c.record_probe_result("a", ok=True, resident=[])  # confirmed cold
    with pytest.raises(ProviderError) as ei:
        await c.chat("r", req(), caller_budget_ms=500)  # 0.5s < the 10s cold-load budget
    assert ei.value.kind == "upstream_timeout"
    assert a.calls == 0  # never attempted — skipped before any network call


async def test_a_short_deadline_still_allows_a_confirmed_resident_deployment():
    """The same 2s budget that a confirmed-*cold* deployment would be skipped for (below the 10s
    cold-load budget) must still go through for a confirmed-*resident* one (above the 1s bare
    floor) — the whole point of residency-awareness rather than one fixed floor for everyone."""
    a = Fake("a")
    a.type = "ollama"
    c = core([Route("r", [dep(a, model="warm")])], cold_load_budget_s=10.0)
    c.record_probe_result("a", ok=True, resident=["warm"])  # confirmed already warm
    result = await c.chat("r", req(), caller_budget_ms=2000)
    assert result.provider == "a" and a.calls == 1
