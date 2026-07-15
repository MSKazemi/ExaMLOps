"""Unit tests for LLMOps calculation providers (INC-5b / ADR 0083).

Covers all four domains: llm_cost, llm_cache, llm_routing, rag_quality.
Verifies default formulas, edge cases, graceful degradation, and registry presence.
"""

from __future__ import annotations

import math
import pytest

from examlops.llmops_providers import (
    HitSavingsCacheProvider,
    LeastCostRoutingProvider,
    RetrievalLiteQualityProvider,
    TokenRateCostProvider,
    register_builtins,
)
from examlops.providers import default_provider_name, list_providers


# ── llm_cost: TokenRateCostProvider ──────────────────────────────────────────

class TestTokenRateCostProvider:
    def _p(self):
        return TokenRateCostProvider()

    def test_basic_cost(self):
        out = self._p().compute({
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "reasoning_tokens": 0,
            "input_price_per_1k": 0.002,
            "output_price_per_1k": 0.004,
        })
        # 1000/1000 * 0.002 + 500/1000 * 0.004 = 0.002 + 0.002 = 0.004
        assert math.isclose(out["cost_usd"], 0.004, rel_tol=1e-6)

    def test_reasoning_tokens_billed_at_output_rate(self):
        out = self._p().compute({
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 1000,
            "input_price_per_1k": 0.001,
            "output_price_per_1k": 0.005,
        })
        # 1000/1000 * 0.005 = 0.005
        assert math.isclose(out["cost_usd"], 0.005, rel_tol=1e-6)

    def test_zero_tokens_zero_cost(self):
        out = self._p().compute({})
        assert out["cost_usd"] == pytest.approx(0.0)

    def test_completion_plus_reasoning_sum(self):
        out = self._p().compute({
            "prompt_tokens": 0,
            "completion_tokens": 500,
            "reasoning_tokens": 500,
            "input_price_per_1k": 0.0,
            "output_price_per_1k": 0.010,
        })
        # (500+500)/1000 * 0.010 = 0.010
        assert math.isclose(out["cost_usd"], 0.010, rel_tol=1e-6)


# ── llm_cache: HitSavingsCacheProvider ───────────────────────────────────────

class TestHitSavingsCacheProvider:
    def _p(self):
        return HitSavingsCacheProvider()

    def test_hit_rate(self):
        out = self._p().compute({"total_calls": 10, "cache_hits": 4})
        assert out["hit_rate"] == pytest.approx(0.4)

    def test_zero_total_calls_no_division_error(self):
        out = self._p().compute({"total_calls": 0, "cache_hits": 0})
        assert out["hit_rate"] == 0.0

    def test_cost_saved(self):
        out = self._p().compute({
            "total_calls": 10,
            "cache_hits": 3,
            "cost_per_call_usd": 0.01,
        })
        assert math.isclose(out["cost_saved_usd"], 0.03, rel_tol=1e-6)

    def test_latency_saved(self):
        out = self._p().compute({
            "total_calls": 10,
            "cache_hits": 5,
            "latency_full_ms": 300,
            "latency_cached_ms": 10,
        })
        # 5 * (300 - 10) = 1450
        assert math.isclose(out["latency_saved_ms"], 1450, rel_tol=1e-6)

    def test_latency_saved_clamps_negative_gap(self):
        # cached latency > full latency (shouldn't happen but must not go negative)
        out = self._p().compute({
            "total_calls": 5,
            "cache_hits": 5,
            "latency_full_ms": 50,
            "latency_cached_ms": 100,
        })
        assert out["latency_saved_ms"] == pytest.approx(0.0)

    def test_all_zeros(self):
        out = self._p().compute({})
        assert out["hit_rate"] == 0.0
        assert out["cost_saved_usd"] == pytest.approx(0.0)
        assert out["latency_saved_ms"] == pytest.approx(0.0)


# ── llm_routing: LeastCostRoutingProvider ────────────────────────────────────

