"""Built-in LLMOps calculation providers (ADR 0083 — programmable MLOps, INC-5b).

Registers **four LLMOps calculation domains** on the existing provider registry (ADR 0074/0077):

* ``llm_cost``    — token usage → USD (default: ``token-rate``).
* ``llm_cache``   — semantic-cache savings metrics (default: ``hit-savings``).
* ``llm_routing`` — per-candidate routing score (default: ``least-cost``).
* ``rag_quality`` — RAG retrieval quality metrics (default: ``retrieval-lite``).

Pre-emptive: no live B-track backend uses these yet. When B2–B4/B8 land, each calls
``get_provider("<domain>")`` and never hard-codes a formula — exactly the discipline that made
``cost``/``carbon`` swappable from their first commit (ADR 0077 §3).

Importing this module registers all four domains as a side effect.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .providers import Provider, ProviderMeta, register_provider

# ── llm_cost ──────────────────────────────────────────────────────────────────

_DOMAIN_COST = "llm_cost"

# OpenAI / Anthropic convention: reasoning tokens billed at the output rate.
_DEFAULT_INPUT_PRICE = 0.002  # USD per 1k tokens (illustrative; override via config)
_DEFAULT_OUTPUT_PRICE = 0.002  # USD per 1k tokens


def _f(inputs: Mapping[str, Any], key: str, default: float) -> float:
    val = inputs.get(key, default)
    return float(val if val is not None else default)


class TokenRateCostProvider(Provider):
    """Default LLM cost formula — token price card.

    ``cost_usd = prompt_tokens/1000 × input_price_per_1k
              + (completion_tokens + reasoning_tokens)/1000 × output_price_per_1k``

    Reasoning tokens are billed at the output rate (OpenAI o-series / Anthropic
    extended-thinking convention).  Prices default to illustrative values; override
    via ``input_price_per_1k`` / ``output_price_per_1k`` in the call inputs or via
    a ``providers.yaml`` formula.
    """

    name = "token-rate"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "cost_usd = prompt_tokens/1000 × input_price_per_1k "
                "+ (completion_tokens + reasoning_tokens)/1000 × output_price_per_1k. "
                "Reasoning tokens billed at the output rate (OpenAI/Anthropic convention)."
            ),
            units={"cost_usd": "USD"},
            outputs=("cost_usd",),
            params=(
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "input_price_per_1k",
                "output_price_per_1k",
            ),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        prompt = _f(inputs, "prompt_tokens", 0.0)
        completion = _f(inputs, "completion_tokens", 0.0)
        reasoning = _f(inputs, "reasoning_tokens", 0.0)
        input_price = _f(inputs, "input_price_per_1k", _DEFAULT_INPUT_PRICE)
        output_price = _f(inputs, "output_price_per_1k", _DEFAULT_OUTPUT_PRICE)
        cost = (prompt / 1000.0) * input_price + (completion + reasoning) / 1000.0 * output_price
        return {"cost_usd": round(cost, 6)}


# ── llm_cache ─────────────────────────────────────────────────────────────────

_DOMAIN_CACHE = "llm_cache"


class HitSavingsCacheProvider(Provider):
    """Default semantic-cache savings provider.

    Computes:
    * ``hit_rate`` = cache_hits / total_calls  (0 when total_calls == 0)
    * ``cost_saved_usd`` = cache_hits × cost_per_call_usd
    * ``latency_saved_ms`` = cache_hits × max(0, latency_full_ms − latency_cached_ms)
    """

    name = "hit-savings"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "hit_rate = cache_hits / total_calls; "
                "cost_saved_usd = cache_hits × cost_per_call_usd; "
                "latency_saved_ms = cache_hits × max(0, latency_full_ms − latency_cached_ms). "
                "Division guarded: total_calls == 0 → hit_rate = 0."
            ),
            units={"cost_saved_usd": "USD", "latency_saved_ms": "ms"},
            outputs=("hit_rate", "cost_saved_usd", "latency_saved_ms"),
            params=(
                "total_calls",
                "cache_hits",
                "cost_per_call_usd",
                "latency_full_ms",
                "latency_cached_ms",
            ),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        total_calls = _f(inputs, "total_calls", 0.0)
        cache_hits = _f(inputs, "cache_hits", 0.0)
        cost_per_call = _f(inputs, "cost_per_call_usd", 0.0)
        latency_full = _f(inputs, "latency_full_ms", 0.0)
        latency_cached = _f(inputs, "latency_cached_ms", 0.0)
        hit_rate = cache_hits / total_calls if total_calls > 0 else 0.0
        cost_saved = cache_hits * cost_per_call
        latency_saved = cache_hits * max(0.0, latency_full - latency_cached)
        return {
            "hit_rate": round(hit_rate, 4),
            "cost_saved_usd": round(cost_saved, 6),
            "latency_saved_ms": round(latency_saved, 2),
        }


# ── llm_routing ───────────────────────────────────────────────────────────────

_DOMAIN_ROUTING = "llm_routing"

_NEG_INF = float("-inf")


class LeastCostRoutingProvider(Provider):
    """Default routing scorer — cheapest healthy candidate wins.

    Returns ``{"score": −cost_usd}`` for a healthy candidate, ``−inf`` if unhealthy.
    The caller maximizes scores, so the cheapest reachable route is selected.

    A ``cost-latency`` variant (``−(cost_usd + latency_ms × latency_weight)``) ships as
    an expression example in providers.yaml; it demonstrates that the *formula*, not just
    the coefficients, is swappable.
    """

    name = "least-cost"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "score = −cost_usd if healthy else −inf. "
                "Caller maximizes: selects cheapest reachable route. "
                "Override with a cost-latency formula: −(cost_usd + latency_ms × weight)."
            ),
            outputs=("score",),
            params=("cost_usd", "latency_ms", "quality", "healthy"),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        healthy = inputs.get("healthy", True)
        if not healthy:
            return {"score": _NEG_INF}
        cost = _f(inputs, "cost_usd", 0.0)
        return {"score": -cost}


# ── rag_quality ───────────────────────────────────────────────────────────────

_DOMAIN_RAG = "rag_quality"

_DEFAULT_RELEVANCE_THRESHOLD = 0.5


class RetrievalLiteQualityProvider(Provider):
    """Default RAG retrieval quality provider.

    Computes:
    * ``precision`` = hits / k   where hits = count(retrieved_relevances[i] ≥ threshold)
    * ``recall``    = hits / relevant_total  (guarded: relevant_total == 0 → 0)
    * ``relevance`` = mean(retrieved_relevances)  (empty list → 0)

    Ragas / MRR / NDCG variants can be registered as entry-point plugins or YAML formulas.
    """

    name = "retrieval-lite"
    version = "1.0"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology=(
                "precision = hits/k; recall = hits/relevant_total; "
                "relevance = mean(retrieved_relevances). "
                "hits = count(relevance_score ≥ threshold, default 0.5). "
                "Ragas / MRR / NDCG variants available as plugins or YAML formulas."
            ),
            outputs=("precision", "recall", "relevance"),
            params=("retrieved_relevances", "relevant_total", "k", "threshold"),
        )

    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        retrieved: list[float] = [float(r) for r in inputs.get("retrieved_relevances", [])]
        relevant_total = _f(inputs, "relevant_total", 0.0)
        k = _f(inputs, "k", float(len(retrieved)) or 1.0)
        threshold = _f(inputs, "threshold", _DEFAULT_RELEVANCE_THRESHOLD)
        hits = sum(1 for r in retrieved if r >= threshold)
        precision = hits / k if k > 0 else 0.0
        recall = hits / relevant_total if relevant_total > 0 else 0.0
        relevance = sum(retrieved) / len(retrieved) if retrieved else 0.0
        return {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "relevance": round(relevance, 4),
        }


# ── registration ──────────────────────────────────────────────────────────────


def register_builtins() -> None:
    """Register all four LLMOps provider domains on the global registry (idempotent)."""
    register_provider(_DOMAIN_COST, "token-rate", TokenRateCostProvider, default=True)
    register_provider(_DOMAIN_CACHE, "hit-savings", HitSavingsCacheProvider, default=True)
    register_provider(_DOMAIN_ROUTING, "least-cost", LeastCostRoutingProvider, default=True)
    register_provider(_DOMAIN_RAG, "retrieval-lite", RetrievalLiteQualityProvider, default=True)


# Register at import time (matches the carbon/cost/placement/drift/promotion convention).
register_builtins()
