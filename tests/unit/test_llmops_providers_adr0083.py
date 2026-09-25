"""ADR 0083 clauses closed on 2026-09-25.

* the ``cost-latency`` routing provider exists and is the swap the ADR promises;
* every LLMOps façade reads its block from ``providers.yaml`` (the ADR's config home), with the
  legacy ``finops.yaml`` block as a fallback, and env beats file;
* the live ``llm_cost`` gateway seam (`gateway._estimate_cost`) honours that resolution;
* `cost_aware` routing uses a per-deployment ``price_per_1k`` from `gateway.yaml` and hands the
  observed TTFT to the provider;
* ``describe_active_providers`` (the F10 console feed) reports the formula that actually runs.

Config files are pointed at ``tmp_path`` so a developer's real ``~/.config/examlops`` never leaks in.
"""

from __future__ import annotations

import math
import random

import pytest
import yaml

from examlops.llmops_providers import (
    LLMOPS_DOMAINS,
    CostLatencyRoutingProvider,
    describe_active_providers,
    domain_config,
    estimate_llm_cost_via_provider,
    route_score_via_provider,
    selected_provider_name,
)
from examlops.providers import get_provider, list_providers, loader

_ENVS = (
    "EXAMLOPS_LLM_COST_PROVIDER",
    "EXAMLOPS_LLM_CACHE_PROVIDER",
    "EXAMLOPS_LLM_ROUTING_PROVIDER",
    "EXAMLOPS_RAG_QUALITY_PROVIDER",
)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Isolated providers.yaml / finops.yaml; returns a writer for each."""
    prov, fin = tmp_path / "providers.yaml", tmp_path / "finops.yaml"
    monkeypatch.setattr(loader, "PROVIDERS_YAML", prov)
    monkeypatch.setattr(loader, "FINOPS_YAML", fin)
    for e in _ENVS:
        monkeypatch.delenv(e, raising=False)

    class W:
        @staticmethod
        def providers(data):
            prov.write_text(yaml.safe_dump(data))

        @staticmethod
        def finops(data):
            fin.write_text(yaml.safe_dump(data))

    return W


# ── cost-latency provider ────────────────────────────────────────────────────


class TestCostLatency:
    def test_formula(self):
        out = CostLatencyRoutingProvider().compute(
            {"cost_usd": 0.5, "latency_ms": 200, "latency_weight": 0.01}
        )
        assert out["score"] == pytest.approx(-(0.5 + 200 * 0.01))

    def test_default_weight_is_one_dollar_per_second(self):
        out = CostLatencyRoutingProvider().compute({"cost_usd": 0.0, "latency_ms": 1000})
        assert out["score"] == pytest.approx(-1.0)

    def test_unhealthy_is_negative_infinity(self):
        out = CostLatencyRoutingProvider().compute({"cost_usd": 0.0, "healthy": False})
        assert out["score"] == -math.inf

    def test_negative_weight_refused(self):
        with pytest.raises(ValueError, match="latency_weight"):
            CostLatencyRoutingProvider().compute({"cost_usd": 1.0, "latency_weight": -1})

    def test_negative_latency_clamped(self):
        out = CostLatencyRoutingProvider().compute({"cost_usd": 1.0, "latency_ms": -50})
        assert out["score"] == pytest.approx(-1.0)

    def test_a_fast_pricey_route_can_beat_a_slow_cheap_one(self):
        p = CostLatencyRoutingProvider()
        fast = p.compute({"cost_usd": 0.2, "latency_ms": 50, "latency_weight": 0.01})["score"]
        slow = p.compute({"cost_usd": 0.0, "latency_ms": 900, "latency_weight": 0.01})["score"]
        assert fast > slow  # least-cost would pick the slow one; this formula does not

    def test_registered_but_not_default(self):
        names = {i.name for i in list_providers("llm_routing") if i.ok}
        assert {"least-cost", "cost-latency"} <= names
        assert get_provider("llm_routing").name == "least-cost"
        assert get_provider("llm_routing", name="cost-latency").name == "cost-latency"


# ── config home: providers.yaml, finops.yaml fallback, env precedence ─────────


class TestDomainConfig:
    def test_providers_yaml_is_the_home(self, cfg):
        cfg.providers({"llm_cost": {"provider": "token-rate", "input_price_per_1k": 3.0}})
        assert domain_config("llm_cost")["input_price_per_1k"] == 3.0

    def test_legacy_finops_block_is_the_fallback(self, cfg):
        cfg.finops({"finops": {"llm_cost": {"provider": "token-rate"}}})
        assert domain_config("llm_cost") == {"provider": "token-rate"}

    def test_providers_yaml_wins_over_the_legacy_block(self, cfg):
        cfg.finops({"finops": {"llm_cost": {"provider": "legacy"}}})
        cfg.providers({"llm_cost": {"provider": "token-rate"}})
        assert selected_provider_name("llm_cost") == "token-rate"

    def test_env_beats_file_and_override_beats_env(self, cfg, monkeypatch):
        cfg.providers({"llm_routing": {"provider": "least-cost"}})
        monkeypatch.setenv("EXAMLOPS_LLM_ROUTING_PROVIDER", "cost-latency")
        assert selected_provider_name("llm_routing") == "cost-latency"
        assert selected_provider_name("llm_routing", override="x") == "x"

    def test_nothing_configured_is_none(self, cfg):
        assert domain_config("rag_quality") == {}
        assert selected_provider_name("rag_quality") is None

    def test_malformed_file_degrades_to_empty(self, cfg, tmp_path):
        (tmp_path / "providers.yaml").write_text("llm_cost: [unclosed")
        assert domain_config("llm_cost") == {}


# ── the live llm_cost gateway seam ───────────────────────────────────────────


class TestLlmCostGatewaySeam:
    def test_providers_yaml_price_card_drives_the_gateway_cost(self, cfg):
        from examlops.gateway import _estimate_cost

        cfg.providers(
            {
                "llm_cost": {
                    "provider": "token-rate",
                    "input_price_per_1k": 1.0,
                    "output_price_per_1k": 2.0,
                }
            }
        )
        # 1000 prompt × $1/1k + 500 completion × $2/1k = $2.00
        assert _estimate_cost("any-model", 1000, 500) == pytest.approx(2.0)

    def test_env_selection_uses_the_provider_defaults(self, cfg, monkeypatch):
        monkeypatch.setenv("EXAMLOPS_LLM_COST_PROVIDER", "token-rate")
        cost = estimate_llm_cost_via_provider(model="m", prompt_tokens=1000, completion_tokens=0)
        assert cost == pytest.approx(0.002)  # built-in default input price

    def test_unconfigured_leaves_the_rate_table_in_charge(self, cfg):
        assert (
            estimate_llm_cost_via_provider(model="m", prompt_tokens=1000, completion_tokens=0)
            is None
        )

    def test_broken_selection_degrades_audibly(self, cfg, monkeypatch, caplog):
        monkeypatch.setenv("EXAMLOPS_LLM_COST_PROVIDER", "no-such-provider")
        with caplog.at_level("WARNING", logger="examlops.llmops_providers"):
            assert (
                estimate_llm_cost_via_provider(model="m", prompt_tokens=10, completion_tokens=10)
                is None
            )
        assert "llm_cost provider 'no-such-provider' failed" in caplog.text

    def test_routing_weight_from_providers_yaml_reaches_the_formula(self, cfg):
        cfg.providers({"llm_routing": {"provider": "cost-latency", "latency_weight": 0.5}})
        assert route_score_via_provider(cost_usd=1.0, latency_ms=4.0) == pytest.approx(-3.0)

    def test_a_config_key_never_overrides_a_runtime_input(self, cfg):
        """A block key shaped like an input must not replace the gateway's measurement.

        Before the fix the block was spread last, so ``cost_usd: 0`` in providers.yaml made every
        deployment score the same and ``cost_aware`` silently stopped ranking by price.
        """
        cfg.providers({"llm_routing": {"provider": "least-cost", "cost_usd": 0.0}})
        assert route_score_via_provider(cost_usd=0.3) == pytest.approx(-0.3)
        cfg.providers({"llm_routing": {"provider": "cost-latency", "latency_ms": 0.0}})
        assert route_score_via_provider(cost_usd=0.0, latency_ms=1000.0) == pytest.approx(-1.0)


# ── F10 console feed ─────────────────────────────────────────────────────────


class TestDescribeActiveProviders:
    def test_one_row_per_domain(self, cfg):
        rows = describe_active_providers()
        assert [r["domain"] for r in rows] == list(LLMOPS_DOMAINS)
        assert all(r["ok"] and r["methodology"] for r in rows)

    def test_unselected_opt_in_domains_report_the_callers_own_math(self, cfg):
        """Nothing selected ⇒ the façade returns None and the caller's arithmetic runs.

        The registered default (``token-rate`` etc.) does NOT compute the figure then, so the
        console must not show its formula: it would claim a generic price card priced a call
        that the gateway's rate table actually priced (local models at 0).
        """
        from examlops.gateway import _estimate_cost
        from examlops.telemetry.genai import estimate_cost

        by = {r["domain"]: r for r in describe_active_providers()}
        for domain in ("llm_cost", "llm_cache", "rag_quality"):
            row = by[domain]
            assert row["mode"] == "builtin" and row["provider"] is None, domain
            assert row["selected"] is False and row["ok"] is True
            assert "No " + domain + " provider selected" in row["methodology"]
        assert "reasoning_tokens" not in by["llm_cost"]["methodology"]
        assert "estimate_cost" in by["llm_cost"]["methodology"]
        # ...and that is really what runs: the gateway figure equals the rate table's.
        assert _estimate_cost("unknown-model", 1000, 500) == estimate_cost(
            "unknown-model", 1000, 500
        )

    def test_routing_default_runs_so_it_is_reported_as_a_provider(self, cfg):
        row = {r["domain"]: r for r in describe_active_providers()}["llm_routing"]
        assert row["mode"] == "provider" and row["provider"] == "least-cost"
        assert row["selected"] is False

    def test_a_selected_opt_in_provider_reports_its_own_metadata(self, cfg):
        cfg.providers({"llm_cache": {"provider": "hit-savings"}})
        row = {r["domain"]: r for r in describe_active_providers()}["llm_cache"]
        assert row["mode"] == "provider" and row["provider"] == "hit-savings"
        assert row["units"]["cost_saved_usd"] == "USD"

    def test_operator_choice_is_reported_as_selected(self, cfg, monkeypatch):
        monkeypatch.setenv("EXAMLOPS_LLM_ROUTING_PROVIDER", "cost-latency")
        row = {r["domain"]: r for r in describe_active_providers()}["llm_routing"]
        assert row["provider"] == "cost-latency" and row["selected"] is True
        assert "latency_weight" in row["methodology"]
        assert row["default"] == "least-cost"

    def test_a_broken_choice_is_shown_not_dropped(self, cfg):
        cfg.providers({"rag_quality": {"provider": "missing-plugin"}})
        rows = describe_active_providers()
        row = {r["domain"]: r for r in rows}["rag_quality"]
        assert len(rows) == 4
        assert row["ok"] is False and "missing-plugin" in row["error"]


# ── cost_aware routing: per-deployment price + observed latency ───────────────


class _P:
    type = "fake"

    def __init__(self, name, locality="local"):
        self.name, self.locality = name, locality


def _core(deps, strategy="cost_aware"):
    from examlops.gateway.routing import Catalog, GatewayCore, Route

    return GatewayCore(Catalog([Route("r", deps, strategy=strategy)]), rng=random.Random(1))


class TestCostAwarePricing:
    def test_declared_price_orders_deployments(self, cfg):
        from examlops.gateway.routing import Deployment, Route

        pricey = Deployment(_P("a"), "m", price_per_1k=0.03)
        cheap = Deployment(_P("b"), "m", price_per_1k=0.001)
        c = _core([pricey, cheap])
        order = c._order(Route("r", [], strategy="cost_aware"), [pricey, cheap])
        assert [d.provider.name for d in order] == ["b", "a"]

    def test_a_priced_external_can_beat_an_expensive_site_deployment(self, cfg):
        from examlops.gateway.routing import Deployment, Route

        site = Deployment(_P("site", "site"), "m", price_per_1k=0.05)  # amortised on-prem GPU
        ext = Deployment(_P("ext", "external"), "m", external_ok=True, price_per_1k=0.01)
        c = _core([site, ext])
        order = c._order(Route("r", [], strategy="cost_aware"), [site, ext])
        assert order[0].provider.name == "ext"

    def test_unpriced_deployments_keep_locality_semantics(self, cfg):
        from examlops.gateway.routing import Deployment, Route

        ext = Deployment(_P("ext", "external"), "m", external_ok=True)
        loc = Deployment(_P("loc"), "m")
        c = _core([ext, loc])
        order = c._order(Route("r", [], strategy="cost_aware"), [ext, loc])
        assert order[0].provider.name == "loc"

    def test_cost_latency_provider_uses_observed_ttft(self, cfg, monkeypatch):
        from examlops.gateway.routing import Deployment, Route

        monkeypatch.setenv("EXAMLOPS_LLM_ROUTING_PROVIDER", "cost-latency")
        slow = Deployment(_P("slow"), "m", price_per_1k=0.0)
        fast = Deployment(_P("fast"), "m", price_per_1k=0.1)
        c = _core([slow, fast])
        c._state(slow).ewma_ttft_ms = 2000.0  # -2.0 with the default weight
        c._state(fast).ewma_ttft_ms = 50.0  # -(0.1 + 0.05)
        order = c._order(Route("r", [], strategy="cost_aware"), [slow, fast])
        assert order[0].provider.name == "fast"
        monkeypatch.delenv("EXAMLOPS_LLM_ROUTING_PROVIDER")
        order = c._order(Route("r", [], strategy="cost_aware"), [slow, fast])
        assert order[0].provider.name == "slow"  # least-cost ignores latency


class TestGatewayYamlPrice:
    _RAW = {
        "version": 1,
        "providers": {"n1": {"type": "ollama", "base_url": "http://127.0.0.1:11434"}},
        "models": {
            "chat": {
                "strategy": "cost_aware",
                "deployments": [{"provider": "n1", "model": "qwen3:8b", "price_per_1k": 0.004}],
            }
        },
    }

    def test_valid_price_accepted(self):
        from examlops.gateway.config import validate_config

        assert validate_config(self._RAW) == []

    def test_negative_price_rejected_with_its_path(self):
        import copy

        from examlops.gateway.config import validate_config

        raw = copy.deepcopy(self._RAW)
        raw["models"]["chat"]["deployments"][0]["price_per_1k"] = -1
        errors = validate_config(raw)
        assert errors and any("price_per_1k" in e for e in errors)

    async def test_price_reaches_the_runtime_deployment(self):
        from examlops.gateway.config import build_runtime
        from tests.unit.test_gateway_config import factory

        rt = await build_runtime(self._RAW, provider_factory=factory({}), source="file")
        assert rt.catalog.routes["chat"].deployments[0].price_per_1k == pytest.approx(0.004)
