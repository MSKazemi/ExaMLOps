"""`GatewayCore.embed()` — the routing core's embeddings path (ADR 0152: `Provider.embed()` has
existed since 2026-09-20; nothing routed to it). Reuses the same resilience primitives as `chat()`
(breaker, retry budget, bulkhead, locality/capability filtering, deadline) rather than a separate,
untested code path — proven here against the same failure classes `test_gateway_routing.py` proves
for chat, not a smaller subset.
"""

from __future__ import annotations

import random

import pytest

from examlops.gateway.providers import Capabilities, EmbedResult, ProviderError, Usage
from examlops.gateway.resilience import CircuitBreaker, RetryBudget
from examlops.gateway.routing import Catalog, Deployment, GatewayCore, Route


class Fake:
    """A scripted embedding provider. ``fail`` is raised on every call; ``script`` first."""

    type = "fake"

    def __init__(self, name, locality="local", *, fail=None, script=None, vectors=None):
        self.name, self.locality = name, locality
        self.fail, self.script = fail, list(script or [])
        self.vectors = vectors if vectors is not None else [[0.1, 0.2]]
        self.calls = 0

    def _err(self, e):
        e.provider = self.name
        return e

    async def embed(self, model, inputs):
        self.calls += 1
        item = self.script.pop(0) if self.script else self.fail
        if item is not None:
            raise self._err(item)
        return EmbedResult(
            vectors=self.vectors, model=model, provider=self.name, usage=Usage(len(inputs))
        )


def upstream_error():
    return ProviderError("upstream_error", "boom")


def core(routes, *, aliases=None, **kw):
    kw.setdefault("rng", random.Random(1))
    return GatewayCore(Catalog(routes, aliases or {}), **kw)


def dep(p, model="m", **kw):
    return Deployment(p, model, **kw)


async def test_embeds_from_the_first_healthy_deployment():
    a, b = Fake("a"), Fake("b")
    attempts: list = []
    res = await core([Route("r", [dep(a), dep(b)])]).embed("r", ["hello"], attempts=attempts)
    assert res.provider == "a" and a.calls == 1 and b.calls == 0
    assert res.vectors == [[0.1, 0.2]] and res.usage.prompt_tokens == 1
    assert [(x["provider"], x["outcome"]) for x in attempts] == [("a", "ok")]


async def test_fails_over_to_the_next_deployment_on_an_upstream_fault():
    a, b = Fake("a", fail=upstream_error()), Fake("b")
    res = await core([Route("r", [dep(a), dep(b)])]).embed("r", ["hi"])
    assert res.provider == "b"


async def test_a_client_error_is_raised_not_failed_over():
    a = Fake("a", fail=ProviderError("invalid_request", "bad input"))
    b = Fake("b")
    with pytest.raises(ProviderError) as ei:
        await core([Route("r", [dep(a), dep(b)])]).embed("r", ["hi"])
    assert ei.value.kind == "invalid_request" and b.calls == 0


async def test_a_non_embedding_deployment_is_skipped_with_capability_unavailable():
    """The chat-only ADR 0152 d3 rule applies here too: a request never reaches a provider that
    declared it cannot serve it."""
    chat_only = dep(Fake("chat"), capabilities=Capabilities(embeddings=False))
    with pytest.raises(ProviderError) as ei:
        await core([Route("r", [chat_only])]).embed("r", ["hi"])
    assert ei.value.kind == "capability_unavailable" and "embed" in ei.value.message


async def test_embedding_capable_deployment_is_preferred_over_a_declared_chat_only_one():
    chat_only = dep(Fake("chat"), capabilities=Capabilities(embeddings=False))
    embedder = dep(Fake("embed"), capabilities=Capabilities(embeddings=True))
    res = await core([Route("r", [chat_only, embedder])]).embed("r", ["hi"])
    assert res.provider == "embed"


