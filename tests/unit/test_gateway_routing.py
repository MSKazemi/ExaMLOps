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