class TestLeastCostRoutingProvider:
    def _p(self):
        return LeastCostRoutingProvider()

    def test_healthy_score_is_neg_cost(self):
        out = self._p().compute({"cost_usd": 0.05, "healthy": True})
        assert out["score"] == pytest.approx(-0.05)

    def test_unhealthy_returns_neg_inf(self):
        out = self._p().compute({"cost_usd": 0.01, "healthy": False})
        assert out["score"] == float("-inf")

    def test_default_healthy_true(self):
        out = self._p().compute({"cost_usd": 0.03})
        assert out["score"] == pytest.approx(-0.03)

    def test_cheaper_candidate_wins(self):
        p = self._p()
        cheap = p.compute({"cost_usd": 0.01, "healthy": True})["score"]
        expensive = p.compute({"cost_usd": 0.05, "healthy": True})["score"]
        # Caller maximizes; cheap has a higher (less negative) score
        assert cheap > expensive

    def test_healthy_always_beats_unhealthy(self):
        p = self._p()
        healthy = p.compute({"cost_usd": 99.0, "healthy": True})["score"]
        unhealthy = p.compute({"cost_usd": 0.001, "healthy": False})["score"]
        assert healthy > unhealthy


# ── rag_quality: RetrievalLiteQualityProvider ─────────────────────────────────

class TestRetrievalLiteQualityProvider:
    def _p(self):
        return RetrievalLiteQualityProvider()

    def test_perfect_retrieval(self):
        out = self._p().compute({
            "retrieved_relevances": [1.0, 1.0, 1.0],
            "relevant_total": 3,
            "k": 3,
        })
        assert out["precision"] == pytest.approx(1.0)
        assert out["recall"] == pytest.approx(1.0)
        assert out["relevance"] == pytest.approx(1.0)

    def test_partial_hit(self):
        # 2 of 3 retrieved are relevant (≥ 0.5), 4 total relevant docs
        out = self._p().compute({
            "retrieved_relevances": [0.8, 0.3, 0.9],
            "relevant_total": 4,
            "k": 3,
        })
        assert out["precision"] == pytest.approx(2 / 3, rel=1e-4)
        assert out["recall"] == pytest.approx(2 / 4, rel=1e-4)
        assert out["relevance"] == pytest.approx((0.8 + 0.3 + 0.9) / 3, rel=1e-4)

    def test_empty_retrieved_list(self):
        out = self._p().compute({"retrieved_relevances": [], "relevant_total": 5, "k": 3})
        assert out["precision"] == 0.0
        assert out["recall"] == 0.0
        assert out["relevance"] == 0.0

    def test_zero_relevant_total_no_division_error(self):
        out = self._p().compute({
            "retrieved_relevances": [0.9, 0.8],
            "relevant_total": 0,
            "k": 2,
        })
        assert out["recall"] == 0.0

    def test_custom_threshold(self):
        # With a high threshold (0.9), only scores ≥ 0.9 count as hits
        out = self._p().compute({
            "retrieved_relevances": [0.95, 0.7, 0.85],
            "relevant_total": 3,
            "k": 3,
            "threshold": 0.9,
        })
        assert out["precision"] == pytest.approx(1 / 3, rel=1e-4)


# ── registry ──────────────────────────────────────────────────────────────────

def test_all_four_domains_registered():
    register_builtins()
    for domain, expected_default in [
        ("llm_cost", "token-rate"),
        ("llm_cache", "hit-savings"),
        ("llm_routing", "least-cost"),
        ("rag_quality", "retrieval-lite"),
    ]:
        names = {i.name for i in list_providers(domain)}
        assert expected_default in names, f"domain={domain}: {expected_default} not found in {names}"
        assert default_provider_name(domain) == expected_default


def test_registration_is_idempotent():
    register_builtins()
    register_builtins()
    # No exception and defaults still correct
    assert default_provider_name("llm_cost") == "token-rate"
    assert default_provider_name("llm_cache") == "hit-savings"
    assert default_provider_name("llm_routing") == "least-cost"
    assert default_provider_name("rag_quality") == "retrieval-lite"


def test_providers_cmd_sees_all_llmops_domains():
    """providers_cmd._DOMAIN_MODULES includes all four LLMOps domains."""
    from examlops.cli.commands.providers_cmd import _DOMAIN_MODULES

    for domain in ("llm_cost", "llm_cache", "llm_routing", "rag_quality"):
        assert domain in _DOMAIN_MODULES, f"{domain} missing from _DOMAIN_MODULES"
