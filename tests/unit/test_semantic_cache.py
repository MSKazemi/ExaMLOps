# tests/unit/test_semantic_cache.py
"""B3 — Semantic caching (ADR 0018, spec B3).

GWT-1 near-dup hit · GWT-2 below-threshold miss · GWT-3 params isolation ·
GWT-4 tenant isolation · GWT-5 bypass · GWT-6 savings recorded.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import semantic_cache as sc  # noqa: E402
from examlops.gateway import Completion  # noqa: E402
from examlops.platform_db import cache_stats, init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    init_db()


def _cache(**kw):
    return sc.SemanticCache(**kw)


def test_gwt1_near_duplicate_hit():
    cache = _cache(threshold=0.8)
    comp = Completion(text="Paris", model="m", backend="b")
    cache.store("what is the capital of france", comp, "m", {}, "acme")
    # a paraphrase sharing most tokens lands within threshold
    hit, sim = cache.lookup("what is the capital of france please", "m", {}, "acme")
    assert hit is not None
    assert hit.text == "Paris"
    assert sim >= 0.8


def test_gwt2_distant_prompt_misses():
    cache = _cache(threshold=0.8)
    cache.store("what is the capital of france", Completion("Paris", "m", "b"), "m", {}, "acme")
    hit, sim = cache.lookup("recipe for chocolate brownies today", "m", {}, "acme")
    assert hit is None
    assert sim < 0.8


def test_gwt3_params_isolation():
    cache = _cache(threshold=0.5)
    cache.store("hello world", Completion("hi", "m", "b"), "m", {"temperature": 0.0}, "acme")
    # same prompt but different max_tokens → different namespace → miss
    hit, _ = cache.lookup("hello world", "m", {"temperature": 0.0, "max_tokens": 500}, "acme")
    assert hit is None
    # exact same params → hit
    hit2, _ = cache.lookup("hello world", "m", {"temperature": 0.0}, "acme")
    assert hit2 is not None


def test_gwt4_tenant_isolation():
    cache = _cache(threshold=0.5)
    cache.store("hello world", Completion("hi", "m", "b"), "m", {}, "tenantA")
    hit, _ = cache.lookup("hello world", "m", {}, "tenantB")
    assert hit is None  # tenant B cannot see tenant A's entry


def test_gwt5_bypass_high_temperature():
    assert sc.is_cacheable({"temperature": 0.9}) is False
    assert sc.is_cacheable({"temperature": 0.0}) is True
    assert sc.is_cacheable({"temperature": 0.0}, no_cache=True) is False
    assert sc.is_cacheable({"temperature": 0.0}, side_effecting=True) is False


def test_gwt6_savings_recorded():
    cache = _cache(threshold=0.8)
    cache.store(
        "token heavy prompt here",
        Completion("ans", "m", "b"),
        "m",
        {},
        "acme",
        tokens=120,
        cost_usd=0.0012,
    )
    cache.lookup("token heavy prompt here now", "m", {}, "acme")  # hit
    stats = cache_stats("acme")
    assert stats["hits"] == 1
    assert stats["tokens_saved"] == 120
    assert stats["cost_saved"] == pytest.approx(0.0012)


def test_ttl_eviction():
    cache = _cache(threshold=0.5, ttl_seconds=0.0)
    cache.store("hello world", Completion("hi", "m", "b"), "m", {}, "acme")
    # ttl 0 → the entry is immediately expired on the next lookup
    hit, _ = cache.lookup("hello world", "m", {}, "acme")
    assert hit is None


def test_max_size_eviction():
    cache = _cache(threshold=0.99, max_size=2)
    for i in range(5):
        cache.store(f"prompt number {i}", Completion(f"a{i}", "m", "b"), "m", {}, "acme")
    assert len(cache._entries) <= 2


def test_bind_to_gateway_roundtrip():
    cache = _cache(threshold=0.8)
    lookup, store = sc.bind_to_gateway(cache, tenant="acme")
    msgs = [{"role": "user", "content": "capital of france"}]
    assert lookup("m", msgs) is None  # miss
    store("m", msgs, Completion("Paris", "m", "b", completion_tokens=3))
    assert lookup("m", msgs) == "Paris"  # hit returns the text


def test_gateway_integration_uses_cache():
    from examlops.gateway import GatewayClient, Router

    cache = _cache(threshold=0.8)
    lookup, store = sc.bind_to_gateway(cache, tenant="acme")

    calls = {"n": 0}

    def backend(model, messages, **kw):
        calls["n"] += 1
        return Completion(text="fresh-answer", model=model, backend="")

    r = Router()
    r.add_route("m", [("primary", backend)])
    client = GatewayClient(r, cache_lookup=lookup, cache_store=store)
    msgs = [{"role": "user", "content": "hello there world"}]
    first = client.chat("m", msgs)
    assert first.text == "fresh-answer" and calls["n"] == 1
    second = client.chat("m", msgs)  # served from cache
    assert second.cached is True and calls["n"] == 1  # backend not called again
