"""ADR 0143 decisions 2, 3, 6 — one EndpointPicker interface, three implementations."""

from __future__ import annotations

import pytest

from examlops.inference_gateway import picker as pk


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for var in ("EXAMLOPS_KV_ROUTING", "EXAMLOPS_KV_ROUTING_BREAK_EVEN", "EXAMLOPS_CACHE_SALT_KEY"):
        monkeypatch.delenv(var, raising=False)


def _eps(n=4, **kw):
    return [pk.EndpointState(id=f"r{i}", **kw) for i in range(n)]


# ── decision 3: salts and request metadata ─────────────────────────────────


def test_cache_salt_is_stable_per_project_and_differs_across_projects():
    assert pk.cache_salt_for("a") == pk.cache_salt_for("a")
    assert pk.cache_salt_for("a") != pk.cache_salt_for("b")


def test_keyed_salt_differs_from_unkeyed_and_is_not_derivable_without_the_key(monkeypatch):
    plain = pk.cache_salt_for("a")
    monkeypatch.setenv("EXAMLOPS_CACHE_SALT_KEY", "s3cret")
    keyed = pk.cache_salt_for("a")
    assert keyed != plain
    assert pk.cache_salt_for("a", key="other") != keyed


def test_same_prompt_under_two_salts_never_shares_a_cache_key():
    a = pk.RequestMeta.for_prompt("m", "SYSTEM", project="p1")
    b = pk.RequestMeta.for_prompt("m", "SYSTEM", project="p2")
    assert a.prefix_hash == b.prefix_hash
    assert a.cache_key != b.cache_key


def test_request_meta_refuses_an_unsalted_request():
    with pytest.raises(ValueError, match="cache_salt"):
        pk.RequestMeta(model="m", prefix_hash="x", cache_salt="")


def test_request_meta_bounds_its_fields():
    with pytest.raises(ValueError, match="exceeds"):
        pk.RequestMeta(model="m", prefix_hash="x", cache_salt="s", session_key="k" * 300)


def test_retention_hint_is_carried_and_ignored():
    meta = pk.RequestMeta(model="m", prefix_hash="x", cache_salt="s", kv_retention_hint="keep")
    eps = _eps()
    scorer = pk.GatewayScorer(kv_aware=True)
    plain = pk.RequestMeta(model="m", prefix_hash="x", cache_salt="s")
    assert [e.id for e in scorer.score(meta, eps)] == [e.id for e in scorer.score(plain, eps)]


# ── decision 2: three implementations + break-even gate ────────────────────


@pytest.mark.parametrize(
    ("substrate", "cls"),
    [
        ("kserve", pk.LLMDEndpointPicker),
        ("compose-ray", pk.RayServeLLMPicker),
        ("hpc-ray", pk.RayServeLLMPicker),
        ("hpc", pk.GatewayScorer),
        ("external", pk.GatewayScorer),
    ],
)
def test_one_picker_per_substrate(substrate, cls):
    p = pk.picker_for_substrate(substrate)
    assert isinstance(p, cls)
    assert isinstance(p, pk.EndpointPicker)
    assert p.substrate == substrate


def test_unknown_substrate_is_refused():
    with pytest.raises(ValueError, match="unknown substrate"):
        pk.picker_for_substrate("mainframe")


def test_network_cost_capability_is_advertised():
    assert pk.RayServeLLMPicker.uses_network_cost is False
    assert pk.LLMDEndpointPicker.uses_network_cost is True
    assert pk.GatewayScorer.uses_network_cost is True


def test_break_even_gate_keeps_round_robin_by_default(monkeypatch):
    s = pk.GatewayScorer()
    assert s.mode(16) == "round_robin_affinity"
    monkeypatch.setenv("EXAMLOPS_KV_ROUTING", "1")
    assert s.mode(3) == "round_robin_affinity"  # below the default break-even of 4
    assert s.mode(4) == "kv_aware"
    monkeypatch.setenv("EXAMLOPS_KV_ROUTING_BREAK_EVEN", "8")
    assert s.mode(4) == "round_robin_affinity"