async def test_unknown_capabilities_are_permissive():
    """No declared capabilities (``None``) means unknown, not incapable — matches chat's rule."""
    unknown = Fake("u")
    res = await core([Route("r", [dep(unknown)])]).embed("r", ["hi"])
    assert res.provider == "u"


async def test_locality_is_enforced_the_same_way_as_chat():
    ext = Fake("omni", "external")
    with pytest.raises(ProviderError) as ei:
        await core([Route("r", [dep(ext, external_ok=True)])]).embed("r", ["hi"])
    assert ei.value.kind == "locality_denied" and ext.calls == 0


async def test_an_open_breaker_is_skipped():
    a, b = Fake("a", fail=upstream_error()), Fake("b")
    breaker_factory = lambda: CircuitBreaker(fail_threshold=1, jitter=0.0, base_open_s=60)  # noqa: E731
    c = core([Route("r", [dep(a), dep(b)])], breaker_factory=breaker_factory)
    await c.embed("r", ["hi"])  # trips a's breaker
    a.calls = 0
    res = await c.embed("r", ["hi"])
    assert res.provider == "b" and a.calls == 0  # a's breaker is open; never called again


async def test_a_retry_storm_is_bounded_by_the_shared_retry_budget():
    bad, good = Fake("bad", fail=upstream_error()), Fake("good")
    c = core(
        [Route("r", [dep(bad), dep(good)])],
        breaker_factory=lambda: CircuitBreaker(fail_threshold=10**9, error_ratio=2.0),
        retry_budget=RetryBudget(ratio=0.2, min_retries=3),
    )
    served = 0
    for _ in range(300):
        try:
            await c.embed("r", ["hi"])
            served += 1
        except ProviderError as e:
            assert e.kind == "upstream_unavailable"
    assert good.calls == served <= 0.2 * 300 + 3


class _WouldAllowButRefuses:
    """A breaker double: ``would_allow()`` says yes (so candidate selection admits it) but
    ``allow()`` says no (so the actual attempt must still refuse) — isolates the race the
    second, consuming check exists for for from the pre-selection filter."""

    state = "half_open"

    def would_allow(self):
        return True

    def allow(self):
        return False

    def record_success(self):
        pass

    def record_failure(self):
        pass

    def open_remaining(self):
        return 0.0


async def test_the_attempt_rechecks_the_breaker_even_though_selection_already_did():
    """Selection and the attempt itself are two different points in time; a trial slot can be
    spent by another concurrent request in between. `_attempt_embed` must not trust the
    selection-time answer."""
    a, b = Fake("a"), Fake("b")
    made = {"n": 0}

    def breaker_factory():
        made["n"] += 1
        return _WouldAllowButRefuses() if made["n"] == 1 else CircuitBreaker(jitter=0.0)

    c = core([Route("r", [dep(a), dep(b)])], breaker_factory=breaker_factory)
    res = await c.embed("r", ["hi"])
    assert res.provider == "b" and a.calls == 0  # `a` was selected but refused at attempt time


async def test_repeated_requests_grow_the_retry_budget_by_calling_note_request():
    """Every request must register itself with the shared budget, not just spend from it — a
    budget that only ever sees `try_retry()` calls stays pinned at its floor forever."""
    bad, good = Fake("bad", fail=upstream_error()), Fake("good")
    rb = RetryBudget(ratio=1.0, min_retries=0, window_s=1000)
    c = core(
        [Route("r", [dep(bad), dep(good)])],
        retry_budget=rb,
        breaker_factory=lambda: CircuitBreaker(fail_threshold=10**9, error_ratio=2.0),
    )
    for _ in range(5):
        res = await c.embed("r", ["hi"])
        assert res.provider == "good"


async def test_snapshot_reports_embed_traffic_alongside_chat():
    a = Fake("a")
    c = core([Route("r", [dep(a)])])
    await c.embed("r", ["hi"])
    assert c.snapshot()["a/m"]["ok"] == 1