def test_round_robin_rotates_without_a_session():
    s = pk.GatewayScorer(kv_aware=False)
    eps = _eps(3)
    firsts = [
        s.score(pk.RequestMeta(model="m", prefix_hash=f"p{i}", cache_salt="s"), eps)[0].id
        for i in range(3)
    ]
    assert sorted(firsts) == ["r0", "r1", "r2"]


def test_kv_aware_prefers_the_replica_holding_the_salted_prefix():
    s = pk.GatewayScorer(kv_aware=True)
    eps = _eps(3)
    meta = pk.RequestMeta.for_prompt("m", "SYS", project="p")
    eps[2].cached_keys.add(meta.cache_key)
    eps[2].queue_depth = 3  # busier, but holds the prefix
    assert s.score(meta, eps)[0].id == "r2"


def test_kv_aware_ignores_a_prefix_warmed_by_another_salt():
    s = pk.GatewayScorer(kv_aware=True)
    eps = _eps(3)
    other = pk.RequestMeta.for_prompt("m", "SYS", project="other")
    eps[2].cached_keys.add(other.cache_key)
    eps[2].queue_depth = 3
    mine = pk.RequestMeta.for_prompt("m", "SYS", project="mine")
    assert s.score(mine, eps)[0].id != "r2"  # least-loaded wins: no hit exists for this salt


def test_load_ranking_uses_waiting_then_kv_usage():
    s = pk.GatewayScorer(kv_aware=True)
    eps = [
        pk.EndpointState("a", queue_depth=2, kv_cache_usage=0.1),
        pk.EndpointState("b", queue_depth=0, kv_cache_usage=0.9),
        pk.EndpointState("c", queue_depth=0, kv_cache_usage=0.1),
    ]
    meta = pk.RequestMeta(model="m", prefix_hash="x", cache_salt="s")
    assert [e.id for e in s.score(meta, eps)] == ["c", "a", "b"]


def test_ray_and_llmd_render_their_standard_configs():
    ray = pk.RayServeLLMPicker.deployment_config("qwen")
    router = ray["deployment_config"]["request_router_config"]["request_router_class"]
    assert router.endswith("PrefixCacheAffinityRouter")
    assert ray["requires"]["ray"] == ">=2.58.0"
    block = pk.LLMDEndpointPicker.router_block()
    assert block["router"]["scheduler"]["pool"]["apiVersion"] == "inference.networking.k8s.io/v1"
    assert "InferenceObjective" not in repr(block) and "InferenceModel" not in repr(block)


# ── decision 6 backstop: overload -> 429 + Retry-After ─────────────────────


def test_saturated_pool_raises_429_with_retry_after():
    eps = _eps(2, queue_depth=8, max_queued=8, ttft_p99_ms=1500.0)
    with pytest.raises(pk.PoolOverloaded) as exc:
        pk.GatewayScorer(kv_aware=True).score(
            pk.RequestMeta(model="m", prefix_hash="x", cache_salt="s"), eps
        )
    assert exc.value.status_code == 429
    assert exc.value.headers() == {"Retry-After": "2"}  # 1.5 s rounded up


def test_saturated_replica_is_skipped_while_another_has_room():
    eps = [pk.EndpointState("full", queue_depth=4, max_queued=4), pk.EndpointState("ok")]
    ranked = pk.RayServeLLMPicker().score(
        pk.RequestMeta(model="m", prefix_hash="x", cache_salt="s"), eps
    )
    assert [e.id for e in ranked] == ["ok"]


def test_no_healthy_endpoint_is_overload_not_a_crash():
    eps = _eps(2, healthy=False)
    with pytest.raises(pk.PoolOverloaded):
        pk.LLMDEndpointPicker().score(
            pk.RequestMeta(model="m", prefix_hash="x", cache_salt="s"), eps
        )


def test_empty_pool_is_a_value_error():
    with pytest.raises(ValueError):
        pk.GatewayScorer().score(pk.RequestMeta(model="m", prefix_hash="x", cache_salt="s"), [])


# ── /metrics scraping ──────────────────────────────────────────────────────

_METRICS = """# HELP vllm:num_requests_waiting x
vllm:num_requests_waiting{model_name="m"} 3.0
vllm:num_requests_running{model_name="m"} 2.0
vllm:kv_cache_usage_perc{model_name="m"} 0.42
vllm:time_to_first_token_seconds_bucket{le="0.1"} 7
"""


def test_scrape_reads_waiting_running_and_kv_usage():
    urls = []

    def fetch(url):
        urls.append(url)
        return _METRICS

    st = pk.scrape_endpoint_state("r0", "http://h:8000/", fetch=fetch, max_queued=10)
    assert urls == ["http://h:8000/metrics"]
    assert (st.queue_depth, st.running, st.kv_cache_usage, st.healthy) == (3, 2, 0.42, True)
    assert st.max_queued == 10


def test_unreachable_metrics_marks_the_endpoint_unhealthy():
    def fetch(url):
        raise OSError("connection refused")

    st = pk.scrape_endpoint_state("r0", "http://h:8000", fetch=fetch)
    assert st.healthy is False


# ── Verification 1: the shared conformance fixture ─────────────────────────


@pytest.mark.parametrize(
    "picker",
    [pk.GatewayScorer(kv_aware=False), pk.GatewayScorer(kv_aware=True)]
    + [pk.RayServeLLMPicker(), pk.LLMDEndpointPicker()],
    ids=["gateway-rr", "gateway-kv", "ray", "llm-d"],
)
def test_conformance_affinity_and_no_cross_salt_hit(picker):
    trace = pk.synthetic_multiturn_trace(projects=3, sessions_per_project=4, turns=5)
    report = pk.conformance_report(picker, trace, replicas=4)
    assert report["cross_salt_hits"] == 0
    assert report["affinity_rate"] is not None and report["affinity_rate"] >= 0.95
    # The shared system prompt is one prefix but three cache keys — one per project.
    assert report["distinct_prefixes"] == 1
    assert report["distinct_cache_keys"] == 3


def test_conformance_fixture_catches_an_unsalted_picker():
    class Unsalted(pk.GatewayScorer):
        def observe(self, meta, chosen):
            chosen.cached_keys.add(meta.prefix_hash)  # the bug decision 3 forbids

        def score(self, meta, endpoints):
            eps = list(endpoints)
            return sorted(eps, key=lambda e: (meta.prefix_hash not in e.cached_keys, e.id))

    report = pk.conformance_report(Unsalted(kv_aware=True), pk.synthetic_multiturn_trace())
    assert report["cross_salt_hits"] > 0


def test_session_pin_table_is_bounded():
    s = pk.GatewayScorer(kv_aware=True, session_cap=2)
    ep = pk.EndpointState("r0")
    for i in range(5):
        s.observe(
            pk.RequestMeta(model="m", prefix_hash="x", cache_salt="s", session_key=f"k{i}"), ep
        )
    assert len(s._sessions) == 2


def test_an_endpoints_warm_key_set_is_bounded(monkeypatch):
    # A long-lived endpoint sees an unbounded stream of distinct prefixes; the picker's memory of
    # them must not grow with it.
    monkeypatch.setattr(pk, "_MAX_WARM_KEYS", 3)
    s = pk.GatewayScorer(kv_aware=True)
    ep = pk.EndpointState("r0")
    for i in range(10):
        s.observe(pk.RequestMeta(model="m", prefix_hash=f"p{i}", cache_salt="s"), ep)
    assert len(ep.cached_keys) == 3
    last = pk.RequestMeta(model="m", prefix_hash="p9", cache_salt="s").cache_key
    assert last in ep.cached_keys  # the newest key is always kept
